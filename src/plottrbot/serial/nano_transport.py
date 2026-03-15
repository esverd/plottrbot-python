from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import serial  # type: ignore
from serial import SerialException  # type: ignore
from serial.tools import list_ports  # type: ignore

from plottrbot.core.models import MachineProfile

LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str
    hwid: str


@dataclass(frozen=True)
class AckResult:
    ok: bool
    timed_out: bool = False
    response: str | None = None
    error: str | None = None
    read_lines: list[str] = field(default_factory=list)


class NanoTransport:
    def __init__(self, profile: MachineProfile, on_log: LogCallback | None = None) -> None:
        self.profile = profile
        self._on_log = on_log
        self._serial: serial.Serial | None = None
        self._send_lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def port_name(self) -> str:
        if self._serial is None:
            return ""
        return str(self._serial.port)

    def list_ports(self) -> list[SerialPortInfo]:
        ports: list[SerialPortInfo] = []
        for port in list_ports.comports():
            ports.append(
                SerialPortInfo(
                    device=str(port.device),
                    description=str(port.description),
                    hwid=str(port.hwid),
                )
            )
        return ports

    def connect(self, port: str) -> None:
        self.disconnect()
        self._serial = serial.Serial(
            port=port,
            baudrate=self.profile.baudrate,
            timeout=0.1,
            write_timeout=2.0,
        )
        self._emit_log(f"Connected: {port} @ {self.profile.baudrate}")
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        time.sleep(0.25)

        warmup_deadline = time.monotonic() + 2.0
        while time.monotonic() < warmup_deadline:
            try:
                raw = self._serial.readline()
            except SerialException:
                break
            if not raw:
                continue
            startup_line = raw.decode("utf-8", errors="ignore").strip()
            if startup_line:
                self._emit_log(f"< {startup_line}")

        preflight_timeout = max(0.2, min(2.0, self.profile.ack_timeout_seconds))
        retry_delay = max(0.05, min(0.4, self.profile.ack_timeout_seconds))
        preflight_ok = False
        last_error = "unknown error"
        for attempt in range(1, 5):
            preflight = self.send_command("G92 H", timeout_seconds=preflight_timeout)
            if preflight.ok:
                preflight_ok = True
                break
            last_error = preflight.error or "unknown error"
            self._emit_log(f"Preflight attempt {attempt} failed: {last_error}")
            time.sleep(retry_delay)
        if not preflight_ok:
            self.disconnect()
            raise RuntimeError(f"USB preflight failed: {last_error}")

    def disconnect(self) -> None:
        if self._serial is None:
            return
        port = str(self._serial.port)
        try:
            self._serial.close()
        finally:
            self._serial = None
            self._emit_log(f"Disconnected: {port}")

    def send_command(self, command: str, *, timeout_seconds: float | None = None) -> AckResult:
        line = command.strip()
        if not line:
            return AckResult(ok=True, response="")
        if self._serial is None or not self._serial.is_open:
            return AckResult(ok=False, error="Not connected")

        with self._send_lock:
            try:
                payload = f"{line}\n".encode("utf-8")
                self._serial.write(payload)
            except SerialException as exc:
                return AckResult(ok=False, error=str(exc))

            self._emit_log(f"> {line}")
            timeout = timeout_seconds if timeout_seconds is not None else self.profile.ack_timeout_seconds
            deadline = time.monotonic() + timeout
            read_lines: list[str] = []

            while time.monotonic() < deadline:
                try:
                    raw = self._serial.readline()
                except SerialException as exc:
                    return AckResult(ok=False, error=str(exc), read_lines=read_lines)
                if not raw:
                    continue

                incoming = raw.decode("utf-8", errors="ignore").strip()
                if not incoming:
                    continue
                read_lines.append(incoming)
                self._emit_log(f"< {incoming}")
                if self.profile.ack_token in incoming:
                    return AckResult(ok=True, timed_out=False, response=incoming, read_lines=read_lines)

            return AckResult(
                ok=False,
                timed_out=True,
                error=f"Timed out waiting for '{self.profile.ack_token}'",
                read_lines=read_lines,
            )

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)
