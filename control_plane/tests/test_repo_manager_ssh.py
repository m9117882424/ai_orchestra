"""Read-only SSH deploy-key transport for trusted repository mirrors."""
from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from uuid import uuid4

import pytest

from control_plane.app.repository_manager import (
    GitCommandRunner,
    GitRepositorySynchronizer,
    GitSSHCredential,
    RepositorySyncLease,
    RepositorySyncResult,
    load_auth_profiles,
)
from control_plane.app import repo_manager_ssh


SSH_CONFIG = {
    "ssh-deploy": {
        "host": "github.com",
        "transport": "ssh",
        "identity_file": "/run/secrets/taxi-deploy-key",
        "known_hosts_file": "/run/secrets/github-known-hosts",
    }
}
HEAD = "c" * 40
REMOTE = "https://github.com/m9117882424/corporate_taxi.git"
IDENTITY = "github.com/m9117882424/corporate_taxi"


def test_ssh_auth_is_host_bound_and_never_accepts_embedded_secret():
    profiles = load_auth_profiles(json.dumps(SSH_CONFIG))
    assert profiles == {
        "ssh-deploy": GitSSHCredential(
            "github.com", "/run/secrets/taxi-deploy-key", "/run/secrets/github-known-hosts"
        )
    }
    for replacement in (
        {"host": "evil.example"},
        {"identity_file": "/etc/passwd"},
        {"identity_file": "/run/secrets/../etc/passwd"},
        {"known_hosts_file": "/run/secrets/known-hosts\nhack"},
        {"transport": "http"},
    ):
        invalid = {**SSH_CONFIG["ssh-deploy"], **replacement}
        with pytest.raises(RuntimeError):
            load_auth_profiles(json.dumps({"ssh-deploy": invalid}))


class FakeRunner(GitCommandRunner):
    def __init__(self):
        self.calls = []

    def run(self, arguments, *, environment, timeout_seconds, failure_code):
        self.calls.append((list(arguments), dict(environment)))
        if "ls-remote" in arguments:
            return f"ref: refs/heads/main\tHEAD\n{HEAD}\tHEAD\n"
        if "--is-bare-repository" in arguments:
            return "true\n"
        if "get-url" in arguments:
            return REMOTE + "\n"
        if "rev-parse" in arguments and "--verify" in arguments:
            return HEAD + "\n"
        return ""


def test_ssh_network_only_and_canonical_registry_mirror(tmp_path):
    def public_resolver(host, port, *, type):
        assert host == "github.com"
        assert port == 443
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.121.4", 443))]

    runner = FakeRunner()
    synchronizer = GitRepositorySynchronizer(
        tmp_path / "mirrors",
        load_auth_profiles(json.dumps(SSH_CONFIG)),
        resolver=public_resolver,
        runner=runner,
        min_free_bytes=0,
        max_repository_bytes=4 * 1024 * 1024,
    )
    lease = RepositorySyncLease(
        repository_id=str(uuid4()),
        generation=1,
        record_version=2,
        remote_url=REMOTE,
        remote_identity=IDENTITY,
        remote_host="github.com",
        provider="github",
        auth_profile_ref="ssh-deploy",
    )
    assert synchronizer.synchronize(lease, heartbeat=lambda: True) == RepositorySyncResult(
        default_branch="main", commit=HEAD
    )
    network_calls = [(a, env) for a, env in runner.calls if "ls-remote" in a or "fetch" in a]
    assert len(network_calls) == 2
    for arguments, environment in network_calls:
        assert "git@github.com:m9117882424/corporate_taxi.git" in arguments
        assert "protocol.ssh.allow=always" in arguments
        assert "protocol.https.allow=always" not in arguments
        assert environment["REPO_MANAGER_SSH_PINNED_IP"] == "140.82.121.4"
        assert environment["GIT_ALLOW_PROTOCOL"] == "ssh"
        assert environment["REPO_MANAGER_SSH_HOST"] == "github.com"
        assert "REPO_MANAGER_GIT_PASSWORD" not in environment
    local_calls = [(a, env) for a, env in runner.calls if "ls-remote" not in a and "fetch" not in a]
    assert local_calls
    assert all("REPO_MANAGER_SSH_KEY_FILE" not in env for _, env in local_calls)
    assert all("GIT_SSH" not in env for _, env in local_calls)
    assert all("protocol.ssh.allow=always" in args for args, _ in local_calls)
    assert (tmp_path / "mirrors" / f"{lease.repository_id}.git").is_dir()


def test_ssh_wrapper_pins_public_endpoint_and_read_command(monkeypatch):
    monkeypatch.setenv("REPO_MANAGER_SSH_HOST", "github.com")
    monkeypatch.setenv("REPO_MANAGER_SSH_PINNED_IP", "140.82.121.4")
    monkeypatch.setenv("REPO_MANAGER_SSH_KEY_FILE", "/run/secrets/taxi-deploy-key")
    monkeypatch.setenv("REPO_MANAGER_SSH_KNOWN_HOSTS_FILE", "/run/secrets/github-known-hosts")
    monkeypatch.setattr(repo_manager_ssh, "_require_regular_file", lambda *_a, **_k: None)
    monkeypatch.setattr(repo_manager_ssh, "_require_github_host_key", lambda *_a: None)
    for argv in (
        ["-G", "git@github.com"],
        ["-o", "SendEnv=GIT_PROTOCOL", "git@github.com",
         "git-upload-pack '/m9117882424/corporate_taxi.git'"],
    ):
        args = repo_manager_ssh.build_ssh_command(argv)
        assert args[0] == "/usr/bin/ssh"
        assert "-F" in args and "/dev/null" in args
        assert "HostName=140.82.121.4" in args
        assert "HostKeyAlias=github.com" in args
        assert "StrictHostKeyChecking=yes" in args
        assert "ProxyCommand=none" in args
        assert args[-len(argv):] == argv
    for argv in (
        ["git@attacker.example", "git-upload-pack '/m9117882424/corporate_taxi.git'"],
        ["git@github.com", "git-receive-pack '/m9117882424/corporate_taxi.git'"],
        ["-o", "ProxyCommand=attacker", "git@github.com"],
        ["git@github.com", "git-upload-pack '../escape'"],
    ):
        with pytest.raises(ValueError):
            repo_manager_ssh.build_ssh_command(argv)
    monkeypatch.setenv("REPO_MANAGER_SSH_PINNED_IP", "127.0.0.1")
    with pytest.raises(ValueError):
        repo_manager_ssh.build_ssh_command(["-G", "git@github.com"])


def test_ssh_material_requires_private_key_0600_and_pinned_github_host(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_manager_ssh, "SECRET_PATH", re.compile(
        re.escape(str(tmp_path)) + r"/[A-Za-z0-9._-]+"
    ))
    identity = tmp_path / "identity"
    identity.write_text("synthetic-unusable-key", encoding="ascii")
    identity.chmod(0o600)
    repo_manager_ssh._require_regular_file(str(identity), private=True)
    identity.chmod(0o644)
    with pytest.raises(ValueError):
        repo_manager_ssh._require_regular_file(str(identity), private=True)
    identity.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(identity)
    with pytest.raises(ValueError):
        repo_manager_ssh._require_regular_file(str(link), private=True)

    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "github.com ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n",
        encoding="ascii",
    )
    repo_manager_ssh._require_github_host_key(str(known_hosts))
    known_hosts.write_text("github.com ssh-ed25519 AAAA\n", encoding="ascii")
    with pytest.raises(ValueError):
        repo_manager_ssh._require_github_host_key(str(known_hosts))
