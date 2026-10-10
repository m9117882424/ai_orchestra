#!/usr/bin/env python3
"""Constrained SSH transport for Repo Manager's canonical GitHub mirrors.

This executable is invoked by Git only for ls-remote/fetch. It never reads or
prints a private key and it does not use the host user's SSH configuration.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import stat
import sys
from pathlib import Path

GITHUB_ED25519_FINGERPRINT = "+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
SECRET_PATH = re.compile(r"/run/secrets/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
REPO_COMMAND = re.compile(
    r"git-upload-pack '/?[A-Za-z0-9][A-Za-z0-9._~-]*/[A-Za-z0-9][A-Za-z0-9._~-]*'"
)
ALLOWED_GIT_OPTION = "SendEnv=GIT_PROTOCOL"


def _require_regular_file(filename: str, *, private: bool) -> None:
    if not SECRET_PATH.fullmatch(filename):
        raise ValueError("SSH material path must be a direct /run/secrets mount")
    st = os.lstat(filename)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError("SSH material must be a regular, non-linked file")
    if private:
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise ValueError("SSH identity must be owned by Repo Manager and mode 0400/0600")
    if not os.access(filename, os.R_OK):
        raise ValueError("SSH material is not readable by Repo Manager")


def _require_github_host_key(filename: str) -> None:
    # A pinned key from https://docs.github.com/.../githubs-ssh-key-fingerprints.
    # No trust-on-first-use and no automatic host-key updates.
    with open(filename, "r", encoding="ascii") as file:
        lines = [line.strip() for line in file if line.strip() and not line.startswith("#")]
    if len(lines) != 1:
        raise ValueError("Expected exactly one pinned GitHub host key")
    parts = lines[0].split()
    if len(parts) != 3 or parts[:2] != ["github.com", "ssh-ed25519"]:
        raise ValueError("GitHub known_hosts entry invalid")
    pubkey = base64.b64decode(parts[2], validate=True)
    digest = base64.b64encode(hashlib.sha256(pubkey).digest()).decode().rstrip("=")
    if digest != GITHUB_ED25519_FINGERPRINT:
        raise ValueError("GitHub host key fingerprint does not match pinned fingerprint")


def _parse_git_ssh_argv(arguments: list[str], host: str) -> list[str]:
    # Git probes OpenSSH capabilities with -G and optionally sends Git protocol.
    extras: list[str] = []
    index = 0
    probe = False
    while index < len(arguments):
        arg = arguments[index]
        if arg == "-G" and not probe:
            probe = True
            extras.append(arg)
            index += 1
        elif arg == "-o" and index + 1 < len(arguments):
            if arguments[index + 1] != ALLOWED_GIT_OPTION:
                raise ValueError("Unexpected SSH argument")
            extras.extend(arguments[index : index + 2])
            index += 2
        elif arg == "-o" + ALLOWED_GIT_OPTION:
            extras.append(arg)
            index += 1
        else:
            break
    remainder = arguments[index:]
    if not remainder or remainder[0] != f"git@{host}":
        raise ValueError("Only the configured GitHub host is allowed")
    if probe:
        if len(remainder) != 1:
            raise ValueError("Invalid SSH capability probe")
    elif len(remainder) != 2 or not REPO_COMMAND.fullmatch(remainder[1]):
        raise ValueError("Only repository read commands are allowed")
    return extras + remainder


def build_ssh_command(arguments: list[str]) -> list[str]:
    host = os.environ.get("REPO_MANAGER_SSH_HOST", "")
    address = os.environ.get("REPO_MANAGER_SSH_PINNED_IP", "")
    identity = os.environ.get("REPO_MANAGER_SSH_KEY_FILE", "")
    known_hosts = os.environ.get("REPO_MANAGER_SSH_KNOWN_HOSTS_FILE", "")
    if host != "github.com":
        raise ValueError("Only github.com SSH deployment is enabled")
    if not ipaddress.ip_address(address).is_global:
        raise ValueError("SSH destination IP is not public")
    _require_regular_file(identity, private=True)
    _require_regular_file(known_hosts, private=False)
    _require_github_host_key(known_hosts)
    return [
        "/usr/bin/ssh",
        "-F", "/dev/null",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "HostKeyAlgorithms=ssh-ed25519",
        "-o", "UpdateHostKeys=no",
        "-o", f"HostKeyAlias={host}",
        "-o", f"HostName={address}",
        "-o", "ConnectTimeout=12",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "ProxyCommand=none",
        "-o", "ProxyJump=none",
        "-o", "ControlMaster=no",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        "-i", identity,
        *_parse_git_ssh_argv(arguments, host),
    ]


def main() -> int:
    try:
        arguments = build_ssh_command(sys.argv[1:])
    except (ValueError, OSError, UnicodeError):
        # Never echo arguments, paths, credentials or SSH configuration.
        return 70
    os.execv("/usr/bin/ssh", arguments)
    return 0


if __name__ == "__main__":
    sys.exit(main())
