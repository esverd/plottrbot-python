from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import time

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox

from plottrbot.config.settings import default_end_gcode_lines
from plottrbot.core.image_prep import ImagePrepSettings
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

    @property
    def port_name(self) -> str:
        return "/dev/ttyUSB0"

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


def _create_simple_jpg(path: Path) -> None:
    image = Image.new("RGB", (20, 20), color=(255, 255, 255))
    for x in range(20):
        shade = int(round((x / 19.0) * 255))
        for y in range(20):
            image.putpixel((x, y), (shade, shade, shade))
    image.save(path, format="JPEG")


def _create_rect_jpg(path: Path, *, width: int, height: int) -> None:
    image = Image.new("RGB", (width, height), color=(255, 255, 255))
    for x in range(width):
        shade = int(round((x / max(width - 1, 1)) * 255))
        for y in range(height):
            image.putpixel((x, y), (shade, shade, shade))
    image.save(path, format="JPEG")


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
    assert int(round(window.job_state.image_dpi)) == 35

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


def test_center_image_uses_top_left_origin_coordinates(qtbot, settings_store, tmp_path: Path) -> None:
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
    assert window.job_state.img_move_x_mm == 548
    assert window.job_state.img_move_y_mm == 318
    assert window.txt_move_x.text() == "548"
    assert window.txt_move_y.text() == "318"

    window._on_center_or_top_left()
    assert window.job_state.img_move_x_mm == 0
    assert window.job_state.img_move_y_mm == 0
    assert window.txt_move_x.text() == "0"
    assert window.txt_move_y.text() == "0"

    window._on_center_or_top_left()
    assert window.job_state.img_move_x_mm == 548
    assert window.job_state.img_move_y_mm == 318
    assert window.txt_move_x.text() == "548"
    assert window.txt_move_y.text() == "318"


def test_dpi_update_preserves_top_left_position(qtbot, settings_store, tmp_path: Path) -> None:
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

    initial_x = window.txt_move_x.text()
    initial_y = window.txt_move_y.text()

    window.txt_dpi.setText("50")
    window._on_update_dpi()

    assert window.txt_move_x.text() == initial_x
    assert window.txt_move_y.text() == initial_y


def test_load_bmp_uses_default_dpi_override(qtbot, settings_store, tmp_path: Path) -> None:
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

    bmp_path = tmp_path / "default_dpi.bmp"
    _create_simple_bmp(bmp_path)

    window._load_bmp(bmp_path)

    assert window.job_state.dpi_override == 35
    assert int(round(window.job_state.image_dpi)) == 35
    assert window.txt_dpi.text() == "35"


def test_legacy_end_gcode_defaults_migrate_to_center_park(qtbot, settings_store) -> None:
    settings = settings_store.load()
    settings.end_gcode_lines = ["G1 Z1", "G28"]
    settings_store.save(settings)

    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    assert window._current_end_gcode_lines() == ["G1 Z1", "G1 X730 Y800"]


def test_slice_uses_center_park_end_gcode_by_default(qtbot, settings_store, tmp_path: Path) -> None:
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

    assert window.job_state.gcode[-2:] == ["G1 Z1", "G1 X730 Y800"]


def test_save_dimensions_updates_builtin_end_park_x(qtbot, settings_store, monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)

    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    window.txt_robot_width.setText("1200")
    window.txt_robot_height.setText("900")
    window._on_save_dimensions()

    assert window._current_end_gcode_lines() == default_end_gcode_lines(window.settings.machine_profile)
    assert window._current_end_gcode_lines() == ["G1 Z1", "G1 X600 Y800"]


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


