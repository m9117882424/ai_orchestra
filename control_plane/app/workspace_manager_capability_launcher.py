#!/usr/bin/env python3
"""Drop to the Workspace Manager UID while retaining two required capabilities."""

from __future__ import annotations

import ctypes
import os
import sys


TARGET_UID = 10001
TARGET_GID = 10001
CAP_DAC_OVERRIDE = 1
CAP_FOWNER = 3
RETAINED_CAPABILITIES = (CAP_DAC_OVERRIDE, CAP_FOWNER)

_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_PR_SET_KEEPCAPS = 8
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_RAISE = 2


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _checked(result: int, operation: str) -> None:
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{operation}: {os.strerror(error)}")


def main() -> None:
    if os.getuid() != 0 or os.getgid() != 0:
        raise RuntimeError("workspace-manager launcher must start as UID/GID 0")
    if len(sys.argv) < 2:
        raise RuntimeError("workspace-manager launcher requires a command")

    libc = ctypes.CDLL(None, use_errno=True)
    _checked(libc.prctl(_PR_SET_KEEPCAPS, 1, 0, 0, 0), "PR_SET_KEEPCAPS")
    os.setgroups([])
    os.setgid(TARGET_GID)
    os.setuid(TARGET_UID)

    mask = sum(1 << capability for capability in RETAINED_CAPABILITIES)
    header = _CapabilityHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapabilityData * 2)()
    data[0] = _CapabilityData(mask, mask, mask)
    _checked(libc.capset(ctypes.byref(header), ctypes.byref(data)), "capset")
    for capability in RETAINED_CAPABILITIES:
        _checked(
            libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_RAISE, capability, 0, 0),
            f"PR_CAP_AMBIENT_RAISE({capability})",
        )

    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
