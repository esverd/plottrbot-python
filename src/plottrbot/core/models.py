from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class MachineProfile:
    canvas_width_mm: int = 1460
    canvas_height_mm: int = 1000
    home_x_mm: float = 730.0
    home_y_mm: float = 240.0
    baudrate: int = 9600
    ack_token: str = "GO"
    ack_timeout_seconds: float = 60.0


@dataclass(slots=True, frozen=True)
class TraceLine:
    x0: float
    y0: float
    x1: float
    y1: float
    draw: bool = False


@dataclass(slots=True, frozen=True)
class BoundingBox:
    min_x: float
    min_y: float
    max_x: float
    max_y: float


@dataclass(slots=True, frozen=True)
class PreviewMetadata:
    image_width_px: int
    image_height_px: int
    dpi_x: float
    dpi_y: float
    image_width_mm: float
    image_height_mm: float


@dataclass(slots=True, frozen=True)
class SliceResult:
    lines: list[TraceLine]
    bbox: BoundingBox | None
    gcode: list[str]
    preview_metadata: PreviewMetadata
    command_to_line_index: list[int]
    line_to_command_index: list[int]


@dataclass(slots=True)
class RetainedImage:
    file_path: Path
    move_x_mm: int
    move_y_mm: int
    dpi_override: int | None = None
    width_mm: float = 0.0
    height_mm: float = 0.0


@dataclass(slots=True)
class JobState:
    loaded_file: Path | None = None
    file_type: str | None = None
    img_move_x_mm: int = 0
    img_move_y_mm: int = 0
    retained_image: RetainedImage | None = None
    preview_scale: float = 0.8
    lines: list[TraceLine] = field(default_factory=list)
    gcode: list[str] = field(default_factory=list)
    bounding_box: BoundingBox | None = None
    current_send_index: int = 0
    paused: bool = False
    selected_line_index: int = 0
    dpi_override: int | None = None
    image_width_mm: float = 0.0
    image_height_mm: float = 0.0
    image_dpi: float = 96.0
    command_to_line_index: list[int] = field(default_factory=list)
    line_to_command_index: list[int] = field(default_factory=list)

    def clear_image(self) -> None:
        self.loaded_file = None
        self.file_type = None
        self.lines.clear()
        self.gcode.clear()
        self.bounding_box = None
        self.current_send_index = 0
        self.paused = False
        self.selected_line_index = 0
        self.dpi_override = None
        self.image_width_mm = 0.0
        self.image_height_mm = 0.0
        self.image_dpi = 96.0
        self.command_to_line_index.clear()
        self.line_to_command_index.clear()