def test_paused_stream_allows_manual_controls(qtbot, settings_store, tmp_path: Path) -> None:
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

    paused_state = SendSessionState(
        status=SendStatus.PAUSED,
        start_index=0,
        current_index=4,
        total_commands=len(window.job_state.gcode),
    )
    streamer._state = paused_state
    window._on_stream_state(paused_state)
    window._update_ui_state()

    assert window.btn_send_cmd.isEnabled() is True
    assert window.btn_pen_touch.isEnabled() is True
    assert window.btn_pen_away.isEnabled() is True
    assert window.btn_enable_stepper.isEnabled() is True
    assert window.btn_disable_stepper.isEnabled() is True
    assert all(button.isEnabled() for button in window.bbox_point_buttons.values())

    window._send_manual_commands_async(["M17"], "Enable motors")
    qtbot.waitUntil(lambda: window._manual_busy is False, timeout=1500)
    assert "M17" in transport.sent


def test_bounding_box_point_move_uses_pen_toggle(qtbot, settings_store, tmp_path: Path) -> None:
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
    assert window.job_state.bounding_box is not None

    qtbot.mouseClick(window.bbox_point_buttons["middle"], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._manual_busy is False, timeout=1500)

    bbox = window.job_state.bounding_box
    assert bbox is not None
    expected_x = bbox.min_x + ((bbox.max_x - bbox.min_x) * 0.5)
    expected_y = bbox.min_y + ((bbox.max_y - bbox.min_y) * 0.5)
    assert transport.sent[-1] == f"G1 X{expected_x:.3f} Y{expected_y:.3f} Z1"

    window.checkbox_bounding_pen.setChecked(True)
    qtbot.mouseClick(window.bbox_point_buttons["middle"], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._manual_busy is False, timeout=1500)
    assert transport.sent[-1] == f"G1 X{expected_x:.3f} Y{expected_y:.3f} Z0"


def test_draw_session_log_captures_stop_metadata(qtbot, settings_store, tmp_path: Path) -> None:
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
    window.checkbox_stop_recovery.setChecked(False)

    window._on_send_image()
    window._on_stream_progress(3, len(window.job_state.gcode))
    window._on_stop_drawing()
    stopped_state = SendSessionState(
        status=SendStatus.STOPPED,
        start_index=0,
        current_index=4,
        total_commands=len(window.job_state.gcode),
    )
    streamer._state = stopped_state
    window._on_stream_state(stopped_state)

    log_files = list((settings_store.path.parent / "draw_logs").glob("draw-session-*.json"))
    assert len(log_files) == 1
    payload = json.loads(log_files[0].read_text(encoding="utf-8"))

    assert payload["status"] == "stopped"
    assert payload["started_at_utc"]
    assert payload["finished_at_utc"]
    assert payload["image"]["file_name"] == "img.bmp"
    assert payload["image"]["file_path"] == str(bmp_path)
    assert payload["draw_plan"]["start_command_index"] == 0
    assert payload["draw_plan"]["total_commands"] == len(window.job_state.gcode)
    assert payload["progress"]["commands_sent_total"] == 4
    assert payload["progress"]["lines_sent_total"] == 1
    assert any(event["event"] == "stop_requested" for event in payload["events"])
    assert any(event["event"] == "session_stopped" for event in payload["events"])
    assert payload["gcode_commands"] == window.job_state.gcode


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


def test_image_prep_apply_to_control_and_slice(qtbot, settings_store, tmp_path: Path) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "prep.jpg"
    _create_simple_jpg(jpg_path)

    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=40),
        mark_dirty=True,
    )
    window._on_prep_apply_to_control()

    assert window.image_prep_state.linked_to_control is True
    assert window.job_state.loaded_file is not None
    assert window.job_state.loaded_file.name == "prep.plottrbot.processed.bmp"
    assert window.job_state.dpi_override == 40

    window._on_slice_image()
    assert window.job_state.lines
    assert window.job_state.gcode


def test_image_prep_dirty_refresh_runs_before_slice(qtbot, settings_store, tmp_path: Path) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "dirty.jpg"
    _create_simple_jpg(jpg_path)
    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=35),
        mark_dirty=True,
    )
    window._on_prep_apply_to_control()
    assert window.job_state.loaded_file is not None

    export_path = window.job_state.loaded_file
    before_bytes = export_path.read_bytes()

    window.spin_prep_levels.setValue(6)
    window.spin_prep_dpi.setValue(52)
    window.combo_prep_strategy.setCurrentText("relative")
    assert window.image_prep_state.dirty is True

    window._on_slice_image()
    after_bytes = export_path.read_bytes()

    assert before_bytes != after_bytes
    assert window.image_prep_state.dirty is False
    assert window.job_state.dpi_override == 52


