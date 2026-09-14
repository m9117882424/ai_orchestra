from __future__ import annotations

import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _task_archive(
    path: Path,
    *,
    escaping_symlink: bool = False,
    git_directory_symlink: bool = False,
) -> None:
    with tarfile.open(path, "w:gz") as bundle:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        root.mode = 0o700
        bundle.addfile(root)
        if escaping_symlink:
            workspace_id = str(uuid4())
            directory = tarfile.TarInfo(workspace_id)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o700
            bundle.addfile(directory)
            link = tarfile.TarInfo(f"{workspace_id}/escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../etc/passwd"
            bundle.addfile(link)
        if git_directory_symlink:
            workspace_id = str(uuid4())
            directory = tarfile.TarInfo(workspace_id)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o700
            bundle.addfile(directory)
            metadata = tarfile.TarInfo(f"{workspace_id}/metadata")
            metadata.type = tarfile.DIRTYPE
            metadata.mode = 0o700
            bundle.addfile(metadata)
            link = tarfile.TarInfo(f"{workspace_id}/.git")
            link.type = tarfile.SYMTYPE
            link.linkname = "metadata"
            bundle.addfile(link)


def _backup_archive(
    tmp_path: Path,
    *,
    unsafe_checksum: bool = False,
    duplicate_path: bool = False,
    escaping_workspace_symlink: bool = False,
    git_directory_symlink: bool = False,
) -> Path:
    payload = tmp_path / "payload"
    app_dir = payload / "configuration" / "control_plane" / "app"
    app_dir.mkdir(parents=True)
    (payload / "BACKUP_FORMAT").write_text("2\n", encoding="utf-8")
    (payload / "control-plane.pgdump").write_bytes(b"test-postgresql-dump")
    (payload / "configuration" / "docker-compose.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (payload / "configuration" / "Makefile").write_text(
        "all:\n\t@true\n",
        encoding="utf-8",
    )
    (payload / "configuration" / "control_plane" / "alembic.ini").write_text(
        "[alembic]\n",
        encoding="utf-8",
    )
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    _task_archive(
        payload / "task-workspaces.tar.gz",
        escaping_symlink=escaping_workspace_symlink,
        git_directory_symlink=git_directory_symlink,
    )

    checksum_lines = []
    for candidate in sorted(path for path in payload.rglob("*") if path.is_file()):
        relative = candidate.relative_to(payload).as_posix()
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  ./{relative}\n")
    if unsafe_checksum:
        checksum_lines.append(f"{'0' * 64}  ../../outside\n")
    (payload / "SHA256SUMS").write_text("".join(checksum_lines), encoding="utf-8")

    archive = tmp_path / "ai-orchestra-test.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname=".")
        if duplicate_path:
            content = (payload / "control-plane.pgdump").read_bytes()
            duplicate = tarfile.TarInfo("././control-plane.pgdump")
            duplicate.size = len(content)
            duplicate.mode = 0o600
            bundle.addfile(duplicate, io.BytesIO(content))
    return archive


def _verify(archive: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "./scripts/verify-backup.sh", str(archive)],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_format_two_backup_verifier_accepts_safe_complete_archive(tmp_path):
    result = _verify(_backup_archive(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "checksum inventory safe" in result.stdout
    assert "task workspace archive safe" in result.stdout
    assert "Backup verified: format=2" in result.stdout


def test_backup_verifier_rejects_checksum_path_traversal(tmp_path):
    result = _verify(_backup_archive(tmp_path, unsafe_checksum=True))

    assert result.returncode != 0
    assert "Unsafe SHA256SUMS path" in result.stdout + result.stderr


def test_backup_verifier_rejects_canonical_duplicate_outer_path(tmp_path):
    result = _verify(_backup_archive(tmp_path, duplicate_path=True))

    assert result.returncode != 0
    assert "duplicate archive path" in result.stdout + result.stderr


def test_backup_verifier_rejects_escaping_workspace_symlink(tmp_path):
    result = _verify(
        _backup_archive(tmp_path, escaping_workspace_symlink=True)
    )

    assert result.returncode != 0
    assert "escaping workspace symlink" in result.stdout + result.stderr


def test_backup_verifier_rejects_symlinked_git_metadata_directory(tmp_path):
    result = _verify(
        _backup_archive(tmp_path, git_directory_symlink=True)
    )

    assert result.returncode != 0
    assert "escaping workspace symlink" in result.stdout + result.stderr
