import os
from pathlib import Path

import pytest

from control_plane.app.source_snapshot import SourceSnapshotError, source_snapshot_digest


def test_snapshot_is_deterministic_and_ignores_root_git(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "b.txt").write_text("beta", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "volatile").write_text("one", encoding="utf-8")
    first = source_snapshot_digest(tmp_path)
    (tmp_path / ".git" / "volatile").write_text("two", encoding="utf-8")
    second = source_snapshot_digest(tmp_path)
    assert first == second


def test_snapshot_changes_for_content_mode_and_symlink_target(tmp_path: Path):
    target = tmp_path / "file.txt"
    target.write_text("one", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to("file.txt")
    first = source_snapshot_digest(tmp_path)
    target.write_text("two", encoding="utf-8")
    second = source_snapshot_digest(tmp_path)
    assert second != first
    os.chmod(target, 0o755)
    third = source_snapshot_digest(tmp_path)
    assert third != second
    link.unlink()
    (tmp_path / "other.txt").write_text("x", encoding="utf-8")
    link.symlink_to("other.txt")
    fourth = source_snapshot_digest(tmp_path)
    assert fourth != third


def test_snapshot_rejects_special_file_and_entry_limit(tmp_path: Path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(SourceSnapshotError, match="source_snapshot_special_file"):
        source_snapshot_digest(tmp_path)
    fifo.unlink()
    (tmp_path / "a").write_text("a", encoding="utf-8")
    (tmp_path / "b").write_text("b", encoding="utf-8")
    with pytest.raises(SourceSnapshotError, match="source_snapshot_entry_limit"):
        source_snapshot_digest(tmp_path, max_entries=1)
