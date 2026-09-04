#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CP = ROOT / "control_plane"
MANIFEST = CP / "dependency-locks.sha256"
TRACKED = (
    "requirements.in",
    "requirements-dev.in",
    "requirements.lock",
    "requirements-dev.lock",
)
PACKAGE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\\\s]+)\s*\\?$")
SOURCE_DIRECTIVE_RE = re.compile(
    r"^\s*--(?:no-index|index-url|extra-index-url|trusted-host|find-links)(?:[=\s]|$)",
    re.IGNORECASE,
)
FORBIDDEN = ("git+", "hg+", "svn+", "bzr+", "-e ", "@ http://", "@ https://")
EXPECTED_HEADERS = {
    "requirements.lock": (
        "# AI Orchestra deterministic dependency lock",
        "# generator: pip-tools==7.6.1; python: 3.12.14; resolver: backtracking",
        "# package-source policy: https://pypi.org/simple (resolution only; not embedded)",
        "# input: requirements.in",
        "# install policy: exact pins + SHA-256 hashes required",
    ),
    "requirements-dev.lock": (
        "# AI Orchestra deterministic dependency lock",
        "# generator: pip-tools==7.6.1; python: 3.12.14; resolver: backtracking",
        "# package-source policy: https://pypi.org/simple (resolution only; not embedded)",
        "# input: requirements-dev.in",
        "# install policy: exact pins + SHA-256 hashes required",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_manifest() -> dict[str, str]:
    entries: dict[str, str] = {}
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(maxsplit=1)
        assert len(parts) == 2, f"Invalid dependency manifest line: {raw!r}"
        digest, name = parts
        name = name.lstrip("*")
        assert re.fullmatch(r"[0-9a-f]{64}", digest), f"Invalid SHA-256: {digest!r}"
        assert name not in entries, f"Duplicate manifest entry: {name}"
        entries[name] = digest
    assert set(entries) == set(TRACKED), (
        f"Manifest entries differ: actual={sorted(entries)}, expected={sorted(TRACKED)}"
    )
    return entries


def verify_lock_policy(path: Path, text: str) -> None:
    lines = text.splitlines()
    expected = EXPECTED_HEADERS[path.name]
    actual = tuple(lines[: len(expected)])
    assert actual == expected, (
        f"Unexpected provenance header in {path.name}: actual={actual!r}, expected={expected!r}"
    )

    for line_number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        assert not SOURCE_DIRECTIVE_RE.match(line), (
            f"Forbidden package-source directive in {path.name}:{line_number}: {line!r}"
        )


def package_blocks(path: Path) -> dict[str, tuple[str, list[str]]]:
    text = path.read_text(encoding="utf-8")
    verify_lock_policy(path, text)
    lowered = text.lower()
    for marker in FORBIDDEN:
        assert marker not in lowered, f"Forbidden dependency source {marker!r} in {path.name}"

    blocks: dict[str, tuple[str, list[str]]] = {}
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        first = current[0].strip()
        match = PACKAGE_RE.match(first)
        if match:
            name = match.group(1).lower().replace("_", "-")
            version = match.group(2)
            assert name not in blocks, f"Duplicate package {name} in {path.name}"
            assert any("--hash=sha256:" in line for line in current), (
                f"Package {name} has no SHA-256 hash in {path.name}"
            )
            blocks[name] = (version, current[:])
        elif not first.startswith(("#", "--")):
            raise AssertionError(f"Unpinned requirement block in {path.name}: {first!r}")
        current = []

    for line in text.splitlines():
        if line and not line.startswith((" ", "#", "--")):
            flush()
            current = [line]
        elif current:
            current.append(line)
    flush()
    assert blocks, f"No pinned packages parsed from {path.name}"
    return blocks


def main() -> int:
    manifest = parse_manifest()
    for name in TRACKED:
        actual = sha256(CP / name)
        assert actual == manifest[name], (
            f"SHA-256 mismatch for {name}: actual={actual}, expected={manifest[name]}"
        )

    runtime = package_blocks(CP / "requirements.lock")
    development = package_blocks(CP / "requirements-dev.lock")
    missing = sorted(set(runtime) - set(development))
    assert not missing, f"Runtime packages missing from development lock: {missing}"
    conflicts = {
        name: (runtime[name][0], development[name][0])
        for name in runtime
        if runtime[name][0] != development[name][0]
    }
    assert not conflicts, f"Runtime/dev lock version conflicts: {conflicts}"

    runtime_wrapper = (CP / "requirements.txt").read_text(encoding="utf-8")
    dev_wrapper = (CP / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "--require-hashes" in runtime_wrapper and "-r requirements.lock" in runtime_wrapper
    assert "--require-hashes" in dev_wrapper and "-r requirements-dev.lock" in dev_wrapper

    print(
        f"[OK] dependency locks: runtime={len(runtime)} packages, "
        f"development={len(development)} packages"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
