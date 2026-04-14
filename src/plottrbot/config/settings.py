from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

from plottrbot.core.models import MachineProfile


@dataclass(slots=True)
class AppSettings:
    machine_profile: MachineProfile = field(default_factory=MachineProfile)
    end_gcode_lines: list[str] = field(default_factory=lambda: ["G1 Z1", "G28"])
    motor_power_commands_enabled: bool = True
    last_port: str = ""
    window_width: int = 1600
    window_height: int = 1000


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            config_dir = Path(user_config_dir("plottrbot"))
            path = config_dir / "config.json"
        self.path = path

    def load(self) -> AppSettings:
        if not self.path.exists():
            settings = AppSettings()
            self.save(settings)
            return settings

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            settings = AppSettings()
            self.save(settings)
            return settings

        profile_raw = raw.get("machine_profile", {})
        profile = MachineProfile(
            canvas_width_mm=int(profile_raw.get("canvas_width_mm", 1460)),
            canvas_height_mm=int(profile_raw.get("canvas_height_mm", 1000)),
            home_x_mm=float(profile_raw.get("home_x_mm", 730.0)),
            home_y_mm=float(profile_raw.get("home_y_mm", 240.0)),
            baudrate=int(profile_raw.get("baudrate", 9600)),
            ack_token=str(profile_raw.get("ack_token", "GO")),
            ack_timeout_seconds=float(profile_raw.get("ack_timeout_seconds", 60.0)),
        )
        end_gcode_raw = raw.get("end_gcode_lines", ["G1 Z1", "G28"])
        end_gcode = [str(line).strip() for line in end_gcode_raw if str(line).strip()]
        if not end_gcode:
            end_gcode = ["G1 Z1", "G28"]

        return AppSettings(
            machine_profile=profile,
            end_gcode_lines=end_gcode,
            motor_power_commands_enabled=bool(raw.get("motor_power_commands_enabled", True)),
            last_port=str(raw.get("last_port", "")),
            window_width=int(raw.get("window_width", 1600)),
            window_height=int(raw.get("window_height", 1000)),
        )

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(settings)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
