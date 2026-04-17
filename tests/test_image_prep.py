from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from plottrbot.core.image_prep import (
    ImagePrepSettings,
    parse_threshold_text,
    process_image_for_prep,
    processed_bmp_path_for_image,
    read_sidecar,
    save_processed_bmp,
    sidecar_path_for_image,
    write_sidecar,
)


def _create_gradient_jpg(path: Path, *, width: int = 80, height: int = 40) -> None:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for x in range(width):
        shade = int(round((x / max(width - 1, 1)) * 255))
        draw.line((x, 0, x, height - 1), fill=(shade, shade, shade))
    image.save(path, format="JPEG")


def _count_black_pixels(image: Image.Image) -> int:
    grayscale = image.convert("L")
    return sum(1 for value in grayscale.tobytes() if value <= 10)


def test_threshold_normalization_and_parse_roundtrip() -> None:
    parsed = parse_threshold_text("300, -10, 64")
    settings = ImagePrepSettings(
        dpi=35,
        blur_radius=0.0,
        levels=4,
        strategy="banded",
        auto_thresholds=False,
        manual_thresholds=parsed,
    ).sanitized()

    assert settings.manual_thresholds == [5, 64, 250]
    assert settings.effective_thresholds() == [5, 64, 250]


def test_relative_strategy_is_denser_than_banded(tmp_path: Path) -> None:
    image_path = tmp_path / "gradient.jpg"
    _create_gradient_jpg(image_path)

    banded_settings = ImagePrepSettings(
        levels=4,
        strategy="banded",
        auto_thresholds=False,
        manual_thresholds=[64, 128, 192],
    )
    relative_settings = ImagePrepSettings(
        levels=4,
        strategy="relative",
        auto_thresholds=False,
        manual_thresholds=[64, 128, 192],
    )

    _, banded = process_image_for_prep(image_path=image_path, settings=banded_settings)
    _, relative = process_image_for_prep(image_path=image_path, settings=relative_settings)

    assert _count_black_pixels(relative.halftone_preview_image) > _count_black_pixels(
        banded.halftone_preview_image
    )


def test_sidecar_roundtrip_and_deterministic_output_paths(tmp_path: Path) -> None:
    image_path = tmp_path / "portrait.jpg"
    _create_gradient_jpg(image_path, width=64, height=64)

    settings = ImagePrepSettings(
        dpi=47,
        blur_radius=1.3,
        levels=5,
        strategy="relative",
        auto_thresholds=False,
        manual_thresholds=[40, 90, 150, 220],
        show_halftone_preview=True,
    )
    sanitized, artifacts = process_image_for_prep(image_path=image_path, settings=settings)

    export_bmp_path = processed_bmp_path_for_image(image_path)
    save_processed_bmp(
        output_path=export_bmp_path,
        image=artifacts.export_bmp_image,
        dpi=sanitized.dpi,
    )
    assert export_bmp_path.exists()
    assert export_bmp_path.name == "portrait.plottrbot.processed.bmp"

    sidecar_path = sidecar_path_for_image(image_path)
    write_sidecar(
        sidecar_path=sidecar_path,
        source_image_path=image_path,
        settings=sanitized,
        effective_thresholds=artifacts.effective_thresholds,
        export_bmp_path=export_bmp_path,
    )
    assert sidecar_path.exists()
    assert sidecar_path.name == "portrait.plottrbot-edit.json"

    loaded_source, loaded_settings, loaded_export = read_sidecar(sidecar_path)
    assert loaded_source.resolve() == image_path.resolve()
    assert loaded_export is not None
    assert loaded_export.resolve() == export_bmp_path.resolve()
    assert loaded_settings.to_dict() == sanitized.to_dict()


def test_dimensions_and_dpi_control_processed_resolution(tmp_path: Path) -> None:
    image_path = tmp_path / "sized.jpg"
    _create_gradient_jpg(image_path, width=100, height=60)

    low_dpi = ImagePrepSettings(
        dpi=50,
        target_width_mm=50.8,
        target_height_mm=25.4,
        levels=4,
        strategy="banded",
        auto_thresholds=True,
    )
    high_dpi = ImagePrepSettings(
        dpi=100,
        target_width_mm=50.8,
        target_height_mm=25.4,
        levels=4,
        strategy="banded",
        auto_thresholds=True,
    )

    _, low_artifacts = process_image_for_prep(image_path=image_path, settings=low_dpi)
    _, high_artifacts = process_image_for_prep(image_path=image_path, settings=high_dpi)

    assert low_artifacts.image_width_mm == high_artifacts.image_width_mm
    assert low_artifacts.image_height_mm == high_artifacts.image_height_mm
    assert high_artifacts.image_width_px > low_artifacts.image_width_px
    assert high_artifacts.image_height_px > low_artifacts.image_height_px
