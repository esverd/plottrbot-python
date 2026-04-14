from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox

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


def _create_clickthrough_bmp(path: Path) -> None:
    image = Image.new("RGB", (12, 12), color=(255, 255, 255))
    for y in range(10):
        image.putpixel((2, y), (0, 0, 0))
    for y in range(2, 12):
        image.putpixel((8, y), (0, 0, 0))
    for x in range(3, 8):
        image.putpixel((x, 5), (0, 0, 0))
    image.save(path, format="BMP", dpi=(25.4, 25.4))


def _wait_manual_idle(qtbot, window: MainWindow) -> None:
    qtbot.waitUntil(lambda: window._manual_busy is False, timeout=2000)


def test_ui_clickthrough_full_operator_flow(qtbot, settings_store, tmp_path: Path, monkeypatch) -> None:
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

    bmp_path = tmp_path / "clickthrough.bmp"
    _create_clickthrough_bmp(bmp_path)

    warnings: list[str] = []
    infos: list[str] = []

    def _info(_parent, _title, text):
        infos.append(str(text))
        return QMessageBox.StandardButton.Ok

    def _warning(_parent, _title, text):
        warnings.append(str(text))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_a, **_k: (str(bmp_path), "Bitmap files (*.bmp)"))
    monkeypatch.setattr(QMessageBox, "information", _info)
    monkeypatch.setattr(QMessageBox, "warning", _warning)

    qtbot.mouseClick(window.btn_refresh_ports, Qt.MouseButton.LeftButton)
    assert window.combo_port.count() == 1

    qtbot.mouseClick(window.btn_select_img, Qt.MouseButton.LeftButton)
    assert window.job_state.loaded_file == bmp_path
    assert window.job_state.file_type == "bmp"

    window.txt_dpi.setText("50")
    qtbot.mouseClick(window.btn_update_dpi, Qt.MouseButton.LeftButton)
    assert int(round(window.job_state.image_dpi)) == 50

    window.txt_move_x.setText("123")
    window.txt_move_y.setText("77")
    qtbot.mouseClick(window.btn_move_img, Qt.MouseButton.LeftButton)
    assert window.job_state.img_move_x_mm == 120
    assert window.job_state.img_move_y_mm == 74
    assert window.txt_move_x.text() == "123"
    assert window.txt_move_y.text() == "77"

    qtbot.mouseClick(window.btn_center_img, Qt.MouseButton.LeftButton)
    assert window.job_state.img_move_x_mm == 0
    assert window.job_state.img_move_y_mm == 0
    assert "center" in window.btn_center_img.text().lower()
    assert int(window.txt_move_x.text()) > 0
    assert int(window.txt_move_y.text()) > 0

    qtbot.mouseClick(window.btn_center_img, Qt.MouseButton.LeftButton)
    assert window.job_state.img_move_x_mm > 0
    assert window.job_state.img_move_y_mm > 0
    assert "top left" in window.btn_center_img.text().lower()
    assert window.txt_move_x.text() == "730"
    assert window.txt_move_y.text() == "500"

    initial_scale = window.preview_canvas.scale
    qtbot.mouseClick(window.btn_zoom_in, Qt.MouseButton.LeftButton)
    assert window.preview_canvas.scale > initial_scale
    qtbot.mouseClick(window.btn_zoom_out, Qt.MouseButton.LeftButton)
    assert window.preview_canvas.scale < initial_scale * 1.05

    qtbot.mouseClick(window.btn_hold_img, Qt.MouseButton.LeftButton)
    assert window.job_state.retained_image is not None
    qtbot.mouseClick(window.btn_hold_img, Qt.MouseButton.LeftButton)
    assert window.job_state.retained_image is None

    qtbot.mouseClick(window.btn_slice_img, Qt.MouseButton.LeftButton)
    assert len(window.job_state.lines) > 1
    assert len(window.job_state.gcode) > 1
    assert window.slider_cmd_count.maximum() == len(window.job_state.lines) - 1

    qtbot.mouseClick(window.btn_slider_inc, Qt.MouseButton.LeftButton)
    assert window.slider_cmd_count.value() == 1
    assert window.preview_canvas.selected_line_index == 1
    qtbot.mouseClick(window.btn_slider_dec, Qt.MouseButton.LeftButton)
    assert window.slider_cmd_count.value() == 0

    window.txt_cmd_start.setText("1")
    window.txt_cmd_start.setFocus()
    qtbot.keyClick(window.txt_cmd_start, Qt.Key.Key_Return)
    assert window.slider_cmd_count.value() == 1

    window.combo_port.setCurrentIndex(0)
    qtbot.mouseClick(window.btn_connect, Qt.MouseButton.LeftButton)
    assert transport.connected is True

    window.txt_serial_cmd.setText("G92 H")
    qtbot.mouseClick(window.btn_send_cmd, Qt.MouseButton.LeftButton)
    _wait_manual_idle(qtbot, window)
    assert "G92 H" in transport.sent

    qtbot.mouseClick(window.btn_enable_stepper, Qt.MouseButton.LeftButton)
    _wait_manual_idle(qtbot, window)
    qtbot.mouseClick(window.btn_disable_stepper, Qt.MouseButton.LeftButton)
    _wait_manual_idle(qtbot, window)
    qtbot.mouseClick(window.btn_pen_touch, Qt.MouseButton.LeftButton)
    _wait_manual_idle(qtbot, window)
    qtbot.mouseClick(window.btn_pen_away, Qt.MouseButton.LeftButton)
    _wait_manual_idle(qtbot, window)
    qtbot.mouseClick(window.btn_home, Qt.MouseButton.LeftButton)
    _wait_manual_idle(qtbot, window)
    assert {"M17", "M18", "G1 Z0", "G1 Z1", "G28"}.issubset(set(transport.sent))

    window.checkbox_bounding_pen.setChecked(False)
    sent_before_bbox = len(transport.sent)
    qtbot.mouseClick(window.btn_bounding_box, Qt.MouseButton.LeftButton)
    _wait_manual_idle(qtbot, window)
    bbox_commands_pen_up = transport.sent[sent_before_bbox:]
    assert len(bbox_commands_pen_up) == 7
    assert "Z1" in bbox_commands_pen_up[1]

    window.checkbox_bounding_pen.setChecked(True)
    sent_before_bbox = len(transport.sent)
    qtbot.mouseClick(window.btn_bounding_box, Qt.MouseButton.LeftButton)
    _wait_manual_idle(qtbot, window)
    bbox_commands_pen_down = transport.sent[sent_before_bbox:]
    assert len(bbox_commands_pen_down) == 7
    assert "Z0" in bbox_commands_pen_down[1]

    sent_calls_before = len(streamer.send_calls)
    qtbot.mouseClick(window.btn_send_img, Qt.MouseButton.LeftButton)
    assert len(streamer.send_calls) == sent_calls_before + 1
    sent_commands, sent_start_index = streamer.send_calls[-1]
    assert len(sent_commands) == len(window.job_state.gcode)
    assert sent_start_index == window.job_state.current_send_index
    assert window._is_drawing is True
    assert inhibitor.started >= 1

    qtbot.mouseClick(window.btn_pause_drawing, Qt.MouseButton.LeftButton)
    assert streamer.state.status == SendStatus.PAUSED
    assert "continue" in window.btn_pause_drawing.text().lower()

    qtbot.mouseClick(window.btn_pause_drawing, Qt.MouseButton.LeftButton)
    assert streamer.state.status == SendStatus.RUNNING
    assert "pause" in window.btn_pause_drawing.text().lower()

    qtbot.mouseClick(window.btn_stop_drawing, Qt.MouseButton.LeftButton)
    assert streamer.state.status == SendStatus.STOPPED
    window._on_stream_state(streamer.state)
    _wait_manual_idle(qtbot, window)
    assert transport.sent[-2:] == ["G1 Z1", "G28"]

    window._on_stream_state(
        SendSessionState(
            status=SendStatus.COMPLETED,
            start_index=0,
            current_index=len(window.job_state.gcode),
            total_commands=len(window.job_state.gcode),
        )
    )
    assert inhibitor.active is False

    expected_start_index = window.job_state.line_to_command_index[1]
    send_calls_before = len(streamer.send_calls)
    window.txt_cmd_start.setText("1")
    qtbot.mouseClick(window.btn_cmd_start, Qt.MouseButton.LeftButton)
    assert len(streamer.send_calls) == send_calls_before + 1
    _, resumed_start_index = streamer.send_calls[-1]
    assert resumed_start_index == expected_start_index
    streamer._state = SendSessionState(
        status=SendStatus.COMPLETED,
        start_index=resumed_start_index,
        current_index=len(window.job_state.gcode),
        total_commands=len(window.job_state.gcode),
    )
    window._on_stream_state(streamer.state)

    window.txt_robot_width.setText("1200")
    window.txt_robot_height.setText("900")
    qtbot.mouseClick(window.btn_save_dims, Qt.MouseButton.LeftButton)
    assert window.settings.machine_profile.canvas_width_mm == 1200
    assert window.settings.machine_profile.canvas_height_mm == 900
    assert any("Dimensions saved." in msg for msg in infos)

    qtbot.mouseClick(window.btn_connect, Qt.MouseButton.LeftButton)
    assert transport.connected is False
    assert window.job_state.current_send_index == 0

    qtbot.mouseClick(window.btn_clear_img, Qt.MouseButton.LeftButton)
    assert window.job_state.loaded_file is None
    assert window.job_state.lines == []
    assert window.job_state.gcode == []
    assert warnings == []
    assert "GCODE commands =" in window.txt_out.toPlainText()
