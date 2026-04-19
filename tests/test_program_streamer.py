from __future__ import annotations

import time

from plottrbot.serial.nano_transport import AckResult
from plottrbot.serial.program_streamer import ProgramStreamer, SendStatus


class FakeTransport:
    def __init__(self, fail_at: int | None = None, delay: float = 0.0) -> None:
        self.fail_at = fail_at
        self.delay = delay
        self.sent: list[str] = []

    def send_command(self, command: str) -> AckResult:
        index = len(self.sent)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_at is not None and index == self.fail_at:
            return AckResult(ok=False, error="failed")
        self.sent.append(command)
        return AckResult(ok=True, response="GO")


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_send_completes() -> None:
    transport = FakeTransport()
    streamer = ProgramStreamer(transport)
    streamer.send(["A", "B", "C"], start_index=1)

    assert _wait_until(lambda: streamer.state.status == SendStatus.COMPLETED)
    assert transport.sent == ["B", "C"]
    assert streamer.state.current_index == 3


def test_pause_resume_preserves_index() -> None:
    transport = FakeTransport(delay=0.03)
    streamer = ProgramStreamer(transport)
    streamer.send(["A", "B", "C", "D", "E"])

    assert _wait_until(lambda: len(transport.sent) >= 2)
    streamer.pause()
    paused_index = streamer.state.current_index
    time.sleep(0.08)
    assert streamer.state.current_index <= paused_index + 1

    streamer.resume()
    assert _wait_until(lambda: streamer.state.status == SendStatus.COMPLETED, timeout=2.0)
    assert streamer.state.current_index == 5


def test_send_stops_on_error() -> None:
    transport = FakeTransport(fail_at=1)
    streamer = ProgramStreamer(transport)
    streamer.send(["A", "B", "C"])

    assert _wait_until(lambda: streamer.state.status == SendStatus.ERROR)
    assert streamer.state.current_index == 1
    assert streamer.state.last_error == "failed"


def test_reset_can_suppress_stopped_state_callback_for_restart() -> None:
    states = []
    transport = FakeTransport(delay=0.03)
    streamer = ProgramStreamer(transport, on_state=states.append)
    streamer.send(["A", "B", "C", "D"])

    assert _wait_until(lambda: len(transport.sent) >= 1)
    streamer.pause()
    assert _wait_until(lambda: streamer.state.status == SendStatus.PAUSED)
    states.clear()

    streamer.reset(emit_stopped=False)

    assert streamer.state.status == SendStatus.IDLE
    assert SendStatus.STOPPED not in [state.status for state in states]
