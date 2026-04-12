from __future__ import annotations

import shutil
import subprocess
from typing import Callable

LogCallback = Callable[[str], None]


class SleepInhibitor:
    def __init__(self, on_log: LogCallback | None = None) -> None:
        self._on_log = on_log
        self._process: subprocess.Popen[bytes] | None = None
        self._supported = shutil.which("systemd-inhibit") is not None
        self._warned_unsupported = False

    @property
    def is_supported(self) -> bool:
        return self._supported

    @property
    def is_active(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.is_active:
            return
        if not self._supported:
            if not self._warned_unsupported:
                self._warned_unsupported = True
                self._emit_log("Sleep inhibitor unavailable: install systemd-inhibit for sleep blocking.")
            return

        try:
            self._process = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=sleep",
                    "--who=plottrbot",
                    "--why=Plottrbot active USB streaming",
                    "--mode=block",
                    "sleep",
                    "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._emit_log("Sleep inhibition active while streaming.")
        except Exception as exc:
            self._process = None
            self._emit_log(f"Failed to start sleep inhibitor: {exc}")

    def stop(self) -> None:
        if self._process is None:
            return
        proc = self._process
        self._process = None
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._emit_log("Sleep inhibition released.")

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)
