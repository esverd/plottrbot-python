from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from plottrbot.serial.nano_transport import NanoTransport

StateCallback = Callable[["SendSessionState"], None]
ProgressCallback = Callable[[int, int], None]
LogCallback = Callable[[str], None]


class SendStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True)
class SendSessionState:
    status: SendStatus
    start_index: int
    current_index: int
    total_commands: int
    last_error: str | None = None


class ProgramStreamer:
    def __init__(
        self,
        transport: NanoTransport,
        *,
        on_state: StateCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_log: LogCallback | None = None,
    ) -> None:
        self.transport = transport
        self._on_state = on_state
        self._on_progress = on_progress
        self._on_log = on_log

        self._commands: list[str] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._state_lock = threading.Lock()
        self._state = SendSessionState(
            status=SendStatus.IDLE,
            start_index=0,
            current_index=0,
            total_commands=0,
            last_error=None,
        )

    def send(self, commands: list[str], start_index: int = 0) -> SendSessionState:
        if start_index < 0 or start_index > len(commands):
            raise ValueError("start_index out of range")
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("A send session is already active")

        self._commands = list(commands)
        self._stop_event.clear()
        self._pause_event.clear()
        self._set_state(
            SendSessionState(
                status=SendStatus.RUNNING,
                start_index=start_index,
                current_index=start_index,
                total_commands=len(self._commands),
                last_error=None,
            )
        )
        self._thread = threading.Thread(
            target=self._send_worker,
            args=(start_index,),
            daemon=True,
            name="plottrbot-program-streamer",
        )
        self._thread.start()
        return self.state

    def pause(self) -> None:
        if self.state.status != SendStatus.RUNNING:
            return
        self._pause_event.set()
        self._set_state(
            SendSessionState(
                status=SendStatus.PAUSED,
                start_index=self.state.start_index,
                current_index=self.state.current_index,
                total_commands=self.state.total_commands,
                last_error=self.state.last_error,
            )
        )
        self._emit_log("Queue paused")

    def resume(self) -> None:
        if self.state.status != SendStatus.PAUSED:
            return
        self._pause_event.clear()
        self._set_state(
            SendSessionState(
                status=SendStatus.RUNNING,
                start_index=self.state.start_index,
                current_index=self.state.current_index,
                total_commands=self.state.total_commands,
                last_error=self.state.last_error,
            )
        )
        self._emit_log("Queue resumed")

    def stop(self) -> None:
        self._stop_event.set()

    def reset(self) -> None:
        self.stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._commands = []
        self._pause_event.clear()
        self._set_state(
            SendSessionState(
                status=SendStatus.IDLE,
                start_index=0,
                current_index=0,
                total_commands=0,
                last_error=None,
            )
        )

    @property
    def state(self) -> SendSessionState:
        with self._state_lock:
            return self._state

    def _send_worker(self, start_index: int) -> None:
        total = len(self._commands)
        next_index = start_index

        while next_index < total:
            if self._stop_event.is_set():
                self._set_state(
                    SendSessionState(
                        status=SendStatus.STOPPED,
                        start_index=start_index,
                        current_index=next_index,
                        total_commands=total,
                    )
                )
                self._emit_log("Queue stopped")
                return

            if self._pause_event.is_set():
                if self.state.status != SendStatus.PAUSED:
                    self._set_state(
                        SendSessionState(
                            status=SendStatus.PAUSED,
                            start_index=start_index,
                            current_index=next_index,
                            total_commands=total,
                        )
                    )
                time.sleep(0.05)
                continue

            command = self._commands[next_index]
            ack = self.transport.send_command(command)
            if not ack.ok:
                self._set_state(
                    SendSessionState(
                        status=SendStatus.ERROR,
                        start_index=start_index,
                        current_index=next_index,
                        total_commands=total,
                        last_error=ack.error,
                    )
                )
                self._emit_log(ack.error or "Unknown streaming error")
                return

            next_index += 1
            next_status = SendStatus.PAUSED if self._pause_event.is_set() else SendStatus.RUNNING
            self._set_state(
                SendSessionState(
                    status=next_status,
                    start_index=start_index,
                    current_index=next_index,
                    total_commands=total,
                )
            )
            if self._on_progress is not None:
                self._on_progress(next_index - 1, total)

        self._set_state(
            SendSessionState(
                status=SendStatus.COMPLETED,
                start_index=start_index,
                current_index=total,
                total_commands=total,
            )
        )
        self._emit_log("Queue completed")

    def _set_state(self, state: SendSessionState) -> None:
        with self._state_lock:
            self._state = state
        if self._on_state is not None:
            self._on_state(state)

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)
