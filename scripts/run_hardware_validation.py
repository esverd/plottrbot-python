#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

from plottrbot.config.settings import SettingsStore
from plottrbot.core.bmp_converter import BmpConverter
from plottrbot.core.models import MachineProfile
from plottrbot.serial.nano_transport import NanoTransport
from plottrbot.serial.program_streamer import ProgramStreamer, SendStatus
from plottrbot.system.sleep_inhibitor import SleepInhibitor
from plottrbot.ui.main_window import MainWindow


def _create_test_bmp(path: Path) -> None:
    image = Image.new("RGB", (12, 10), color=(255, 255, 255))
    for y in range(1, 9):
        image.putpixel((2, y), (0, 0, 0))
    for x in range(4, 10):
        image.putpixel((x, 4), (0, 0, 0))
    image.save(path, format="BMP", dpi=(25.4, 25.4))


def _wait_for_terminal_state(streamer: ProgramStreamer, timeout_seconds: float) -> SendStatus:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = streamer.state.status
        if status in {SendStatus.COMPLETED, SendStatus.ERROR, SendStatus.STOPPED}:
            return status
        time.sleep(0.02)
    raise RuntimeError(f"Timed out waiting for terminal state. Last status: {streamer.state.status.value}")


def _run_backend_suite(port: str, bmp_path: Path, profile: MachineProfile) -> None:
    print("[backend] start")
    converter = BmpConverter(profile)
    result = converter.generate(
        image_path=bmp_path,
        img_move_x_mm=20,
        img_move_y_mm=20,
        dpi_override=25,
        black_threshold=70,
        start_gcode_lines=["G1 Z1"],
        end_gcode_lines=["G1 Z1", "G28"],
    )
    if not result.gcode:
        raise RuntimeError("Generated G-code is empty.")

    logs: list[str] = []
    transport = NanoTransport(profile, on_log=logs.append)
    inhibitor = SleepInhibitor(on_log=logs.append)
    saw_inhibitor_active = {"value": False}

    def on_state(state) -> None:
        if transport.is_connected and state.status == SendStatus.RUNNING:
            inhibitor.start()
        else:
            inhibitor.stop()
        if inhibitor.is_active:
            saw_inhibitor_active["value"] = True

    streamer = ProgramStreamer(
        transport,
        on_state=on_state,
        on_progress=lambda _idx, _total: None,
        on_log=logs.append,
    )

    transport.connect(port)
    print(f"[backend] connected={transport.is_connected} port={port}")

    manual_commands = ["M17", "M18", "G1 Z0", "G1 Z1", "G28", "G92 H"]
    for command in manual_commands:
        ack = transport.send_command(command)
        if not ack.ok:
            raise RuntimeError(f"Manual command failed for '{command}': {ack.error}")
    print(f"[backend] manual commands ok ({len(manual_commands)})")

    streamer.send(result.gcode, start_index=0)
    pause_deadline = time.monotonic() + 20.0
    while time.monotonic() < pause_deadline:
        if streamer.state.current_index >= min(8, max(1, len(result.gcode) // 3)):
            break
        time.sleep(0.02)
    streamer.pause()
    paused_index = streamer.state.current_index
    time.sleep(0.3)
    streamer.resume()
    timeout_seconds = max(120.0, float(len(result.gcode)) * 6.0)
    status = _wait_for_terminal_state(streamer, timeout_seconds=timeout_seconds)
    if status != SendStatus.COMPLETED:
        raise RuntimeError(f"Full stream failed: {streamer.state}")
    print(f"[backend] full image stream ok (pause index {paused_index}, commands {len(result.gcode)})")

    # Reset between runs to avoid thread-liveness race on immediate resend.
    streamer.reset()

    restart_index = result.line_to_command_index[min(2, len(result.line_to_command_index) - 1)]
    streamer.send(result.gcode, start_index=restart_index)
    status = _wait_for_terminal_state(streamer, timeout_seconds=timeout_seconds)
    if status != SendStatus.COMPLETED:
        raise RuntimeError(f"Restart stream failed: {streamer.state}")
    print(f"[backend] restart from index ok (start={restart_index})")

    bbox = result.bbox
    if bbox is None:
        raise RuntimeError("Bounding box missing from conversion result.")
    for pen_z in (1, 0):
        commands = [
            f"G1 X{bbox.min_x:.3f} Y{bbox.min_y:.3f}",
            f"G1 X{bbox.max_x:.3f} Y{bbox.min_y:.3f} Z{pen_z}",
            f"G1 X{bbox.max_x:.3f} Y{bbox.max_y:.3f}",
            f"G1 X{bbox.min_x:.3f} Y{bbox.max_y:.3f}",
            f"G1 X{bbox.min_x:.3f} Y{bbox.min_y:.3f}",
            "G1 Z1",
            "G28",
        ]
        for command in commands:
            ack = transport.send_command(command)
            if not ack.ok:
                raise RuntimeError(f"Bounding-box command failed for '{command}': {ack.error}")
    print("[backend] bounding-box traces ok (pen up/down)")

    inhibitor.stop()
    transport.disconnect()
    if transport.is_connected:
        raise RuntimeError("Transport still connected after disconnect.")
    if not saw_inhibitor_active["value"]:
        raise RuntimeError("Sleep inhibitor was never observed active during stream.")
    print("[backend] sleep inhibitor active during stream and released after")
    print("[backend] done")


def _process_qt_until(app: QApplication, predicate, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError("Timed out waiting for Qt condition.")


def _run_ui_suite(port: str, bmp_path: Path) -> None:
    print("[ui] start")
    app = QApplication.instance() or QApplication(sys.argv)
    settings_path = Path(tempfile.gettempdir()) / "plottrbot_ui_hardware_validation_config.json"
    window = MainWindow(settings_store=SettingsStore(path=settings_path))
    window.show()
    app.processEvents()

    try:
        window._load_bmp(bmp_path)
        window.btn_slice_img.click()
        app.processEvents()
        if not window.job_state.gcode:
            raise RuntimeError("UI slice produced empty G-code.")
        print(f"[ui] slice ok (commands={len(window.job_state.gcode)})")

        window._refresh_ports()
        app.processEvents()
        index = window.combo_port.findText(port)
        if index < 0:
            raise RuntimeError(f"Port '{port}' not available in UI port list.")
        window.combo_port.setCurrentIndex(index)
        window.btn_connect.click()
        app.processEvents()
        if not window.transport.is_connected:
            raise RuntimeError("UI connect failed.")
        print("[ui] connect ok")

        window.txt_serial_cmd.setText("G92 H")
        window.btn_send_cmd.click()
        _process_qt_until(app, lambda: window._manual_busy is False, timeout_seconds=10.0)
        print("[ui] manual command ok")

        window.btn_send_img.click()
        app.processEvents()
        if not window._is_drawing:
            raise RuntimeError("UI did not enter drawing state after send.")
        print("[ui] send started")

        _process_qt_until(app, lambda: window.job_state.current_send_index >= 1, timeout_seconds=30.0)
        window.btn_pause_drawing.click()
        app.processEvents()
        if window.streamer.state.status != SendStatus.PAUSED:
            raise RuntimeError("UI pause did not set PAUSED status.")
        window.btn_pause_drawing.click()
        app.processEvents()
        if window.streamer.state.status != SendStatus.RUNNING:
            raise RuntimeError("UI resume did not set RUNNING status.")
        print("[ui] pause/resume ok")

        _process_qt_until(
            app,
            lambda: window.streamer.state.status in {SendStatus.COMPLETED, SendStatus.ERROR, SendStatus.STOPPED},
            timeout_seconds=max(120.0, float(len(window.job_state.gcode)) * 6.0),
        )
        final_state = window.streamer.state
        if final_state.status != SendStatus.COMPLETED:
            raise RuntimeError(f"UI stream failed: {final_state}")
        print("[ui] full image stream ok")

        window.btn_connect.click()
        app.processEvents()
        if window.transport.is_connected:
            raise RuntimeError("UI disconnect failed.")
        print("[ui] disconnect ok")
        print("[ui] done")
    finally:
        window.close()
        app.processEvents()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Plottrbot hardware validation suite.")
    parser.add_argument("--port", default="/dev/ttyACM0", help="Serial port for the Arduino/Nano")
    parser.add_argument(
        "--skip-ui",
        action="store_true",
        help="Run backend hardware checks only (skip Qt MainWindow clickthrough).",
    )
    args = parser.parse_args()

    profile = MachineProfile()
    with tempfile.TemporaryDirectory(prefix="plottrbot-hw-") as tmp_dir:
        bmp_path = Path(tmp_dir) / "hardware_validation.bmp"
        _create_test_bmp(bmp_path)
        _run_backend_suite(args.port, bmp_path, profile)
        if not args.skip_ui:
            _run_ui_suite(args.port, bmp_path)

    print("Hardware validation suite passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
