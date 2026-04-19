from __future__ import annotations

from plottrbot.core.models import MachineProfile
from plottrbot.serial.dummy_transport import DummyTransport


def test_dummy_transport_lists_debug_port_and_streams_commands() -> None:
    logs: list[str] = []
    transport = DummyTransport(MachineProfile(), on_log=logs.append)

    ports = transport.list_ports()
    assert [port.device for port in ports] == ["DUMMY-PLOTTRBOT"]
    assert transport.is_connected is False
    assert transport.send_command("G28").ok is False

    transport.connect(ports[0].device)
    ack = transport.send_command("  G28  ")

    assert transport.is_connected is True
    assert ack.ok is True
    assert ack.response == "GO"
    assert transport.sent_commands == ["G28"]
    assert any("Connected: DUMMY-PLOTTRBOT" in message for message in logs)
    assert any("< GO (dummy)" == message for message in logs)

    transport.disconnect()
    assert transport.is_connected is False
