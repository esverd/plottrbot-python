from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from plottrbot.serial.nano_transport import AckResult
from plottrbot.serial.program_streamer import SendSessionState, SendStatus
from plottrbot.ui.main_window import MainWindow


@dataclass(frozen=True)
class _Port:
    device: str
    description: str = "test"
    hwid: str = "test"


class FakeTransport:
    def __init__(self) -> None:
        self.connected = False
        self.sent: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self.connected

    def list_ports(self) -> list[_Port]:
        return [_Port(device="/dev/ttyUSB0")]

    def connect(self, _port: str) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def send_command(self, command: str) -> AckResult:
        self.sent.append(command)
        return AckResult(ok=True, response="GO")


class FakeStreamer:
    def __init__(self) -> None:
        self._state = SendSessionState(
            status=SendStatus.IDLE,
            start_index=0,
            current_index=0,
            total_commands=0,
        )

    @property
    def state(self) -> SendSessionState:
        return self._state

    def send(self, commands: list[str], start_index: int = 0) -> SendSessionState:
        self._state = SendSessionState(
            status=SendStatus.RUNNING,
            start_index=start_index,
            current_index=start_index,
            total_commands=len(commands),
        )
        return self._state

    def pause(self) -> None:
        self._state = SendSessionState(
            status=SendStatus.PAUSED,
            start_index=self._state.start_index,
            current_index=self._state.current_index,
            total_commands=self._state.total_commands,
        )

    def resume(self) -> None:
        self._state = SendSessionState(
            status=SendStatus.RUNNING,
            start_index=self._state.start_index,
            current_index=self._state.current_index,
            total_commands=self._state.total_commands,
        )

    def reset(self) -> None:
        self._state = SendSessionState(
            status=SendStatus.IDLE,
            start_index=0,
            current_index=0,
            total_commands=0,
        )


def _create_simple_bmp(path: Path) -> None:
    image = Image.new("RGB", (3, 3), color=(255, 255, 255))
    image.putpixel((0, 0), (0, 0, 0))
    image.putpixel((0, 1), (0, 0, 0))
    image.putpixel((1, 2), (0, 0, 0))
    image.save(path, format="BMP", dpi=(25.4, 25.4))


def test_main_window_enablement_flow(qtbot, settings_store, tmp_path: Path) -> None:
    transport = FakeTransport()
    streamer = FakeStreamer()
    window = MainWindow(settings_store=settings_store, transport=transport, streamer=streamer)
    qtbot.addWidget(window)

    assert window.btn_slice_img.isEnabled() is False
    assert window.slider_cmd_count.isEnabled() is False

    bmp_path = tmp_path / "img.bmp"
    _create_simple_bmp(bmp_path)
    window._load_bmp(bmp_path)

    assert window.btn_slice_img.isEnabled() is True
    assert window.slider_cmd_count.isEnabled() is False

    window._on_slice_image()
    assert window.slider_cmd_count.isEnabled() is True
    assert window.btn_send_img.isEnabled() is False

    transport.connected = True
    window._update_ui_state()
    assert window.btn_send_img.isEnabled() is True
    assert window.btn_bounding_box.isEnabled() is True


def test_slider_and_hold_release_behavior(qtbot, settings_store, tmp_path: Path) -> None:
    transport = FakeTransport()
    streamer = FakeStreamer()
    window = MainWindow(settings_store=settings_store, transport=transport, streamer=streamer)
    qtbot.addWidget(window)

    bmp_path = tmp_path / "img.bmp"
    _create_simple_bmp(bmp_path)
    window._load_bmp(bmp_path)
    window._on_slice_image()

    window.slider_cmd_count.setValue(1)
    assert window.preview_canvas.selected_line_index == 1
    assert window.txt_cmd_start.text() == "1"

    command_index = window.job_state.line_to_command_index[2]
    window._on_stream_progress(command_index, len(window.job_state.gcode))
    assert window.slider_cmd_count.value() == 2

    window._on_hold_release_image()
    assert window.job_state.retained_image is not None
    window._on_hold_release_image()
    assert window.job_state.retained_image is None


def test_pause_resume_keeps_send_index(qtbot, settings_store, tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.connected = True
    streamer = FakeStreamer()
    window = MainWindow(settings_store=settings_store, transport=transport, streamer=streamer)
    qtbot.addWidget(window)

    bmp_path = tmp_path / "img.bmp"
    _create_simple_bmp(bmp_path)
    window._load_bmp(bmp_path)
    window._on_slice_image()
    window._update_ui_state()

    window._is_drawing = True
    window.job_state.current_send_index = 4
    streamer._state = SendSessionState(
        status=SendStatus.RUNNING,
        start_index=0,
        current_index=4,
        total_commands=10,
    )
    window._on_pause_resume()
    assert window.job_state.current_send_index == 4
    assert window.btn_pause_drawing.text() == "Continue drawing"

    streamer._state = SendSessionState(
        status=SendStatus.PAUSED,
        start_index=0,
        current_index=4,
        total_commands=10,
    )
    window._on_pause_resume()
    assert window.job_state.current_send_index == 4
    assert window.btn_pause_drawing.text() == "Pause drawing"
