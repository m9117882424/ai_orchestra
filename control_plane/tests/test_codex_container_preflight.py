"""Offline checks for the staging gate's mount and private-path primitives."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / "deploy/codex-worker/preflight.py"
SPEC = importlib.util.spec_from_file_location("codex_container_preflight", SOURCE)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class ContainerPreflightTest(unittest.TestCase):
    def test_private_directory_rejects_broad_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "home"
            path.mkdir(mode=0o700)
            if os.getuid() == 10001:
                preflight.private_directory(path)
            path.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "Invalid private directory"):
                preflight.private_directory(path)

    def test_mount_id_uses_mount_identity_not_device(self):
        original = Path.read_text

        def mountinfo(path, *args, **kwargs):
            if str(path) == "/proc/self/mountinfo":
                return "11 1 0:1 / /home/orchestra rw - ext4 /dev/sda rw\n12 1 0:1 / /workspace/worktrees/managed ro - ext4 /dev/sda ro\n"
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", mountinfo):
            self.assertEqual(preflight.mount_id(Path("/home/orchestra")), "11")
            self.assertEqual(preflight.mount_id(Path("/workspace/worktrees/managed")), "12")
            with self.assertRaisesRegex(RuntimeError, "Expected a dedicated mount"):
                preflight.mount_id(Path("/private/codex-results"))
