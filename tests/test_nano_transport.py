from __future__ import annotations

import types

import plottrbot.serial.nano_transport as transport_mod
from plottrbot.core.models import MachineProfile
from plottrbot.serial.nano_transport import NanoTransport


class FakeSerial:
    def __init__(
        self,
        *,
        lines: list[bytes] | None = None,
        responses_per_write: list[list[bytes]] | None = None,
        **_: object,
    ) -> None:
        self._lines = list(lines or [])
        self._responses_per_write = list(responses_per_write or [])
        self.is_open = True
        self.port = "ttyFAKE0"
        self.written: list[bytes] = []
        self.input_reset = 0
        self.output_reset = 0

    def write(self, payload: bytes) -> None:
        self.written.append(payload)
        write_index = len(self.written) - 1
        if write_index < len(self._responses_per_write):
            self._lines.extend(self._responses_per_write[write_index])

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""

    def close(self) -> None:
        self.is_open = False

    def reset_input_buffer(self) -> None:
        self.input_reset += 1

    def reset_output_buffer(self) -> None:
        self.output_reset += 1


class StepClock:
    def __init__(self, *, step: float = 0.11) -> None:
        self.value = 0.0
        self.step = step

    def monotonic(self) -> float:
        self.value += self.step
        return self.value


def test_send_command_ack_success(monkeypatch) -> None:
    fake = FakeSerial(responses_per_write=[[b"GO\n"], [b"GO\n"]])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda **_: fake)

    profile = MachineProfile(ack_timeout_seconds=0.1)
    transport = NanoTransport(profile)
    transport.connect("ttyFAKE0")
    ack = transport.send_command("G28")

    assert ack.ok is True
    assert ack.timed_out is False
    assert fake.written[0] == b"G92 H\n"
    assert fake.written[1] == b"G28\n"
    assert fake.input_reset >= 1
    assert fake.output_reset >= 1


def test_send_command_timeout(monkeypatch) -> None:
    fake = FakeSerial(responses_per_write=[[b"GO\n"], []])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda **_: fake)

    profile = MachineProfile(ack_timeout_seconds=0.05)
    transport = NanoTransport(profile)
    transport.connect("ttyFAKE0")
    ack = transport.send_command("M17")

    assert ack.ok is False
    assert ack.timed_out is True
    assert ack.error is not None


def test_connect_preflight_failure(monkeypatch) -> None:
    fake = FakeSerial(lines=[])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda **_: fake)

    profile = MachineProfile(ack_timeout_seconds=0.05)
    transport = NanoTransport(profile)
    try:
        transport.connect("ttyFAKE0")
    except RuntimeError as exc:
        assert "USB preflight failed" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError for failed preflight")


def test_connect_preflight_retries_then_succeeds(monkeypatch) -> None:
    fake = FakeSerial(responses_per_write=[[], [b"GO\n"]])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda **_: fake)
    monkeypatch.setattr(transport_mod.time, "sleep", lambda _seconds: None)

    clock = StepClock()
    monkeypatch.setattr(transport_mod.time, "monotonic", clock.monotonic)

    logs: list[str] = []
    profile = MachineProfile(ack_timeout_seconds=0.05)
    transport = NanoTransport(profile, on_log=logs.append)
    transport.connect("ttyFAKE0")

    assert transport.is_connected is True
    assert fake.written[:2] == [b"G92 H\n", b"G92 H\n"]
    assert any("Preflight attempt 1 failed" in line for line in logs)


def test_list_ports(monkeypatch) -> None:
    fake_port = types.SimpleNamespace(device="/dev/ttyUSB0", description="usb", hwid="abc")
    monkeypatch.setattr(transport_mod.list_ports, "comports", lambda: [fake_port])
    transport = NanoTransport(MachineProfile())
    ports = transport.list_ports()
    assert len(ports) == 1
    assert ports[0].device == "/dev/ttyUSB0"
