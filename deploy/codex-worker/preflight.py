"""Fail before claiming if the staged container cannot honor the G2 boundary."""
import os
import re
import stat
import subprocess
from pathlib import Path

from scripts.codex_task import subscription_environment, verify_chatgpt_auth


def private_directory(path: Path) -> None:
    info = path.stat()
    if not path.is_dir() or info.st_uid != 10001 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError(f"Invalid private directory: {path}")


def mount_id(path: Path) -> str:
    # Bind mounts can have the same st_dev while remaining independent volumes.
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] == str(path):
            return fields[0]
    raise RuntimeError(f"Expected a dedicated mount: {path}")


def main() -> None:
    if os.geteuid() != 10001 or os.getegid() != 10001:
        raise RuntimeError("Worker identity must be 10001:10001")
    root = Path("/workspace/worktrees/managed")
    if not os.statvfs("/").f_flag & os.ST_RDONLY:
        raise RuntimeError("Container root filesystem must be read-only")
    if not os.statvfs(root).f_flag & os.ST_RDONLY:
        raise RuntimeError("G2 workspace mount must be read-only")
    if Path("/var/run/docker.sock").exists() or Path("/run/docker.sock").exists():
        raise RuntimeError("Docker socket is accessible")
    forbidden = {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                 "AITUNNEL_API_KEY", "CONTROL_PLANE_DB_PASSWORD", "CONTROL_PLANE_SERVER_PASSWORD",
                 "MODEL_ROUTER_MASTER_KEY", "MODEL_ROUTER_CLIENT_KEY"}
    if forbidden.intersection(os.environ) or any(key.endswith("_TOKEN") for key in os.environ):
        raise RuntimeError("Forbidden credential environment variable")
    home = Path("/home/orchestra")
    private_directory(home)
    private_directory(home / ".codex")
    private_directory(Path("/private/codex-results"))
    token_dir = Path("/run/codex-subscription-private")
    private_directory(token_dir)
    token = token_dir / "one-shot.token"
    info = token.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 10001 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
        raise RuntimeError("Invalid one-shot token file")
    if mount_id(home) == mount_id(root):
        raise RuntimeError("HOME must reside on a separate mount from G2")
    workspace_id = os.environ.get("CODEX_PREFLIGHT_WORKSPACE_ID", "")
    if not re.fullmatch(r"[0-9a-f-]{36}", workspace_id):
        raise RuntimeError("A prepared G2 workspace ID is required")
    workspace = root / workspace_id
    manifest = workspace / ".git/ai-orchestra-workspace.json"
    for path, mode in ((workspace, 0o700), (workspace / ".git", 0o700), (manifest, 0o400)):
        item = path.lstat()
        if item.st_uid != 10001 or stat.S_IMODE(item.st_mode) != mode or path.is_symlink():
            raise RuntimeError(f"Invalid G2 ownership or mode: {path}")
    if not manifest.is_file() or not os.access(manifest, os.R_OK):
        raise RuntimeError("G2 manifest is unreadable")
    status = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain",
                             "--untracked-files=all", "--ignored=matching"],
                            capture_output=True, text=True, check=True, timeout=30)
    if status.stdout:
        raise RuntimeError("G2 workspace is dirty")
    env = subscription_environment()
    if Path(env["HOME"]) != home or Path(env["CODEX_HOME"]) != home / ".codex":
        raise RuntimeError("Unexpected ChatGPT home")
    verify_chatgpt_auth("codex", env)


if __name__ == "__main__":
    main()
