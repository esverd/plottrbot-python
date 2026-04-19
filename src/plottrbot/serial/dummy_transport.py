from __future__ import annotations

import threading
import time

from plottrbot.core.models import MachineProfile
from plottrbot.serial.nano_transport import AckResult, LogCallback, SerialPortInfo


class DummyTransport:
    """Hardware-free serial transport for exercising the real operator flow."""

    DEFAULT_PORT = "DUMMY-PLOTTRBOT"

    def __init__(
        self,
        profile: MachineProfile,
        on_log: LogCallback | None = None,
        *,
        ack_delay_seconds: float = 0.01,
    ) -> None:
        self.profile = profile
        self._on_log = on_log
        self._ack_delay_seconds = ack_delay_seconds
        self._send_lock = threading.Lock()
        self._connected = False
        self._port_name = ""
        self.sent_commands: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def port_name(self) -> str:
        return self._port_name

    def list_ports(self) -> list[SerialPortInfo]:
        return [
            SerialPortInfo(
                device=self.DEFAULT_PORT,
                description="Dummy Warhol Slicer serial port",
                hwid="debug-no-hardware",
            )
        ]

    def connect(self, port: str) -> None:
        self.disconnect()
        self._connected = True
        self._port_name = port or self.DEFAULT_PORT
        self._emit_log(f"Connected: {self._port_name} @ {self.profile.baudrate} (dummy)")

    def disconnect(self) -> None:
        if not self._connected:
            return
        port = self._port_name
        self._connected = False
        self._port_name = ""
        self._emit_log(f"Disconnected: {port} (dummy)")

    def send_command(self, command: str, *, timeout_seconds: float | None = None) -> AckResult:
        line = command.strip()
        if not line:
            return AckResult(ok=True, response="")
        if not self._connected:
            return AckResult(ok=False, error="Not connected")

        with self._send_lock:
            if self._ack_delay_seconds:
                time.sleep(self._ack_delay_seconds)
            self.sent_commands.append(line)
            self._emit_log(f"> {line}")
            response = self.profile.ack_token
            self._emit_log(f"< {response} (dummy)")
            return AckResult(ok=True, response=response, read_lines=[response])

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)
