from __future__ import annotations

import plottrbot.system.sleep_inhibitor as inhibitor_mod
from plottrbot.system.sleep_inhibitor import SleepInhibitor


class _FakeProcess:
    def __init__(self) -> None:
        self._poll = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._poll

    def terminate(self) -> None:
        self.terminated = True
        self._poll = 0

    def wait(self, timeout: float | None = None) -> None:
        _ = timeout
        self._poll = 0

    def kill(self) -> None:
        self.killed = True
        self._poll = 0


def test_sleep_inhibitor_start_stop(monkeypatch) -> None:
    fake_process = _FakeProcess()
    monkeypatch.setattr(inhibitor_mod.shutil, "which", lambda _name: "/usr/bin/systemd-inhibit")
    monkeypatch.setattr(inhibitor_mod.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    logs: list[str] = []
    inhibitor = SleepInhibitor(on_log=logs.append)
    assert inhibitor.is_supported is True
    inhibitor.start()
    assert inhibitor.is_active is True
    inhibitor.stop()
    assert fake_process.terminated is True
    assert inhibitor.is_active is False
    assert any("Sleep inhibition active" in line for line in logs)
    assert any("Sleep inhibition released" in line for line in logs)


def test_sleep_inhibitor_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(inhibitor_mod.shutil, "which", lambda _name: None)

    logs: list[str] = []
    inhibitor = SleepInhibitor(on_log=logs.append)
    assert inhibitor.is_supported is False
    inhibitor.start()
    inhibitor.start()
    assert inhibitor.is_active is False
    assert len([line for line in logs if "unavailable" in line]) == 1
