from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from plottrbot.core.bmp_converter import BmpConverter
from plottrbot.core.models import MachineProfile


LINE_SUFFIX_PATTERN = re.compile(r"L\d+$")


def _normalize_commands(raw_lines: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("M220"):
            continue
        stripped = LINE_SUFFIX_PATTERN.sub("", stripped).strip()
        if stripped:
            normalized.append(stripped)
    return normalized


def test_csharp_bmp_fixture_matches_python_normalized_output(tmp_path: Path) -> None:
    image = Image.new("RGB", (3, 3), color=(255, 255, 255))
    image.putpixel((0, 0), (0, 0, 0))
    image.putpixel((0, 1), (0, 0, 0))
    image.putpixel((1, 2), (0, 0, 0))
    bmp_path = tmp_path / "parity.bmp"
    image.save(bmp_path, format="BMP", dpi=(25.4, 25.4))

    converter = BmpConverter(MachineProfile())
    result = converter.generate(
        image_path=bmp_path,
        img_move_x_mm=0,
        img_move_y_mm=0,
        start_gcode_lines=["G1 Z1"],
        end_gcode_lines=["G1 Z1", "G28"],
    )

    fixture_path = Path(__file__).parent / "fixtures" / "csharp_parity_sample_raw.gcode"
    csharp_lines = fixture_path.read_text(encoding="utf-8").splitlines()
    csharp_normalized = _normalize_commands(csharp_lines)
    python_normalized = _normalize_commands(result.gcode)

    assert python_normalized == csharp_normalized