def test_image_prep_sidecar_load_restores_settings(qtbot, settings_store, tmp_path: Path, monkeypatch) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "restore.jpg"
    _create_simple_jpg(jpg_path)
    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=44, blur_radius=1.2, levels=5, strategy="relative"),
        mark_dirty=True,
    )
    window.checkbox_prep_auto_thresholds.setChecked(False)
    for index, value in enumerate([30, 90, 150, 220]):
        window._prep_threshold_sliders[index].setValue(value)
    window._on_prep_settings_changed()
    window._on_prep_save_sidecar()

    sidecar_path = window.image_prep_state.sidecar_path
    assert sidecar_path is not None
    assert sidecar_path.exists()

    window.image_prep_state.clear()
    window._sync_prep_controls_from_state()

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_a, **_k: (str(sidecar_path), "Plottrbot sidecar (*.plottrbot-edit.json)"),
    )
    window._on_prep_load_sidecar()

    assert window.image_prep_state.source_image_path is not None
    assert window.image_prep_state.source_image_path.resolve() == jpg_path.resolve()
    assert window.spin_prep_dpi.value() == 44
    assert window.spin_prep_levels.value() == 5
    assert window.combo_prep_strategy.currentText() == "relative"
    assert window.prep_preview_label.pixmap() is not None


def test_halftone_preview_toggle_does_not_change_export_bmp(qtbot, settings_store, tmp_path: Path) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "preview.jpg"
    _create_simple_jpg(jpg_path)
    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=35, levels=4),
        mark_dirty=True,
    )
    window._on_prep_save_bmp()
    export_path = window.image_prep_state.export_bmp_path
    assert export_path is not None
    before = export_path.read_bytes()

    window.checkbox_prep_halftone_preview.setChecked(True)
    window._on_prep_save_bmp()
    after = export_path.read_bytes()

    assert before == after


def test_image_prep_default_dimensions_use_400mm_long_side(qtbot, settings_store, tmp_path: Path) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "default_dims.jpg"
    _create_rect_jpg(jpg_path, width=80, height=40)

    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=35),
        mark_dirty=True,
    )

    assert window.spin_prep_width_mm.value() == pytest.approx(400.0, abs=0.5)
    assert window.spin_prep_height_mm.value() == pytest.approx(200.0, abs=0.5)


def test_image_prep_dimensions_are_clamped_to_robot_limits(
    qtbot, settings_store, tmp_path: Path
) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "clamp_dims.jpg"
    _create_simple_jpg(jpg_path)
    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=35),
        mark_dirty=True,
    )

    window.checkbox_prep_lock_aspect.setChecked(False)
    window.spin_prep_width_mm.setValue(5000.0)
    window.spin_prep_height_mm.setValue(5000.0)

    max_width = float(window.settings.machine_profile.canvas_width_mm)
    max_height = float(window.settings.machine_profile.canvas_height_mm)

    assert window.spin_prep_width_mm.value() == pytest.approx(max_width, abs=0.01)
    assert window.spin_prep_height_mm.value() == pytest.approx(max_height, abs=0.01)
    assert window.image_prep_state.artifacts is not None
    assert window.image_prep_state.artifacts.image_width_mm <= max_width + 0.2
    assert window.image_prep_state.artifacts.image_height_mm <= max_height + 0.2


def test_image_prep_dpi_change_updates_render_resolution_with_fixed_dimensions(
    qtbot, settings_store, tmp_path: Path
) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "dpi_effect.jpg"
    _create_simple_jpg(jpg_path)
    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=35),
        mark_dirty=True,
    )

    window.spin_prep_width_mm.setValue(200.0)
    window.spin_prep_height_mm.setValue(200.0)
    low_width_px = window.image_prep_state.artifacts.image_width_px if window.image_prep_state.artifacts else 0

    window.spin_prep_dpi.setValue(70)
    high_width_px = window.image_prep_state.artifacts.image_width_px if window.image_prep_state.artifacts else 0

    assert high_width_px > low_width_px


