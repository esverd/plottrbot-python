from __future__ import annotations

import json
from pathlib import Path

from plottrbot.core.draw_session_logger import DrawSessionLogger


def test_draw_session_logger_writes_start_progress_and_final_status(tmp_path: Path) -> None:
    logger = DrawSessionLogger(tmp_path / "logs")
    path = logger.start_session(
        image_path=Path("C:/tmp/sample.bmp"),
        image_width_px=100,
        image_height_px=80,
        image_width_mm=72.5,
        image_height_mm=58.1,
        dpi=35.0,
        move_x_mm=331,
        move_y_mm=250,
        gcode_commands=["G1 Z1", "G1 X0 Y0", "G1 Z0", "G1 X0 Y10", "G1 Z1", "G1 X1 Y0"],
        end_gcode_lines=["G1 Z1", "G1 X581 Y800"],
        start_command_index=2,
        command_to_line_index=[-1, -1, -1, 0, -1, 1],
        line_to_command_index=[3, 5],
        machine_profile={"canvas_width_mm": 1162, "canvas_height_mm": 1000},
        serial_port="COM3",
        image_prep={
            "source_image_path": "C:/tmp/source.jpg",
            "settings": {"dpi": 35, "levels": 4},
        },
    )

    assert path.exists()
    started_payload = json.loads(path.read_text(encoding="utf-8"))
    assert started_payload["status"] == "running"
    assert started_payload["draw_plan"]["start_command_index"] == 2
    assert started_payload["draw_plan"]["start_line_index"] is None
    assert started_payload["image"]["placement_top_left_mm"] == {"x": 331, "y": 250}
    assert started_payload["gcode_commands"][3] == "G1 X0 Y10"
    assert started_payload["image_prep"]["source_image_path"] == "C:/tmp/source.jpg"

    logger.add_event("stop_requested", details={"reason": "operator"})
    logger.update_progress(
        current_command_index=4,
        current_line_index=0,
        commands_sent_total=4,
        commands_sent_this_run=2,
        lines_sent_total=1,
        lines_sent_this_run=1,
        force_flush=True,
    )
    logger.finalize(
        status="stopped",
        current_command_index=4,
        current_line_index=0,
        commands_sent_total=4,
        commands_sent_this_run=2,
        lines_sent_total=1,
        lines_sent_this_run=1,
    )

    stopped_payload = json.loads(path.read_text(encoding="utf-8"))
    assert stopped_payload["status"] == "stopped"
    assert stopped_payload["finished_at_utc"] is not None
    assert stopped_payload["progress"]["lines_sent_total"] == 1
    assert stopped_payload["progress"]["commands_sent_this_run"] == 2
    assert any(event["event"] == "stop_requested" for event in stopped_payload["events"])
    assert any(event["event"] == "session_stopped" for event in stopped_payload["events"])
