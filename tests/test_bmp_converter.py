from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from plottrbot.core.bmp_converter import BmpConverter
from plottrbot.core.models import MachineProfile


def _create_bmp(path: Path, *, width: int, height: int, dpi: float, black_pixels: list[tuple[int, int]]) -> None:
    image = Image.new("RGB", (width, height), color=(255, 255, 255))
    for x, y in black_pixels:
        image.putpixel((x, y), (0, 0, 0))
    image.save(path, format="BMP", dpi=(dpi, dpi))


def test_inspect_image_uses_dpi(tmp_path: Path) -> None:
    bmp_path = tmp_path / "dpi.bmp"
    _create_bmp(bmp_path, width=100, height=50, dpi=200.0, black_pixels=[])

    converter = BmpConverter(MachineProfile())
    meta = converter.inspect_image(bmp_path)

    assert meta.image_width_px == 100
    assert meta.image_height_px == 50
    assert meta.dpi_x == pytest.approx(200.0, rel=0.0, abs=0.01)
    assert meta.dpi_y == pytest.approx(200.0, rel=0.0, abs=0.01)
    assert meta.image_width_mm == pytest.approx(12.7, rel=0.0, abs=0.01)
    assert meta.image_height_mm == pytest.approx(6.35, rel=0.0, abs=0.01)


def test_generate_vertical_serpentine_and_gcode(tmp_path: Path) -> None:
    bmp_path = tmp_path / "sample.bmp"
    _create_bmp(
        bmp_path,
        width=3,
        height=3,
        dpi=25.4,  # 1px == 1mm
        black_pixels=[(0, 0), (0, 1), (1, 2)],
    )

    converter = BmpConverter(MachineProfile())
    result = converter.generate(
        image_path=bmp_path,
        img_move_x_mm=0,
        img_move_y_mm=0,
        start_gcode_lines=["G1 Z1"],
        end_gcode_lines=["G1 Z1", "G28"],
    )

    assert len(result.lines) == 3
    assert result.lines[0].draw is True
    assert result.lines[0].x0 == pytest.approx(0.0)
    assert result.lines[0].y0 == pytest.approx(0.0)
    assert result.lines[0].x1 == pytest.approx(0.0)
    assert result.lines[0].y1 == pytest.approx(1.0, abs=1e-4)
    assert result.lines[1].draw is False
    assert result.lines[1].x0 == pytest.approx(0.0)
    assert result.lines[1].y0 == pytest.approx(1.0, abs=1e-4)
    assert result.lines[1].x1 == pytest.approx(1.0, abs=1e-4)
    assert result.lines[1].y1 == pytest.approx(2.0, abs=1e-4)
    assert result.lines[2].draw is True
    assert result.lines[2].x0 == pytest.approx(1.0, abs=1e-4)
    assert result.lines[2].y0 == pytest.approx(2.0, abs=1e-4)
    assert result.lines[2].x1 == pytest.approx(1.0, abs=1e-4)
    assert result.lines[2].y1 == pytest.approx(2.0, abs=1e-4)

    assert result.bbox is not None
    assert result.bbox.min_x == pytest.approx(0.0)
    assert result.bbox.min_y == pytest.approx(0.0)
    assert result.bbox.max_x == pytest.approx(1.0, abs=1e-4)
    assert result.bbox.max_y == pytest.approx(2.0, abs=1e-4)

    assert result.gcode == [
        "G1 Z1",
        "G1 X0 Y0",
        "G1 Z0",
        "G1 X0 Y1",
        "G1 Z1",
        "G1 X1 Y2",
        "G1 Z0",
        "G1 X1 Y2",
        "G1 Z1",
        "G28",
    ]

    assert result.command_to_line_index == [-1, -1, -1, 0, -1, 1, -1, 2, -1, -1]
    assert result.line_to_command_index == [3, 5, 7]


def test_dpi_override_changes_mm_dimensions(tmp_path: Path) -> None:
    bmp_path = tmp_path / "dpi_override.bmp"
    _create_bmp(bmp_path, width=100, height=100, dpi=100.0, black_pixels=[])

    converter = BmpConverter(MachineProfile())
    no_override = converter.inspect_image(bmp_path)
    with_override = converter.inspect_image(bmp_path, dpi_override=200)

    assert no_override.image_width_mm == pytest.approx(25.4, rel=0.0, abs=0.02)
    assert with_override.image_width_mm == pytest.approx(12.7, rel=0.0, abs=0.02)
