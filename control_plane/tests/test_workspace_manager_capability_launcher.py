from __future__ import annotations

import ctypes

import pytest

from control_plane.app import workspace_manager_capability_launcher as launcher


class _ExecCalled(Exception):
    pass


class _FakeLibc:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events

    def prctl(self, *arguments: object) -> int:
        self.events.append(("prctl", *arguments))
        return 0

    def capset(self, header_pointer: object, data_pointer: object) -> int:
        header = ctypes.cast(
            header_pointer, ctypes.POINTER(launcher._CapabilityHeader)
        ).contents
        data = ctypes.cast(
            data_pointer, ctypes.POINTER(launcher._CapabilityData * 2)
        ).contents
        self.events.append(
            (
                "capset",
                header.version,
                header.pid,
                data[0].effective,
                data[0].permitted,
                data[0].inheritable,
                data[1].effective,
            )
        )
        return 0


def test_launcher_drops_identity_and_retains_only_required_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(launcher.os, "getuid", lambda: 0)
    monkeypatch.setattr(launcher.os, "getgid", lambda: 0)
    monkeypatch.setattr(launcher.os, "setgroups", lambda value: events.append(("groups", value)))
    monkeypatch.setattr(launcher.os, "setgid", lambda value: events.append(("gid", value)))
    monkeypatch.setattr(launcher.os, "setuid", lambda value: events.append(("uid", value)))
    monkeypatch.setattr(launcher.ctypes, "CDLL", lambda *_args, **_kwargs: _FakeLibc(events))
    monkeypatch.setattr(launcher.sys, "argv", ["launcher", "sh", "-c", "true"])

    def fake_exec(file: str, arguments: list[str]) -> None:
        events.append(("exec", file, arguments))
        raise _ExecCalled

    monkeypatch.setattr(launcher.os, "execvp", fake_exec)

    with pytest.raises(_ExecCalled):
        launcher.main()

    mask = (1 << launcher.CAP_DAC_OVERRIDE) | (1 << launcher.CAP_FOWNER)
    assert events == [
        ("prctl", launcher._PR_SET_KEEPCAPS, 1, 0, 0, 0),
        ("groups", []),
        ("gid", launcher.TARGET_GID),
        ("uid", launcher.TARGET_UID),
        ("capset", launcher._LINUX_CAPABILITY_VERSION_3, 0, mask, mask, mask, 0),
        ("prctl", launcher._PR_CAP_AMBIENT, launcher._PR_CAP_AMBIENT_RAISE, 1, 0, 0),
        ("prctl", launcher._PR_CAP_AMBIENT, launcher._PR_CAP_AMBIENT_RAISE, 3, 0, 0),
        ("exec", "sh", ["sh", "-c", "true"]),
    ]


@pytest.mark.parametrize("uid,gid", [(10001, 0), (0, 10001), (10001, 10001)])
def test_launcher_refuses_non_root_start(
    monkeypatch: pytest.MonkeyPatch, uid: int, gid: int
) -> None:
    monkeypatch.setattr(launcher.os, "getuid", lambda: uid)
    monkeypatch.setattr(launcher.os, "getgid", lambda: gid)
    monkeypatch.setattr(launcher.sys, "argv", ["launcher", "true"])

    with pytest.raises(RuntimeError, match="must start as UID/GID 0"):
        launcher.main()


def test_launcher_refuses_missing_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.os, "getuid", lambda: 0)
    monkeypatch.setattr(launcher.os, "getgid", lambda: 0)
    monkeypatch.setattr(launcher.sys, "argv", ["launcher"])

    with pytest.raises(RuntimeError, match="requires a command"):
        launcher.main()
