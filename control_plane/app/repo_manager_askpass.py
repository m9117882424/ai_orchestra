#!/usr/bin/env python3
"""Minimal Git askpass boundary used only inside the Repo Manager service.

The parent worker validates and selects an auth profile, then gives this helper
only one username/password pair. The helper never receives the registry JSON and
never writes credentials to disk, argv, stdout logs or Git configuration.
"""
from __future__ import annotations

import os
import re
import sys


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    expected_host = os.getenv("REPO_MANAGER_GIT_HOST", "").lower()
    match = re.search(
        r"https://(?:[^/\s']+@)?([a-z0-9.-]+)(?=[:/'\s]|$)",
        prompt,
        re.IGNORECASE,
    )
    if not expected_host or match is None or match.group(1).lower() != expected_host:
        return 1
    prompt = prompt.lower()
    if "username" in prompt:
        value = os.getenv("REPO_MANAGER_GIT_USERNAME")
    elif "password" in prompt:
        value = os.getenv("REPO_MANAGER_GIT_PASSWORD")
    else:
        return 1
    if value is None:
        return 1
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
