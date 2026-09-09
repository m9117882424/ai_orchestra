from __future__ import annotations

import json
import socket
import subprocess
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from control_plane.app.db import SessionLocal
from control_plane.app.models import AuditEvent, Repository
from control_plane.app.repository_manager import (
    GitCommandRunner,
    GitRepositorySynchronizer,
    RepositoryLeaseManager,
    RepositorySyncError,
    RepositorySyncLease,
    RepositorySyncResult,
    load_auth_profiles,
    resolve_public_addresses,
)
from control_plane.app import repository_manager as repository_manager_module
from control_plane.app.repo_manager_askpass import main as askpass_main


REMOTE = "https://github.com/example/repository.git"
IDENTITY = "github.com/example/repository"
COMMIT = "a" * 40


def _repository(*, sync_next_at: datetime | None = None, enabled: bool = True) -> Repository:
    now = datetime.now(timezone.utc)
    return Repository(
        id=str(uuid4()),
        name=f"repository-{uuid4().hex[:8]}",
        remote_url=REMOTE,
        remote_identity=f"{IDENTITY}-{uuid4().hex[:8]}",
        remote_host="github.com",
        provider="github",
        enabled=enabled,
        status="pending_validation",
        execution_profile="development",
        assurance_tier="general-standard",
        sync_requested_at=now,
        sync_next_at=sync_next_at if sync_next_at is not None else now,
        version=1,
    )


def _public_resolver(host, port, *, type):  # noqa: A002
    assert host == "github.com"
    assert port == 443
    assert type == socket.SOCK_STREAM
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.121.4", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.121.4", 443)),
    ]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def test_auth_profiles_are_canonical_host_bound_and_do_not_echo_password():
    secret = "read-only-password-value"
    profiles = load_auth_profiles(
        json.dumps(
            {
                "git-readonly": {
                    "host": "github.com",
                    "username": "x-access-token",
                    "password": secret,
                }
            }
        )
    )

    assert profiles["git-readonly"].host == "github.com"
    assert profiles["git-readonly"].password == secret

    with pytest.raises(RuntimeError) as error:
        load_auth_profiles(
            json.dumps(
                {
                    "GIT-READONLY": {
                        "host": "github.com",
                        "username": "x",
                        "password": secret,
                    }
                }
            )
        )
    assert secret not in str(error.value)