def test_right_preview_switches_with_tab_and_bmp_save_shows_toast(
    qtbot, settings_store, tmp_path: Path
) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    assert window.tab_control.tabText(0) == "Control"
    assert window.tab_control.tabText(1) == "Advanced"
    assert window.tab_control.tabText(2) == "Image Prep"
    assert window.right_preview_stack.currentWidget() is window.machine_preview_panel
    window.tab_control.setCurrentWidget(window.control_tab)
    assert window.right_preview_stack.currentWidget() is window.machine_preview_panel
    window.tab_control.setCurrentWidget(window.image_prep_tab)
    assert window.right_preview_stack.currentWidget() is window.prep_preview_panel

    jpg_path = tmp_path / "toast.jpg"
    _create_simple_jpg(jpg_path)
    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=35),
        mark_dirty=True,
    )
    window._on_prep_save_bmp()

    assert "Saved BMP:" in window.statusBar().currentMessage()


def test_image_prep_sliders_and_manual_threshold_rows(qtbot, settings_store, tmp_path: Path) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "sliders.jpg"
    _create_simple_jpg(jpg_path)
    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=35),
        mark_dirty=True,
    )

    window.slider_prep_contrast.setValue(120)
    window.slider_prep_blur.setValue(15)
    assert window.image_prep_state.settings.contrast_percent == 120
    assert window.image_prep_state.settings.blur_radius == pytest.approx(1.5, abs=0.01)
    assert window.lbl_prep_contrast_value.text() == "120"
    assert window.lbl_prep_blur_value.text() == "1.5"

    window.checkbox_prep_auto_thresholds.setChecked(False)
    assert window.prep_threshold_container.isHidden() is False
    window.spin_prep_levels.setValue(6)
    visible_threshold_rows = sum(1 for row in window._prep_threshold_rows if not row.isHidden())
    assert visible_threshold_rows == 5


def test_image_prep_dimension_entry_clamps_after_commit(qtbot, settings_store, tmp_path: Path) -> None:
    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    jpg_path = tmp_path / "typing_clamp.jpg"
    _create_simple_jpg(jpg_path)
    assert window._load_prep_source_image(
        jpg_path,
        settings=ImagePrepSettings(dpi=35),
        mark_dirty=True,
    )
    window.checkbox_prep_lock_aspect.setChecked(False)
    max_height = float(window.settings.machine_profile.canvas_height_mm)
    assert window.spin_prep_height_mm.maximum() > max_height

    window.spin_prep_height_mm.lineEdit().setText(str(int(max_height * 2)))
    window.spin_prep_height_mm.interpretText()
    assert window.spin_prep_height_mm.value() == pytest.approx(max_height, abs=0.01)


def test_file_dialogs_use_and_persist_last_open_dir(
    qtbot, settings_store, tmp_path: Path, monkeypatch
) -> None:
    settings = settings_store.load()
    settings.last_open_dir = str(tmp_path.resolve())
    settings_store.save(settings)

    window = MainWindow(
        settings_store=settings_store,
        transport=FakeTransport(),
        streamer=FakeStreamer(),
        sleep_inhibitor=FakeSleepInhibitor(),
    )
    qtbot.addWidget(window)

    bmp_path = tmp_path / "remember.bmp"
    _create_simple_bmp(bmp_path)
    calls: list[str] = []

    def _fake_open_file_name(_parent, _title, start_dir, *_args):
        calls.append(str(start_dir))
        return (str(bmp_path), "Bitmap files (*.bmp)")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", _fake_open_file_name)
    window._on_select_image()

    assert calls
    assert Path(calls[0]).resolve() == tmp_path.resolve()
    assert settings_store.load().last_open_dir == str(tmp_path.resolve())
