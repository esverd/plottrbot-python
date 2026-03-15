from __future__ import annotations

from pathlib import Path

from PIL import Image

from plottrbot.core.bmp_converter import BmpConverter
from plottrbot.core.models import MachineProfile


def test_line_to_command_index_matches_legacy_resume_math(tmp_path: Path) -> None:
    image = Image.new("RGB", (3, 3), color=(255, 255, 255))
    image.putpixel((0, 0), (0, 0, 0))
    image.putpixel((0, 1), (0, 0, 0))
    image.putpixel((1, 2), (0, 0, 0))
    path = tmp_path / "img.bmp"
    image.save(path, format="BMP", dpi=(25.4, 25.4))

    converter = BmpConverter(MachineProfile())
    result = converter.generate(image_path=path, img_move_x_mm=0, img_move_y_mm=0)

    for line_no, cmd_index in enumerate(result.line_to_command_index):
        assert cmd_index == 3 + (line_no * 2)
