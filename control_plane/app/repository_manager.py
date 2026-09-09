from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import signal
import shutil
import socket
import stat
import subprocess
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Repository
from .repository_policy import (
    RepositoryPolicyError,
    normalize_profile_reference,
    normalize_repository_host,
    normalize_repository_remote,
)
from .schema import assert_database_shape
from .services import write_audit


LOGGER = logging.getLogger("ai_orchestra.repository_manager")
SAFE_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,79}")
SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?")
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
PROXY_ENV_NAMES = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


class RepositorySyncError(RuntimeError):
    """A classified, credential-free synchronization failure."""

    def __init__(self, code: str, *, terminal: bool = False):
        if not SAFE_ERROR_CODE_RE.fullmatch(code):
            raise ValueError("Unsafe repository sync error code")
        super().__init__(code)
        self.code = code
        self.terminal = terminal


class RepositoryLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class GitCredential:
    host: str
    username: str
    password: str


def load_auth_profiles(raw: str | None = None) -> dict[str, GitCredential]:
    payload = raw if raw is not None else os.getenv("REPO_MANAGER_AUTH_PROFILES_JSON", "{}")
    if len(payload) > 65536:
        raise RuntimeError("REPO_MANAGER_AUTH_PROFILES_JSON is too large")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("REPO_MANAGER_AUTH_PROFILES_JSON is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("REPO_MANAGER_AUTH_PROFILES_JSON must be a JSON object")

    profiles: dict[str, GitCredential] = {}
    for raw_name, raw_profile in decoded.items():
        if not isinstance(raw_name, str):
            raise RuntimeError("Repository auth profile names must be strings")
        try:
            name = normalize_profile_reference(raw_name, field_name="auth profile")
        except RepositoryPolicyError as exc:
            raise RuntimeError("Repository auth profile name is invalid") from exc
        if name != raw_name:
            raise RuntimeError("Repository auth profile names must already be canonical")
        if not isinstance(raw_profile, dict) or set(raw_profile) != {
            "host",
            "username",
            "password",
        }:
            raise RuntimeError(
                f"Repository auth profile {name!r} must contain exactly host, username and password"
            )
        host_value = raw_profile.get("host")
        username = raw_profile.get("username")
        password = raw_profile.get("password")
        if not all(isinstance(value, str) for value in (host_value, username, password)):
            raise RuntimeError(f"Repository auth profile {name!r} fields must be strings")
        try:
            host = normalize_repository_host(host_value)
        except RepositoryPolicyError as exc:
            raise RuntimeError(f"Repository auth profile {name!r} host is invalid") from exc
        if host != host_value:
            raise RuntimeError(f"Repository auth profile {name!r} host must be canonical")
        if not 1 <= len(username) <= 256 or not username.isprintable() or "\n" in username:
            raise RuntimeError(f"Repository auth profile {name!r} username is invalid")
        if not 1 <= len(password) <= 4096 or not password.isprintable() or "\n" in password:
            raise RuntimeError(f"Repository auth profile {name!r} password is invalid")
        profiles[name] = GitCredential(host=host, username=username, password=password)
    return profiles


def resolve_public_addresses(
    host: str,
    *,
    resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
) -> tuple[str, ...]:
    """Resolve every address and reject the entire host if any answer is non-public."""
    try:
        answers = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RepositorySyncError("dns_resolution_failed") from exc

    addresses: set[str] = set()
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0])
        except (IndexError, TypeError, ValueError) as exc:
            raise RepositorySyncError("dns_answer_invalid", terminal=True) from exc
        if not address.is_global or address.is_multicast:
            raise RepositorySyncError("remote_address_forbidden", terminal=True)
        addresses.add(address.compressed)
    if not addresses:
        raise RepositorySyncError("dns_no_addresses")
    return tuple(sorted(addresses))


@dataclass(frozen=True)
class RepositorySyncLease:
    repository_id: str
    generation: int
    record_version: int
    remote_url: str
    remote_identity: str
    remote_host: str
    provider: str
    auth_profile_ref: str | None


@dataclass(frozen=True)
class RepositorySyncResult:
    default_branch: str
    commit: str


