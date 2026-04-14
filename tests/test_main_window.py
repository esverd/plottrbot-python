from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from PIL import Image
from PySide6.QtWidgets import QMessageBox

from plottrbot.serial.nano_transport import AckResult
from plottrbot.serial.program_streamer import SendSessionState, SendStatus
from plottrbot.ui.main_window import MainWindow


@dataclass(frozen=True)
class _Port:
    device: str
    description: str = "test"
    hwid: str = "test"


class FakeTransport:
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.connected = False
        self.sent: list[str] = []
        self.delay_seconds = delay_seconds

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
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
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
        self.send_calls: list[tuple[list[str], int]] = []

    @property
    def state(self) -> SendSessionState:
        return self._state

    def send(self, commands: list[str], start_index: int = 0) -> SendSessionState:
        self.send_calls.append((list(commands), start_index))
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

    def stop(self) -> None:
        self._state = SendSessionState(
            status=SendStatus.STOPPED,
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


class FakeSleepInhibitor:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.active = False

    @property
    def is_active(self) -> bool:
        return self.active

    def start(self) -> None:
        self.started += 1
        self.active = True

    def stop(self) -> None:
        self.stopped += 1
        self.active = False


def _create_simple_bmp(path: Path) -> None:
    image = Image.new("RGB", (3, 3), color=(255, 255, 255))
    image.putpixel((0, 0), (0, 0, 0))
    image.putpixel((0, 1), (0, 0, 0))
    image.putpixel((1, 2), (0, 0, 0))
    image.save(path, format="BMP", dpi=(25.4, 25.4))


def test_main_window_enablement_flow(qtbot, settings_store, tmp_path: Path) -> None:
    transport = FakeTransport()
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
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
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
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
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
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


def test_manual_command_async_does_not_block_ui(qtbot, settings_store) -> None:
    transport = FakeTransport(delay_seconds=0.12)
    transport.connected = True
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    start = time.monotonic()
    window._send_manual_commands_async(["M17"], "Enable motors")
    elapsed = time.monotonic() - start
    assert elapsed < 0.05
    assert window._manual_busy is True

    qtbot.waitUntil(lambda: window._manual_busy is False, timeout=1500)
    assert "M17" in transport.sent


def test_close_waits_for_manual_worker_completion(qtbot, settings_store) -> None:
    transport = FakeTransport(delay_seconds=0.15)
    transport.connected = True
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    window._send_manual_commands_async(["M17"], "Enable motors")
    start = time.monotonic()
    window.close()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.10
    assert "M17" in transport.sent
    assert window._manual_worker is None


def test_center_image_uses_center_display_coordinates(qtbot, settings_store, tmp_path: Path) -> None:
    transport = FakeTransport()
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    bmp_path = tmp_path / "square.bmp"
    image = Image.new("RGB", (502, 502), color=(255, 255, 255))
    image.save(bmp_path, format="BMP", dpi=(25.4, 25.4))

    window._load_bmp(bmp_path)
    assert window.job_state.img_move_x_mm == 479
    assert window.job_state.img_move_y_mm == 249
    assert window.txt_move_x.text() == "730"
    assert window.txt_move_y.text() == "500"

    window._on_center_or_top_left()
    assert window.job_state.img_move_x_mm == 0
    assert window.job_state.img_move_y_mm == 0
    assert window.txt_move_x.text() == "251"
    assert window.txt_move_y.text() == "251"

    window._on_center_or_top_left()
    assert window.job_state.img_move_x_mm == 479
    assert window.job_state.img_move_y_mm == 249
    assert window.txt_move_x.text() == "730"
    assert window.txt_move_y.text() == "500"


def test_dpi_update_preserves_display_center_position(qtbot, settings_store, tmp_path: Path) -> None:
    transport = FakeTransport()
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    bmp_path = tmp_path / "dpi.bmp"
    _create_simple_bmp(bmp_path)
    window._load_bmp(bmp_path)

    assert window.txt_move_x.text() == "730"
    assert window.txt_move_y.text() == "500"

    window.txt_dpi.setText("50")
    window._on_update_dpi()

    assert window.txt_move_x.text() == "730"
    assert window.txt_move_y.text() == "500"


def test_motor_power_buttons_follow_saved_setting(qtbot, settings_store) -> None:
    transport = FakeTransport()
    transport.connected = True
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    settings = settings_store.load()
    settings.motor_power_commands_enabled = False
    settings_store.save(settings)

    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    window._update_ui_state()
    assert window.checkbox_motor_power_commands.isChecked() is False
    assert window.btn_enable_stepper.isEnabled() is False
    assert window.btn_disable_stepper.isEnabled() is False

    window.checkbox_motor_power_commands.setChecked(True)

    assert window.btn_enable_stepper.isEnabled() is True
    assert window.btn_disable_stepper.isEnabled() is True
    assert settings_store.load().motor_power_commands_enabled is True


def test_bounds_validation_blocks_send(qtbot, settings_store, tmp_path: Path, monkeypatch) -> None:
    transport = FakeTransport()
    transport.connected = True
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    bmp_path = tmp_path / "img.bmp"
    _create_simple_bmp(bmp_path)
    window._load_bmp(bmp_path)
    window._on_slice_image()
    assert window.job_state.lines
    out_of_bounds_line = window.job_state.lines[0]
    window.job_state.lines[0] = type(out_of_bounds_line)(
        x0=-5.0,
        y0=out_of_bounds_line.y0,
        x1=out_of_bounds_line.x1,
        y1=out_of_bounds_line.y1,
        draw=out_of_bounds_line.draw,
    )

    warnings: list[str] = []

    def _fake_warning(_parent, _title, text):
        warnings.append(str(text))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", _fake_warning)
    window._on_send_image()

    assert streamer.send_calls == []
    assert any("Out-of-bounds" in message for message in warnings)


def test_stop_recovery_and_sleep_inhibitor_flow(qtbot, settings_store, tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.connected = True
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    bmp_path = tmp_path / "img.bmp"
    _create_simple_bmp(bmp_path)
    window._load_bmp(bmp_path)
    window._on_slice_image()

    window._on_send_image()
    assert inhibitor.started >= 1
    assert window._stream_is_active() is True

    window._on_stop_drawing()
    assert window._pending_stop_recovery is True

    window._on_stream_state(
        SendSessionState(
            status=SendStatus.STOPPED,
            start_index=0,
            current_index=5,
            total_commands=10,
        )
    )
    qtbot.waitUntil(lambda: window._manual_busy is False, timeout=1500)
    assert "G1 Z1" in transport.sent
    assert "G28" in transport.sent
    assert inhibitor.stopped >= 1


def test_stream_active_locks_mutating_controls_and_completion_resets_send_index(
    qtbot, settings_store, tmp_path: Path
) -> None:
    transport = FakeTransport()
    transport.connected = True
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    bmp_path = tmp_path / "img.bmp"
    _create_simple_bmp(bmp_path)
    window._load_bmp(bmp_path)
    window._on_slice_image()
    window._update_ui_state()

    assert window.btn_select_img.isEnabled() is True
    assert window.btn_clear_img.isEnabled() is True
    assert window.btn_connect.isEnabled() is True

    window._on_send_image()

    assert window.btn_select_img.isEnabled() is False
    assert window.btn_clear_img.isEnabled() is False
    assert window.btn_slice_img.isEnabled() is False
    assert window.btn_connect.isEnabled() is False
    assert window.btn_pause_drawing.isEnabled() is True
    assert window.btn_stop_drawing.isEnabled() is True

    streamer._state = SendSessionState(
        status=SendStatus.COMPLETED,
        start_index=0,
        current_index=len(window.job_state.gcode),
        total_commands=len(window.job_state.gcode),
    )
    window._on_stream_state(
        SendSessionState(
            status=SendStatus.COMPLETED,
            start_index=0,
            current_index=len(window.job_state.gcode),
            total_commands=len(window.job_state.gcode),
        )
    )

    assert window.job_state.current_send_index == 0
    assert window.btn_select_img.isEnabled() is True
    assert window.btn_clear_img.isEnabled() is True
    assert window.btn_connect.isEnabled() is True


def test_clear_image_preserves_retained_overlay(qtbot, settings_store, tmp_path: Path) -> None:
    transport = FakeTransport()
    streamer = FakeStreamer()
    inhibitor = FakeSleepInhibitor()
    window = MainWindow(
        settings_store=settings_store,
        transport=transport,
        streamer=streamer,
        sleep_inhibitor=inhibitor,
    )
    qtbot.addWidget(window)

    bmp_path = tmp_path / "img.bmp"
    _create_simple_bmp(bmp_path)
    window._load_bmp(bmp_path)
    window._on_hold_release_image()

    assert window.job_state.retained_image is not None
    assert window.preview_canvas._retained_image is not None

    window._on_clear_image()

    assert window.job_state.loaded_file is None
    assert window.job_state.retained_image is not None
    assert window.preview_canvas._primary_image is None
    assert window.preview_canvas._retained_image is not None
    assert window.preview_canvas.render_mode == "image"
