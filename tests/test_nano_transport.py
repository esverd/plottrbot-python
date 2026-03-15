from __future__ import annotations

import types

import plottrbot.serial.nano_transport as transport_mod
from plottrbot.core.models import MachineProfile
from plottrbot.serial.nano_transport import NanoTransport


class FakeSerial:
    def __init__(self, *, lines: list[bytes] | None = None, **_: object) -> None:
        self._lines = list(lines or [])
        self.is_open = True
        self.port = "ttyFAKE0"
        self.written: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.written.append(payload)

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""

    def close(self) -> None:
        self.is_open = False


def test_send_command_ack_success(monkeypatch) -> None:
    fake = FakeSerial(lines=[b"Starting\n", b"GO\n"])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda **_: fake)

    profile = MachineProfile(ack_timeout_seconds=0.1)
    transport = NanoTransport(profile)
    transport.connect("ttyFAKE0")
    ack = transport.send_command("G28")

    assert ack.ok is True
    assert ack.timed_out is False
    assert fake.written[0] == b"G28\n"


def test_send_command_timeout(monkeypatch) -> None:
    fake = FakeSerial(lines=[])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda **_: fake)

    profile = MachineProfile(ack_timeout_seconds=0.05)
    transport = NanoTransport(profile)
    transport.connect("ttyFAKE0")
    ack = transport.send_command("M17")

    assert ack.ok is False
    assert ack.timed_out is True
    assert ack.error is not None


def test_list_ports(monkeypatch) -> None:
    fake_port = types.SimpleNamespace(device="/dev/ttyUSB0", description="usb", hwid="abc")
    monkeypatch.setattr(transport_mod.list_ports, "comports", lambda: [fake_port])
    transport = NanoTransport(MachineProfile())
    ports = transport.list_ports()
    assert len(ports) == 1
    assert ports[0].device == "/dev/ttyUSB0"
