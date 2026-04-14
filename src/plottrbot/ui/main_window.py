from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from plottrbot.config.settings import AppSettings, SettingsStore
from plottrbot.core.bmp_converter import BmpConverter
from plottrbot.core.models import JobState, RetainedImage
from plottrbot.core.state_machine import UiState, derive_ui_state
from plottrbot.serial.nano_transport import NanoTransport
from plottrbot.serial.program_streamer import ProgramStreamer, SendSessionState, SendStatus
from plottrbot.system.sleep_inhibitor import SleepInhibitor
from plottrbot.ui.preview_canvas import PreviewCanvas


class UiBridge(QObject):
    log_signal = Signal(str)
    stream_state_signal = Signal(object)
    stream_progress_signal = Signal(int, int)
    manual_result_signal = Signal(object)


@dataclass(slots=True, frozen=True)
class ManualCommandResult:
    ok: bool
    label: str
    error: str | None = None


class MainWindow(QMainWindow):
    DEFAULT_BMP_DPI = 35

    def __init__(
        self,
        *,
        settings_store: SettingsStore | None = None,
        transport: NanoTransport | None = None,
        streamer: ProgramStreamer | None = None,
        converter: BmpConverter | None = None,
        sleep_inhibitor: SleepInhibitor | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Plottrbot")

        self.settings_store = settings_store or SettingsStore()
        self.settings = self.settings_store.load()
        self.job_state = JobState(preview_scale=0.8)

        self.converter = converter or BmpConverter(self.settings.machine_profile)
        self.bridge = UiBridge()

        self.transport = transport or NanoTransport(
            self.settings.machine_profile,
            on_log=self.bridge.log_signal.emit,
        )
        self.streamer = streamer or ProgramStreamer(
            self.transport,
            on_state=self.bridge.stream_state_signal.emit,
            on_progress=self.bridge.stream_progress_signal.emit,
            on_log=self.bridge.log_signal.emit,
        )
        self.sleep_inhibitor = sleep_inhibitor or SleepInhibitor(on_log=self.bridge.log_signal.emit)

        self._is_drawing = False
        self._manual_busy = False
        self._manual_worker: threading.Thread | None = None
        self._pending_stop_recovery = False
        self.current_ui_state = UiState.BLANK

        self._build_ui()
        self.resize(self.settings.window_width, self.settings.window_height)
        self._connect_signals()
        self._refresh_ports()
        self._populate_defaults()
        self._update_ui_state()

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QHBoxLayout(root)
        self.setCentralWidget(root)

        self.tab_control = QTabWidget()
        self.tab_control.setMinimumWidth(470)
        layout.addWidget(self.tab_control, 0)

        self.control_tab = QWidget()
        self.advanced_tab = QWidget()
        self.tab_control.addTab(self.control_tab, "Control")
        self.tab_control.addTab(self.advanced_tab, "Advanced")

        self._build_control_tab()
        self._build_advanced_tab()

        self.preview_canvas = PreviewCanvas(self.settings.machine_profile)
        self.preview_canvas.set_scale(self.job_state.preview_scale)

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setWidget(self.preview_canvas)
        layout.addWidget(self.preview_scroll, 1)

    def _build_control_tab(self) -> None:
        layout = QVBoxLayout(self.control_tab)

        image_group = QGroupBox("Image options")
        image_layout = QVBoxLayout(image_group)
        self.btn_select_img = QPushButton("Select image")
        self.btn_move_img = QPushButton("Set position")
        self.btn_center_img = QPushButton("Move top left")
        self.btn_clear_img = QPushButton("Clear")
        self.btn_zoom_out = QPushButton("Zoom out")
        self.btn_zoom_in = QPushButton("Zoom in")
        self.btn_slice_img = QPushButton("Slice image")
        self.btn_hold_img = QPushButton("Hold image")

        move_grid = QGridLayout()
        self.txt_move_x = QLineEdit("0")
        self.txt_move_y = QLineEdit("0")
        move_grid.addWidget(QLabel("Top-left X [mm]"), 0, 0)
        move_grid.addWidget(self.txt_move_x, 0, 1)
        move_grid.addWidget(self.btn_move_img, 0, 2)
        move_grid.addWidget(QLabel("Top-left Y [mm]"), 1, 0)
        move_grid.addWidget(self.txt_move_y, 1, 1)
        move_grid.addWidget(self.btn_center_img, 1, 2)
        self.lbl_center_position = QLabel("Image center: X 0 mm, Y 0 mm")
        move_grid.addWidget(self.lbl_center_position, 2, 0, 1, 3)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(self.btn_zoom_out)
        zoom_row.addWidget(self.btn_zoom_in)

        image_layout.addWidget(self.btn_select_img)
        image_layout.addLayout(move_grid)
        image_layout.addWidget(self.btn_clear_img)
        image_layout.addLayout(zoom_row)
        image_layout.addWidget(self.btn_slice_img)
        image_layout.addWidget(self.btn_hold_img)
        layout.addWidget(image_group)

        robot_group = QGroupBox("Robot control")
        robot_layout = QVBoxLayout(robot_group)
        port_row = QHBoxLayout()
        self.combo_port = QComboBox()
        self.btn_refresh_ports = QPushButton("Refresh")
        self.btn_connect = QPushButton("Connect USB")
        port_row.addWidget(self.combo_port, 1)
        port_row.addWidget(self.btn_refresh_ports)
        port_row.addWidget(self.btn_connect)

        self.btn_bounding_box = QPushButton("Move in bounding box formation")
        self.btn_pause_drawing = QPushButton("Pause drawing")
        self.btn_stop_drawing = QPushButton("Stop drawing")
        self.btn_send_img = QPushButton("Send image to robot")
        robot_layout.addLayout(port_row)
        robot_layout.addWidget(self.btn_bounding_box)
        robot_layout.addWidget(self.btn_pause_drawing)
        robot_layout.addWidget(self.btn_stop_drawing)
        robot_layout.addWidget(self.btn_send_img)
        layout.addWidget(robot_group)
        layout.addStretch(1)

    def _build_advanced_tab(self) -> None:
        layout = QVBoxLayout(self.advanced_tab)

        self.txt_out = QPlainTextEdit()
        self.txt_out.setReadOnly(True)
        self.txt_out.setPlaceholderText("Status messages")
        layout.addWidget(self.txt_out)

        serial_row = QHBoxLayout()
        self.txt_serial_cmd = QLineEdit()
        self.btn_send_cmd = QPushButton("Send serial msg")
        serial_row.addWidget(self.txt_serial_cmd, 1)
        serial_row.addWidget(self.btn_send_cmd)
        layout.addLayout(serial_row)

        self.checkbox_bounding_pen = QCheckBox("Use pen when indicating bounding box")
        layout.addWidget(self.checkbox_bounding_pen)
        self.checkbox_stop_recovery = QCheckBox("On stop: lift tool and home")
        self.checkbox_stop_recovery.setChecked(True)
        layout.addWidget(self.checkbox_stop_recovery)
        self.checkbox_motor_power_commands = QCheckBox("Enable motor power commands (M17/M18)")
        layout.addWidget(self.checkbox_motor_power_commands)

        layout.addWidget(QLabel("End GCODE"))
        self.txt_end_gcode = QPlainTextEdit()
        self.txt_end_gcode.setFixedHeight(90)
        layout.addWidget(self.txt_end_gcode)

        cmd_row = QHBoxLayout()
        self.txt_cmd_start = QLineEdit("0")
        self.btn_cmd_start = QPushButton("Start from line number")
        cmd_row.addWidget(self.txt_cmd_start)
        cmd_row.addWidget(self.btn_cmd_start)
        layout.addLayout(cmd_row)

        slider_row = QHBoxLayout()
        self.slider_cmd_count = QSlider(Qt.Orientation.Horizontal)
        self.slider_cmd_count.setMinimum(0)
        self.slider_cmd_count.setMaximum(0)
        self.btn_slider_dec = QPushButton("<")
        self.btn_slider_inc = QPushButton(">")
        slider_row.addWidget(self.slider_cmd_count, 1)
        slider_row.addWidget(self.btn_slider_dec)
        slider_row.addWidget(self.btn_slider_inc)
        layout.addLayout(slider_row)

        motor_row = QHBoxLayout()
        self.btn_enable_stepper = QPushButton("Enable motors")
        self.btn_disable_stepper = QPushButton("Disable motors")
        motor_row.addWidget(self.btn_enable_stepper)
        motor_row.addWidget(self.btn_disable_stepper)
        layout.addLayout(motor_row)

        pen_row = QHBoxLayout()
        self.btn_pen_touch = QPushButton("Set tool to canvas")
        self.btn_pen_away = QPushButton("Set away from canvas")
        pen_row.addWidget(self.btn_pen_touch)
        pen_row.addWidget(self.btn_pen_away)
        layout.addLayout(pen_row)

        self.btn_home = QPushButton("Move to home position")
        layout.addWidget(self.btn_home)

        dpi_row = QHBoxLayout()
        dpi_row.addWidget(QLabel("Current DPI"))
        self.txt_dpi = QLineEdit()
        self.txt_dpi.setMaximumWidth(100)
        self.btn_update_dpi = QPushButton("Update DPI")
        dpi_row.addWidget(self.txt_dpi)
        dpi_row.addWidget(self.btn_update_dpi)
        dpi_row.addStretch(1)
        layout.addLayout(dpi_row)

        dims_grid = QGridLayout()
        self.txt_robot_width = QLineEdit()
        self.txt_robot_height = QLineEdit()
        self.btn_save_dims = QPushButton("Save dimensions")
        dims_grid.addWidget(QLabel("Robot width [mm]"), 0, 0)
        dims_grid.addWidget(self.txt_robot_width, 0, 1)
        dims_grid.addWidget(QLabel("Robot height [mm]"), 1, 0)
        dims_grid.addWidget(self.txt_robot_height, 1, 1)
        dims_grid.addWidget(self.btn_save_dims, 2, 0, 1, 2)
        layout.addLayout(dims_grid)
        layout.addStretch(1)

    def _connect_signals(self) -> None:
        self.btn_select_img.clicked.connect(self._on_select_image)
        self.btn_move_img.clicked.connect(self._on_move_image)
        self.btn_center_img.clicked.connect(self._on_center_or_top_left)
        self.btn_clear_img.clicked.connect(self._on_clear_image)
        self.btn_zoom_in.clicked.connect(lambda: self._on_zoom(1.2))
        self.btn_zoom_out.clicked.connect(lambda: self._on_zoom(1 / 1.2))
        self.btn_slice_img.clicked.connect(self._on_slice_image)
        self.btn_hold_img.clicked.connect(self._on_hold_release_image)

        self.btn_refresh_ports.clicked.connect(self._refresh_ports)
        self.btn_connect.clicked.connect(self._on_connect_toggle)
        self.btn_bounding_box.clicked.connect(self._on_bounding_box)
        self.btn_pause_drawing.clicked.connect(self._on_pause_resume)
        self.btn_stop_drawing.clicked.connect(self._on_stop_drawing)
        self.btn_send_img.clicked.connect(self._on_send_image)

        self.btn_send_cmd.clicked.connect(self._on_send_raw_serial)
        self.txt_serial_cmd.returnPressed.connect(self._on_send_raw_serial)
        self.btn_cmd_start.clicked.connect(self._on_start_from_command_number)
        self.txt_cmd_start.returnPressed.connect(self._on_slider_from_text)
        self.slider_cmd_count.valueChanged.connect(self._on_slider_changed)
        self.btn_slider_dec.clicked.connect(lambda: self.slider_cmd_count.setValue(self.slider_cmd_count.value() - 1))
        self.btn_slider_inc.clicked.connect(lambda: self.slider_cmd_count.setValue(self.slider_cmd_count.value() + 1))

        self.btn_enable_stepper.clicked.connect(lambda: self._send_manual_commands_async(["M17"], "Enable motors"))
        self.btn_disable_stepper.clicked.connect(lambda: self._send_manual_commands_async(["M18"], "Disable motors"))
        self.btn_pen_touch.clicked.connect(
            lambda: self._send_manual_commands_async(["G1 Z0"], "Set tool to canvas")
        )
        self.btn_pen_away.clicked.connect(
            lambda: self._send_manual_commands_async(["G1 Z1"], "Set away from canvas")
        )
        self.btn_home.clicked.connect(lambda: self._send_manual_commands_async(["G28"], "Move to home position"))
        self.btn_update_dpi.clicked.connect(self._on_update_dpi)
        self.btn_save_dims.clicked.connect(self._on_save_dimensions)
        self.checkbox_motor_power_commands.toggled.connect(self._on_motor_power_commands_toggled)

        self.bridge.log_signal.connect(self._append_log)
        self.bridge.stream_state_signal.connect(self._on_stream_state)
        self.bridge.stream_progress_signal.connect(self._on_stream_progress)
        self.bridge.manual_result_signal.connect(self._on_manual_command_result)

    def _populate_defaults(self) -> None:
        profile = self.settings.machine_profile
        self.txt_robot_width.setText(str(profile.canvas_width_mm))
        self.txt_robot_height.setText(str(profile.canvas_height_mm))
        self.txt_dpi.setText(str(self.DEFAULT_BMP_DPI))
        self.txt_end_gcode.setPlainText("\n".join(self.settings.end_gcode_lines) + "\n")
        self.checkbox_motor_power_commands.setChecked(self.settings.motor_power_commands_enabled)
        self.btn_pause_drawing.setText("Pause drawing")
        self._append_log("Ready")

    def _append_log(self, message: str) -> None:
        self.txt_out.appendPlainText(message)
        cursor = self.txt_out.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.txt_out.setTextCursor(cursor)

    def _refresh_ports(self) -> None:
        current = self.combo_port.currentText()
        self.combo_port.clear()
        ports = self.transport.list_ports()
        for port in ports:
            self.combo_port.addItem(port.device)

        if current:
            index = self.combo_port.findText(current)
            if index >= 0:
                self.combo_port.setCurrentIndex(index)
        elif self.settings.last_port:
            index = self.combo_port.findText(self.settings.last_port)
            if index >= 0:
                self.combo_port.setCurrentIndex(index)

    def _parse_int(self, line_edit: QLineEdit, field_name: str) -> int | None:
        try:
            return int(line_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid value", f"{field_name} must be an integer.")
            return None

    def _render_retained_overlay(self) -> None:
        retained = self.job_state.retained_image
        if retained is None:
            self.preview_canvas.clear_retained_image()
            return
        self.preview_canvas.set_retained_image(
            image_path=str(retained.file_path),
            x_mm=retained.move_x_mm,
            y_mm=retained.move_y_mm,
            width_mm=retained.width_mm,
            height_mm=retained.height_mm,
        )

    def _render_primary_image(self) -> None:
        if self.job_state.loaded_file is None:
            self.preview_canvas.clear_primary_image()
            return
        self.preview_canvas.set_primary_image(
            image_path=str(self.job_state.loaded_file),
            x_mm=self.job_state.img_move_x_mm,
            y_mm=self.job_state.img_move_y_mm,
            width_mm=self.job_state.image_width_mm,
            height_mm=self.job_state.image_height_mm,
        )

    def _render_image_preview(self) -> None:
        self.preview_canvas.set_render_mode("image")
        self.preview_canvas.clear_trace_lines()
        self.preview_canvas.clear_bbox_overlay()
        self._render_retained_overlay()
        self._render_primary_image()
        self._update_position_fields()

    def _get_centered_image_origin(self, width_mm: float, height_mm: float) -> tuple[int, int]:
        return (
            max(int(round((self.settings.machine_profile.canvas_width_mm - width_mm) / 2)), 0),
            max(int(round((self.settings.machine_profile.canvas_height_mm - height_mm) / 2)), 0),
        )

    def _get_display_center_position(self) -> tuple[int, int]:
        return (
            int(round(self.job_state.img_move_x_mm + (self.job_state.image_width_mm / 2.0))),
            int(round(self.job_state.img_move_y_mm + (self.job_state.image_height_mm / 2.0))),
        )

    def _update_position_fields(self) -> None:
        self.txt_move_x.setText(str(self.job_state.img_move_x_mm))
        self.txt_move_y.setText(str(self.job_state.img_move_y_mm))
        if self.job_state.loaded_file is None:
            self.lbl_center_position.setText("Image center: X 0 mm, Y 0 mm")
            return
        center_x_mm, center_y_mm = self._get_display_center_position()
        self.lbl_center_position.setText(f"Image center: X {center_x_mm} mm, Y {center_y_mm} mm")

    def _on_select_image(self) -> None:
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select BMP image",
            "",
            "Bitmap files (*.bmp);;All files (*.*)",
        )
        if not selected_file:
            return
        self._load_bmp(Path(selected_file))

    def _load_bmp(self, image_path: Path) -> None:
        if image_path.suffix.lower() != ".bmp":
            QMessageBox.information(self, "Not supported", "Only BMP is enabled in phase 1.")
            return

        self.job_state.loaded_file = image_path
        self.job_state.file_type = "bmp"
        self.job_state.lines.clear()
        self.job_state.gcode.clear()
        self.job_state.bounding_box = None
        self.job_state.current_send_index = 0
        self.job_state.command_to_line_index.clear()
        self.job_state.line_to_command_index.clear()
        self.job_state.dpi_override = self.DEFAULT_BMP_DPI

        metadata = self.converter.inspect_image(image_path, dpi_override=self.job_state.dpi_override)
        self.job_state.image_width_mm = metadata.image_width_mm
        self.job_state.image_height_mm = metadata.image_height_mm
        self.job_state.image_dpi = metadata.dpi_x

        if self.job_state.retained_image is None:
            self.job_state.img_move_x_mm, self.job_state.img_move_y_mm = self._get_centered_image_origin(
                metadata.image_width_mm,
                metadata.image_height_mm,
            )
        else:
            self.job_state.img_move_x_mm = self.job_state.retained_image.move_x_mm
            self.job_state.img_move_y_mm = self.job_state.retained_image.move_y_mm

        self.txt_dpi.setText(str(int(round(metadata.dpi_x))))
        self.slider_cmd_count.setMaximum(0)
        self.slider_cmd_count.setValue(0)
        self.btn_center_img.setText("Move top left")
        self._render_image_preview()
        self._update_ui_state()

    def _on_update_dpi(self) -> None:
        dpi_value = self._parse_int(self.txt_dpi, "DPI")
        if dpi_value is None or dpi_value <= 0:
            QMessageBox.warning(self, "Invalid value", "DPI must be a positive integer.")
            return
        self.job_state.dpi_override = dpi_value
        if self.job_state.loaded_file is None:
            return
        metadata = self.converter.inspect_image(self.job_state.loaded_file, dpi_override=dpi_value)
        self.job_state.image_width_mm = metadata.image_width_mm
        self.job_state.image_height_mm = metadata.image_height_mm
        self.job_state.image_dpi = metadata.dpi_x
        self._render_image_preview()

    def _on_move_image(self) -> None:
        x = self._parse_int(self.txt_move_x, "X")
        y = self._parse_int(self.txt_move_y, "Y")
        if x is None or y is None:
            return
        self.job_state.img_move_x_mm = max(0, x)
        self.job_state.img_move_y_mm = max(0, y)
        self._render_image_preview()

    def _on_center_or_top_left(self) -> None:
        if self.job_state.loaded_file is None:
            return
        if "top left" in self.btn_center_img.text().lower():
            self.job_state.img_move_x_mm = 0
            self.job_state.img_move_y_mm = 0
            self.btn_center_img.setText("Center image")
        else:
            self.job_state.img_move_x_mm, self.job_state.img_move_y_mm = self._get_centered_image_origin(
                self.job_state.image_width_mm,
                self.job_state.image_height_mm,
            )
            self.btn_center_img.setText("Move top left")
        self._render_image_preview()

    def _on_motor_power_commands_toggled(self, checked: bool) -> None:
        self.settings.motor_power_commands_enabled = checked
        self.settings_store.save(self.settings)
        if checked:
            self._append_log("Motor power commands enabled (M17/M18).")
        else:
            self._append_log("Motor power commands disabled for legacy controller mode.")
        self._update_ui_state()

    def _on_clear_image(self) -> None:
        self.job_state.clear_image()
        self.preview_canvas.clear_all()
        if self.job_state.retained_image is not None:
            self.preview_canvas.set_render_mode("image")
            self._render_retained_overlay()
        self._update_position_fields()
        self.slider_cmd_count.setMaximum(0)
        self.slider_cmd_count.setValue(0)
        self._update_ui_state()

    def _on_hold_release_image(self) -> None:
        if "hold" in self.btn_hold_img.text().lower():
            if self.job_state.loaded_file is None:
                QMessageBox.information(self, "No image", "Load a BMP image first.")
                return
            self.job_state.retained_image = RetainedImage(
                file_path=self.job_state.loaded_file,
                move_x_mm=self.job_state.img_move_x_mm,
                move_y_mm=self.job_state.img_move_y_mm,
                dpi_override=self.job_state.dpi_override,
                width_mm=self.job_state.image_width_mm,
                height_mm=self.job_state.image_height_mm,
            )
            self.btn_hold_img.setText("Release first image")
            self._render_retained_overlay()
        else:
            self.job_state.retained_image = None
            self.btn_hold_img.setText("Hold image")
            self.preview_canvas.clear_retained_image()
            if self.preview_canvas.render_mode == "lines":
                self.preview_canvas.set_trace_lines(self.job_state.lines)
            else:
                self._render_primary_image()

    def _on_zoom(self, factor: float) -> None:
        self.preview_canvas.zoom(factor)
        self.job_state.preview_scale = self.preview_canvas.scale

    def _on_slice_image(self) -> None:
        if self.job_state.loaded_file is None:
            QMessageBox.information(self, "No image", "Load a BMP image first.")
            return

        end_gcode_lines = [line.strip() for line in self.txt_end_gcode.toPlainText().splitlines() if line.strip()]
        if not end_gcode_lines:
            end_gcode_lines = ["G1 Z1", "G28"]

        result = self.converter.generate(
            image_path=self.job_state.loaded_file,
            img_move_x_mm=self.job_state.img_move_x_mm,
            img_move_y_mm=self.job_state.img_move_y_mm,
            dpi_override=self.job_state.dpi_override,
            black_threshold=70,
            start_gcode_lines=["G1 Z1"],
            end_gcode_lines=end_gcode_lines,
        )

        self.job_state.lines = result.lines
        self.job_state.gcode = result.gcode
        self.job_state.bounding_box = result.bbox
        self.job_state.command_to_line_index = result.command_to_line_index
        self.job_state.line_to_command_index = result.line_to_command_index
        self.job_state.current_send_index = 0
        self.job_state.paused = False

        max_index = max(0, len(self.job_state.lines) - 1)
        self.slider_cmd_count.setMaximum(max_index)
        self.slider_cmd_count.setValue(0)
        self.txt_cmd_start.setText("0")

        self.preview_canvas.set_render_mode("lines")
        self.preview_canvas.set_trace_lines(self.job_state.lines)
        self.preview_canvas.set_bbox_overlay(None, visible=False)
        self.preview_canvas.set_selected_line(0 if self.job_state.lines else -1)
        self._render_retained_overlay()

        self._append_log(f"GCODE commands = {len(self.job_state.gcode)}")
        self._append_log(f"Number of lines = {len(self.job_state.lines)}")
        self._update_ui_state()

    def _on_connect_toggle(self) -> None:
        try:
            if self.transport.is_connected:
                self.streamer.reset()
                self.transport.disconnect()
                self._is_drawing = False
                self.job_state.current_send_index = 0
            else:
                if self.combo_port.count() == 0:
                    self._refresh_ports()
                port = self.combo_port.currentText().strip()
                if not port:
                    QMessageBox.information(self, "No port", "Select a serial port first.")
                    return
                self.transport.connect(port)
                self.settings.last_port = port
                self.settings_store.save(self.settings)
        except Exception as exc:
            QMessageBox.warning(self, "USB error", str(exc))
        self._sync_sleep_inhibitor()
        self._update_ui_state()

    def _stream_is_active(self) -> bool:
        return self.streamer.state.status in {SendStatus.RUNNING, SendStatus.PAUSED}

    def _is_point_within_bounds(self, x_mm: float, y_mm: float, eps: float = 1e-3) -> bool:
        profile = self.settings.machine_profile
        return (
            -eps <= x_mm <= float(profile.canvas_width_mm) + eps
            and -eps <= y_mm <= float(profile.canvas_height_mm) + eps
        )

    def _validate_lines_within_bounds(self) -> tuple[bool, str | None]:
        for line in self.job_state.lines:
            if not self._is_point_within_bounds(line.x0, line.y0):
                return (
                    False,
                    f"Out-of-bounds line start detected at X{line.x0:.3f} Y{line.y0:.3f}.",
                )
            if not self._is_point_within_bounds(line.x1, line.y1):
                return (
                    False,
                    f"Out-of-bounds line end detected at X{line.x1:.3f} Y{line.y1:.3f}.",
                )
        return True, None

    def _validate_bbox_within_bounds(self) -> tuple[bool, str | None]:
        bbox = self.job_state.bounding_box
        if bbox is None:
            return False, "No bounding box available."
        points = [
            (bbox.min_x, bbox.min_y),
            (bbox.max_x, bbox.min_y),
            (bbox.max_x, bbox.max_y),
            (bbox.min_x, bbox.max_y),
        ]
        for x, y in points:
            if not self._is_point_within_bounds(x, y):
                return False, f"Bounding box corner is out of machine bounds at X{x:.3f} Y{y:.3f}."
        return True, None

    def _set_manual_busy(self, is_busy: bool) -> None:
        self._manual_busy = is_busy
        self._update_ui_state()

    def _send_manual_commands_async(self, commands: list[str], label: str) -> None:
        if not self.transport.is_connected:
            QMessageBox.information(self, "Not connected", "Connect USB first.")
            return
        if self._manual_busy:
            QMessageBox.information(self, "Busy", "Another manual command is still running.")
            return
        if self._stream_is_active():
            QMessageBox.information(
                self,
                "Busy",
                "Pause/stop the active stream before sending manual commands.",
            )
            return

        self._set_manual_busy(True)
        worker = threading.Thread(
            target=self._manual_command_worker,
            args=(list(commands), label),
            daemon=True,
            name="plottrbot-manual-command-worker",
        )
        self._manual_worker = worker
        worker.start()

    def _manual_command_worker(self, commands: list[str], label: str) -> None:
        result = ManualCommandResult(ok=True, label=label, error=None)
        for command in commands:
            ack = self.transport.send_command(command)
            if not ack.ok:
                result = ManualCommandResult(
                    ok=False,
                    label=label,
                    error=ack.error or f"Failed to send command: {command}",
                )
                break
        self.bridge.manual_result_signal.emit(result)

    def _on_manual_command_result(self, result_obj: object) -> None:
        if not isinstance(result_obj, ManualCommandResult):
            return
        self._manual_worker = None
        self._set_manual_busy(False)
        if result_obj.ok:
            self._append_log(f"{result_obj.label}: done")
            return
        QMessageBox.warning(self, "Serial error", result_obj.error or "Unknown error")

    def _wait_for_manual_worker(self, timeout_seconds: float = 1.0) -> None:
        worker = self._manual_worker
        if worker is None:
            return
        worker.join(timeout=timeout_seconds)
        if worker.is_alive():
            self._append_log("Manual command worker still active during shutdown; closing anyway.")
            return
        self._manual_worker = None
        self._manual_busy = False

    def _on_send_raw_serial(self) -> None:
        command = self.txt_serial_cmd.text().strip()
        if not command:
            return
        self._send_manual_commands_async([command], "Manual serial send")

    def _on_send_image(self) -> None:
        if not self.transport.is_connected:
            QMessageBox.information(self, "Not connected", "Connect USB first.")
            return
        if not self.job_state.gcode:
            QMessageBox.information(self, "No commands", "Slice the image first.")
            return
        if self._manual_busy:
            QMessageBox.information(self, "Busy", "Wait for current manual command to finish.")
            return
        within_bounds, error = self._validate_lines_within_bounds()
        if not within_bounds:
            QMessageBox.warning(self, "Bounds check failed", error or "Generated lines are out of bounds.")
            return

        start_index = self.job_state.current_send_index
        try:
            self.streamer.send(self.job_state.gcode, start_index=start_index)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Streaming", str(exc))
            return

        self.job_state.paused = False
        self._is_drawing = True
        self.btn_pause_drawing.setText("Pause drawing")
        self._pending_stop_recovery = False
        self._append_log(
            f"Drawing image. Starting at command {start_index} of {len(self.job_state.gcode)}"
        )
        self._sync_sleep_inhibitor()
        self._update_ui_state()

    def _on_pause_resume(self) -> None:
        if not self._is_drawing:
            return
        state = self.streamer.state.status
        if state == SendStatus.RUNNING:
            self.streamer.pause()
            self.job_state.paused = True
            self.btn_pause_drawing.setText("Continue drawing")
            self._append_log(f"Commands successfully sent = {self.job_state.current_send_index}")
        elif state == SendStatus.PAUSED:
            self.streamer.resume()
            self.job_state.paused = False
            self.btn_pause_drawing.setText("Pause drawing")
        self._sync_sleep_inhibitor()

    def _on_stop_drawing(self) -> None:
        if not self._stream_is_active():
            return
        self._pending_stop_recovery = self.checkbox_stop_recovery.isChecked()
        self.streamer.stop()
        self._append_log("Stop requested")

    def _on_start_from_command_number(self) -> None:
        if not self.job_state.lines:
            QMessageBox.information(self, "No slice", "Slice the image first.")
            return
        line_number = self._parse_int(self.txt_cmd_start, "Line number")
        if line_number is None:
            return
        if line_number < 0 or line_number >= len(self.job_state.lines):
            QMessageBox.warning(self, "Out of range", "Line number out of range.")
            return

        if self.job_state.line_to_command_index:
            start_index = self.job_state.line_to_command_index[line_number]
        else:
            start_index = 3 + (line_number * 2)
        self.job_state.current_send_index = start_index
        self._on_send_image()

    def _on_slider_changed(self, value: int) -> None:
        self.job_state.selected_line_index = value
        self.txt_cmd_start.setText(str(value))
        self.preview_canvas.set_selected_line(value)

    def _on_slider_from_text(self) -> None:
        value = self._parse_int(self.txt_cmd_start, "Line number")
        if value is None:
            return
        self.slider_cmd_count.setValue(value)

    def _on_bounding_box(self) -> None:
        bbox = self.job_state.bounding_box
        if bbox is None:
            QMessageBox.information(self, "No slice", "Slice the image first.")
            return

        self.preview_canvas.set_bbox_overlay(bbox, visible=True)

        if not self.transport.is_connected:
            return
        within_bounds, error = self._validate_bbox_within_bounds()
        if not within_bounds:
            QMessageBox.warning(self, "Bounds check failed", error or "Bounding box is out of bounds.")
            return

        pen_position = 0 if self.checkbox_bounding_pen.isChecked() else 1
        commands = [
            f"G1 X{bbox.min_x:.3f} Y{bbox.min_y:.3f}",
            f"G1 X{bbox.max_x:.3f} Y{bbox.min_y:.3f} Z{pen_position}",
            f"G1 X{bbox.max_x:.3f} Y{bbox.max_y:.3f}",
            f"G1 X{bbox.min_x:.3f} Y{bbox.max_y:.3f}",
            f"G1 X{bbox.min_x:.3f} Y{bbox.min_y:.3f}",
            "G1 Z1",
            "G28",
        ]
        self._send_manual_commands_async(commands, "Bounding box trace")

    def _on_save_dimensions(self) -> None:
        width = self._parse_int(self.txt_robot_width, "Robot width")
        height = self._parse_int(self.txt_robot_height, "Robot height")
        if width is None or height is None:
            return
        if width <= 0 or height <= 0:
            QMessageBox.warning(self, "Invalid dimensions", "Dimensions must be positive.")
            return

        profile = self.settings.machine_profile
        profile.canvas_width_mm = width
        profile.canvas_height_mm = height
        profile.home_x_mm = width / 2.0
        self.converter.machine_profile = profile
        self.preview_canvas.set_machine_profile(profile)
        self.settings_store.save(self.settings)
        QMessageBox.information(self, "Saved", "Dimensions saved.")

    def _on_stream_state(self, state_obj: object) -> None:
        if not isinstance(state_obj, SendSessionState):
            return
        self.job_state.current_send_index = state_obj.current_index
        if state_obj.status == SendStatus.RUNNING:
            self._is_drawing = True
        elif state_obj.status == SendStatus.PAUSED:
            self._is_drawing = True
            self.job_state.paused = True
            self.btn_pause_drawing.setText("Continue drawing")
        elif state_obj.status in {SendStatus.COMPLETED, SendStatus.STOPPED, SendStatus.ERROR}:
            self._is_drawing = False
            self.job_state.paused = False
            self.btn_pause_drawing.setText("Pause drawing")
            if state_obj.status == SendStatus.ERROR and state_obj.last_error:
                QMessageBox.warning(self, "Streaming error", state_obj.last_error)
            self._append_log(f"Commands successfully sent = {state_obj.current_index}")
            if state_obj.status == SendStatus.COMPLETED:
                self.job_state.current_send_index = 0
                self._pending_stop_recovery = False
            if (
                self._pending_stop_recovery
                and self.transport.is_connected
                and state_obj.status in {SendStatus.STOPPED, SendStatus.ERROR}
            ):
                self._pending_stop_recovery = False
                self._send_manual_commands_async(["G1 Z1", "G28"], "Stop recovery")
        self._sync_sleep_inhibitor()
        self._update_ui_state()

    def _on_stream_progress(self, sent_command_index: int, _total_commands: int) -> None:
        self.job_state.current_send_index = sent_command_index + 1
        if 0 <= sent_command_index < len(self.job_state.command_to_line_index):
            line_index = self.job_state.command_to_line_index[sent_command_index]
            if line_index >= 0:
                self.slider_cmd_count.setValue(line_index)

    def _update_ui_state(self) -> None:
        has_image = self.job_state.loaded_file is not None
        is_sliced = bool(self.job_state.lines)
        usb_connected = self.transport.is_connected
        stream_active = self._stream_is_active()
        interaction_locked = stream_active or self._manual_busy
        retained_available = self.job_state.retained_image is not None
        self.current_ui_state = derive_ui_state(
            has_image=has_image,
            is_sliced=is_sliced,
            usb_connected=usb_connected,
            is_drawing=self._is_drawing,
        )

        self.btn_connect.setText("Disconnect" if usb_connected else "Connect USB")

        self.btn_select_img.setEnabled(not interaction_locked)
        self.txt_move_x.setEnabled(has_image and not interaction_locked)
        self.txt_move_y.setEnabled(has_image and not interaction_locked)
        self.btn_move_img.setEnabled(has_image and not interaction_locked)
        self.btn_center_img.setEnabled(has_image and not interaction_locked)
        self.btn_clear_img.setEnabled(has_image and not interaction_locked)
        self.btn_slice_img.setEnabled(has_image and not interaction_locked)
        self.btn_hold_img.setEnabled((has_image or retained_available) and not interaction_locked)
        self.txt_dpi.setEnabled(has_image and not interaction_locked)
        self.btn_update_dpi.setEnabled(has_image and not interaction_locked)
        self.combo_port.setEnabled((not usb_connected) and not interaction_locked)
        self.btn_refresh_ports.setEnabled((not usb_connected) and not interaction_locked)
        self.btn_connect.setEnabled(not interaction_locked)
        sliced_controls = is_sliced
        self.slider_cmd_count.setEnabled(sliced_controls)
        self.btn_slider_dec.setEnabled(sliced_controls)
        self.btn_slider_inc.setEnabled(sliced_controls)

        connected_controls = usb_connected and not stream_active and not self._manual_busy
        self.txt_serial_cmd.setEnabled(connected_controls)
        self.btn_send_cmd.setEnabled(connected_controls)
        motor_power_controls = connected_controls and self.settings.motor_power_commands_enabled
        self.btn_enable_stepper.setEnabled(motor_power_controls)
        self.btn_disable_stepper.setEnabled(motor_power_controls)
        self.btn_pen_touch.setEnabled(connected_controls)
        self.btn_pen_away.setEnabled(connected_controls)
        self.btn_home.setEnabled(connected_controls)

        can_draw = is_sliced and usb_connected
        self.btn_bounding_box.setEnabled(can_draw and not stream_active and not self._manual_busy)
        self.btn_send_img.setEnabled(can_draw and not stream_active and not self._manual_busy)
        self.btn_pause_drawing.setEnabled(can_draw and stream_active)
        self.btn_stop_drawing.setEnabled(can_draw and stream_active)
        self.btn_cmd_start.setEnabled(can_draw and not stream_active and not self._manual_busy)

    def _sync_sleep_inhibitor(self) -> None:
        if self.transport.is_connected and self.streamer.state.status == SendStatus.RUNNING:
            self.sleep_inhibitor.start()
            return
        self.sleep_inhibitor.stop()

    def closeEvent(self, event: object) -> None:  # noqa: N802 (Qt API)
        self.settings.window_width = self.width()
        self.settings.window_height = self.height()
        self.settings.end_gcode_lines = [
            line.strip() for line in self.txt_end_gcode.toPlainText().splitlines() if line.strip()
        ] or ["G1 Z1", "G28"]
        self.settings_store.save(self.settings)
        self.streamer.reset()
        self._wait_for_manual_worker()
        self.transport.disconnect()
        self.sleep_inhibitor.stop()
        super().closeEvent(event)  # type: ignore[arg-type]
