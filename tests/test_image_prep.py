from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from plottrbot.core.image_prep import (
    ImagePrepCrop,
    ImagePrepMask,
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
        exposure_percent=25,
        blur_radius=1.3,
        levels=5,
        strategy="relative",
        auto_thresholds=False,
        manual_thresholds=[40, 90, 150, 220],
        show_halftone_preview=True,
        crop=ImagePrepCrop(enabled=True, center_x=0.4, center_y=0.6, width=0.7, height=0.8),
        local_masks=[
            ImagePrepMask(
                center_x=0.25,
                center_y=0.75,
                width=0.42,
                height=0.26,
                roundness_percent=65,
                rotation_degrees=32.0,
                feather=0.02,
                exposure_percent=35,
                contrast_percent=180,
                blur_radius=1.4,
            )
        ],
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


def test_sidecar_write_resolves_relative_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    image_path = Path("relative.jpg")
    _create_gradient_jpg(image_path, width=32, height=32)
    settings, artifacts = process_image_for_prep(image_path=image_path, settings=ImagePrepSettings())
    sidecar_path = Path("relative.plottrbot-edit.json")

    write_sidecar(
        sidecar_path=sidecar_path,
        source_image_path=image_path,
        settings=settings,
        effective_thresholds=artifacts.effective_thresholds,
        export_bmp_path=Path("relative.plottrbot.processed.bmp"),
    )

    loaded_source, _loaded_settings, loaded_export = read_sidecar(sidecar_path)
    assert loaded_source == image_path.resolve()
    assert loaded_export == Path("relative.plottrbot.processed.bmp").resolve()


def test_legacy_radius_mask_migrates_to_rounded_square() -> None:
    mask = ImagePrepMask.from_dict({"center_x": 0.2, "center_y": 0.3, "radius": 0.25})

    assert mask.center_x == pytest.approx(0.2)
    assert mask.center_y == pytest.approx(0.3)
    assert mask.width == pytest.approx(0.5)
    assert mask.height == pytest.approx(0.5)
    assert mask.roundness_percent == 100
    assert mask.rotation_degrees == pytest.approx(0.0)


def test_source_crop_changes_processed_content_and_keeps_source_preview(tmp_path: Path) -> None:
    image_path = tmp_path / "cropped.jpg"
    _create_gradient_jpg(image_path, width=120, height=80)

    base_settings = ImagePrepSettings(
        dpi=40,
        target_width_mm=40.0,
        target_height_mm=40.0,
    )
    crop_settings = ImagePrepSettings(
        dpi=40,
        target_width_mm=40.0,
        target_height_mm=40.0,
        crop=ImagePrepCrop(enabled=True, center_x=0.25, center_y=0.5, width=0.45, height=1.0),
    )

    _, base_artifacts = process_image_for_prep(image_path=image_path, settings=base_settings)
    sanitized, cropped_artifacts = process_image_for_prep(image_path=image_path, settings=crop_settings)

    assert sanitized.crop.enabled is True
    assert cropped_artifacts.source_preview_image.size == (120, 80)
    assert cropped_artifacts.tonal_preview_image.size == base_artifacts.tonal_preview_image.size
    assert cropped_artifacts.tonal_preview_image.tobytes() != base_artifacts.tonal_preview_image.tobytes()


def test_local_mask_adjustment_changes_only_masked_region(tmp_path: Path) -> None:
    image_path = tmp_path / "masked.jpg"
    _create_gradient_jpg(image_path, width=80, height=80)

    base_settings = ImagePrepSettings(
        dpi=40,
        target_width_mm=40.0,
        target_height_mm=40.0,
        levels=4,
        strategy="banded",
        auto_thresholds=False,
        manual_thresholds=[64, 128, 192],
    )
    masked_settings = ImagePrepSettings(
        dpi=40,
        target_width_mm=40.0,
        target_height_mm=40.0,
        levels=4,
        strategy="banded",
        auto_thresholds=False,
        manual_thresholds=[64, 128, 192],
        local_masks=[
            ImagePrepMask(
                center_x=0.4,
                center_y=0.5,
                width=0.5,
                height=0.25,
                roundness_percent=0,
                rotation_degrees=90.0,
                feather=0.0,
                exposure_percent=-35,
                contrast_percent=350,
                blur_radius=0.0,
            )
        ],
    )

    _, base_artifacts = process_image_for_prep(image_path=image_path, settings=base_settings)
    sanitized, masked_artifacts = process_image_for_prep(image_path=image_path, settings=masked_settings)

    assert sanitized.local_masks[0].contrast_percent == 350
    assert sanitized.local_masks[0].exposure_percent == -35
    assert sanitized.local_masks[0].width == pytest.approx(0.5, abs=0.01)
    assert sanitized.local_masks[0].height == pytest.approx(0.25, abs=0.01)
    assert sanitized.local_masks[0].roundness_percent == 0
    assert sanitized.local_masks[0].rotation_degrees == pytest.approx(90.0, abs=0.01)
    base_tonal = base_artifacts.tonal_preview_image.convert("L")
    masked_tonal = masked_artifacts.tonal_preview_image.convert("L")
    center = (int(round(base_tonal.width * 0.4)), base_tonal.height // 2)
    corner = (0, 0)

    assert masked_tonal.getpixel(center) != base_tonal.getpixel(center)
    assert masked_tonal.getpixel(corner) == base_tonal.getpixel(corner)


def test_local_mask_matching_global_settings_is_noop(tmp_path: Path) -> None:
    image_path = tmp_path / "noop_mask.jpg"
    _create_gradient_jpg(image_path, width=60, height=60)

    base_settings = ImagePrepSettings(
        dpi=35,
        exposure_percent=45,
        contrast_percent=120,
        blur_radius=1.2,
        levels=4,
        strategy="relative",
        auto_thresholds=True,
    )
    masked_settings = ImagePrepSettings(
        dpi=35,
        exposure_percent=45,
        contrast_percent=120,
        blur_radius=1.2,
        levels=4,
        strategy="relative",
        auto_thresholds=True,
        local_masks=[
            ImagePrepMask(
                center_x=0.5,
                center_y=0.5,
                width=0.6,
                height=0.6,
                roundness_percent=100,
                rotation_degrees=45.0,
                feather=0.08,
                exposure_percent=45,
                contrast_percent=120,
                blur_radius=1.2,
            )
        ],
    )

    _, base_artifacts = process_image_for_prep(image_path=image_path, settings=base_settings)
    _, masked_artifacts = process_image_for_prep(image_path=image_path, settings=masked_settings)

    assert masked_artifacts.tonal_preview_image.tobytes() == base_artifacts.tonal_preview_image.tobytes()
    assert masked_artifacts.export_bmp_image.tobytes() == base_artifacts.export_bmp_image.tobytes()


def test_rotated_local_mask_changes_mask_footprint(tmp_path: Path) -> None:
    image_path = tmp_path / "rotated_mask.jpg"
    _create_gradient_jpg(image_path, width=80, height=80)

    base_mask = ImagePrepMask(
        center_x=0.5,
        center_y=0.5,
        width=0.7,
        height=0.18,
        roundness_percent=0,
        exposure_percent=-60,
    )
    settings = ImagePrepSettings(
        dpi=40,
        target_width_mm=40.0,
        target_height_mm=40.0,
        local_masks=[base_mask],
    )
    rotated_settings = ImagePrepSettings(
        dpi=40,
        target_width_mm=40.0,
        target_height_mm=40.0,
        local_masks=[replace(base_mask, rotation_degrees=90.0)],
    )

    _, straight_artifacts = process_image_for_prep(image_path=image_path, settings=settings)
    sanitized, rotated_artifacts = process_image_for_prep(image_path=image_path, settings=rotated_settings)

    assert sanitized.local_masks[0].rotation_degrees == pytest.approx(90.0)
    assert straight_artifacts.tonal_preview_image.tobytes() != rotated_artifacts.tonal_preview_image.tobytes()


def test_global_exposure_adjusts_tonal_output(tmp_path: Path) -> None:
    image_path = tmp_path / "exposure.jpg"
    _create_gradient_jpg(image_path, width=64, height=64)

    _, normal_artifacts = process_image_for_prep(image_path=image_path, settings=ImagePrepSettings())
    sanitized, bright_artifacts = process_image_for_prep(
        image_path=image_path,
        settings=ImagePrepSettings(exposure_percent=80),
    )

    assert sanitized.exposure_percent == 80
    normal_values = list(normal_artifacts.tonal_preview_image.convert("L").tobytes())
    bright_values = list(bright_artifacts.tonal_preview_image.convert("L").tobytes())
    assert sum(bright_values) > sum(normal_values)


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


def test_overly_large_prep_render_is_rejected(tmp_path: Path) -> None:
    image_path = tmp_path / "large_guard.jpg"
    _create_gradient_jpg(image_path, width=120, height=120)

    settings = ImagePrepSettings(
        dpi=1200,
        target_width_mm=1460.0,
        target_height_mm=1000.0,
        levels=4,
        strategy="banded",
        auto_thresholds=True,
    )
    with pytest.raises(ValueError, match="too large"):
        process_image_for_prep(image_path=image_path, settings=settings)


def test_contrast_allows_values_above_slider_range(tmp_path: Path) -> None:
    image_path = tmp_path / "contrast.jpg"
    _create_gradient_jpg(image_path, width=64, height=64)

    settings = ImagePrepSettings(
        dpi=35,
        target_width_mm=120.0,
        target_height_mm=120.0,
        contrast_percent=450,
        levels=4,
        strategy="banded",
        auto_thresholds=True,
    )
    sanitized, artifacts = process_image_for_prep(image_path=image_path, settings=settings)

    assert sanitized.contrast_percent == 450
    assert artifacts.image_width_px > 0
    assert artifacts.image_height_px > 0