def test_askpass_releases_selected_credential_only_for_bound_host(
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("REPO_MANAGER_GIT_HOST", "github.com")
    monkeypatch.setenv("REPO_MANAGER_GIT_USERNAME", "x-access-token")
    monkeypatch.setenv("REPO_MANAGER_GIT_PASSWORD", "read-only-secret")

    monkeypatch.setattr(
        sys,
        "argv",
        ["askpass", "Password for 'https://x-access-token@github.com/example/repo.git':"],
    )
    assert askpass_main() == 0
    assert capsys.readouterr().out == "read-only-secret"

    monkeypatch.setattr(
        sys,
        "argv",
        ["askpass", "Password for 'https://x-access-token@attacker.example/repo.git':"],
    )
    assert askpass_main() == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "ff02::1",
    ],
)
def test_dns_validation_rejects_every_non_global_address(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def resolver(*_args, **_kwargs):
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    with pytest.raises(RepositorySyncError) as error:
        resolve_public_addresses("github.com", resolver=resolver)

    assert error.value.code == "remote_address_forbidden"
    assert error.value.terminal is True


def test_dns_validation_rejects_mixed_public_private_answers():
    def resolver(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.121.4", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ]

    with pytest.raises(RepositorySyncError) as error:
        resolve_public_addresses("github.com", resolver=resolver)

    assert error.value.code == "remote_address_forbidden"


@pytest.mark.parametrize(
    "branch",
    ["@", "main/.hidden", "main/release.lock/candidate"],
)
def test_remote_head_parser_rejects_unsafe_ref_components(branch):
    with pytest.raises(RepositorySyncError) as error:
        GitRepositorySynchronizer._parse_remote_head(
            f"ref: refs/heads/{branch}\tHEAD\n{COMMIT}\tHEAD\n"
        )

    assert error.value.code == "remote_default_branch_invalid"
    assert error.value.terminal is True


def test_repository_lease_recovery_fences_stale_completion_and_audits():
    start = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
    repository = _repository(sync_next_at=start)
    repository_id = repository.id
    with SessionLocal() as db:
        db.add(repository)
        db.commit()

    first_manager = RepositoryLeaseManager(
        "worker-one", lease_seconds=120, refresh_seconds=3600
    )
    with SessionLocal() as db:
        first = first_manager.claim_available(db, limit=1, now=start)[0]

    with SessionLocal() as db:
        claimed = db.get(Repository, repository_id)
        assert claimed.status == "validating"
        assert claimed.sync_generation == 1
        assert claimed.version == 2

    second_manager = RepositoryLeaseManager(
        "worker-two", lease_seconds=120, refresh_seconds=3600
    )
    recovered_at = start + timedelta(seconds=121)
    with SessionLocal() as db:
        second = second_manager.claim_available(db, limit=1, now=recovered_at)[0]

    with SessionLocal() as db:
        stale = first_manager.mark_success(
            db,
            first,
            RepositorySyncResult(default_branch="main", commit=COMMIT),
            now=recovered_at + timedelta(seconds=1),
        )
    assert stale == "lost"

    with SessionLocal() as db:
        outcome = second_manager.mark_success(
            db,
            second,
            RepositorySyncResult(default_branch="main", commit=COMMIT),
            now=recovered_at + timedelta(seconds=2),
        )
        current = db.get(Repository, repository_id)
        events = list(
            db.query(AuditEvent)
            .filter(AuditEvent.entity_id == repository_id)
            .order_by(AuditEvent.created_at)
        )

    assert outcome == "ready"
    assert current.status == "ready"
    assert current.sync_generation == 2
    assert current.default_branch == "main"
    assert current.last_known_commit == COMMIT
    assert _utc(current.sync_next_at) == recovered_at + timedelta(seconds=3602)
    assert [event.action for event in events] == [
        "repository.sync_claimed",
        "repository.sync_recovered",
        "repository.sync_succeeded",
    ]
    assert all("auth" not in str(event.details).lower() for event in events)


def test_transient_failure_uses_capped_retry_and_terminal_failure_stops():
    start = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
    transient_repository = _repository(sync_next_at=start)
    terminal_repository = _repository(sync_next_at=start)
    with SessionLocal() as db:
        db.add_all([transient_repository, terminal_repository])
        db.commit()

    manager = RepositoryLeaseManager(
        "worker", lease_seconds=120, retry_base_seconds=30, retry_max_seconds=45
    )
    with SessionLocal() as db:
        leases = manager.claim_available(db, limit=2, now=start)
    by_id = {lease.repository_id: lease for lease in leases}

    with SessionLocal() as db:
        transient = manager.mark_failure(
            db,
            by_id[transient_repository.id],
            RepositorySyncError("dns_resolution_failed"),
            now=start + timedelta(seconds=1),
        )
    with SessionLocal() as db:
        terminal = manager.mark_failure(
            db,
            by_id[terminal_repository.id],
            RepositorySyncError("remote_address_forbidden", terminal=True),
            now=start + timedelta(seconds=1),
        )
        transient_row = db.get(Repository, transient_repository.id)
        terminal_row = db.get(Repository, terminal_repository.id)

    assert transient == "unavailable"
    assert _utc(transient_row.sync_next_at) == start + timedelta(seconds=31)
    assert transient_row.last_sync_error_code == "dns_resolution_failed"
    assert terminal == "invalid"
    assert terminal_row.sync_next_at is None
    assert terminal_row.last_sync_error_code == "remote_address_forbidden"


class FakeGitRunner(GitCommandRunner):
    def __init__(self, *, remote_url: str, commit: str):
        self.remote_url = remote_url
        self.commit = commit
        self.calls: list[tuple[list[str], dict[str, str], str]] = []

    def run(
        self,
        arguments,
        *,
        environment,
        timeout_seconds,
        failure_code,
    ):
        self.calls.append((list(arguments), dict(environment), failure_code))
        joined = " ".join(arguments)
        if "ls-remote" in arguments:
            return f"ref: refs/heads/main\tHEAD\n{self.commit}\tHEAD\n"
        if "--is-bare-repository" in arguments:
            return "true\n"
        if "get-url" in arguments:
            return self.remote_url + "\n"
        if "rev-parse" in arguments and "--verify" in arguments:
            return self.commit + "\n"
        return ""


def test_synchronizer_pins_dns_blocks_redirects_and_keeps_secret_out_of_argv(tmp_path):
    secret = "private-read-only-value"
    runner = FakeGitRunner(remote_url=REMOTE, commit=COMMIT)
    repository_id = str(uuid4())
    lease = RepositorySyncLease(
        repository_id=repository_id,
        generation=1,
        record_version=2,
        remote_url=REMOTE,
        remote_identity=IDENTITY,
        remote_host="github.com",
        provider="github",
        auth_profile_ref="git-readonly",
    )
    synchronizer = GitRepositorySynchronizer(
        tmp_path / "mirrors",
        {
            "git-readonly": load_auth_profiles(
                json.dumps(
                    {
                        "git-readonly": {
                            "host": "github.com",
                            "username": "x-access-token",
                            "password": secret,
                        }
                    }
                )
            )["git-readonly"]
        },
        max_repository_bytes=2 * 1024 * 1024,
        min_free_bytes=1024 * 1024,
        resolver=_public_resolver,
        runner=runner,
    )

    result = synchronizer.synchronize(lease, heartbeat=lambda: True)

    assert result == RepositorySyncResult(default_branch="main", commit=COMMIT)
    assert (tmp_path / "mirrors" / f"{repository_id}.git").is_dir()
    all_arguments = "\n".join("\0".join(call[0]) for call in runner.calls)
    assert "http.followRedirects=false" in all_arguments
    assert "http.proxy=" in all_arguments
    assert "http.extraHeader=" in all_arguments
    assert "http.curloptResolve=github.com:443:140.82.121.4" in all_arguments
    assert "protocol.allow=never" in all_arguments
    assert "--prune" in all_arguments
    assert secret not in all_arguments
    for arguments, environment, _ in runner.calls:
        is_network_command = "ls-remote" in arguments or "fetch" in arguments
        if is_network_command:
            assert environment["REPO_MANAGER_GIT_PASSWORD"] == secret
            assert environment["REPO_MANAGER_GIT_HOST"] == "github.com"
        else:
            assert "REPO_MANAGER_GIT_PASSWORD" not in environment
            assert "REPO_MANAGER_GIT_HOST" not in environment
            assert "REPO_MANAGER_GIT_USERNAME" not in environment
        assert environment["GIT_PROTOCOL_FROM_USER"] == "0"
        assert environment["REPO_MANAGER_GIT_MAX_FILE_BLOCKS"] == "4096"
        assert "REPO_MANAGER_AUTH_PROFILES_JSON" not in environment
        assert not any(name in environment for name in ("HTTP_PROXY", "HTTPS_PROXY"))


def test_synchronizer_cleans_only_own_staging_and_stale_git_locks(tmp_path):
    runner = FakeGitRunner(remote_url=REMOTE, commit=COMMIT)
    repository_id = str(uuid4())
    storage_root = tmp_path / "mirrors"
    mirror_path = storage_root / f"{repository_id}.git"
    stale_staging = storage_root / f".{repository_id}.abandoned.tmp"
    unrelated_staging = storage_root / f".{uuid4()}.unrelated.tmp"
    lock_path = mirror_path / "refs" / "heads" / "main.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("stale", encoding="utf-8")
    stale_staging.mkdir()
    (stale_staging / "partial.pack").write_text("partial", encoding="utf-8")
    unrelated_staging.mkdir()

    lease = RepositorySyncLease(
        repository_id=repository_id,
        generation=2,
        record_version=3,
        remote_url=REMOTE,
        remote_identity=IDENTITY,
        remote_host="github.com",
        provider="github",
        auth_profile_ref=None,
    )
    synchronizer = GitRepositorySynchronizer(
        storage_root,
        {},
        min_free_bytes=1024 * 1024,
        resolver=_public_resolver,
        runner=runner,
    )

    result = synchronizer.synchronize(lease, heartbeat=lambda: True)

    assert result.commit == COMMIT
    assert not stale_staging.exists()
    assert not lock_path.exists()
    assert unrelated_staging.is_dir()


def test_synchronizer_stops_before_git_when_storage_reserve_is_exhausted(
    tmp_path,
    monkeypatch,
):
    runner = FakeGitRunner(remote_url=REMOTE, commit=COMMIT)
    lease = RepositorySyncLease(
        repository_id=str(uuid4()),
        generation=1,
        record_version=2,
        remote_url=REMOTE,
        remote_identity=IDENTITY,
        remote_host="github.com",
        provider="github",
        auth_profile_ref=None,
    )
    synchronizer = GitRepositorySynchronizer(
        tmp_path / "mirrors",
        {},
        min_free_bytes=512 * 1024 * 1024,
        resolver=_public_resolver,
        runner=runner,
    )
    monkeypatch.setattr(
        repository_manager_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=512 * 1024 * 1024),
    )

    with pytest.raises(RepositorySyncError) as error:
        synchronizer.synchronize(lease, heartbeat=lambda: True)

    assert error.value.code == "storage_capacity_low"
    assert error.value.terminal is False
    assert runner.calls == []


def test_synchronizer_rejects_symlinked_directory_inside_existing_mirror(tmp_path):
    repository_id = str(uuid4())
    storage_root = tmp_path / "mirrors"
    mirror_path = storage_root / f"{repository_id}.git"
    mirror_path.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (mirror_path / "objects").symlink_to(outside, target_is_directory=True)
    runner = FakeGitRunner(remote_url=REMOTE, commit=COMMIT)
    synchronizer = GitRepositorySynchronizer(
        storage_root,
        {},
        min_free_bytes=0,
        resolver=_public_resolver,
        runner=runner,
    )
    lease = RepositorySyncLease(
        repository_id=repository_id,
        generation=1,
        record_version=2,
        remote_url=REMOTE,
        remote_identity=IDENTITY,
        remote_host="github.com",
        provider="github",
        auth_profile_ref=None,
    )

    with pytest.raises(RepositorySyncError) as error:
        synchronizer.synchronize(lease, heartbeat=lambda: True)

    assert error.value.code == "mirror_symlink_detected"
    assert error.value.terminal is True
    assert runner.calls == []


def test_synchronizer_formats_ipv6_pins_without_expiring_them(tmp_path):
    synchronizer = GitRepositorySynchronizer(
        tmp_path / "mirrors",
        {},
        min_free_bytes=0,
    )

    prefix = synchronizer._git_prefix(
        "github.com",
        ("140.82.121.4", "2606:50c0:8000::154"),
    )
    joined = "\n".join(prefix)

    assert "http.curloptResolve=github.com:443:140.82.121.4,[2606:50c0:8000::154]" in joined
    assert "http.curloptResolve=+" not in joined


def test_repository_filesystem_lock_fails_closed_for_overlapping_generation(tmp_path):
    repository_id = str(uuid4())
    storage_root = tmp_path / "mirrors"
    first = GitRepositorySynchronizer(storage_root, {}, min_free_bytes=0)
    runner = FakeGitRunner(remote_url=REMOTE, commit=COMMIT)
    second = GitRepositorySynchronizer(
        storage_root,
        {},
        min_free_bytes=0,
        resolver=_public_resolver,
        runner=runner,
    )
    lease = RepositorySyncLease(
        repository_id=repository_id,
        generation=2,
        record_version=3,
        remote_url=REMOTE,
        remote_identity=IDENTITY,
        remote_host="github.com",
        provider="github",
        auth_profile_ref=None,
    )
    first._prepare_storage_root()

    with first._repository_lock(repository_id):
        with pytest.raises(RepositorySyncError) as error:
            second.synchronize(lease, heartbeat=lambda: True)

    assert error.value.code == "mirror_busy"
    assert error.value.terminal is False
    assert runner.calls == []


def test_synchronizer_revalidates_registry_identity_before_network(tmp_path):
    called = False

    def resolver(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    lease = RepositorySyncLease(
        repository_id=str(uuid4()),
        generation=1,
        record_version=2,
        remote_url=REMOTE,
        remote_identity="github.com/attacker/repository",
        remote_host="github.com",
        provider="github",
        auth_profile_ref=None,
    )
    synchronizer = GitRepositorySynchronizer(
        tmp_path / "mirrors", {}, resolver=resolver
    )

    with pytest.raises(RepositorySyncError) as error:
        synchronizer.synchronize(lease, heartbeat=lambda: True)

    assert error.value.code == "registry_identity_mismatch"
    assert error.value.terminal is True
    assert called is False


def test_auth_profile_host_mismatch_is_terminal_before_dns_or_git(tmp_path):
    called = False

    def resolver(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    profiles = load_auth_profiles(
        json.dumps(
            {
                "git-readonly": {
                    "host": "gitlab.com",
                    "username": "reader",
                    "password": "read-only-secret",
                }
            }
        )
    )
    runner = FakeGitRunner(remote_url=REMOTE, commit=COMMIT)
    synchronizer = GitRepositorySynchronizer(
        tmp_path / "mirrors",
        profiles,
        resolver=resolver,
        runner=runner,
    )
    lease = RepositorySyncLease(
        repository_id=str(uuid4()),
        generation=1,
        record_version=2,
        remote_url=REMOTE,
        remote_identity=IDENTITY,
        remote_host="github.com",
        provider="github",
        auth_profile_ref="git-readonly",
    )

    with pytest.raises(RepositorySyncError) as error:
        synchronizer.synchronize(lease, heartbeat=lambda: True)

    assert error.value.code == "auth_profile_host_mismatch"
    assert error.value.terminal is True
    assert called is False
    assert runner.calls == []


def test_git_runner_discards_stderr_and_returns_only_classified_error(monkeypatch):
    secret = "server-echoed-secret"

    class FailedProcess:
        returncode = 1

        def communicate(self, *, timeout):
            assert timeout == 10
            return "", None

    def fake_popen(*_args, **_kwargs):
        assert _kwargs["stderr"] is subprocess.DEVNULL
        assert _kwargs["start_new_session"] is True
        return FailedProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    runner = GitCommandRunner()
    with pytest.raises(RepositorySyncError) as error:
        runner.run(
            ["git", "fetch", secret],
            environment={},
            timeout_seconds=10,
            failure_code="git_fetch_failed",
        )

    assert str(error.value) == "git_fetch_failed"
    assert secret not in str(error.value)


def test_git_runner_kills_process_group_on_timeout(monkeypatch):
    killed: list[tuple[int, int]] = []

    class TimedOutProcess:
        pid = 4242
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, *, timeout=None):
            self.calls += 1
            if self.calls == 1:
                assert timeout == 7
                raise subprocess.TimeoutExpired(cmd="git", timeout=7)
            assert timeout is None
            return "", None

    process = TimedOutProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        repository_manager_module.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    with pytest.raises(RepositorySyncError) as error:
        GitCommandRunner().run(
            ["git", "fetch"],
            environment={},
            timeout_seconds=7,
            failure_code="git_fetch_failed",
        )

    assert error.value.code == "git_operation_timed_out"
    assert killed == [(4242, repository_manager_module.signal.SIGKILL)]
    assert process.calls == 2
