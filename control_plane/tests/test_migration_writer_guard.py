import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("writer", [
    "control-plane", "execution-worker", "repo-manager", "workspace-manager",
    "opencode", "runner-manager",
])
def test_migration_refuses_running_writer_before_backup_or_schema_change(tmp_path, writer):
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\n'
        'if [ "$*" = "compose ps --status running --services" ]; then\n'
        '  printf "postgres\\n%s\\n" "$TEST_RUNNING_WRITER"\n'
        'else\n'
        '  echo "unexpected docker call" >&2\n'
        '  exit 91\n'
        'fi\n'
    )
    docker.chmod(0o755)
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, TEST_RUNNING_WRITER=writer)
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    result = subprocess.run(
        ["bash", str(root / "scripts/migrate-control-plane.sh")],
        env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 1
    assert f"Остановите writer {writer}" in result.stderr
    assert "unexpected docker call" not in result.stderr
    assert "Создаю обязательную резервную копию" not in result.stdout