class RepositoryLeaseManager:
    """Durable PostgreSQL lease and fencing boundary for repository synchronization."""

    def __init__(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 300,
        refresh_seconds: int = 3600,
        retry_base_seconds: int = 30,
        retry_max_seconds: int = 1800,
    ):
        if lease_seconds < 120:
            raise ValueError("lease_seconds must be at least 120")
        if refresh_seconds < 60:
            raise ValueError("refresh_seconds must be at least 60")
        if retry_base_seconds < 5 or retry_max_seconds < retry_base_seconds:
            raise ValueError("invalid retry interval")
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.refresh_seconds = refresh_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    @property
    def audit_actor(self) -> str:
        return f"repo-manager:{self.worker_id}"[:100]

    def _lease_deadline(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.lease_seconds)

    def claim_available(
        self,
        db: Session,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> list[RepositorySyncLease]:
        if limit <= 0:
            return []
        now = now or utc_now()
        rows = list(
            db.scalars(
                select(Repository)
                .where(
                    Repository.enabled.is_(True),
                    Repository.sync_next_at.is_not(None),
                    Repository.sync_next_at <= now,
                    or_(
                        Repository.sync_lease_expires_at.is_(None),
                        Repository.sync_lease_expires_at <= now,
                    ),
                )
                .order_by(Repository.sync_next_at.asc(), Repository.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        leases: list[RepositorySyncLease] = []
        for repository in rows:
            previous_generation = int(repository.sync_generation or 0)
            previous_owner = repository.sync_lease_owner
            previous_expiry = _as_utc(repository.sync_lease_expires_at)
            repository.sync_generation = previous_generation + 1
            repository.sync_lease_owner = self.worker_id
            repository.sync_lease_expires_at = self._lease_deadline(now)
            repository.sync_started_at = now
            repository.status = "validating"
            repository.version += 1
            repository.updated_at = now
            write_audit(
                db,
                actor=self.audit_actor,
                action=(
                    "repository.sync_recovered"
                    if previous_generation > 0 and previous_expiry is not None
                    else "repository.sync_claimed"
                ),
                entity_type="repository",
                entity_id=repository.id,
                details={
                    "generation": repository.sync_generation,
                    "version": repository.version,
                    "previous_owner": previous_owner,
                    "previous_lease_expires_at": (
                        previous_expiry.isoformat() if previous_expiry else None
                    ),
                },
            )
            leases.append(
                RepositorySyncLease(
                    repository_id=repository.id,
                    generation=repository.sync_generation,
                    record_version=repository.version,
                    remote_url=repository.remote_url,
                    remote_identity=repository.remote_identity,
                    remote_host=repository.remote_host,
                    provider=repository.provider,
                    auth_profile_ref=repository.auth_profile_ref,
                )
            )
        db.commit()
        return leases

    def _locked_owned_repository(
        self,
        db: Session,
        lease: RepositorySyncLease,
        now: datetime,
    ) -> Repository | None:
        repository = db.scalar(
            select(Repository)
            .where(Repository.id == lease.repository_id)
            .with_for_update()
        )
        expiry = _as_utc(repository.sync_lease_expires_at) if repository else None
        if (
            repository is None
            or not repository.enabled
            or repository.status != "validating"
            or repository.version != lease.record_version
            or repository.sync_lease_owner != self.worker_id
            or int(repository.sync_generation or 0) != lease.generation
            or expiry is None
            or expiry <= now
        ):
            return None
        return repository

    def heartbeat(
        self,
        db: Session,
        lease: RepositorySyncLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        repository = self._locked_owned_repository(db, lease, now)
        if repository is None:
            db.rollback()
            return False
        repository.sync_lease_expires_at = self._lease_deadline(now)
        repository.updated_at = now
        db.commit()
        return True

    def mark_success(
        self,
        db: Session,
        lease: RepositorySyncLease,
        result: RepositorySyncResult,
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        repository = self._locked_owned_repository(db, lease, now)
        if repository is None:
            db.rollback()
            return "lost"
        repository.status = "ready"
        repository.default_branch = result.default_branch
        repository.last_known_commit = result.commit
        repository.last_fetched_at = now
        repository.sync_finished_at = now
        repository.sync_next_at = now + timedelta(seconds=self.refresh_seconds)
        repository.sync_lease_owner = None
        repository.sync_lease_expires_at = None
        repository.sync_failure_count = 0
        repository.last_sync_error_code = None
        repository.version += 1
        repository.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="repository.sync_succeeded",
            entity_type="repository",
            entity_id=repository.id,
            details={
                "generation": lease.generation,
                "version": repository.version,
                "default_branch": result.default_branch,
                "commit": result.commit,
            },
        )
        db.commit()
        return "ready"

    def mark_failure(
        self,
        db: Session,
        lease: RepositorySyncLease,
        error: RepositorySyncError,
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        repository = self._locked_owned_repository(db, lease, now)
        if repository is None:
            db.rollback()
            return "lost"
        repository.sync_failure_count += 1
        repository.status = "invalid" if error.terminal else "unavailable"
        repository.sync_finished_at = now
        repository.sync_lease_owner = None
        repository.sync_lease_expires_at = None
        repository.last_sync_error_code = error.code
        if error.terminal:
            repository.sync_next_at = None
        else:
            exponent = min(repository.sync_failure_count - 1, 20)
            delay = min(self.retry_max_seconds, self.retry_base_seconds * (2**exponent))
            repository.sync_next_at = now + timedelta(seconds=delay)
        repository.version += 1
        repository.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="repository.sync_failed",
            entity_type="repository",
            entity_id=repository.id,
            details={
                "generation": lease.generation,
                "version": repository.version,
                "error_code": error.code,
                "terminal": error.terminal,
                "retry_at": (
                    repository.sync_next_at.isoformat() if repository.sync_next_at else None
                ),
            },
        )
        db.commit()
        return repository.status


class GitCommandRunner:
    def run(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str],
        timeout_seconds: int,
        failure_code: str,
    ) -> str:
        try:
            process = subprocess.Popen(
                arguments,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RepositorySyncError("git_not_available") from exc
        try:
            stdout, _ = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                process.kill()
            process.communicate()
            raise RepositorySyncError("git_operation_timed_out") from exc
        if process.returncode != 0:
            raise RepositorySyncError(failure_code)
        if len(stdout) > 65536:
            raise RepositorySyncError("git_output_too_large", terminal=True)
        return stdout


class GitRepositorySynchronizer:
    """Synchronize controlled bare mirrors without checking out untrusted code."""

    def __init__(
        self,
        storage_root: Path,
        auth_profiles: dict[str, GitCredential],
        *,
        command_timeout_seconds: int = 60,
        max_repository_bytes: int = 5 * 1024 * 1024 * 1024,
        min_free_bytes: int = 512 * 1024 * 1024,
        resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
        runner: GitCommandRunner | None = None,
        liveness_callback: Callable[[], None] | None = None,
    ):
        if max_repository_bytes < 1024 * 1024:
            raise ValueError("max_repository_bytes must be at least 1 MiB")
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must be non-negative")
        self.storage_root = storage_root
        self.auth_profiles = auth_profiles
        self.command_timeout_seconds = command_timeout_seconds
        self.max_repository_bytes = max_repository_bytes
        self.min_free_bytes = min_free_bytes
        self.resolver = resolver
        self.runner = runner or GitCommandRunner()
        self.liveness_callback = liveness_callback or (lambda: None)
        self.askpass_path = Path(__file__).with_name("repo_manager_askpass.py")
        self.git_wrapper_path = Path(__file__).with_name("repo_manager_git_wrapper.sh")

    def _credential_for(self, lease: RepositorySyncLease) -> GitCredential | None:
        if lease.auth_profile_ref is None:
            return None
        credential = self.auth_profiles.get(lease.auth_profile_ref)
        if credential is None:
            raise RepositorySyncError("auth_profile_unavailable")
        if credential.host != lease.remote_host:
            raise RepositorySyncError("auth_profile_host_mismatch", terminal=True)
        return credential

    def _environment(
        self,
        credential: GitCredential | None,
        *,
        max_file_bytes: int,
    ) -> dict[str, str]:
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(self.askpass_path),
            "REPO_MANAGER_GIT_MAX_FILE_BLOCKS": str(
                max(2048, max_file_bytes // 512)
            ),
        }
        if credential is not None:
            environment["REPO_MANAGER_GIT_HOST"] = credential.host
            environment["REPO_MANAGER_GIT_USERNAME"] = credential.username
            environment["REPO_MANAGER_GIT_PASSWORD"] = credential.password
        return {key: value for key, value in environment.items() if key not in PROXY_ENV_NAMES}

    def _git_prefix(self, host: str, addresses: tuple[str, ...]) -> list[str]:
        arguments = [
            str(self.git_wrapper_path),
            "-c",
            "credential.helper=",
            "-c",
            f"core.askPass={self.askpass_path}",
            "-c",
            "credential.useHttpPath=true",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.sslVerify=true",
            "-c",
            "http.proxy=",
            "-c",
            "http.extraHeader=",
            "-c",
            "remote.origin.proxy=",
            "-c",
            "http.lowSpeedLimit=1024",
            "-c",
            "http.lowSpeedTime=30",
            "-c",
            "http.maxRequests=2",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "fetch.fsckObjects=true",
            "-c",
            "transfer.fsckObjects=true",
            "-c",
            "fetch.recurseSubmodules=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "core.hooksPath=/dev/null",
        ]
        pinned_addresses = ",".join(
            f"[{address}]" if ":" in address else address for address in addresses
        )
        arguments.extend(
            ["-c", f"http.curloptResolve={host}:443:{pinned_addresses}"]
        )
        return arguments

    def _run(
        self,
        prefix: list[str],
        arguments: list[str],
        environment: dict[str, str],
        heartbeat: Callable[[], bool],
        *,
        failure_code: str,
    ) -> str:
        self._ensure_lease(heartbeat)
        return self.runner.run(
            [*prefix, *arguments],
            environment=environment,
            timeout_seconds=self.command_timeout_seconds,
            failure_code=failure_code,
        )

    def _ensure_lease(self, heartbeat: Callable[[], bool]) -> None:
        if not heartbeat():
            raise RepositoryLeaseLost()
        self.liveness_callback()

    @staticmethod
    def _parse_remote_head(output: str) -> RepositorySyncResult:
        branch: str | None = None
        commit: str | None = None
        for line in output.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                candidate = line[len("ref: refs/heads/") : -len("\tHEAD")]
                if branch is not None:
                    raise RepositorySyncError("remote_head_ambiguous", terminal=True)
                branch = candidate
            elif line.endswith("\tHEAD"):
                candidate = line[: -len("\tHEAD")].lower()
                if COMMIT_RE.fullmatch(candidate):
                    if commit is not None:
                        raise RepositorySyncError("remote_head_ambiguous", terminal=True)
                    commit = candidate
        if branch is None or commit is None:
            raise RepositorySyncError("remote_head_unavailable")
        if (
            not SAFE_BRANCH_RE.fullmatch(branch)
            or ".." in branch
            or "//" in branch
            or "@{" in branch
            or branch == "@"
            or branch.endswith(("/", ".", ".lock"))
            or any(
                component.startswith(".") or component.endswith(".lock")
                for component in branch.split("/")
            )
        ):
            raise RepositorySyncError("remote_default_branch_invalid", terminal=True)
        return RepositorySyncResult(default_branch=branch, commit=commit)

    def _verify_directory_size(
        self,
        path: Path,
        heartbeat: Callable[[], bool],
    ) -> None:
        total = 0
        next_liveness_check = time.monotonic() + 10
        self._ensure_lease(heartbeat)
        for root, directories, files in os.walk(path, followlinks=False):
            for name in directories:
                if (Path(root) / name).is_symlink():
                    raise RepositorySyncError("mirror_symlink_detected", terminal=True)
            for name in files:
                candidate = Path(root) / name
                if candidate.is_symlink():
                    raise RepositorySyncError("mirror_symlink_detected", terminal=True)
                total += candidate.stat().st_size
                if total > self.max_repository_bytes:
                    raise RepositorySyncError(
                        "repository_size_limit_exceeded",
                        terminal=True,
                    )
                if time.monotonic() >= next_liveness_check:
                    self._ensure_lease(heartbeat)
                    next_liveness_check = time.monotonic() + 10
        self._ensure_lease(heartbeat)

    def _prepare_storage_root(self) -> None:
        if self.storage_root.is_symlink():
            raise RepositorySyncError("mirror_root_symlink", terminal=True)
        self.storage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_stat = self.storage_root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.geteuid():
            raise RepositorySyncError("mirror_root_invalid", terminal=True)
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            try:
                self.storage_root.chmod(0o700)
            except OSError as exc:
                raise RepositorySyncError("mirror_root_permissions", terminal=True) from exc

    @contextmanager
    def _repository_lock(self, repository_id: str) -> Iterator[None]:
        lock_path = self.storage_root / f".{repository_id}.sync.lock"
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise RepositorySyncError("mirror_lock_invalid", terminal=True) from exc
        try:
            lock_stat = os.fstat(descriptor)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                raise RepositorySyncError("mirror_lock_invalid", terminal=True)
            os.fchmod(descriptor, 0o600)
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RepositorySyncError("mirror_busy") from exc
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _cleanup_staging(self, repository_id: str) -> None:
        prefix = f".{repository_id}."
        for candidate in self.storage_root.iterdir():
            if not candidate.name.startswith(prefix) or not candidate.name.endswith(".tmp"):
                continue
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
            elif candidate.is_dir():
                shutil.rmtree(candidate)

    @staticmethod
    def _cleanup_stale_git_locks(mirror_path: Path) -> None:
        candidates = [
            mirror_path / "HEAD.lock",
            mirror_path / "config.lock",
            mirror_path / "packed-refs.lock",
            mirror_path / "shallow.lock",
        ]
        refs = mirror_path / "refs"
        if refs.is_dir() and not refs.is_symlink():
            candidates.extend(refs.rglob("*.lock"))
        for candidate in candidates:
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()

    def _max_file_bytes_for_current_capacity(self) -> int:
        free_bytes = shutil.disk_usage(self.storage_root).free
        available = free_bytes - self.min_free_bytes
        if available < 1024 * 1024:
            raise RepositorySyncError("storage_capacity_low")
        return min(self.max_repository_bytes, available)

    def synchronize(
        self,
        lease: RepositorySyncLease,
        *,
        heartbeat: Callable[[], bool],
    ) -> RepositorySyncResult:
        try:
            repository_id = str(UUID(lease.repository_id))
        except ValueError as exc:
            raise RepositorySyncError("repository_id_invalid", terminal=True) from exc

        try:
            normalized = normalize_repository_remote(lease.remote_url)
        except RepositoryPolicyError as exc:
            raise RepositorySyncError("registry_remote_invalid", terminal=True) from exc
        if (
            normalized.url != lease.remote_url
            or normalized.identity != lease.remote_identity
            or normalized.host != lease.remote_host
            or normalized.provider != lease.provider
        ):
            raise RepositorySyncError("registry_identity_mismatch", terminal=True)

        credential = self._credential_for(lease)
        addresses = resolve_public_addresses(lease.remote_host, resolver=self.resolver)
        self._prepare_storage_root()
        with self._repository_lock(repository_id):
            return self._synchronize_locked(
                lease,
                repository_id=repository_id,
                credential=credential,
                addresses=addresses,
                heartbeat=heartbeat,
            )

    def _synchronize_locked(
        self,
        lease: RepositorySyncLease,
        *,
        repository_id: str,
        credential: GitCredential | None,
        addresses: tuple[str, ...],
        heartbeat: Callable[[], bool],
    ) -> RepositorySyncResult:
        self._ensure_lease(heartbeat)
        self._cleanup_staging(repository_id)
        mirror_path = self.storage_root / f"{repository_id}.git"
        if mirror_path.is_symlink():
            raise RepositorySyncError("mirror_symlink_detected", terminal=True)
        created = not mirror_path.exists()
        if not created:
            if not mirror_path.is_dir():
                raise RepositorySyncError("mirror_path_invalid", terminal=True)
            self._verify_directory_size(mirror_path, heartbeat)
            self._cleanup_stale_git_locks(mirror_path)
        max_file_bytes = self._max_file_bytes_for_current_capacity()
        network_environment = self._environment(
            credential,
            max_file_bytes=max_file_bytes,
        )
        local_environment = self._environment(None, max_file_bytes=max_file_bytes)
        prefix = self._git_prefix(lease.remote_host, addresses)
        remote_head = self._parse_remote_head(
            self._run(
                prefix,
                ["ls-remote", "--symref", lease.remote_url, "HEAD"],
                network_environment,
                heartbeat,
                failure_code="git_remote_unavailable",
            )
        )
        work_path = (
            self.storage_root / f".{repository_id}.{uuid4().hex}.tmp"
            if created
            else mirror_path
        )
        try:
            if created:
                work_path.mkdir(mode=0o700)
                self._run(
                    prefix,
                    ["init", "--bare", str(work_path)],
                    local_environment,
                    heartbeat,
                    failure_code="mirror_initialization_failed",
                )
                self._run(
                    prefix,
                    ["-C", str(work_path), "remote", "add", "origin", lease.remote_url],
                    local_environment,
                    heartbeat,
                    failure_code="mirror_initialization_failed",
                )
            bare = self._run(
                prefix,
                ["-C", str(work_path), "rev-parse", "--is-bare-repository"],
                local_environment,
                heartbeat,
                failure_code="mirror_integrity_failed",
            ).strip()
            if bare != "true":
                raise RepositorySyncError("mirror_integrity_failed", terminal=True)
            origin_lines = self._run(
                prefix,
                ["-C", str(work_path), "remote", "get-url", "--all", "origin"],
                local_environment,
                heartbeat,
                failure_code="mirror_integrity_failed",
            ).splitlines()
            if origin_lines != [lease.remote_url]:
                raise RepositorySyncError("mirror_origin_mismatch", terminal=True)

            fetch_arguments = [
                "-C",
                str(work_path),
                "fetch",
                "--force",
                "--prune",
                "--no-tags",
                "origin",
                "+refs/heads/*:refs/remotes/origin/*",
            ]
            self._run(
                prefix,
                fetch_arguments,
                network_environment,
                heartbeat,
                failure_code="git_fetch_failed",
            )
            local_ref = f"refs/remotes/origin/{remote_head.default_branch}^{{commit}}"
            local_commit = self._run(
                prefix,
                ["-C", str(work_path), "rev-parse", "--verify", local_ref],
                local_environment,
                heartbeat,
                failure_code="default_branch_not_fetched",
            ).strip().lower()
            if local_commit != remote_head.commit:
                self._run(
                    prefix,
                    fetch_arguments,
                    network_environment,
                    heartbeat,
                    failure_code="git_fetch_failed",
                )
                local_commit = self._run(
                    prefix,
                    ["-C", str(work_path), "rev-parse", "--verify", local_ref],
                    local_environment,
                    heartbeat,
                    failure_code="default_branch_not_fetched",
                ).strip().lower()
            if local_commit != remote_head.commit:
                raise RepositorySyncError("remote_changed_during_sync")
            self._run(
                prefix,
                ["-C", str(work_path), "fsck", "--connectivity-only", "--no-dangling"],
                local_environment,
                heartbeat,
                failure_code="mirror_integrity_failed",
            )
            self._verify_directory_size(work_path, heartbeat)
            if created:
                work_path.rename(mirror_path)
            return remote_head
        finally:
            if created and work_path.exists():
                shutil.rmtree(work_path)


def synchronize_repository(
    manager: RepositoryLeaseManager,
    synchronizer: GitRepositorySynchronizer,
    lease: RepositorySyncLease,
) -> str:
    def heartbeat() -> bool:
        with SessionLocal() as db:
            return manager.heartbeat(db, lease)

    try:
        result = synchronizer.synchronize(lease, heartbeat=heartbeat)
    except RepositoryLeaseLost:
        LOGGER.warning(
            "Repository synchronization lost lease repository=%s generation=%s",
            lease.repository_id,
            lease.generation,
        )
        return "lost"
    except RepositorySyncError as exc:
        LOGGER.warning(
            "Repository synchronization failed repository=%s generation=%s code=%s terminal=%s",
            lease.repository_id,
            lease.generation,
            exc.code,
            exc.terminal,
        )
        with SessionLocal() as db:
            return manager.mark_failure(db, lease, exc)
    except Exception:
        LOGGER.exception(
            "Unexpected repository synchronization failure repository=%s generation=%s",
            lease.repository_id,
            lease.generation,
        )
        with SessionLocal() as db:
            return manager.mark_failure(db, lease, RepositorySyncError("internal_error"))

    with SessionLocal() as db:
        return manager.mark_success(db, lease, result)


def write_worker_health(path: Path) -> None:
    path.touch(exist_ok=True)


def run_forever(
    manager: RepositoryLeaseManager,
    synchronizer: GitRepositorySynchronizer,
    *,
    poll_seconds: int,
    max_active: int,
    health_path: Path,
) -> None:
    with ThreadPoolExecutor(max_workers=max_active, thread_name_prefix="repository-sync") as pool:
        while True:
            write_worker_health(health_path)
            with SessionLocal() as db:
                leases = manager.claim_available(db, limit=max_active)
            futures = {
                pool.submit(synchronize_repository, manager, synchronizer, lease): lease
                for lease in leases
            }
            for future in as_completed(futures):
                lease = futures[future]
                try:
                    future.result()
                except Exception:
                    # synchronize_repository contains the durable failure boundary;
                    # this is process containment for truly unexpected bugs.
                    LOGGER.exception(
                        "Uncontained Repo Manager failure repository=%s generation=%s",
                        lease.repository_id,
                        lease.generation,
                    )
            write_worker_health(health_path)
            time.sleep(poll_seconds)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    os.umask(0o077)
    worker_id = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid4().hex[:12]}"
    lease_seconds = _bounded_int(
        "REPO_MANAGER_LEASE_SECONDS", 300, minimum=120, maximum=3600
    )
    command_timeout_seconds = _bounded_int(
        "REPO_MANAGER_GIT_TIMEOUT_SECONDS", 60, minimum=10, maximum=600
    )
    if lease_seconds < command_timeout_seconds + 30:
        raise RuntimeError(
            "REPO_MANAGER_LEASE_SECONDS must exceed REPO_MANAGER_GIT_TIMEOUT_SECONDS by 30s"
        )
    refresh_seconds = _bounded_int(
        "REPO_MANAGER_REFRESH_SECONDS", 3600, minimum=60, maximum=604800
    )
    retry_base_seconds = _bounded_int(
        "REPO_MANAGER_RETRY_BASE_SECONDS", 30, minimum=5, maximum=3600
    )
    retry_max_seconds = _bounded_int(
        "REPO_MANAGER_RETRY_MAX_SECONDS", 1800, minimum=5, maximum=86400
    )
    if retry_max_seconds < retry_base_seconds:
        raise RuntimeError("REPO_MANAGER_RETRY_MAX_SECONDS must be >= retry base")
    max_active = _bounded_int("REPO_MANAGER_MAX_ACTIVE", 1, minimum=1, maximum=8)
    poll_seconds = _bounded_int("REPO_MANAGER_POLL_SECONDS", 10, minimum=1, maximum=60)
    max_repository_bytes = _bounded_int(
        "REPO_MANAGER_MAX_REPOSITORY_BYTES",
        5 * 1024 * 1024 * 1024,
        minimum=1024 * 1024,
        maximum=1024 * 1024 * 1024 * 1024,
    )
    min_free_bytes = _bounded_int(
        "REPO_MANAGER_MIN_FREE_BYTES",
        512 * 1024 * 1024,
        minimum=64 * 1024 * 1024,
        maximum=1024 * 1024 * 1024 * 1024,
    )
    storage_root = Path(
        os.getenv("REPO_MANAGER_STORAGE_ROOT", "/var/lib/ai-orchestra/repositories")
    )
    health_path = Path(
        os.getenv("REPO_MANAGER_HEALTH_PATH", "/tmp/ai-orchestra-repo-manager.heartbeat")
    )
    auth_profiles = load_auth_profiles()

    with SessionLocal() as db:
        assert_database_shape(db.get_bind())

    manager = RepositoryLeaseManager(
        worker_id,
        lease_seconds=lease_seconds,
        refresh_seconds=refresh_seconds,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
    )
    synchronizer = GitRepositorySynchronizer(
        storage_root,
        auth_profiles,
        command_timeout_seconds=command_timeout_seconds,
        max_repository_bytes=max_repository_bytes,
        min_free_bytes=min_free_bytes,
        liveness_callback=lambda: write_worker_health(health_path),
    )
    LOGGER.info(
        "Repo Manager started worker_id=%s lease_seconds=%s timeout_seconds=%s max_active=%s poll_seconds=%s",
        worker_id,
        lease_seconds,
        command_timeout_seconds,
        max_active,
        poll_seconds,
    )
    run_forever(
        manager,
        synchronizer,
        poll_seconds=poll_seconds,
        max_active=max_active,
        health_path=health_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
