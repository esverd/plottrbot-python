from __future__ import annotations

import bisect
import io
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
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
    QSpinBox,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PIL import Image

from plottrbot.config.settings import SettingsStore, default_end_gcode_lines, uses_builtin_end_gcode
from plottrbot.core.bmp_converter import BmpConverter
from plottrbot.core.draw_session_logger import DrawSessionLogger
from plottrbot.core.image_prep import (
    ImagePrepSettings,
    ImagePrepState,
    expected_threshold_count,
    is_supported_source_image,
    process_image_for_prep,
    processed_bmp_path_for_image,
    read_sidecar,
    save_processed_bmp,
    sidecar_path_for_image,
    write_sidecar,
)
from plottrbot.core.models import JobState, RetainedImage
from plottrbot.core.state_machine import UiState, derive_ui_state
from plottrbot.serial.dummy_transport import DummyTransport
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
    BBOX_POINT_GRID: tuple[tuple[tuple[str, float, float], ...], ...] = (
        (
            ("top left", 0.0, 0.0),
            ("top middle", 0.5, 0.0),
            ("top right", 1.0, 0.0),
        ),
        (
            ("middle left", 0.0, 0.5),
            ("middle", 0.5, 0.5),
            ("middle right", 1.0, 0.5),
        ),
        (
            ("bottom left", 0.0, 1.0),
            ("bottom middle", 0.5, 1.0),
            ("bottom right", 1.0, 1.0),
        ),
    )

    def __init__(
        self,
        *,
        settings_store: SettingsStore | None = None,
        transport: NanoTransport | None = None,
        streamer: ProgramStreamer | None = None,
        converter: BmpConverter | None = None,
        sleep_inhibitor: SleepInhibitor | None = None,
        draw_session_logger: DrawSessionLogger | None = None,
        dummy_serial: bool = False,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Warhol Slicer")

        self.settings_store = settings_store or SettingsStore()
        self.settings = self.settings_store.load()
        self.job_state = JobState(preview_scale=0.8)
        self.image_prep_state = ImagePrepState()
        self._prep_updating_controls = False
        self._prep_preview_full_pixmap = QPixmap()
        self._prep_threshold_rows: list[QWidget] = []
        self._prep_threshold_sliders: list[QSlider] = []
        self._prep_threshold_spinboxes: list[QSpinBox] = []
        self._prep_slider_syncing = False
        self._prep_recompute_timer = QTimer(self)
        self._prep_recompute_timer.setSingleShot(True)
        self._prep_recompute_timer.setInterval(80)
        self._prep_recompute_timer.timeout.connect(self._on_prep_settings_changed)
        self._pending_line_restart: tuple[int, int] | None = None

        self.converter = converter or BmpConverter(self.settings.machine_profile)
        self.bridge = UiBridge()

        self._dummy_serial_enabled = dummy_serial and transport is None
        if transport is not None:
            self.transport = transport
        elif dummy_serial:
            self.transport = DummyTransport(
                self.settings.machine_profile,
                on_log=self.bridge.log_signal.emit,
            )
        else:
            self.transport = NanoTransport(
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
        self.draw_session_logger = draw_session_logger or DrawSessionLogger(
            self.settings_store.path.parent / "draw_logs"
        )

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
        if self._dummy_serial_enabled:
            self._append_log("Dummy serial mode enabled. Hardware commands will be simulated.")
        self._update_ui_state()

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("appRoot")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        self.setCentralWidget(root)

        self.workflow_nav = QWidget()
        self.workflow_nav.setObjectName("workflowNav")
        nav_layout = QVBoxLayout(self.workflow_nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(6)

        nav_title = QLabel("Warhol Slicer")
        nav_title.setObjectName("workflowNavTitle")
        nav_layout.addWidget(nav_title)

        self.workflow_button_group = QButtonGroup(self)
        self.workflow_button_group.setExclusive(True)
        self.workflow_buttons: dict[str, QPushButton] = {}
        self.workflow_order: tuple[tuple[str, str], ...] = (
            ("prep", "Prep"),
            ("place", "Place Job"),
            ("connect", "Run"),
            ("advanced", "Advanced"),
        )
        for index, (key, label) in enumerate(self.workflow_order, start=1):
            button = QPushButton(f"{index}. {label}")
            button.setCheckable(True)
            button.setObjectName("workflowNavButton")
            self.workflow_buttons[key] = button
            self.workflow_button_group.addButton(button)
            nav_layout.addWidget(button)
        nav_layout.addStretch(1)
        layout.addWidget(self.workflow_nav, 0)

        self.workflow_stack = QStackedWidget()
        self.workflow_stack.setObjectName("workflowStack")
        self.workflow_stack.setMinimumWidth(500)

        self.prep_page = QWidget()
        self.place_page = QWidget()
        self.connect_page = QWidget()
        self.advanced_page = QWidget()

        self.image_prep_tab = self.prep_page
        self.control_tab = self.place_page
        self.advanced_tab = self.advanced_page
        self.workflow_pages: dict[str, QWidget] = {
            "prep": self.prep_page,
            "place": self.place_page,
            "connect": self.connect_page,
            "advanced": self.advanced_page,
        }

        self._build_image_prep_tab()
        self._build_place_page()
        self._build_connect_page()
        self._build_advanced_tab()

        for key, _label in self.workflow_order:
            self.workflow_stack.addWidget(self.workflow_pages[key])
        layout.addWidget(self.workflow_stack, 0)

        self.preview_panel = QWidget()
        self.preview_panel.setObjectName("previewPanel")
        preview_panel_layout = QVBoxLayout(self.preview_panel)
        preview_panel_layout.setContentsMargins(0, 0, 0, 0)
        preview_panel_layout.setSpacing(6)

        preview_header = QWidget()
        preview_header.setObjectName("previewHeader")
        preview_header_layout = QHBoxLayout(preview_header)
        preview_header_layout.setContentsMargins(10, 8, 10, 8)
        preview_header_layout.setSpacing(8)

        preview_text = QVBoxLayout()
        preview_text.setContentsMargins(0, 0, 0, 0)
        preview_text.setSpacing(2)
        self.lbl_preview_title = QLabel("Preview")
        self.lbl_preview_title.setObjectName("previewTitle")
        self.lbl_preview_status = QLabel("No job image selected.")
        self.lbl_preview_status.setObjectName("previewStatus")
        self.lbl_preview_status.setWordWrap(True)
        preview_text.addWidget(self.lbl_preview_title)
        preview_text.addWidget(self.lbl_preview_status)
        preview_header_layout.addLayout(preview_text, 1)

        self.btn_zoom_out = QPushButton("Zoom out")
        self.btn_zoom_in = QPushButton("Zoom in")
        self.btn_zoom_out.setToolTip("Zoom machine preview out")
        self.btn_zoom_in.setToolTip("Zoom machine preview in")
        preview_header_layout.addWidget(self.btn_zoom_out)
        preview_header_layout.addWidget(self.btn_zoom_in)
        preview_panel_layout.addWidget(preview_header, 0)

        self.prep_preview_label = QLabel("Load a JPG to begin.")
        self.prep_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prep_preview_label.setMinimumSize(1, 1)
        self.prep_preview_scroll = QScrollArea()
        self.prep_preview_scroll.setWidgetResizable(True)
        self.prep_preview_scroll.setWidget(self.prep_preview_label)
        self.prep_preview_scroll.viewport().installEventFilter(self)

        self.preview_canvas = PreviewCanvas(self.settings.machine_profile)
        self.preview_canvas.set_scale(self.job_state.preview_scale)

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setWidget(self.preview_canvas)

        self.machine_preview_panel = QWidget()
        machine_preview_layout = QVBoxLayout(self.machine_preview_panel)
        machine_preview_layout.setContentsMargins(0, 0, 0, 0)
        machine_preview_layout.addWidget(self.preview_scroll)

        self.prep_preview_panel = QWidget()
        prep_preview_layout = QVBoxLayout(self.prep_preview_panel)
        prep_preview_layout.setContentsMargins(0, 0, 0, 0)
        prep_preview_layout.addWidget(self.prep_preview_scroll)

        self.right_preview_stack = QStackedWidget()
        self.right_preview_stack.addWidget(self.prep_preview_panel)
        self.right_preview_stack.addWidget(self.machine_preview_panel)
        preview_panel_layout.addWidget(self.right_preview_stack, 1)
        layout.addWidget(self.preview_panel, 1)

        self._apply_operator_style()
        self._set_workflow_page("prep")

    def _build_image_prep_tab(self) -> None:
        layout = QVBoxLayout(self.image_prep_tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        source_group = QGroupBox("Image source")
        source_layout = QVBoxLayout(source_group)

        source_buttons = QHBoxLayout()
        self.btn_prep_load_jpg = QPushButton("Open JPG/sidecar")
        self.btn_prep_load_sidecar = QPushButton("Load sidecar")
        self.btn_prep_load_sidecar.hide()
        self.btn_prep_skip_to_place = QPushButton("Place BMP/job")
        source_buttons.addWidget(self.btn_prep_load_jpg)
        source_buttons.addWidget(self.btn_prep_skip_to_place)
        source_layout.addLayout(source_buttons)

        self.lbl_prep_source = QLabel("Source: none")
        self.lbl_prep_source.setWordWrap(True)
        self.lbl_prep_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        source_layout.addWidget(self.lbl_prep_source)

        self.lbl_prep_folder = QLabel("Folder: n/a")
        self.lbl_prep_folder.setWordWrap(True)
        self.lbl_prep_folder.setObjectName("secondaryInfo")
        self.lbl_prep_folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        source_layout.addWidget(self.lbl_prep_folder)

        self.lbl_prep_dimensions = QLabel("Image size: n/a")
        self.lbl_prep_dimensions.setWordWrap(True)
        source_layout.addWidget(self.lbl_prep_dimensions)
        layout.addWidget(source_group)

        controls_group = QGroupBox("Prep controls")
        controls_layout = QGridLayout(controls_group)
        controls_layout.setColumnStretch(1, 1)
        controls_layout.setColumnStretch(3, 1)

        self.spin_prep_dpi = QSpinBox()
        self.spin_prep_dpi.setRange(1, 1200)
        self.spin_prep_dpi.setValue(self.DEFAULT_BMP_DPI)
        controls_layout.addWidget(QLabel("DPI"), 0, 0)
        controls_layout.addWidget(self.spin_prep_dpi, 0, 1)

        self.spin_prep_width_mm = QDoubleSpinBox()
        self.spin_prep_width_mm.setRange(1.0, 100000.0)
        self.spin_prep_width_mm.setSingleStep(1.0)
        self.spin_prep_width_mm.setDecimals(1)
        self.spin_prep_width_mm.setKeyboardTracking(False)
        self.spin_prep_width_mm.setValue(100.0)
        controls_layout.addWidget(QLabel("Width [mm]"), 0, 2)
        controls_layout.addWidget(self.spin_prep_width_mm, 0, 3)

        self.spin_prep_height_mm = QDoubleSpinBox()
        self.spin_prep_height_mm.setRange(1.0, 100000.0)
        self.spin_prep_height_mm.setSingleStep(1.0)
        self.spin_prep_height_mm.setDecimals(1)
        self.spin_prep_height_mm.setKeyboardTracking(False)
        self.spin_prep_height_mm.setValue(100.0)
        controls_layout.addWidget(QLabel("Height [mm]"), 1, 2)
        controls_layout.addWidget(self.spin_prep_height_mm, 1, 3)

        controls_layout.addWidget(QLabel("Contrast"), 2, 0)
        contrast_row = QHBoxLayout()
        contrast_row.setContentsMargins(0, 0, 0, 0)
        self.slider_prep_contrast = QSlider(Qt.Orientation.Horizontal)
        self.slider_prep_contrast.setRange(-100, 300)
        self.slider_prep_contrast.setValue(0)
        self.spin_prep_contrast = QSpinBox()
        self.spin_prep_contrast.setRange(-100, 1000)
        self.spin_prep_contrast.setSingleStep(5)
        self.spin_prep_contrast.setKeyboardTracking(False)
        self.spin_prep_contrast.setValue(0)
        self.spin_prep_contrast.setMinimumWidth(84)
        contrast_row.addWidget(self.slider_prep_contrast, 1)
        contrast_row.addWidget(self.spin_prep_contrast)
        controls_layout.addLayout(contrast_row, 2, 1, 1, 3)

        controls_layout.addWidget(QLabel("Blur"), 3, 0)
        blur_row = QHBoxLayout()
        blur_row.setContentsMargins(0, 0, 0, 0)
        self.slider_prep_blur = QSlider(Qt.Orientation.Horizontal)
        self.slider_prep_blur.setRange(0, 100)
        self.slider_prep_blur.setValue(0)
        self.spin_prep_blur = QDoubleSpinBox()
        self.spin_prep_blur.setRange(0.0, 10.0)
        self.spin_prep_blur.setSingleStep(0.1)
        self.spin_prep_blur.setDecimals(1)
        self.spin_prep_blur.setKeyboardTracking(False)
        self.spin_prep_blur.setValue(0.0)
        self.spin_prep_blur.setMinimumWidth(84)
        blur_row.addWidget(self.slider_prep_blur, 1)
        blur_row.addWidget(self.spin_prep_blur)
        controls_layout.addLayout(blur_row, 3, 1, 1, 3)

        self.spin_prep_levels = QSpinBox()
        self.spin_prep_levels.setRange(2, 8)
        self.spin_prep_levels.setValue(4)
        controls_layout.addWidget(QLabel("Tone levels"), 4, 0)
        controls_layout.addWidget(self.spin_prep_levels, 4, 1)

        self.combo_prep_strategy = QComboBox()
        self.combo_prep_strategy.addItems(["banded", "relative"])
        controls_layout.addWidget(QLabel("Threshold mode"), 4, 2)
        controls_layout.addWidget(self.combo_prep_strategy, 4, 3)

        self.checkbox_prep_lock_aspect = QCheckBox("Lock aspect ratio")
        self.checkbox_prep_lock_aspect.setChecked(True)
        controls_layout.addWidget(self.checkbox_prep_lock_aspect, 5, 0, 1, 2)

        self.checkbox_prep_auto_thresholds = QCheckBox("Use auto thresholds")
        self.checkbox_prep_auto_thresholds.setChecked(True)
        controls_layout.addWidget(self.checkbox_prep_auto_thresholds, 5, 2, 1, 2)

        self.checkbox_prep_halftone_preview = QCheckBox("Show halftone preview")
        self.checkbox_prep_halftone_preview.setChecked(False)
        controls_layout.addWidget(self.checkbox_prep_halftone_preview, 6, 0, 1, 2)

        self.lbl_prep_manual_thresholds = QLabel("Manual thresholds")
        controls_layout.addWidget(self.lbl_prep_manual_thresholds, 7, 0)
        self.prep_threshold_container = QWidget()
        threshold_layout = QVBoxLayout(self.prep_threshold_container)
        threshold_layout.setContentsMargins(0, 0, 0, 0)
        threshold_layout.setSpacing(4)
        for index in range(7):
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            label = QLabel(f"T{index + 1}")
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 255)
            slider.setValue(0)
            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setValue(0)
            spin.setMinimumWidth(72)
            row_layout.addWidget(label)
            row_layout.addWidget(slider, 1)
            row_layout.addWidget(spin)
            threshold_layout.addWidget(row_widget)
            self._prep_threshold_rows.append(row_widget)
            self._prep_threshold_sliders.append(slider)
            self._prep_threshold_spinboxes.append(spin)
        controls_layout.addWidget(self.prep_threshold_container, 7, 1, 1, 3)

        self.lbl_prep_effective_thresholds = QLabel("Effective thresholds: n/a")
        self.lbl_prep_effective_thresholds.setWordWrap(True)
        controls_layout.addWidget(self.lbl_prep_effective_thresholds, 8, 0, 1, 4)
        layout.addWidget(controls_group)

        action_row = QHBoxLayout()
        self.btn_prep_save_outputs = QPushButton("Export BMP + sidecar")
        self.btn_prep_apply_to_control = QPushButton("Use in Place")
        self.btn_prep_reset_defaults = QPushButton("Reset defaults")
        self.btn_prep_apply_to_control.setProperty("role", "primary")
        action_row.addWidget(self.btn_prep_save_outputs)
        action_row.addWidget(self.btn_prep_apply_to_control)
        action_row.addWidget(self.btn_prep_reset_defaults)
        layout.addLayout(action_row)

        layout.addStretch(1)

    def _build_place_page(self) -> None:
        layout = QVBoxLayout(self.place_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        image_group = QGroupBox("Job image")
        image_layout = QVBoxLayout(image_group)
        self.btn_select_img = QPushButton("Select image")
        self.btn_move_img = QPushButton("Set position")
        self.btn_center_img = QPushButton("Move top left")
        self.btn_clear_img = QPushButton("Clear")
        self.btn_slice_img = QPushButton("Slice image")
        self.btn_slice_img.setProperty("role", "primary")

        move_grid = QGridLayout()
        self.txt_move_x = QLineEdit("0")
        self.txt_move_y = QLineEdit("0")
        move_grid.addWidget(QLabel("Top-left X [mm]"), 0, 0)
        move_grid.addWidget(self.txt_move_x, 0, 1)
        move_grid.addWidget(self.btn_move_img, 0, 2)
        move_grid.addWidget(QLabel("Top-left Y [mm]"), 1, 0)
        move_grid.addWidget(self.txt_move_y, 1, 1)
        move_grid.addWidget(self.btn_center_img, 1, 2)

        dpi_row = QHBoxLayout()
        dpi_row.addWidget(QLabel("Current DPI"))
        self.txt_dpi = QLineEdit()
        self.txt_dpi.setMaximumWidth(100)
        self.btn_update_dpi = QPushButton("Update DPI")
        dpi_row.addWidget(self.txt_dpi)
        dpi_row.addWidget(self.btn_update_dpi)
        dpi_row.addStretch(1)

        image_layout.addWidget(self.btn_select_img)
        image_layout.addLayout(move_grid)
        image_layout.addLayout(dpi_row)
        image_layout.addWidget(self.btn_clear_img)
        image_layout.addWidget(self.btn_slice_img)
        layout.addWidget(image_group)

        footprint_group = QGroupBox("Canvas footprint")
        footprint_layout = QVBoxLayout(footprint_group)
        self.lbl_bbox_hint = QLabel("Slice image, then connect USB to use footprint tools.")
        self.lbl_bbox_hint.setObjectName("secondaryInfo")
        self.lbl_bbox_hint.setWordWrap(True)
        self.btn_bounding_box = QPushButton("Trace bounding box")
        self.checkbox_bounding_pen = QCheckBox("Use pen when tracing bounding box")

        self.bbox_point_buttons: dict[str, QPushButton] = {}
        self.bbox_points_panel = QWidget()
        bbox_points_panel_layout = QVBoxLayout(self.bbox_points_panel)
        bbox_points_panel_layout.setContentsMargins(0, 0, 0, 0)
        bbox_points_panel_layout.setSpacing(3)
        bbox_points_label = QLabel("Bounding-box points")
        bbox_points_label.setObjectName("bboxPointsHeader")
        bbox_points_panel_layout.addWidget(bbox_points_label)

        self.bbox_points_grid = QWidget()
        bbox_points_layout = QGridLayout(self.bbox_points_grid)
        bbox_points_layout.setContentsMargins(0, 0, 0, 0)
        bbox_points_layout.setHorizontalSpacing(4)
        bbox_points_layout.setVerticalSpacing(4)
        for row_index, row in enumerate(self.BBOX_POINT_GRID):
            for col_index, (point_label, _x_ratio, _y_ratio) in enumerate(row):
                button = QPushButton("")
                button.setObjectName("bboxPointButton")
                button.setFixedSize(36, 36)
                button.setToolTip(f"Move to {point_label.title()} of bounding box")
                button.setAccessibleName(f"Bounding box point {point_label}")
                self.bbox_point_buttons[point_label] = button
                bbox_points_layout.addWidget(button, row_index, col_index)
        bbox_points_panel_layout.addWidget(self.bbox_points_grid)
        self.bbox_points_panel.setStyleSheet(
            """
            QLabel#bboxPointsHeader {
                color: #4f5561;
                font-size: 11px;
            }
            QPushButton#bboxPointButton {
                border: 1px solid #b8bec8;
                border-radius: 4px;
                background: #f7f9fc;
                padding: 0px;
            }
            QPushButton#bboxPointButton:hover:!disabled {
                background: #edf2f8;
                border-color: #8f99a8;
            }
            QPushButton#bboxPointButton:pressed:!disabled {
                background: #e1e7f0;
            }
            QPushButton#bboxPointButton:disabled {
                color: transparent;
                border-color: #d0d4db;
                background: #f2f4f7;
            }
            """
        )
        footprint_layout.addWidget(self.lbl_bbox_hint)
        footprint_layout.addWidget(self.btn_bounding_box)
        footprint_layout.addWidget(self.checkbox_bounding_pen)
        footprint_layout.addWidget(self.bbox_points_panel)
        layout.addWidget(footprint_group)
        layout.addStretch(1)

    def _build_connect_page(self) -> None:
        layout = QVBoxLayout(self.connect_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        robot_group = QGroupBox("Robot setup")
        robot_layout = QVBoxLayout(robot_group)
        port_row = QHBoxLayout()
        self.combo_port = QComboBox()
        self.btn_refresh_ports = QPushButton("Refresh")
        self.btn_connect = QPushButton("Connect USB")
        self.btn_connect.setProperty("role", "primary")
        port_row.addWidget(self.combo_port, 1)
        port_row.addWidget(self.btn_refresh_ports)
        port_row.addWidget(self.btn_connect)
        robot_layout.addLayout(port_row)

        motor_row = QHBoxLayout()
        self.btn_enable_stepper = QPushButton("Enable motors")
        self.btn_disable_stepper = QPushButton("Disable motors")
        motor_row.addWidget(self.btn_enable_stepper)
        motor_row.addWidget(self.btn_disable_stepper)
        robot_layout.addLayout(motor_row)

        pen_row = QHBoxLayout()
        self.btn_pen_touch = QPushButton("Set tool to canvas")
        self.btn_pen_away = QPushButton("Set away from canvas")
        pen_row.addWidget(self.btn_pen_touch)
        pen_row.addWidget(self.btn_pen_away)
        robot_layout.addLayout(pen_row)

        self.btn_home = QPushButton("Move to home position")
        robot_layout.addWidget(self.btn_home)
        layout.addWidget(robot_group)

        run_group = QGroupBox("Run job")
        run_layout = QVBoxLayout(run_group)
        self.lbl_run_state = QLabel("Slice needed")
        self.lbl_run_state.setObjectName("runStatePill")
        self.lbl_run_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.checkbox_stop_recovery = QCheckBox("On stop: lift tool and home")
        self.checkbox_stop_recovery.setChecked(True)

        cmd_row = QHBoxLayout()
        self.txt_cmd_start = QLineEdit("0")
        self.btn_cmd_start = QPushButton("Draw from selected line")
        self.lbl_resume_line = QLabel("Selected line 0 / 0")
        self.lbl_resume_line.setObjectName("secondaryInfo")
        cmd_row.addWidget(self.txt_cmd_start)
        cmd_row.addWidget(self.btn_cmd_start)

        slider_row = QHBoxLayout()
        self.slider_cmd_count = QSlider(Qt.Orientation.Horizontal)
        self.slider_cmd_count.setMinimum(0)
        self.slider_cmd_count.setMaximum(0)
        self.btn_slider_dec = QPushButton("<")
        self.btn_slider_inc = QPushButton(">")
        slider_row.addWidget(self.slider_cmd_count, 1)
        slider_row.addWidget(self.btn_slider_dec)
        slider_row.addWidget(self.btn_slider_inc)

        self.btn_pause_drawing = QPushButton("Pause drawing")
        self.btn_stop_drawing = QPushButton("Stop drawing")
        self.btn_send_img = QPushButton("Send image to robot")
        self.btn_send_img.setProperty("role", "primary")
        self.btn_stop_drawing.setProperty("role", "danger")
        run_layout.addWidget(self.lbl_run_state)
        run_layout.addWidget(self.checkbox_stop_recovery)
        run_layout.addWidget(self.lbl_resume_line)
        run_layout.addLayout(cmd_row)
        run_layout.addLayout(slider_row)
        run_layout.addWidget(self.btn_send_img)
        run_layout.addWidget(self.btn_pause_drawing)
        run_layout.addWidget(self.btn_stop_drawing)
        layout.addWidget(run_group)
        layout.addStretch(1)

    def _build_advanced_tab(self) -> None:
        layout = QVBoxLayout(self.advanced_tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        overlay_group = QGroupBox("Image overlay")
        overlay_layout = QVBoxLayout(overlay_group)
        self.btn_hold_img = QPushButton("Hold image")
        overlay_layout.addWidget(self.btn_hold_img)
        layout.addWidget(overlay_group)

        serial_group = QGroupBox("Raw serial")
        serial_layout = QVBoxLayout(serial_group)
        serial_row = QHBoxLayout()
        self.txt_serial_cmd = QLineEdit()
        self.btn_send_cmd = QPushButton("Send serial msg")
        serial_row.addWidget(self.txt_serial_cmd, 1)
        serial_row.addWidget(self.btn_send_cmd)
        serial_layout.addLayout(serial_row)
        layout.addWidget(serial_group)

        settings_group = QGroupBox("Machine settings")
        settings_layout = QVBoxLayout(settings_group)
        self.checkbox_motor_power_commands = QCheckBox("Enable motor power commands (M17/M18)")
        settings_layout.addWidget(self.checkbox_motor_power_commands)
        settings_layout.addWidget(QLabel("End GCODE"))
        self.txt_end_gcode = QPlainTextEdit()
        self.txt_end_gcode.setFixedHeight(90)
        settings_layout.addWidget(self.txt_end_gcode)

        dims_grid = QGridLayout()
        self.txt_robot_width = QLineEdit()
        self.txt_robot_height = QLineEdit()
        self.btn_save_dims = QPushButton("Save machine settings")
        dims_grid.addWidget(QLabel("Robot width [mm]"), 0, 0)
        dims_grid.addWidget(self.txt_robot_width, 0, 1)
        dims_grid.addWidget(QLabel("Robot height [mm]"), 1, 0)
        dims_grid.addWidget(self.txt_robot_height, 1, 1)
        dims_grid.addWidget(self.btn_save_dims, 2, 0, 1, 2)
        settings_layout.addLayout(dims_grid)
        layout.addWidget(settings_group)

        log_group = QGroupBox("Status log")
        log_layout = QVBoxLayout(log_group)
        self.txt_out = QPlainTextEdit()
        self.txt_out.setReadOnly(True)
        self.txt_out.setPlaceholderText("Status messages")
        self.txt_out.setMaximumBlockCount(2000)
        self.txt_out.setMinimumHeight(220)
        log_layout.addWidget(self.txt_out)
        layout.addWidget(log_group, 2)

    def _apply_operator_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget#appRoot {
                background: #f6f8fa;
            }
            QWidget#workflowNav {
                background: #ffffff;
                border: 1px solid #d8dee6;
                border-radius: 6px;
                min-width: 136px;
                max-width: 136px;
            }
            QLabel#workflowNavTitle {
                color: #1f2933;
                font-weight: 600;
                padding: 10px 8px 6px 8px;
            }
            QPushButton#workflowNavButton {
                border: 0;
                border-radius: 5px;
                color: #2f3a45;
                padding: 8px 10px;
                text-align: left;
            }
            QPushButton#workflowNavButton:hover {
                background: #eef3f8;
            }
            QPushButton#workflowNavButton:checked {
                background: #dcebf7;
                color: #0f4f79;
                font-weight: 600;
            }
            QStackedWidget#workflowStack {
                background: transparent;
            }
            QWidget#previewPanel {
                background: #ffffff;
                border: 1px solid #d8dee6;
                border-radius: 6px;
            }
            QWidget#previewHeader {
                background: #f9fbfd;
                border-bottom: 1px solid #d8dee6;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }
            QLabel#previewTitle {
                color: #1f2933;
                font-weight: 600;
            }
            QLabel#previewStatus {
                color: #52606d;
                font-size: 11px;
            }
            QLabel#secondaryInfo {
                color: #52606d;
                font-size: 11px;
            }
            QLabel#runStatePill {
                border-radius: 5px;
                padding: 6px 10px;
                font-weight: 600;
            }
            QLabel#runStatePill[state="blocked"] {
                background: #f2f4f7;
                border: 1px solid #d5dce4;
                color: #596574;
            }
            QLabel#runStatePill[state="ready"] {
                background: #e6f3ec;
                border: 1px solid #abd6bd;
                color: #1f6f43;
            }
            QLabel#runStatePill[state="running"] {
                background: #e7f0fb;
                border: 1px solid #9fc4e8;
                color: #155d91;
            }
            QLabel#runStatePill[state="paused"] {
                background: #fff4d8;
                border: 1px solid #e5c66f;
                color: #7a5600;
            }
            QPushButton[role="primary"] {
                background: #0f6da8;
                color: #ffffff;
                border: 1px solid #0c5d8f;
                border-radius: 5px;
                padding: 5px 10px;
            }
            QPushButton[role="primary"]:disabled {
                background: #edf1f5;
                color: #8d99a6;
                border-color: #d5dce4;
            }
            QPushButton[role="danger"] {
                color: #9b1c1c;
                border-color: #d7a7a7;
            }
            QGroupBox {
                border: 1px solid #cfd7df;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 10px;
                background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
                color: #2f3a45;
                font-weight: 600;
            }
            """
        )

    def _connect_signals(self) -> None:
        for key, button in self.workflow_buttons.items():
            button.clicked.connect(lambda _checked=False, page_key=key: self._set_workflow_page(page_key))
        self.workflow_stack.currentChanged.connect(self._on_workflow_stack_changed)

        self.btn_prep_load_jpg.clicked.connect(self._on_prep_open_source)
        self.btn_prep_load_sidecar.clicked.connect(self._on_prep_load_sidecar)
        self.btn_prep_skip_to_place.clicked.connect(lambda: self._set_workflow_page("place"))
        self.btn_prep_save_outputs.clicked.connect(self._on_prep_save_outputs)
        self.btn_prep_apply_to_control.clicked.connect(self._on_prep_apply_to_control)
        self.btn_prep_reset_defaults.clicked.connect(self._on_prep_reset_defaults)
        self.spin_prep_dpi.valueChanged.connect(self._on_prep_settings_changed)
        self.spin_prep_width_mm.valueChanged.connect(self._on_prep_dimension_changed)
        self.spin_prep_height_mm.valueChanged.connect(self._on_prep_dimension_changed)
        self.slider_prep_contrast.valueChanged.connect(self._on_prep_contrast_slider_changed)
        self.slider_prep_contrast.sliderReleased.connect(self._flush_prep_recompute)
        self.spin_prep_contrast.valueChanged.connect(self._on_prep_contrast_spin_changed)
        self.slider_prep_blur.valueChanged.connect(self._on_prep_blur_slider_changed)
        self.slider_prep_blur.sliderReleased.connect(self._flush_prep_recompute)
        self.spin_prep_blur.valueChanged.connect(self._on_prep_blur_spin_changed)
        self.spin_prep_levels.valueChanged.connect(self._on_prep_levels_changed)
        self.combo_prep_strategy.currentTextChanged.connect(self._on_prep_settings_changed)
        self.checkbox_prep_lock_aspect.toggled.connect(self._on_prep_settings_changed)
        self.checkbox_prep_auto_thresholds.toggled.connect(self._on_prep_auto_thresholds_toggled)
        for index, slider in enumerate(self._prep_threshold_sliders):
            slider.valueChanged.connect(
                lambda _value, threshold_index=index: self._on_prep_threshold_slider_changed(threshold_index)
            )
            slider.sliderReleased.connect(self._flush_prep_recompute)
        for index, spin in enumerate(self._prep_threshold_spinboxes):
            spin.valueChanged.connect(
                lambda _value, threshold_index=index: self._on_prep_threshold_spin_changed(threshold_index)
            )
        self.checkbox_prep_halftone_preview.toggled.connect(self._on_prep_preview_toggle_changed)

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
        for row in self.BBOX_POINT_GRID:
            for point_label, x_ratio, y_ratio in row:
                self.bbox_point_buttons[point_label].clicked.connect(
                    lambda _checked=False, label=point_label, xr=x_ratio, yr=y_ratio: self._on_move_to_bbox_point(
                        label,
                        xr,
                        yr,
                    )
                )
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
        self._sync_prep_controls_from_state()
        self.btn_pause_drawing.setText("Pause drawing")
        self._append_log("Ready")

    def _show_toast(self, message: str, timeout_ms: int = 2200) -> None:
        self.statusBar().showMessage(message, timeout_ms)

    def _compact_path(self, path: Path, *, max_chars: int = 72) -> str:
        text = str(path)
        if len(text) <= max_chars:
            return text
        return f"...{text[-(max_chars - 3):]}"

    def _machine_prep_limits(self) -> tuple[float, float]:
        max_width = float(max(1, int(self.settings.machine_profile.canvas_width_mm)))
        max_height = float(max(1, int(self.settings.machine_profile.canvas_height_mm)))
        return max_width, max_height

    def _default_target_dimensions_for_source(self, image_path: Path, *, long_side_mm: float = 400.0) -> tuple[float, float]:
        with Image.open(image_path) as image:
            width_px, height_px = image.size
        if width_px <= 0 or height_px <= 0:
            return 400.0, 400.0

        long_side = max(1.0, float(long_side_mm))
        if width_px >= height_px:
            width_mm = long_side
            height_mm = long_side * (height_px / width_px)
        else:
            height_mm = long_side
            width_mm = long_side * (width_px / height_px)

        max_width, max_height = self._machine_prep_limits()
        scale = min(1.0, max_width / max(width_mm, 1e-9), max_height / max(height_mm, 1e-9))
        return max(1.0, width_mm * scale), max(1.0, height_mm * scale)

    def _clamp_prep_dimensions_to_machine(
        self,
        *,
        width_mm: float,
        height_mm: float,
    ) -> tuple[float, float, bool]:
        max_width, max_height = self._machine_prep_limits()
        clamped_width = max(1.0, min(float(width_mm), max_width))
        clamped_height = max(1.0, min(float(height_mm), max_height))
        changed = (
            abs(clamped_width - float(width_mm)) > 1e-9
            or abs(clamped_height - float(height_mm)) > 1e-9
        )
        return clamped_width, clamped_height, changed

    def _sanitize_prep_settings_for_machine(self, settings: ImagePrepSettings) -> tuple[ImagePrepSettings, bool]:
        sanitized = settings.sanitized()
        width_mm, height_mm, clamped = self._clamp_prep_dimensions_to_machine(
            width_mm=sanitized.target_width_mm,
            height_mm=sanitized.target_height_mm,
        )
        sanitized.target_width_mm = width_mm
        sanitized.target_height_mm = height_mm
        return sanitized, clamped

    def _update_prep_dimension_spin_limits(self) -> None:
        self._prep_updating_controls = True
        self.spin_prep_width_mm.setRange(1.0, 100000.0)
        self.spin_prep_height_mm.setRange(1.0, 100000.0)
        self._prep_updating_controls = False

    def _dialog_start_dir(self) -> str:
        stored = self.settings.last_open_dir.strip()
        if not stored:
            return ""
        try:
            candidate = Path(stored)
            if candidate.is_file():
                candidate = candidate.parent
            if candidate.exists():
                return str(candidate)
        except OSError:
            return ""
        return ""

    def _remember_open_dir(self, selected_path: str | Path) -> None:
        try:
            path = Path(selected_path)
            directory = path.parent if path.suffix else path
            resolved_dir = directory.resolve()
        except (TypeError, ValueError, OSError):
            return
        if not resolved_dir.exists():
            return
        directory_text = str(resolved_dir)
        if directory_text == self.settings.last_open_dir:
            return
        self.settings.last_open_dir = directory_text
        self.settings_store.save(self.settings)

    def _next_non_overwriting_save_paths(self, base_bmp_path: Path, base_sidecar_path: Path) -> tuple[Path, Path]:
        if not base_bmp_path.exists() and not base_sidecar_path.exists():
            return base_bmp_path, base_sidecar_path
        for index in range(1, 10000):
            suffix = f"-{index:03d}"
            bmp_candidate = base_bmp_path.with_name(f"{base_bmp_path.stem}{suffix}{base_bmp_path.suffix}")
            sidecar_candidate = base_sidecar_path.with_name(
                f"{base_sidecar_path.stem}{suffix}{base_sidecar_path.suffix}"
            )
            if not bmp_candidate.exists() and not sidecar_candidate.exists():
                return bmp_candidate, sidecar_candidate
        raise RuntimeError("Could not find an available filename for image prep save output.")

    def _current_workflow_key(self) -> str:
        current_page = self.workflow_stack.currentWidget()
        for key, page in self.workflow_pages.items():
            if current_page is page:
                return key
        return "prep"

    def _set_workflow_page(self, key: str) -> None:
        page = self.workflow_pages.get(key)
        if page is None:
            return
        if self.workflow_stack.currentWidget() is not page:
            self.statusBar().clearMessage()
        self.workflow_stack.setCurrentWidget(page)
        self.workflow_buttons[key].setChecked(True)
        self._sync_preview_panel()

    def _on_workflow_stack_changed(self, _index: int) -> None:
        key = self._current_workflow_key()
        if key in self.workflow_buttons:
            self.workflow_buttons[key].setChecked(True)
        self._sync_preview_panel()

    def _sync_preview_panel(self) -> None:
        is_prep_page = self._current_workflow_key() == "prep"
        self.right_preview_stack.setCurrentWidget(
            self.prep_preview_panel if is_prep_page else self.machine_preview_panel
        )
        self.btn_zoom_out.setEnabled(not is_prep_page)
        self.btn_zoom_in.setEnabled(not is_prep_page)
        if is_prep_page:
            self._update_prep_preview_fit()
        self._update_preview_header()

    def _update_preview_header(self) -> None:
        key = self._current_workflow_key()
        if key == "prep":
            self.lbl_preview_title.setText("Image prep preview")
            source = self.image_prep_state.source_image_path
            artifacts = self.image_prep_state.artifacts
            if source is None:
                self.lbl_preview_status.setText("Load a JPG or sidecar to preview prep output.")
            elif artifacts is None:
                self.lbl_preview_status.setText(f"{source.name} is loaded. Preview is not available yet.")
            else:
                self.lbl_preview_status.setText(
                    f"{source.name} | "
                    f"{artifacts.image_width_mm:.1f}x{artifacts.image_height_mm:.1f} mm | "
                    f"{artifacts.image_width_px}x{artifacts.image_height_px} px"
                )
            return

        title_by_key = {
            "place": "Placement preview",
            "connect": "Run preview",
            "advanced": "Machine preview",
        }
        self.lbl_preview_title.setText(title_by_key.get(key, "Preview"))
        profile = self.settings.machine_profile
        if self.job_state.loaded_file is None:
            self.lbl_preview_status.setText(
                f"Canvas {profile.canvas_width_mm}x{profile.canvas_height_mm} mm. No job image selected."
            )
            return
        image_status = (
            f"{self.job_state.loaded_file.name} | "
            f"X{self.job_state.img_move_x_mm} Y{self.job_state.img_move_y_mm} mm | "
            f"{self.job_state.image_width_mm:.1f}x{self.job_state.image_height_mm:.1f} mm"
        )
        if self.job_state.lines:
            image_status += f" | {len(self.job_state.lines)} lines, {len(self.job_state.gcode)} commands"
        if self._stream_is_active():
            image_status += f" | sending command {self.job_state.current_send_index}"
        self.lbl_preview_status.setText(image_status)

    def _update_prep_preview_fit(self) -> None:
        if self._prep_preview_full_pixmap.isNull():
            return
        viewport_size = self.prep_preview_scroll.viewport().size()
        if viewport_size.width() <= 1 or viewport_size.height() <= 1:
            return
        fitted = self._prep_preview_full_pixmap.scaled(
            viewport_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.prep_preview_label.setPixmap(fitted)
        self.prep_preview_label.resize(fitted.size())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.prep_preview_scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._update_prep_preview_fit()
        return super().eventFilter(watched, event)

    def _sync_prep_controls_from_state(self) -> None:
        settings = self.image_prep_state.settings.sanitized()
        self.image_prep_state.settings = settings
        self._update_prep_dimension_spin_limits()
        self._prep_updating_controls = True
        self._prep_slider_syncing = True
        self.spin_prep_dpi.setValue(settings.dpi)
        width_mm = settings.target_width_mm if settings.target_width_mm > 0.0 else 400.0
        height_mm = settings.target_height_mm if settings.target_height_mm > 0.0 else 400.0
        self.spin_prep_width_mm.setValue(width_mm)
        self.spin_prep_height_mm.setValue(height_mm)
        self.spin_prep_contrast.setValue(settings.contrast_percent)
        self.slider_prep_contrast.setValue(
            max(self.slider_prep_contrast.minimum(), min(self.slider_prep_contrast.maximum(), settings.contrast_percent))
        )
        blur_slider_value = int(round(settings.blur_radius * 10.0))
        self.spin_prep_blur.setValue(settings.blur_radius)
        self.slider_prep_blur.setValue(
            max(self.slider_prep_blur.minimum(), min(self.slider_prep_blur.maximum(), blur_slider_value))
        )
        self.spin_prep_levels.setValue(settings.levels)
        self.combo_prep_strategy.setCurrentText(settings.strategy)
        self.checkbox_prep_auto_thresholds.setChecked(settings.auto_thresholds)
        self._sync_threshold_slider_rows(settings)
        self.checkbox_prep_halftone_preview.setChecked(settings.show_halftone_preview)
        self._prep_slider_syncing = False
        self._set_manual_threshold_controls_visible(not settings.auto_thresholds)
        self._prep_updating_controls = False
        self._update_prep_status_labels()
        self._render_prep_preview()

    def _read_prep_settings_from_controls(self) -> ImagePrepSettings:
        levels = self.spin_prep_levels.value()
        auto_thresholds = self.checkbox_prep_auto_thresholds.isChecked()
        if auto_thresholds:
            manual_thresholds = list(self.image_prep_state.settings.manual_thresholds)
        else:
            manual_thresholds = self._manual_threshold_values_from_sliders(levels)
        settings = ImagePrepSettings(
            dpi=self.spin_prep_dpi.value(),
            target_width_mm=self.spin_prep_width_mm.value(),
            target_height_mm=self.spin_prep_height_mm.value(),
            contrast_percent=self.spin_prep_contrast.value(),
            blur_radius=float(self.spin_prep_blur.value()),
            levels=levels,
            strategy=self.combo_prep_strategy.currentText().strip().lower(),
            auto_thresholds=auto_thresholds,
            manual_thresholds=manual_thresholds,
            show_halftone_preview=self.checkbox_prep_halftone_preview.isChecked(),
        )
        return settings.sanitized()

    def _set_manual_threshold_controls_visible(self, visible: bool) -> None:
        self.lbl_prep_manual_thresholds.setVisible(visible)
        self.prep_threshold_container.setVisible(visible)

    def _sync_threshold_slider_rows(self, settings: ImagePrepSettings) -> None:
        threshold_count = expected_threshold_count(settings.levels)
        manual_values = list(settings.manual_thresholds)
        if len(manual_values) < threshold_count:
            manual_values = settings.effective_thresholds()[:threshold_count]
        for index, row in enumerate(self._prep_threshold_rows):
            active = index < threshold_count
            row.setVisible(active)
            if not active:
                continue
            value = int(manual_values[index]) if index < len(manual_values) else 0
            self._prep_threshold_sliders[index].setValue(value)
            self._prep_threshold_spinboxes[index].setValue(value)

    def _manual_threshold_values_from_sliders(self, levels: int) -> list[int]:
        threshold_count = expected_threshold_count(levels)
        return [
            int(self._prep_threshold_sliders[index].value())
            for index in range(min(threshold_count, len(self._prep_threshold_sliders)))
        ]

    def _pil_image_to_pixmap(self, image: Image.Image) -> QPixmap:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        qimage = QImage.fromData(buffer.getvalue(), "PNG")
        return QPixmap.fromImage(qimage)

    def _update_prep_status_labels(self) -> None:
        source = self.image_prep_state.source_image_path
        if source is None:
            self.lbl_prep_source.setText("Source: none")
            self.lbl_prep_source.setToolTip("")
            self.lbl_prep_folder.setText("Folder: n/a")
            self.lbl_prep_folder.setToolTip("")
            self.lbl_prep_dimensions.setText("Image size: n/a")
            self.lbl_prep_effective_thresholds.setText("Effective thresholds: n/a")
            return
        self.lbl_prep_source.setText(f"Source: {source.name}")
        self.lbl_prep_source.setToolTip(str(source))
        self.lbl_prep_folder.setText(f"Folder: {self._compact_path(source.parent)}")
        self.lbl_prep_folder.setToolTip(str(source.parent))
        artifacts = self.image_prep_state.artifacts
        if artifacts is None:
            self.lbl_prep_dimensions.setText("Image size: n/a")
            self.lbl_prep_effective_thresholds.setText("Effective thresholds: n/a")
            return
        self.lbl_prep_dimensions.setText(
            "Image size: "
            f"{artifacts.image_width_px}x{artifacts.image_height_px}px | "
            f"{artifacts.image_width_mm:.1f}x{artifacts.image_height_mm:.1f}mm @ {self.image_prep_state.settings.dpi} DPI"
        )
        if artifacts.effective_thresholds:
            thresholds_text = ", ".join(str(value) for value in artifacts.effective_thresholds)
        else:
            thresholds_text = "none"
        self.lbl_prep_effective_thresholds.setText(f"Effective thresholds: {thresholds_text}")

    def _render_prep_preview(self) -> None:
        artifacts = self.image_prep_state.artifacts
        if artifacts is None:
            self._prep_preview_full_pixmap = QPixmap()
            self.prep_preview_label.setPixmap(QPixmap())
            self.prep_preview_label.setText("Load a JPG to begin.")
            return

        preview_image = (
            artifacts.halftone_preview_image
            if self.image_prep_state.settings.show_halftone_preview
            else artifacts.tonal_preview_image
        )
        pixmap = self._pil_image_to_pixmap(preview_image)
        if pixmap.isNull():
            self._prep_preview_full_pixmap = QPixmap()
            self.prep_preview_label.setPixmap(QPixmap())
            self.prep_preview_label.setText("Preview unavailable.")
            return
        self._prep_preview_full_pixmap = pixmap
        self.prep_preview_label.setText("")
        self._update_prep_preview_fit()

    def _recompute_prep_artifacts(self, *, mark_dirty: bool) -> bool:
        source = self.image_prep_state.source_image_path
        if source is None:
            return False

        settings, clamped = self._sanitize_prep_settings_for_machine(self._read_prep_settings_from_controls())
        if clamped:
            self._show_toast("Image dimensions were clamped to robot limits.")
        try:
            settings, artifacts = process_image_for_prep(
                image_path=source,
                settings=settings,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Image prep", str(exc))
            return False

        self.image_prep_state.settings = settings
        self.image_prep_state.artifacts = artifacts
        if self.image_prep_state.export_bmp_path is None:
            self.image_prep_state.export_bmp_path = processed_bmp_path_for_image(source)
        if self.image_prep_state.sidecar_path is None:
            self.image_prep_state.sidecar_path = sidecar_path_for_image(source)
        if mark_dirty:
            self.image_prep_state.dirty = True
        self._sync_prep_controls_from_state()
        return True

    def _load_prep_source_image(
        self,
        image_path: Path,
        *,
        settings: ImagePrepSettings | None = None,
        sidecar_path: Path | None = None,
        export_bmp_path: Path | None = None,
        mark_dirty: bool,
    ) -> bool:
        if not is_supported_source_image(image_path):
            QMessageBox.information(self, "Not supported", "Image Prep currently supports JPG/JPEG only.")
            return False
        if not image_path.exists():
            QMessageBox.warning(self, "Missing image", f"Source image does not exist:\n{image_path}")
            return False

        effective_settings = (settings or self.image_prep_state.settings).sanitized()
        if effective_settings.target_width_mm <= 0.0 or effective_settings.target_height_mm <= 0.0:
            default_width_mm, default_height_mm = self._default_target_dimensions_for_source(image_path)
            effective_settings.target_width_mm = default_width_mm
            effective_settings.target_height_mm = default_height_mm
        effective_settings, clamped = self._sanitize_prep_settings_for_machine(effective_settings)
        if clamped:
            self._show_toast("Image dimensions were clamped to robot limits.")

        self.image_prep_state.source_image_path = image_path
        self.image_prep_state.settings = effective_settings
        self.image_prep_state.sidecar_path = sidecar_path or sidecar_path_for_image(image_path)
        self.image_prep_state.export_bmp_path = export_bmp_path or processed_bmp_path_for_image(image_path)
        self.image_prep_state.linked_to_control = False
        self.image_prep_state.artifacts = None
        self.image_prep_state.dirty = bool(mark_dirty)

        self._sync_prep_controls_from_state()
        if not self._recompute_prep_artifacts(mark_dirty=mark_dirty):
            return False
        return True

    def _save_prep_bmp(self, *, show_toast: bool = True) -> Path | None:
        source = self.image_prep_state.source_image_path
        if source is None:
            QMessageBox.information(self, "No image", "Load a JPG image first.")
            return None
        if self.image_prep_state.artifacts is None:
            if not self._recompute_prep_artifacts(mark_dirty=False):
                return None
        artifacts = self.image_prep_state.artifacts
        if artifacts is None:
            return None

        output_path = self.image_prep_state.export_bmp_path or processed_bmp_path_for_image(source)
        save_processed_bmp(
            output_path=output_path,
            image=artifacts.export_bmp_image,
            dpi=self.image_prep_state.settings.dpi,
        )
        self.image_prep_state.export_bmp_path = output_path
        self.image_prep_state.dirty = False
        if self.image_prep_state.linked_to_control and self.job_state.loaded_file is not None:
            try:
                loaded = self.job_state.loaded_file.resolve()
                expected = output_path.resolve()
            except OSError:
                loaded = self.job_state.loaded_file
                expected = output_path
            if loaded == expected:
                metadata = self.converter.inspect_image(
                    output_path,
                    dpi_override=self.image_prep_state.settings.dpi,
                )
                self.job_state.dpi_override = self.image_prep_state.settings.dpi
                self.job_state.image_width_mm = metadata.image_width_mm
                self.job_state.image_height_mm = metadata.image_height_mm
                self.job_state.image_dpi = metadata.dpi_x
                self._render_image_preview()
            elif self.image_prep_state.linked_to_control:
                metadata = self.converter.inspect_image(
                    output_path,
                    dpi_override=self.image_prep_state.settings.dpi,
                )
                self.job_state.loaded_file = output_path
                self.job_state.file_type = "bmp"
                self.job_state.dpi_override = self.image_prep_state.settings.dpi
                self.job_state.image_width_mm = metadata.image_width_mm
                self.job_state.image_height_mm = metadata.image_height_mm
                self.job_state.image_dpi = metadata.dpi_x
                self._render_image_preview()
        self._append_log(f"Saved processed BMP: {output_path}")
        if show_toast:
            self._show_toast(f"Saved BMP: {output_path.name}")
        return output_path

    def _save_prep_sidecar(self) -> Path | None:
        source = self.image_prep_state.source_image_path
        if source is None:
            QMessageBox.information(self, "No image", "Load a JPG image first.")
            return None
        if self.image_prep_state.artifacts is None:
            if not self._recompute_prep_artifacts(mark_dirty=False):
                return None
        artifacts = self.image_prep_state.artifacts
        if artifacts is None:
            return None
        sidecar_path = self.image_prep_state.sidecar_path or sidecar_path_for_image(source)
        write_sidecar(
            sidecar_path=sidecar_path,
            source_image_path=source,
            settings=self.image_prep_state.settings,
            effective_thresholds=artifacts.effective_thresholds,
            export_bmp_path=self.image_prep_state.export_bmp_path,
        )
        self.image_prep_state.sidecar_path = sidecar_path
        self._append_log(f"Saved image prep sidecar: {sidecar_path}")
        return sidecar_path

    def _refresh_linked_prep_bmp_for_slice(self) -> bool:
        if not self.image_prep_state.linked_to_control or not self.image_prep_state.dirty:
            return True
        if self.image_prep_state.source_image_path is None:
            return True
        if self.job_state.loaded_file is None:
            return True

        export_path = self.image_prep_state.export_bmp_path
        if export_path is None:
            return True

        try:
            loaded = self.job_state.loaded_file.resolve()
            expected = export_path.resolve()
        except OSError:
            loaded = self.job_state.loaded_file
            expected = export_path
        if loaded != expected:
            return True

        if self.image_prep_state.artifacts is None:
            if not self._recompute_prep_artifacts(mark_dirty=False):
                return False
        if self.image_prep_state.artifacts is None:
            return False

        save_processed_bmp(
            output_path=export_path,
            image=self.image_prep_state.artifacts.export_bmp_image,
            dpi=self.image_prep_state.settings.dpi,
        )
        metadata = self.converter.inspect_image(export_path, dpi_override=self.image_prep_state.settings.dpi)
        self.job_state.loaded_file = export_path
        self.job_state.file_type = "bmp"
        self.job_state.dpi_override = self.image_prep_state.settings.dpi
        self.job_state.image_width_mm = metadata.image_width_mm
        self.job_state.image_height_mm = metadata.image_height_mm
        self.job_state.image_dpi = metadata.dpi_x
        self.image_prep_state.dirty = False
        self._render_image_preview()
        self._append_log("Image Prep changes detected; refreshed processed BMP before slicing.")
        return True

    def _on_prep_load_jpg(self) -> None:
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select JPG image",
            self._dialog_start_dir(),
            "JPEG files (*.jpg *.jpeg);;All files (*.*)",
        )
        if not selected_file:
            return
        self._remember_open_dir(selected_file)
        image_path = Path(selected_file)
        if self._load_prep_source_image(
            image_path,
            settings=ImagePrepSettings(dpi=self.DEFAULT_BMP_DPI),
            mark_dirty=True,
        ):
            self._append_log(f"Loaded image prep source: {image_path.name}")
        self._update_ui_state()

    def _on_prep_open_source(self) -> None:
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            "Open image prep source",
            self._dialog_start_dir(),
            "JPG or sidecar (*.jpg *.jpeg *.plottrbot-edit.json *.json);;"
            "JPEG files (*.jpg *.jpeg);;"
            "Warhol Slicer sidecar (*.plottrbot-edit.json);;"
            "JSON files (*.json);;"
            "All files (*.*)",
        )
        if not selected_file:
            return
        self._remember_open_dir(selected_file)
        source_path = Path(selected_file)
        if is_supported_source_image(source_path):
            if self._load_prep_source_image(
                source_path,
                settings=ImagePrepSettings(dpi=self.DEFAULT_BMP_DPI),
                mark_dirty=True,
            ):
                self._append_log(f"Loaded image prep source: {source_path.name}")
            self._update_ui_state()
            return
        if source_path.suffix.lower() == ".json":
            self._load_prep_sidecar_path(source_path)
            self._update_ui_state()
            return
        QMessageBox.information(self, "Not supported", "Open a JPG/JPEG image or a Warhol Slicer sidecar JSON.")

    def _on_prep_load_sidecar(self) -> None:
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select image prep sidecar",
            self._dialog_start_dir(),
            "Warhol Slicer sidecar (*.plottrbot-edit.json);;JSON files (*.json);;All files (*.*)",
        )
        if not selected_file:
            return
        self._remember_open_dir(selected_file)
        self._load_prep_sidecar_path(Path(selected_file))
        self._update_ui_state()

    def _load_prep_sidecar_path(self, sidecar_path: Path) -> None:
        try:
            source_path, settings, export_bmp_path = read_sidecar(sidecar_path)
        except Exception as exc:
            QMessageBox.warning(self, "Sidecar", f"Could not load sidecar:\n{exc}")
            return

        if self._load_prep_source_image(
            source_path,
            settings=settings,
            sidecar_path=sidecar_path,
            export_bmp_path=export_bmp_path,
            mark_dirty=False,
        ):
            self.image_prep_state.dirty = False
            self._append_log(f"Loaded image prep sidecar: {sidecar_path.name}")

    def _on_prep_save_outputs(self) -> None:
        if self.image_prep_state.source_image_path is None:
            QMessageBox.information(self, "No image", "Load a JPG image first.")
            return
        if not self._recompute_prep_artifacts(mark_dirty=False):
            return
        source = self.image_prep_state.source_image_path
        if source is None:
            return
        base_bmp_path = processed_bmp_path_for_image(source)
        base_sidecar_path = sidecar_path_for_image(source)
        save_bmp_path, save_sidecar_path = self._next_non_overwriting_save_paths(base_bmp_path, base_sidecar_path)
        self.image_prep_state.export_bmp_path = save_bmp_path
        self.image_prep_state.sidecar_path = save_sidecar_path
        bmp_path = self._save_prep_bmp(show_toast=False)
        sidecar_path = self._save_prep_sidecar()
        if bmp_path is None or sidecar_path is None:
            return
        self._show_toast(f"Saved {bmp_path.name} and {sidecar_path.name}")
        self._update_ui_state()

    def _on_prep_reset_defaults(self) -> None:
        source = self.image_prep_state.source_image_path
        default_settings = ImagePrepSettings(
            dpi=self.DEFAULT_BMP_DPI,
            contrast_percent=0,
            blur_radius=0.0,
            levels=4,
            strategy="banded",
            auto_thresholds=True,
            manual_thresholds=[],
            show_halftone_preview=False,
        )
        if source is not None:
            default_width_mm, default_height_mm = self._default_target_dimensions_for_source(source)
            default_settings.target_width_mm = default_width_mm
            default_settings.target_height_mm = default_height_mm
        else:
            default_settings.target_width_mm = 400.0
            default_settings.target_height_mm = 400.0

        self._prep_updating_controls = True
        self.checkbox_prep_lock_aspect.setChecked(True)
        self._prep_updating_controls = False
        self.image_prep_state.settings = default_settings.sanitized()
        self._sync_prep_controls_from_state()
        if source is not None:
            self._recompute_prep_artifacts(mark_dirty=True)
        self._show_toast("Image prep settings reset to defaults.")
        self._update_ui_state()

    def _on_prep_apply_to_control(self) -> None:
        if self.image_prep_state.source_image_path is None:
            QMessageBox.information(self, "No image", "Load a JPG image first.")
            return
        if not self._recompute_prep_artifacts(mark_dirty=False):
            return
        output_path = self._save_prep_bmp()
        if output_path is None:
            return
        self._load_bmp(
            output_path,
            dpi_override=self.image_prep_state.settings.dpi,
            linked_from_prep=True,
        )
        self._set_workflow_page("place")
        self._append_log("Applied processed image to job.")

    def _on_prep_dimension_changed(self, *_args: object) -> None:
        if self._prep_updating_controls:
            return
        if not self.checkbox_prep_lock_aspect.isChecked():
            self._on_prep_settings_changed()
            return

        sender = self.sender()
        if sender not in {self.spin_prep_width_mm, self.spin_prep_height_mm}:
            self._on_prep_settings_changed()
            return

        current_width = self.spin_prep_width_mm.value()
        current_height = self.spin_prep_height_mm.value()
        if current_width <= 0.0 or current_height <= 0.0:
            self._on_prep_settings_changed()
            return

        aspect_ratio: float | None = None
        if (
            self.image_prep_state.settings.target_width_mm > 0.0
            and self.image_prep_state.settings.target_height_mm > 0.0
        ):
            aspect_ratio = (
                self.image_prep_state.settings.target_width_mm
                / self.image_prep_state.settings.target_height_mm
            )
        elif self.image_prep_state.artifacts is not None and self.image_prep_state.artifacts.image_height_mm > 0.0:
            aspect_ratio = (
                self.image_prep_state.artifacts.image_width_mm
                / self.image_prep_state.artifacts.image_height_mm
            )

        if aspect_ratio is None or aspect_ratio <= 0.0:
            self._on_prep_settings_changed()
            return

        self._prep_updating_controls = True
        if sender is self.spin_prep_width_mm:
            new_height = max(1.0, current_width / aspect_ratio)
            self.spin_prep_height_mm.setValue(new_height)
        else:
            new_width = max(1.0, current_height * aspect_ratio)
            self.spin_prep_width_mm.setValue(new_width)
        self._prep_updating_controls = False
        self._on_prep_settings_changed()

    def _schedule_prep_recompute(self, *, delay_ms: int = 80) -> None:
        if self._prep_updating_controls:
            return
        if self.image_prep_state.source_image_path is None:
            self._on_prep_settings_changed()
            return
        self._prep_recompute_timer.start(max(10, int(delay_ms)))

    def _flush_prep_recompute(self) -> None:
        if self._prep_recompute_timer.isActive():
            self._prep_recompute_timer.stop()
        self._on_prep_settings_changed()

    def _on_prep_contrast_slider_changed(self, value: int) -> None:
        if self._prep_updating_controls:
            return
        if self._prep_slider_syncing:
            return
        self._prep_slider_syncing = True
        self.spin_prep_contrast.setValue(int(value))
        self._prep_slider_syncing = False
        self._schedule_prep_recompute(delay_ms=60)

    def _on_prep_contrast_spin_changed(self, value: int) -> None:
        if self._prep_updating_controls:
            return
        if not self._prep_slider_syncing:
            slider_value = max(self.slider_prep_contrast.minimum(), min(self.slider_prep_contrast.maximum(), int(value)))
            self._prep_slider_syncing = True
            self.slider_prep_contrast.setValue(slider_value)
            self._prep_slider_syncing = False
        self._schedule_prep_recompute(delay_ms=60)

    def _on_prep_blur_slider_changed(self, value: int) -> None:
        if self._prep_updating_controls:
            return
        if self._prep_slider_syncing:
            return
        self._prep_slider_syncing = True
        self.spin_prep_blur.setValue(float(value) / 10.0)
        self._prep_slider_syncing = False
        self._schedule_prep_recompute(delay_ms=60)

    def _on_prep_blur_spin_changed(self, value: float) -> None:
        if self._prep_updating_controls:
            return
        if not self._prep_slider_syncing:
            slider_value = int(round(float(value) * 10.0))
            slider_value = max(self.slider_prep_blur.minimum(), min(self.slider_prep_blur.maximum(), slider_value))
            self._prep_slider_syncing = True
            self.slider_prep_blur.setValue(slider_value)
            self._prep_slider_syncing = False
        self._schedule_prep_recompute(delay_ms=60)

    def _on_prep_levels_changed(self, *_args: object) -> None:
        if self._prep_updating_controls:
            return
        settings = self._read_prep_settings_from_controls()
        self._prep_updating_controls = True
        self._prep_slider_syncing = True
        self._sync_threshold_slider_rows(settings)
        self._prep_slider_syncing = False
        self._prep_updating_controls = False
        self._on_prep_settings_changed()

    def _on_prep_threshold_slider_changed(self, threshold_index: int) -> None:
        if 0 <= threshold_index < len(self._prep_threshold_sliders):
            value = int(self._prep_threshold_sliders[threshold_index].value())
            if self._prep_slider_syncing:
                return
            if threshold_index < len(self._prep_threshold_spinboxes):
                self._prep_slider_syncing = True
                self._prep_threshold_spinboxes[threshold_index].setValue(value)
                self._prep_slider_syncing = False
        if self._prep_updating_controls:
            return
        if self.checkbox_prep_auto_thresholds.isChecked():
            return
        self._schedule_prep_recompute(delay_ms=80)

    def _on_prep_threshold_spin_changed(self, threshold_index: int) -> None:
        if threshold_index < 0 or threshold_index >= len(self._prep_threshold_spinboxes):
            return
        if not self._prep_slider_syncing and threshold_index < len(self._prep_threshold_sliders):
            slider_value = int(self._prep_threshold_spinboxes[threshold_index].value())
            self._prep_slider_syncing = True
            self._prep_threshold_sliders[threshold_index].setValue(slider_value)
            self._prep_slider_syncing = False
        if self._prep_updating_controls:
            return
        if self.checkbox_prep_auto_thresholds.isChecked():
            return
        self._schedule_prep_recompute(delay_ms=80)

    def _on_prep_settings_changed(self, *_args: object) -> None:
        if self._prep_updating_controls:
            return
        if self._prep_recompute_timer.isActive():
            self._prep_recompute_timer.stop()
        if self.image_prep_state.source_image_path is None:
            self.image_prep_state.settings = self._read_prep_settings_from_controls()
            self._update_prep_status_labels()
            self._update_ui_state()
            return
        self._recompute_prep_artifacts(mark_dirty=True)
        self._update_ui_state()

    def _on_prep_auto_thresholds_toggled(self, *_args: object) -> None:
        if self._prep_updating_controls:
            return
        self._set_manual_threshold_controls_visible(not self.checkbox_prep_auto_thresholds.isChecked())
        self._on_prep_settings_changed()

    def _on_prep_preview_toggle_changed(self, checked: bool) -> None:
        if self._prep_updating_controls:
            return
        self.image_prep_state.settings.show_halftone_preview = checked
        self._render_prep_preview()

    def _active_draw_prep_context(self) -> dict[str, object] | None:
        if not self.image_prep_state.linked_to_control:
            return None
        if self.image_prep_state.source_image_path is None:
            return None
        if self.job_state.loaded_file is None:
            return None
        export_path = self.image_prep_state.export_bmp_path
        if export_path is None:
            return None
        try:
            loaded = self.job_state.loaded_file.resolve()
            expected = export_path.resolve()
        except OSError:
            loaded = self.job_state.loaded_file
            expected = export_path
        if loaded != expected:
            return None

        payload: dict[str, object] = {
            "source_image_path": str(self.image_prep_state.source_image_path),
            "settings": self.image_prep_state.settings.to_dict(),
        }
        if self.image_prep_state.sidecar_path is not None:
            payload["sidecar_path"] = str(self.image_prep_state.sidecar_path)
        if self.image_prep_state.artifacts is not None:
            payload["effective_thresholds"] = list(self.image_prep_state.artifacts.effective_thresholds)
        return payload

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

    def _current_end_gcode_lines(self) -> list[str]:
        return [line.strip() for line in self.txt_end_gcode.toPlainText().splitlines() if line.strip()]

    def _resolve_line_index_for_command(self, command_index: int) -> int | None:
        mapping = self.job_state.command_to_line_index
        if not mapping or command_index < 0:
            return None
        safe_index = min(command_index, len(mapping) - 1)
        for idx in range(safe_index, -1, -1):
            line_index = mapping[idx]
            if line_index >= 0:
                return line_index
        return None

    def _count_lines_sent(self, command_index: int) -> int:
        line_command_indices = self.job_state.line_to_command_index
        if not line_command_indices:
            return 0
        return bisect.bisect_left(line_command_indices, command_index)

    def _update_resume_line_label(self) -> None:
        max_line = max(0, len(self.job_state.lines) - 1)
        current_line = max(0, min(self.slider_cmd_count.value(), max_line))
        self.lbl_resume_line.setText(f"Selected line {current_line} / {max_line}")

    def _update_draw_session_progress(self, current_command_index: int, *, force_flush: bool = False) -> None:
        start_index = self.streamer.state.start_index
        lines_sent_total = self._count_lines_sent(current_command_index)
        lines_sent_before_start = self._count_lines_sent(start_index)
        current_line_index = self._resolve_line_index_for_command(max(current_command_index - 1, 0))
        self.draw_session_logger.update_progress(
            current_command_index=current_command_index,
            current_line_index=current_line_index,
            commands_sent_total=current_command_index,
            commands_sent_this_run=max(current_command_index - start_index, 0),
            lines_sent_total=lines_sent_total,
            lines_sent_this_run=max(lines_sent_total - lines_sent_before_start, 0),
            force_flush=force_flush,
        )

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

    def _on_select_image(self) -> None:
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select BMP image",
            self._dialog_start_dir(),
            "Bitmap files (*.bmp);;All files (*.*)",
        )
        if not selected_file:
            return
        self._remember_open_dir(selected_file)
        self._load_bmp(Path(selected_file))

    def _load_bmp(
        self,
        image_path: Path,
        *,
        dpi_override: int | None = None,
        linked_from_prep: bool = False,
    ) -> None:
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
        self.job_state.dpi_override = (
            dpi_override if dpi_override is not None and dpi_override > 0 else self.DEFAULT_BMP_DPI
        )

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
        self.image_prep_state.linked_to_control = linked_from_prep
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
        self.image_prep_state.linked_to_control = False
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
        if not self._refresh_linked_prep_bmp_for_slice():
            return

        end_gcode_lines = self._current_end_gcode_lines()
        if not end_gcode_lines:
            end_gcode_lines = default_end_gcode_lines(self.settings.machine_profile)

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
        if self._stream_is_active() and self.streamer.state.status != SendStatus.PAUSED:
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
        if self.job_state.loaded_file is not None:
            try:
                metadata = self.converter.inspect_image(
                    self.job_state.loaded_file,
                    dpi_override=self.job_state.dpi_override,
                )
                draw_log_path = self.draw_session_logger.start_session(
                    image_path=self.job_state.loaded_file,
                    image_width_px=metadata.image_width_px,
                    image_height_px=metadata.image_height_px,
                    image_width_mm=metadata.image_width_mm,
                    image_height_mm=metadata.image_height_mm,
                    dpi=metadata.dpi_x,
                    move_x_mm=self.job_state.img_move_x_mm,
                    move_y_mm=self.job_state.img_move_y_mm,
                    gcode_commands=self.job_state.gcode,
                    end_gcode_lines=self._current_end_gcode_lines(),
                    start_command_index=start_index,
                    command_to_line_index=self.job_state.command_to_line_index,
                    line_to_command_index=self.job_state.line_to_command_index,
                    machine_profile=asdict(self.settings.machine_profile),
                    serial_port=self.transport.port_name,
                    image_prep=self._active_draw_prep_context(),
                )
                self._append_log(f"Draw session log: {draw_log_path}")
            except Exception as exc:
                self._append_log(f"Draw session logging unavailable: {exc}")
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
            self._update_draw_session_progress(self.job_state.current_send_index, force_flush=True)
            self.draw_session_logger.add_event(
                "paused",
                details={
                    "current_command_index": self.job_state.current_send_index,
                    "current_line_index": self._resolve_line_index_for_command(
                        max(self.job_state.current_send_index - 1, 0)
                    ),
                    "lines_sent_total": self._count_lines_sent(self.job_state.current_send_index),
                },
            )
        elif state == SendStatus.PAUSED:
            self.streamer.resume()
            self.job_state.paused = False
            self.btn_pause_drawing.setText("Pause drawing")
            self.draw_session_logger.add_event(
                "resumed",
                details={
                    "current_command_index": self.job_state.current_send_index,
                    "current_line_index": self._resolve_line_index_for_command(
                        max(self.job_state.current_send_index - 1, 0)
                    ),
                    "lines_sent_total": self._count_lines_sent(self.job_state.current_send_index),
                },
            )
        self._sync_sleep_inhibitor()

    def _on_stop_drawing(self) -> None:
        if not self._stream_is_active():
            return
        self._pending_stop_recovery = self.checkbox_stop_recovery.isChecked()
        self.streamer.stop()
        self.draw_session_logger.add_event(
            "stop_requested",
            details={
                "current_command_index": self.job_state.current_send_index,
                "current_line_index": self._resolve_line_index_for_command(
                    max(self.job_state.current_send_index - 1, 0)
                ),
                "lines_sent_total": self._count_lines_sent(self.job_state.current_send_index),
            },
        )
        self._append_log("Stop requested")

    def _on_start_from_command_number(self) -> None:
        if not self.job_state.lines:
            QMessageBox.information(self, "No slice", "Slice the image first.")
            return
        if self.streamer.state.status == SendStatus.RUNNING:
            QMessageBox.information(self, "Drawing", "Pause drawing before choosing a different start line.")
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
        if self.streamer.state.status == SendStatus.PAUSED:
            self._restart_paused_stream_from_line(line_number, start_index)
            return
        self.job_state.current_send_index = start_index
        self._on_send_image()

    def _restart_paused_stream_from_line(self, line_number: int, start_index: int) -> None:
        self.draw_session_logger.add_event(
            "restart_from_line_requested",
            details={
                "selected_line_index": line_number,
                "start_command_index": start_index,
                "paused_command_index": self.job_state.current_send_index,
            },
        )
        self._append_log(
            f"Restarting paused draw from selected line {line_number} "
            f"(command {start_index})."
        )
        self._pending_line_restart = (line_number, start_index)
        self.streamer.reset(emit_stopped=False)
        self.job_state.paused = False
        self._is_drawing = False
        self.btn_pause_drawing.setText("Pause drawing")
        self._sync_sleep_inhibitor()
        self._update_ui_state()
        QTimer.singleShot(0, self._start_pending_line_restart)

    def _start_pending_line_restart(self) -> None:
        pending = self._pending_line_restart
        self._pending_line_restart = None
        if pending is None:
            return
        line_number, start_index = pending
        self.job_state.current_send_index = start_index
        self.slider_cmd_count.setValue(line_number)
        self.preview_canvas.set_selected_line(line_number)
        self._on_send_image()

    def _on_slider_changed(self, value: int) -> None:
        self.job_state.selected_line_index = value
        self.txt_cmd_start.setText(str(value))
        self.preview_canvas.set_selected_line(value)
        self._update_resume_line_label()

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

        if not self.transport.is_connected:
            QMessageBox.information(self, "USB not connected", "Connect USB in Run before tracing the footprint.")
            return

        self.preview_canvas.set_bbox_overlay(bbox, visible=True)
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

    def _on_move_to_bbox_point(self, point_label: str, x_ratio: float, y_ratio: float) -> None:
        bbox = self.job_state.bounding_box
        if bbox is None:
            QMessageBox.information(self, "No slice", "Slice the image first.")
            return
        if not self.transport.is_connected:
            QMessageBox.information(self, "USB not connected", "Connect USB in Run before moving to footprint points.")
            return

        target_x = bbox.min_x + ((bbox.max_x - bbox.min_x) * x_ratio)
        target_y = bbox.min_y + ((bbox.max_y - bbox.min_y) * y_ratio)
        if not self._is_point_within_bounds(target_x, target_y):
            QMessageBox.warning(
                self,
                "Bounds check failed",
                f"Target point is out of machine bounds at X{target_x:.3f} Y{target_y:.3f}.",
            )
            return

        pen_position = 0 if self.checkbox_bounding_pen.isChecked() else 1
        self._send_manual_commands_async(
            [
                f"G1 X{target_x:.3f} Y{target_y:.3f} Z{pen_position}",
            ],
            f"Move to bounding-box point ({point_label})",
        )

    def _on_save_dimensions(self) -> None:
        width = self._parse_int(self.txt_robot_width, "Robot width")
        height = self._parse_int(self.txt_robot_height, "Robot height")
        if width is None or height is None:
            return
        if width <= 0 or height <= 0:
            QMessageBox.warning(self, "Invalid dimensions", "Dimensions must be positive.")
            return

        profile = self.settings.machine_profile
        previous_profile = type(profile)(
            canvas_width_mm=profile.canvas_width_mm,
            canvas_height_mm=profile.canvas_height_mm,
            home_x_mm=profile.home_x_mm,
            home_y_mm=profile.home_y_mm,
            baudrate=profile.baudrate,
            ack_token=profile.ack_token,
            ack_timeout_seconds=profile.ack_timeout_seconds,
        )
        should_refresh_end_gcode = uses_builtin_end_gcode(self._current_end_gcode_lines(), previous_profile)
        profile.canvas_width_mm = width
        profile.canvas_height_mm = height
        profile.home_x_mm = width / 2.0
        self.converter.machine_profile = profile
        self.preview_canvas.set_machine_profile(profile)
        self._update_prep_dimension_spin_limits()
        prep_settings, prep_clamped = self._sanitize_prep_settings_for_machine(self.image_prep_state.settings)
        self.image_prep_state.settings = prep_settings
        if prep_clamped and self.image_prep_state.source_image_path is not None:
            self._recompute_prep_artifacts(mark_dirty=True)
        if should_refresh_end_gcode:
            self.settings.end_gcode_lines = default_end_gcode_lines(profile)
            self.txt_end_gcode.setPlainText("\n".join(self.settings.end_gcode_lines) + "\n")
        self.settings_store.save(self.settings)
        QMessageBox.information(self, "Saved", "Machine settings saved.")

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
            self._update_draw_session_progress(state_obj.current_index, force_flush=True)
            lines_sent_total = self._count_lines_sent(state_obj.current_index)
            lines_sent_before_start = self._count_lines_sent(state_obj.start_index)
            current_line_index = self._resolve_line_index_for_command(
                max(state_obj.current_index - 1, 0)
            )
            if state_obj.status == SendStatus.COMPLETED:
                self.draw_session_logger.finalize(
                    status="completed",
                    current_command_index=state_obj.current_index,
                    current_line_index=current_line_index,
                    commands_sent_total=state_obj.current_index,
                    commands_sent_this_run=max(state_obj.current_index - state_obj.start_index, 0),
                    lines_sent_total=lines_sent_total,
                    lines_sent_this_run=max(lines_sent_total - lines_sent_before_start, 0),
                )
            elif state_obj.status == SendStatus.STOPPED:
                self.draw_session_logger.finalize(
                    status="stopped",
                    current_command_index=state_obj.current_index,
                    current_line_index=current_line_index,
                    commands_sent_total=state_obj.current_index,
                    commands_sent_this_run=max(state_obj.current_index - state_obj.start_index, 0),
                    lines_sent_total=lines_sent_total,
                    lines_sent_this_run=max(lines_sent_total - lines_sent_before_start, 0),
                )
            else:
                self.draw_session_logger.finalize(
                    status="error",
                    current_command_index=state_obj.current_index,
                    current_line_index=current_line_index,
                    commands_sent_total=state_obj.current_index,
                    commands_sent_this_run=max(state_obj.current_index - state_obj.start_index, 0),
                    lines_sent_total=lines_sent_total,
                    lines_sent_this_run=max(lines_sent_total - lines_sent_before_start, 0),
                    error=state_obj.last_error,
                )
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
        self._update_draw_session_progress(self.job_state.current_send_index)
        if 0 <= sent_command_index < len(self.job_state.command_to_line_index):
            line_index = self.job_state.command_to_line_index[sent_command_index]
            if line_index >= 0:
                self.slider_cmd_count.setValue(line_index)

    def _set_run_state_indicator(self, text: str, state: str) -> None:
        self.lbl_run_state.setText(text)
        self.lbl_run_state.setProperty("state", state)
        self.lbl_run_state.style().unpolish(self.lbl_run_state)
        self.lbl_run_state.style().polish(self.lbl_run_state)

    def _update_run_state_indicator(
        self,
        *,
        has_image: bool,
        is_sliced: bool,
        usb_connected: bool,
        stream_status: SendStatus,
    ) -> None:
        if stream_status == SendStatus.RUNNING:
            self._set_run_state_indicator("Streaming", "running")
            return
        if stream_status == SendStatus.PAUSED:
            self._set_run_state_indicator("Paused", "paused")
            return
        if not has_image:
            self._set_run_state_indicator("No job image", "blocked")
            return
        if not is_sliced:
            self._set_run_state_indicator("Slice needed", "blocked")
            return
        if not usb_connected:
            self._set_run_state_indicator("Ready to connect", "blocked")
            return
        self._set_run_state_indicator("Ready to send", "ready")

    def _update_ui_state(self) -> None:
        has_image = self.job_state.loaded_file is not None
        is_sliced = bool(self.job_state.lines)
        usb_connected = self.transport.is_connected
        stream_status = self.streamer.state.status
        stream_active = self._stream_is_active()
        stream_paused = stream_status == SendStatus.PAUSED
        interaction_locked = stream_active or self._manual_busy
        retained_available = self.job_state.retained_image is not None
        prep_has_source = self.image_prep_state.source_image_path is not None
        prep_has_artifacts = self.image_prep_state.artifacts is not None
        self.current_ui_state = derive_ui_state(
            has_image=has_image,
            is_sliced=is_sliced,
            usb_connected=usb_connected,
            is_drawing=self._is_drawing,
        )
        self._update_run_state_indicator(
            has_image=has_image,
            is_sliced=is_sliced,
            usb_connected=usb_connected,
            stream_status=stream_status,
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

        prep_controls_enabled = not interaction_locked
        self.btn_prep_load_jpg.setEnabled(prep_controls_enabled)
        self.btn_prep_load_sidecar.setEnabled(prep_controls_enabled)
        self.btn_prep_skip_to_place.setEnabled(prep_controls_enabled)
        self.btn_prep_save_outputs.setEnabled(prep_has_source and prep_controls_enabled)
        self.btn_prep_apply_to_control.setEnabled(prep_has_artifacts and prep_controls_enabled)
        self.btn_prep_reset_defaults.setEnabled(prep_controls_enabled)
        self.spin_prep_dpi.setEnabled(prep_has_source and prep_controls_enabled)
        self.spin_prep_width_mm.setEnabled(prep_has_source and prep_controls_enabled)
        self.spin_prep_height_mm.setEnabled(prep_has_source and prep_controls_enabled)
        self.checkbox_prep_lock_aspect.setEnabled(prep_has_source and prep_controls_enabled)
        self.slider_prep_contrast.setEnabled(prep_has_source and prep_controls_enabled)
        self.spin_prep_contrast.setEnabled(prep_has_source and prep_controls_enabled)
        self.slider_prep_blur.setEnabled(prep_has_source and prep_controls_enabled)
        self.spin_prep_blur.setEnabled(prep_has_source and prep_controls_enabled)
        self.spin_prep_levels.setEnabled(prep_has_source and prep_controls_enabled)
        self.combo_prep_strategy.setEnabled(prep_has_source and prep_controls_enabled)
        self.checkbox_prep_auto_thresholds.setEnabled(prep_has_source and prep_controls_enabled)
        manual_threshold_controls_enabled = (
            prep_has_source
            and prep_controls_enabled
            and (not self.checkbox_prep_auto_thresholds.isChecked())
        )
        self.lbl_prep_manual_thresholds.setEnabled(manual_threshold_controls_enabled)
        for index, slider in enumerate(self._prep_threshold_sliders):
            slider.setEnabled(manual_threshold_controls_enabled and self._prep_threshold_rows[index].isVisible())
        for index, spin in enumerate(self._prep_threshold_spinboxes):
            spin.setEnabled(manual_threshold_controls_enabled and self._prep_threshold_rows[index].isVisible())
        self.checkbox_prep_halftone_preview.setEnabled(prep_has_source and prep_controls_enabled)

        self.combo_port.setEnabled((not usb_connected) and not interaction_locked)
        self.btn_refresh_ports.setEnabled((not usb_connected) and not interaction_locked)
        self.btn_connect.setEnabled(not interaction_locked)
        line_selection_controls = is_sliced and (not stream_active or stream_paused)
        self.txt_cmd_start.setEnabled(line_selection_controls)
        self.slider_cmd_count.setEnabled(line_selection_controls)
        self.btn_slider_dec.setEnabled(line_selection_controls)
        self.btn_slider_inc.setEnabled(line_selection_controls)
        self._update_resume_line_label()

        connected_controls = usb_connected and not self._manual_busy and (not stream_active or stream_paused)
        self.txt_serial_cmd.setEnabled(connected_controls)
        self.btn_send_cmd.setEnabled(connected_controls)
        motor_power_controls = connected_controls and self.settings.motor_power_commands_enabled
        self.btn_enable_stepper.setEnabled(motor_power_controls)
        self.btn_disable_stepper.setEnabled(motor_power_controls)
        self.btn_pen_touch.setEnabled(connected_controls)
        self.btn_pen_away.setEnabled(connected_controls)
        self.btn_home.setEnabled(connected_controls)
        bbox_point_controls = connected_controls and self.job_state.bounding_box is not None
        for button in self.bbox_point_buttons.values():
            button.setEnabled(bbox_point_controls)

        can_trace_bbox = is_sliced and usb_connected and not stream_active and not self._manual_busy
        can_draw = is_sliced and usb_connected
        if not is_sliced:
            self.lbl_bbox_hint.setText("Slice image, then connect USB to use footprint tools.")
        elif usb_connected:
            self.lbl_bbox_hint.setText("Trace the footprint or move to a bounding-box point on the canvas.")
        else:
            self.lbl_bbox_hint.setText("Connect USB in Run to trace the footprint on the canvas.")
        self.btn_bounding_box.setEnabled(can_trace_bbox)
        self.checkbox_bounding_pen.setEnabled(can_trace_bbox)
        self.btn_send_img.setEnabled(can_draw and not stream_active and not self._manual_busy)
        self.btn_pause_drawing.setEnabled(can_draw and stream_active)
        self.btn_stop_drawing.setEnabled(can_draw and stream_active)
        self.btn_cmd_start.setEnabled(
            can_draw and line_selection_controls and not self._manual_busy
        )
        self._sync_preview_panel()

    def _sync_sleep_inhibitor(self) -> None:
        if self.transport.is_connected and self.streamer.state.status == SendStatus.RUNNING:
            self.sleep_inhibitor.start()
            return
        self.sleep_inhibitor.stop()

    def closeEvent(self, event: object) -> None:  # noqa: N802 (Qt API)
        if self._stream_is_active():
            current_index = self.job_state.current_send_index
            lines_sent_total = self._count_lines_sent(current_index)
            start_index = self.streamer.state.start_index
            lines_sent_before_start = self._count_lines_sent(start_index)
            self.draw_session_logger.finalize(
                status="closed_while_active",
                current_command_index=current_index,
                current_line_index=self._resolve_line_index_for_command(max(current_index - 1, 0)),
                commands_sent_total=current_index,
                commands_sent_this_run=max(current_index - start_index, 0),
                lines_sent_total=lines_sent_total,
                lines_sent_this_run=max(lines_sent_total - lines_sent_before_start, 0),
            )
        self.settings.window_width = self.width()
        self.settings.window_height = self.height()
        self.settings.end_gcode_lines = [
            line.strip() for line in self.txt_end_gcode.toPlainText().splitlines() if line.strip()
        ] or default_end_gcode_lines(self.settings.machine_profile)
        self.settings_store.save(self.settings)
        self.streamer.reset()
        self._wait_for_manual_worker()
        self.transport.disconnect()
        self.sleep_inhibitor.stop()
        super().closeEvent(event)  # type: ignore[arg-type]
