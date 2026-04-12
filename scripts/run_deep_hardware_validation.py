#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

# Keep the Qt workflow runnable on headless test hosts unless the user overrides it.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from plottrbot.config.settings import SettingsStore
from plottrbot.serial.program_streamer import SendStatus
from plottrbot.ui.main_window import MainWindow


def _create_test_bmp(path: Path) -> None:
    image = Image.new("RGB", (8, 8), color=(255, 255, 255))
    for y in range(1, 7):
        image.putpixel((1, y), (0, 0, 0))
    for x in range(2, 7):
        image.putpixel((x, 2), (0, 0, 0))
    for x in range(2, 6):
        image.putpixel((x, 5), (0, 0, 0))
    image.save(path, format="BMP", dpi=(50.0, 50.0))


def _pump_events(app: QApplication, seconds: float = 0.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def _wait_until(app: QApplication, predicate, timeout_seconds: float, label: str) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError(f"Timed out waiting for {label}.")


def _run_deep_ui_suite(port: str, bmp_path: Path) -> None:
    print("[deep-ui] start")
    app = QApplication.instance() or QApplication(sys.argv)
    settings_path = Path(tempfile.gettempdir()) / "plottrbot_deep_hardware_validation_config.json"
    window = MainWindow(settings_store=SettingsStore(path=settings_path))
    window.show()
    _pump_events(app, 0.1)

    try:
        print("[deep-ui] load bmp")
        window._load_bmp(bmp_path)
        _pump_events(app, 0.05)
        print(
            "[deep-ui] loaded "
            f"size_mm={window.job_state.image_width_mm:.3f}x{window.job_state.image_height_mm:.3f} "
            f"dpi={window.job_state.image_dpi:.2f}"
        )

        print("[deep-ui] dpi + placement workflow")
        window.txt_dpi.setText("50")
        window._on_update_dpi()
        window._on_center_or_top_left()
        window._on_center_or_top_left()
        window.txt_move_x.setText("724")
        window.txt_move_y.setText("238")
        window._on_move_image()
        window._on_hold_release_image()
        window._on_hold_release_image()
        print(
            f"[deep-ui] placement x={window.job_state.img_move_x_mm} "
            f"y={window.job_state.img_move_y_mm}"
        )

        print("[deep-ui] slice bmp")
        window._on_slice_image()
        _pump_events(app, 0.05)
        print(
            f"[deep-ui] slice lines={len(window.job_state.lines)} "
            f"commands={len(window.job_state.gcode)} bbox={window.job_state.bounding_box}"
        )

        print("[deep-ui] connect")
        window._refresh_ports()
        port_index = window.combo_port.findText(port)
        if port_index < 0:
            raise RuntimeError(f"Port '{port}' not found in the UI port list.")
        window.combo_port.setCurrentIndex(port_index)
        window._on_connect_toggle()
        _pump_events(app, 0.2)
        if not window.transport.is_connected:
            raise RuntimeError("UI transport failed to connect.")
        print("[deep-ui] connected")

        print("[deep-ui] low-level command sweep")
        command_sweep = [
            "G92 H",
            "G92 X730 Y240",
            "G1 Z1",
            "G1 Z0",
            "G1 X735 Y240",
            "G1 Y245",
            "G1 X740 Y245 Z1",
            "G01 X738 Y243",
            "G1 L1",
            "G1 R1",
            "M17",
            "M18",
            "G28",
        ]
        for command in command_sweep:
            ack = window.transport.send_command(command, timeout_seconds=20.0)
            if not ack.ok:
                raise RuntimeError(f"Command failed for '{command}': {ack.error}")
            print(f"[deep-ui] ack ok: {command} -> {ack.response}")

        print("[deep-ui] bounding box trace (pen up)")
        window.checkbox_bounding_pen.setChecked(False)
        window._on_bounding_box()
        _wait_until(app, lambda: window._manual_busy is False, 120.0, "bounding box pen-up")

        print("[deep-ui] bounding box trace (pen down)")
        window.checkbox_bounding_pen.setChecked(True)
        window._on_bounding_box()
        _wait_until(app, lambda: window._manual_busy is False, 120.0, "bounding box pen-down")

        print("[deep-ui] full stream with pause/resume")
        window.job_state.current_send_index = 0
        window._on_send_image()
        _wait_until(app, lambda: window.job_state.current_send_index >= 2, 120.0, "stream progress")
        window._on_pause_resume()
        _wait_until(app, lambda: window.streamer.state.status == SendStatus.PAUSED, 20.0, "pause state")
        paused_index = window.job_state.current_send_index
        print(f"[deep-ui] paused at command index {paused_index}")
        window._on_pause_resume()
        _wait_until(app, lambda: window.streamer.state.status == SendStatus.RUNNING, 20.0, "resume state")
        _wait_until(
            app,
            lambda: window.streamer.state.status == SendStatus.COMPLETED,
            max(180.0, float(len(window.job_state.gcode)) * 6.0),
            "full stream completion",
        )
        print("[deep-ui] full stream completed")

        if len(window.job_state.line_to_command_index) > 1:
            print("[deep-ui] restart from line number 1")
            window.txt_cmd_start.setText("1")
            window._on_start_from_command_number()
            _wait_until(
                app,
                lambda: window.streamer.state.status == SendStatus.COMPLETED,
                max(180.0, float(len(window.job_state.gcode)) * 6.0),
                "restart completion",
            )
            print("[deep-ui] restart stream completed")

        print("[deep-ui] stop recovery run")
        window.job_state.current_send_index = 0
        window.checkbox_stop_recovery.setChecked(True)
        window._on_send_image()
        _wait_until(
            app,
            lambda: window.job_state.current_send_index >= 2,
            120.0,
            "stream progress before stop",
        )
        window._on_stop_drawing()
        _wait_until(
            app,
            lambda: window.streamer.state.status in {SendStatus.STOPPED, SendStatus.ERROR},
            120.0,
            "stop terminal state",
        )
        _wait_until(app, lambda: window._manual_busy is False, 120.0, "stop recovery commands")
        print(f"[deep-ui] stop recovery finished with status={window.streamer.state.status.value}")

        print("[deep-ui] disconnect")
        window._on_connect_toggle()
        _pump_events(app, 0.1)
        if window.transport.is_connected:
            raise RuntimeError("UI transport is still connected after disconnect.")
        print("[deep-ui] disconnect ok")
        print("[deep-ui] done")
    finally:
        window.close()
        _pump_events(app, 0.1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deeper Plottrbot hardware validation workflow.")
    parser.add_argument("--port", default="COM3", help="Serial port for the Arduino Nano firmware under test")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="plottrbot-deep-hw-") as tmp_dir:
        bmp_path = Path(tmp_dir) / "deep_hardware_validation.bmp"
        _create_test_bmp(bmp_path)
        _run_deep_ui_suite(args.port, bmp_path)

    print("Deep hardware validation suite passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
