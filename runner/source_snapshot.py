from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


class SourceSnapshotError(RuntimeError):
    pass


def _field(value: bytes) -> bytes:
    return len(value).to_bytes(8, "big") + value


def _relative_bytes(path: Path, root: Path) -> bytes:
    return path.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape")


def _file_digest(path: Path, before: os.stat_result) -> bytes:
    digest = hashlib.sha256()
    try:
        with path.open("rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise SourceSnapshotError("source_snapshot_changed")
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise SourceSnapshotError("source_snapshot_unavailable") from exc
    identity_before = (
        before.st_size,
        before.st_mtime_ns,
        before.st_mode,
    )
    identity_after = (
        after.st_size,
        after.st_mtime_ns,
        after.st_mode,
    )
    if identity_before != identity_after:
        raise SourceSnapshotError("source_snapshot_changed")
    return digest.digest()


def source_snapshot_digest(root: Path, *, max_entries: int = 100_000) -> str:
    if not 1 <= max_entries <= 1_000_000:
        raise ValueError("max_entries must be between 1 and 1000000")
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise SourceSnapshotError("source_snapshot_unavailable") from exc
    if root.is_symlink() or not stat.S_ISDIR(root_stat.st_mode):
        raise SourceSnapshotError("source_snapshot_unavailable")

    digest = hashlib.sha256()
    count = 0
    for current_text, dir_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_text)
        if current == root:
            dir_names[:] = [name for name in dir_names if name != ".git"]
            file_names[:] = [name for name in file_names if name != ".git"]
        dir_names.sort()
        file_names.sort()
        traversable: list[str] = []
        for name in dir_names:
            path = current / name
            try:
                item = path.lstat()
            except OSError as exc:
                raise SourceSnapshotError("source_snapshot_unavailable") from exc
            count += 1
            if count > max_entries:
                raise SourceSnapshotError("source_snapshot_entry_limit")
            rel = _relative_bytes(path, root)
            if stat.S_ISLNK(item.st_mode):
                try:
                    target = os.readlink(path).encode("utf-8", errors="surrogateescape")
                except OSError as exc:
                    raise SourceSnapshotError("source_snapshot_unavailable") from exc
                digest.update(b"L" + _field(rel) + _field(target))
                continue
            if not stat.S_ISDIR(item.st_mode):
                raise SourceSnapshotError("source_snapshot_special_file")
            digest.update(b"D" + _field(rel) + _field(str(stat.S_IMODE(item.st_mode)).encode()))
            traversable.append(name)
        dir_names[:] = traversable

        for name in file_names:
            path = current / name
            try:
                item = path.lstat()
            except OSError as exc:
                raise SourceSnapshotError("source_snapshot_unavailable") from exc
            count += 1
            if count > max_entries:
                raise SourceSnapshotError("source_snapshot_entry_limit")
            rel = _relative_bytes(path, root)
            if stat.S_ISLNK(item.st_mode):
                try:
                    target = os.readlink(path).encode("utf-8", errors="surrogateescape")
                except OSError as exc:
                    raise SourceSnapshotError("source_snapshot_unavailable") from exc
                digest.update(b"L" + _field(rel) + _field(target))
                continue
            if not stat.S_ISREG(item.st_mode):
                raise SourceSnapshotError("source_snapshot_special_file")
            content_digest = _file_digest(path, item)
            digest.update(
                b"F"
                + _field(rel)
                + _field(str(stat.S_IMODE(item.st_mode)).encode())
                + _field(str(item.st_size).encode())
                + _field(content_digest)
            )

    return digest.hexdigest()
