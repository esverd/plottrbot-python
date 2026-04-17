from __future__ import annotations

import json
import platform
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DrawSessionLogger:
    """Persists per-draw-session metadata and timeline events for debugging."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self._active_path: Path | None = None
        self._session: dict[str, Any] | None = None

    @property
    def active_path(self) -> Path | None:
        with self._lock:
            return self._active_path

    def start_session(
        self,
        *,
        image_path: Path,
        image_width_px: int,
        image_height_px: int,
        image_width_mm: float,
        image_height_mm: float,
        dpi: float,
        move_x_mm: int,
        move_y_mm: int,
        gcode_commands: list[str],
        end_gcode_lines: list[str],
        start_command_index: int,
        command_to_line_index: list[int],
        line_to_command_index: list[int],
        machine_profile: dict[str, Any],
        serial_port: str,
    ) -> Path:
        started_at = _utc_now_iso()
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        session_path = self.log_dir / f"draw-session-{session_id}.json"

        start_line_index = self._resolve_line_index(start_command_index, command_to_line_index)
        session = {
            "schema_version": 1,
            "session_id": session_id,
            "status": "running",
            "started_at_utc": started_at,
            "finished_at_utc": None,
            "host": {
                "platform": platform.platform(),
                "python_version": platform.python_version(),
            },
            "serial": {
                "port": serial_port,
            },
            "machine_profile": machine_profile,
            "image": {
                "file_name": image_path.name,
                "file_path": str(image_path),
                "placement_top_left_mm": {
                    "x": move_x_mm,
                    "y": move_y_mm,
                },
                "size_px": {
                    "width": image_width_px,
                    "height": image_height_px,
                },
                "size_mm": {
                    "width": image_width_mm,
                    "height": image_height_mm,
                },
                "dpi": dpi,
            },
            "draw_plan": {
                "start_command_index": start_command_index,
                "start_line_index": start_line_index,
                "total_commands": len(gcode_commands),
                "total_lines": len(line_to_command_index),
                "end_gcode_lines": list(end_gcode_lines),
            },
            "progress": {
                "current_command_index": start_command_index,
                "current_line_index": start_line_index,
                "commands_sent_total": start_command_index,
                "commands_sent_this_run": 0,
                "lines_sent_total": self._count_lines_sent(start_command_index, line_to_command_index),
                "lines_sent_this_run": 0,
            },
            "events": [
                {
                    "event": "started",
                    "time_utc": started_at,
                    "details": {
                        "start_command_index": start_command_index,
                        "start_line_index": start_line_index,
                    },
                }
            ],
            "gcode_commands": list(gcode_commands),
        }

        with self._lock:
            self._active_path = session_path
            self._session = session
            self._write_unlocked()
            return session_path

    def add_event(self, event: str, *, details: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self._session is None:
                return
            event_entry = {
                "event": event,
                "time_utc": _utc_now_iso(),
            }
            if details:
                event_entry["details"] = details
            self._session["events"].append(event_entry)
            self._write_unlocked()

    def update_progress(
        self,
        *,
        current_command_index: int,
        current_line_index: int | None,
        commands_sent_total: int,
        commands_sent_this_run: int,
        lines_sent_total: int,
        lines_sent_this_run: int,
        force_flush: bool = False,
    ) -> None:
        with self._lock:
            if self._session is None:
                return
            progress = self._session["progress"]
            progress["current_command_index"] = current_command_index
            progress["current_line_index"] = current_line_index
            progress["commands_sent_total"] = commands_sent_total
            progress["commands_sent_this_run"] = commands_sent_this_run
            progress["lines_sent_total"] = lines_sent_total
            progress["lines_sent_this_run"] = lines_sent_this_run

            if force_flush or (commands_sent_this_run % 25 == 0):
                self._write_unlocked()

    def finalize(
        self,
        *,
        status: str,
        current_command_index: int,
        current_line_index: int | None,
        commands_sent_total: int,
        commands_sent_this_run: int,
        lines_sent_total: int,
        lines_sent_this_run: int,
        error: str | None = None,
    ) -> None:
        with self._lock:
            if self._session is None:
                return
            finished_at = _utc_now_iso()
            self._session["status"] = status
            self._session["finished_at_utc"] = finished_at
            progress = self._session["progress"]
            progress["current_command_index"] = current_command_index
            progress["current_line_index"] = current_line_index
            progress["commands_sent_total"] = commands_sent_total
            progress["commands_sent_this_run"] = commands_sent_this_run
            progress["lines_sent_total"] = lines_sent_total
            progress["lines_sent_this_run"] = lines_sent_this_run
            if error:
                self._session["error"] = error
            self._session["events"].append(
                {
                    "event": f"session_{status}",
                    "time_utc": finished_at,
                    "details": {
                        "commands_sent_total": commands_sent_total,
                        "commands_sent_this_run": commands_sent_this_run,
                        "lines_sent_total": lines_sent_total,
                        "lines_sent_this_run": lines_sent_this_run,
                    },
                }
            )
            self._write_unlocked()
            self._session = None
            self._active_path = None

    @staticmethod
    def _resolve_line_index(command_index: int, command_to_line_index: list[int]) -> int | None:
        if not command_to_line_index:
            return None
        if command_index < 0:
            return None
        safe_index = min(command_index, len(command_to_line_index) - 1)
        for idx in range(safe_index, -1, -1):
            line_index = command_to_line_index[idx]
            if line_index >= 0:
                return line_index
        return None

    @staticmethod
    def _count_lines_sent(command_index: int, line_to_command_index: list[int]) -> int:
        if not line_to_command_index:
            return 0
        sent = 0
        for line_command_index in line_to_command_index:
            if line_command_index < command_index:
                sent += 1
        return sent

    def _write_unlocked(self) -> None:
        if self._active_path is None or self._session is None:
            return
        tmp_path = self._active_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._session, indent=2), encoding="utf-8")
        tmp_path.replace(self._active_path)
