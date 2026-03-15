from __future__ import annotations

from pathlib import Path

from PIL import Image

from plottrbot.core.models import BoundingBox, MachineProfile, PreviewMetadata, SliceResult, TraceLine


def _format_mm(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    if text == "-0":
        return "0"
    return text


class BmpConverter:
    def __init__(self, machine_profile: MachineProfile) -> None:
        self.machine_profile = machine_profile

    def inspect_image(self, image_path: Path, dpi_override: int | None = None) -> PreviewMetadata:
        with Image.open(image_path) as image:
            width_px, height_px = image.size
            dpi_raw = image.info.get("dpi", (96.0, 96.0))
            if isinstance(dpi_raw, tuple) and len(dpi_raw) >= 2:
                dpi_x, dpi_y = float(dpi_raw[0] or 96.0), float(dpi_raw[1] or 96.0)
            else:
                dpi_x, dpi_y = 96.0, 96.0

        if dpi_override is not None and dpi_override > 0:
            dpi_x = float(dpi_override)
            dpi_y = float(dpi_override)

        width_mm = (width_px / dpi_x) * 25.4
        height_mm = (height_px / dpi_y) * 25.4
        return PreviewMetadata(
            image_width_px=width_px,
            image_height_px=height_px,
            dpi_x=dpi_x,
            dpi_y=dpi_y,
            image_width_mm=width_mm,
            image_height_mm=height_mm,
        )

    def generate(
        self,
        *,
        image_path: Path,
        img_move_x_mm: int,
        img_move_y_mm: int,
        dpi_override: int | None = None,
        black_threshold: int = 70,
        start_gcode_lines: list[str] | None = None,
        end_gcode_lines: list[str] | None = None,
    ) -> SliceResult:
        preview = self.inspect_image(image_path, dpi_override=dpi_override)
        with Image.open(image_path) as image:
            rgb_image = image.convert("RGB")
            pixels = rgb_image.load()
            width_px, height_px = rgb_image.size

        ratio_width_to_px = preview.image_width_mm / preview.image_width_px
        ratio_height_to_px = preview.image_height_mm / preview.image_height_px

        pixel_array: list[list[bool]] = [[False for _ in range(height_px)] for _ in range(width_px)]
        for x in range(width_px):
            for y in range(height_px):
                red, green, blue = pixels[x, y]
                pixel_array[x][y] = (
                    red <= black_threshold and green <= black_threshold and blue <= black_threshold
                )

        black_lines = self._calc_vertical_serpentine_lines(
            pixel_array=pixel_array,
            width_px=width_px,
            height_px=height_px,
            ratio_width_to_px=ratio_width_to_px,
            ratio_height_to_px=ratio_height_to_px,
            move_x=img_move_x_mm,
            move_y=img_move_y_mm,
        )

        all_lines = self._expand_to_draw_and_travel_lines(black_lines)
        bbox = self._calc_bbox(black_lines)

        commands: list[str] = []
        command_to_line_index: list[int] = []
        line_to_command_index: list[int] = []

        start_lines = start_gcode_lines if start_gcode_lines is not None else ["G1 Z1"]
        end_lines = end_gcode_lines if end_gcode_lines is not None else ["G1 Z1", "G28"]

        for line in start_lines:
            stripped = line.strip()
            if stripped:
                commands.append(stripped)
                command_to_line_index.append(-1)

        if all_lines:
            commands.append(f"G1 X{_format_mm(all_lines[0].x0)} Y{_format_mm(all_lines[0].y0)}")
            command_to_line_index.append(-1)

        for line_index, line in enumerate(all_lines):
            commands.append(f"G1 Z{0 if line.draw else 1}")
            command_to_line_index.append(-1)
            commands.append(f"G1 X{_format_mm(line.x1)} Y{_format_mm(line.y1)}")
            command_to_line_index.append(line_index)
            line_to_command_index.append(len(commands) - 1)

        for line in end_lines:
            stripped = line.strip()
            if stripped:
                commands.append(stripped)
                command_to_line_index.append(-1)

        return SliceResult(
            lines=all_lines,
            bbox=bbox,
            gcode=commands,
            preview_metadata=preview,
            command_to_line_index=command_to_line_index,
            line_to_command_index=line_to_command_index,
        )

    def _calc_vertical_serpentine_lines(
        self,
        *,
        pixel_array: list[list[bool]],
        width_px: int,
        height_px: int,
        ratio_width_to_px: float,
        ratio_height_to_px: float,
        move_x: int,
        move_y: int,
    ) -> list[TraceLine]:
        black_lines: list[TraceLine] = []
        going_down = True

        for x in range(width_px):
            line_started = False
            x0 = 0
            y0 = 0

            y_range = range(height_px) if going_down else range(height_px - 1, -1, -1)
            for y in y_range:
                if (not line_started) and pixel_array[x][y]:
                    x0 = x
                    y0 = y
                    line_started = True

                if not line_started:
                    continue

                if going_down:
                    if (y + 1 >= height_px) or (not pixel_array[x][y + 1]):
                        line_started = False
                else:
                    if (y - 1 < 0) or (not pixel_array[x][y - 1]):
                        line_started = False

                if not line_started:
                    total_x0 = (x0 * ratio_width_to_px) + move_x
                    total_y0 = (y0 * ratio_height_to_px) + move_y
                    total_x1 = (x * ratio_width_to_px) + move_x
                    total_y1 = (y * ratio_height_to_px) + move_y
                    black_lines.append(TraceLine(total_x0, total_y0, total_x1, total_y1, draw=True))

            going_down = not going_down

        return black_lines

    @staticmethod
    def _expand_to_draw_and_travel_lines(black_lines: list[TraceLine]) -> list[TraceLine]:
        if not black_lines:
            return []

        if len(black_lines) == 1:
            line = black_lines[0]
            return [TraceLine(line.x0, line.y0, line.x1, line.y1, draw=True)]

        all_lines: list[TraceLine] = []
        for i in range(len(black_lines) - 1):
            current = black_lines[i]
            next_line = black_lines[i + 1]
            all_lines.append(TraceLine(current.x0, current.y0, current.x1, current.y1, draw=True))
            all_lines.append(TraceLine(current.x1, current.y1, next_line.x0, next_line.y0, draw=False))
            if i == len(black_lines) - 2:
                all_lines.append(
                    TraceLine(next_line.x0, next_line.y0, next_line.x1, next_line.y1, draw=True)
                )
        return all_lines

    @staticmethod
    def _calc_bbox(lines: list[TraceLine]) -> BoundingBox | None:
        if not lines:
            return None
        min_x = min(min(line.x0, line.x1) for line in lines)
        min_y = min(min(line.y0, line.y1) for line in lines)
        max_x = max(max(line.x0, line.x1) for line in lines)
        max_y = max(max(line.y0, line.y1) for line in lines)
        return BoundingBox(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)
