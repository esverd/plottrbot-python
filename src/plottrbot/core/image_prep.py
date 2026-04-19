from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

PrepStrategy = Literal["banded", "relative"]

MIN_LEVELS = 2
MAX_LEVELS = 8
MIN_THRESHOLD = 5
MAX_THRESHOLD = 250
MIN_THRESHOLD_GAP = 8
SIDECAR_SCHEMA_VERSION = 2
MAX_RENDER_SIDE_PX = 12000
MAX_RENDER_PIXELS = 36_000_000
MIN_CONTRAST_PERCENT = -100
MAX_CONTRAST_PERCENT = 1000
MIN_EXPOSURE_PERCENT = -100
MAX_EXPOSURE_PERCENT = 300


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clamp(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, int(value)))


def _clamp_float(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, float(value)))


def _coerce_int(value: object, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _coerce_float(value: object, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _coerce_bool(value: object, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def _normalize_sorted_thresholds(values: list[int], expected_count: int, minimum_gap: int) -> list[int]:
    normalized: list[int] = []
    min_allowed = MIN_THRESHOLD
    max_bound = MAX_THRESHOLD
    for index, raw_value in enumerate(values):
        remaining = expected_count - index - 1
        max_allowed = max_bound - (remaining * minimum_gap)
        if max_allowed < min_allowed:
            max_allowed = min_allowed
        value = _clamp(raw_value, min_allowed, max_allowed)
        normalized.append(value)
        min_allowed = value + minimum_gap
    return normalized


def expected_threshold_count(levels: int) -> int:
    bounded_levels = _clamp(levels, MIN_LEVELS, MAX_LEVELS)
    return max(0, bounded_levels - 1)


def generate_auto_thresholds(levels: int) -> list[int]:
    count = expected_threshold_count(levels)
    if count <= 0:
        return []
    raw_thresholds = [int(round((256.0 * idx) / (count + 1))) for idx in range(1, count + 1)]
    sorted_values = sorted(_clamp(value, 0, 255) for value in raw_thresholds)
    return _normalize_sorted_thresholds(sorted_values, count, MIN_THRESHOLD_GAP)


def normalize_thresholds(
    thresholds: Iterable[int | float | str],
    *,
    levels: int,
    minimum_gap: int = MIN_THRESHOLD_GAP,
) -> list[int]:
    count = expected_threshold_count(levels)
    if count <= 0:
        return []

    parsed: list[int] = []
    for threshold in thresholds:
        try:
            parsed.append(int(float(str(threshold).strip())))
        except (TypeError, ValueError):
            continue

    if len(parsed) < count:
        fallback = generate_auto_thresholds(levels)
        parsed.extend(fallback[len(parsed) : count])
    elif len(parsed) > count:
        parsed = parsed[:count]

    sorted_values = sorted(_clamp(value, 0, 255) for value in parsed)
    return _normalize_sorted_thresholds(sorted_values, count, max(1, minimum_gap))


def parse_threshold_text(text: str) -> list[int]:
    tokens = text.replace(";", ",").replace(" ", ",").split(",")
    values: list[int] = []
    for token in tokens:
        stripped = token.strip()
        if not stripped:
            continue
        try:
            values.append(int(float(stripped)))
        except ValueError:
            continue
    return values


def processed_bmp_path_for_image(image_path: Path) -> Path:
    return image_path.with_name(f"{image_path.stem}.plottrbot.processed.bmp")


def sidecar_path_for_image(image_path: Path) -> Path:
    return image_path.with_name(f"{image_path.stem}.plottrbot-edit.json")


def _safe_resolved_path(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def is_supported_source_image(image_path: Path) -> bool:
    return image_path.suffix.lower() in {".jpg", ".jpeg"}


@dataclass(slots=True)
class ImagePrepMask:
    center_x: float = 0.5
    center_y: float = 0.5
    radius: float = 0.2
    feather: float = 0.04
    exposure_percent: int = 0
    contrast_percent: int = 0
    blur_radius: float = 0.0

    def sanitized(self) -> ImagePrepMask:
        return ImagePrepMask(
            center_x=_clamp_float(self.center_x, 0.0, 1.0),
            center_y=_clamp_float(self.center_y, 0.0, 1.0),
            radius=_clamp_float(self.radius, 0.01, 1.0),
            feather=_clamp_float(self.feather, 0.0, 0.5),
            exposure_percent=_clamp(
                int(round(self.exposure_percent)),
                MIN_EXPOSURE_PERCENT,
                MAX_EXPOSURE_PERCENT,
            ),
            contrast_percent=_clamp(
                int(round(self.contrast_percent)),
                MIN_CONTRAST_PERCENT,
                MAX_CONTRAST_PERCENT,
            ),
            blur_radius=max(0.0, float(self.blur_radius)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_x": float(self.center_x),
            "center_y": float(self.center_y),
            "radius": float(self.radius),
            "feather": float(self.feather),
            "exposure_percent": int(self.exposure_percent),
            "contrast_percent": int(self.contrast_percent),
            "blur_radius": float(self.blur_radius),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ImagePrepMask:
        return cls(
            center_x=_coerce_float(payload.get("center_x"), 0.5),
            center_y=_coerce_float(payload.get("center_y"), 0.5),
            radius=_coerce_float(payload.get("radius"), 0.2),
            feather=_coerce_float(payload.get("feather"), 0.04),
            exposure_percent=_coerce_int(payload.get("exposure_percent"), 0),
            contrast_percent=_coerce_int(payload.get("contrast_percent"), 0),
            blur_radius=_coerce_float(payload.get("blur_radius"), 0.0),
        ).sanitized()


@dataclass(slots=True)
class ImagePrepSettings:
    dpi: int = 35
    target_width_mm: float = 0.0
    target_height_mm: float = 0.0
    exposure_percent: int = 0
    contrast_percent: int = 0
    blur_radius: float = 0.0
    levels: int = 4
    strategy: PrepStrategy = "banded"
    auto_thresholds: bool = True
    manual_thresholds: list[int] = field(default_factory=list)
    show_halftone_preview: bool = False
    local_masks: list[ImagePrepMask] = field(default_factory=list)

    def sanitized(self) -> ImagePrepSettings:
        strategy: PrepStrategy
        strategy = "relative" if self.strategy == "relative" else "banded"
        levels = _clamp(int(round(self.levels)), MIN_LEVELS, MAX_LEVELS)
        dpi = max(1, int(round(self.dpi)))
        target_width_mm = max(0.0, float(self.target_width_mm))
        target_height_mm = max(0.0, float(self.target_height_mm))
        exposure_percent = _clamp(
            int(round(self.exposure_percent)),
            MIN_EXPOSURE_PERCENT,
            MAX_EXPOSURE_PERCENT,
        )
        contrast_percent = _clamp(int(round(self.contrast_percent)), MIN_CONTRAST_PERCENT, MAX_CONTRAST_PERCENT)
        blur_radius = max(0.0, float(self.blur_radius))
        auto_thresholds = bool(self.auto_thresholds)
        manual_thresholds = normalize_thresholds(self.manual_thresholds, levels=levels)
        local_masks = [mask.sanitized() for mask in self.local_masks]
        return ImagePrepSettings(
            dpi=dpi,
            target_width_mm=target_width_mm,
            target_height_mm=target_height_mm,
            exposure_percent=exposure_percent,
            contrast_percent=contrast_percent,
            blur_radius=blur_radius,
            levels=levels,
            strategy=strategy,
            auto_thresholds=auto_thresholds,
            manual_thresholds=manual_thresholds,
            show_halftone_preview=bool(self.show_halftone_preview),
            local_masks=local_masks,
        )

    def effective_thresholds(self) -> list[int]:
        bounded_levels = _clamp(self.levels, MIN_LEVELS, MAX_LEVELS)
        if self.auto_thresholds:
            return generate_auto_thresholds(bounded_levels)
        return normalize_thresholds(self.manual_thresholds, levels=bounded_levels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dpi": int(self.dpi),
            "target_width_mm": float(self.target_width_mm),
            "target_height_mm": float(self.target_height_mm),
            "exposure_percent": int(self.exposure_percent),
            "contrast_percent": int(self.contrast_percent),
            "blur_radius": float(self.blur_radius),
            "levels": int(self.levels),
            "strategy": self.strategy,
            "auto_thresholds": bool(self.auto_thresholds),
            "manual_thresholds": [int(value) for value in self.manual_thresholds],
            "show_halftone_preview": bool(self.show_halftone_preview),
            "local_masks": [mask.sanitized().to_dict() for mask in self.local_masks],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ImagePrepSettings:
        strategy_value = str(payload.get("strategy", "banded")).strip().lower()
        strategy: PrepStrategy = "relative" if strategy_value == "relative" else "banded"
        masks: list[ImagePrepMask] = []
        raw_masks = payload.get("local_masks", payload.get("masks", []))
        if isinstance(raw_masks, list):
            for raw_mask in raw_masks:
                if isinstance(raw_mask, dict):
                    masks.append(ImagePrepMask.from_dict(raw_mask))

        settings = cls(
            dpi=max(1, _coerce_int(payload.get("dpi"), 35)),
            target_width_mm=max(0.0, _coerce_float(payload.get("target_width_mm"), 0.0)),
            target_height_mm=max(0.0, _coerce_float(payload.get("target_height_mm"), 0.0)),
            exposure_percent=_clamp(
                _coerce_int(payload.get("exposure_percent"), 0),
                MIN_EXPOSURE_PERCENT,
                MAX_EXPOSURE_PERCENT,
            ),
            contrast_percent=_clamp(
                _coerce_int(payload.get("contrast_percent"), 0),
                MIN_CONTRAST_PERCENT,
                MAX_CONTRAST_PERCENT,
            ),
            blur_radius=max(0.0, _coerce_float(payload.get("blur_radius"), 0.0)),
            levels=_clamp(_coerce_int(payload.get("levels"), 4), MIN_LEVELS, MAX_LEVELS),
            strategy=strategy,
            auto_thresholds=_coerce_bool(payload.get("auto_thresholds"), True),
            manual_thresholds=[
                _coerce_int(value, 0) for value in payload.get("manual_thresholds", []) if value is not None
            ],
            show_halftone_preview=_coerce_bool(payload.get("show_halftone_preview"), False),
            local_masks=masks,
        )
        return settings.sanitized()


@dataclass(slots=True, frozen=True)
class ImagePrepArtifacts:
    tonal_preview_image: Image.Image
    halftone_preview_image: Image.Image
    export_bmp_image: Image.Image
    effective_thresholds: list[int]
    image_width_px: int
    image_height_px: int
    image_width_mm: float
    image_height_mm: float


@dataclass(slots=True)
class ImagePrepState:
    source_image_path: Path | None = None
    settings: ImagePrepSettings = field(default_factory=ImagePrepSettings)
    artifacts: ImagePrepArtifacts | None = None
    export_bmp_path: Path | None = None
    sidecar_path: Path | None = None
    dirty: bool = False
    linked_to_control: bool = False

    def clear(self) -> None:
        self.source_image_path = None
        self.artifacts = None
        self.export_bmp_path = None
        self.sidecar_path = None
        self.dirty = False
        self.linked_to_control = False
        self.settings = ImagePrepSettings()


def _level_index_for_value(value: int, thresholds: list[int]) -> int:
    level_index = 0
    for threshold in thresholds:
        if value <= threshold:
            return level_index
        level_index += 1
    return level_index


def _layer_stride(*, layer_index: int, layer_count: int, strategy: PrepStrategy) -> int:
    if layer_count <= 1:
        return 1
    fraction = layer_index / max(layer_count - 1, 1)
    max_stride = layer_count + (4 if strategy == "relative" else 3)
    return max(1, int(round(1 + (max_stride - 1) * fraction)))


def _build_line_halftone_pixels(
    *,
    width: int,
    height: int,
    level_indices: list[int],
    layer_count: int,
    strategy: PrepStrategy,
) -> bytearray:
    output = bytearray([255] * (width * height))
    if layer_count <= 0:
        return output

    for layer_index in range(layer_count):
        stride = _layer_stride(layer_index=layer_index, layer_count=layer_count, strategy=strategy)
        phase = (layer_index * 2) % stride
        for row in range(height):
            row_start = row * width
            for col in range(phase, width, stride):
                idx = row_start + col
                current_level = level_indices[idx]
                if strategy == "banded":
                    should_draw = current_level == layer_index
                else:
                    should_draw = current_level <= layer_index
                if should_draw:
                    output[idx] = 0
    return output


def _circle_mask_image(
    *,
    size: tuple[int, int],
    prep_mask: ImagePrepMask,
) -> Image.Image:
    width, height = size
    span = max(1, min(width, height))
    radius_px = max(1.0, prep_mask.radius * span)
    center_x = prep_mask.center_x * max(width - 1, 1)
    center_y = prep_mask.center_y * max(height - 1, 1)
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        (
            center_x - radius_px,
            center_y - radius_px,
            center_x + radius_px,
            center_y + radius_px,
        ),
        fill=255,
    )
    feather_px = prep_mask.feather * span
    if feather_px > 0.0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=feather_px / 2.0))
    return mask


def _adjust_grayscale(
    image: Image.Image,
    *,
    exposure_percent: int,
    contrast_percent: int,
    blur_radius: float,
) -> Image.Image:
    adjusted = image
    if exposure_percent != 0:
        exposure_factor = max(0.0, 1.0 + (exposure_percent / 100.0))
        adjusted = ImageEnhance.Brightness(adjusted).enhance(exposure_factor)
    if contrast_percent != 0:
        contrast_factor = max(0.0, 1.0 + (contrast_percent / 100.0))
        adjusted = ImageEnhance.Contrast(adjusted).enhance(contrast_factor)
    if blur_radius > 0.0:
        adjusted = adjusted.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    return adjusted


def _apply_local_masks(
    *,
    base_image: Image.Image,
    source_image: Image.Image,
    masks: list[ImagePrepMask],
    global_exposure_percent: int,
    global_contrast_percent: int,
    global_blur_radius: float,
) -> Image.Image:
    if not masks:
        return base_image
    result = base_image.copy()
    for prep_mask in masks:
        mask = prep_mask.sanitized()
        if (
            mask.exposure_percent == global_exposure_percent
            and
            mask.contrast_percent == global_contrast_percent
            and abs(mask.blur_radius - global_blur_radius) < 0.001
        ):
            local_adjusted = base_image
        else:
            local_adjusted = _adjust_grayscale(
                source_image,
                exposure_percent=mask.exposure_percent,
                contrast_percent=mask.contrast_percent,
                blur_radius=mask.blur_radius,
            )
        alpha = _circle_mask_image(size=result.size, prep_mask=mask)
        result = Image.composite(local_adjusted, result, alpha)
    return result


def process_image_for_prep(
    *,
    image_path: Path,
    settings: ImagePrepSettings,
) -> tuple[ImagePrepSettings, ImagePrepArtifacts]:
    if not is_supported_source_image(image_path):
        raise ValueError("Only JPG/JPEG files are supported in Image Prep mode.")
    if not image_path.exists():
        raise FileNotFoundError(f"Source image does not exist: {image_path}")

    sanitized = settings.sanitized()
    thresholds = sanitized.effective_thresholds()

    with Image.open(image_path) as source:
        raw_grayscale = source.convert("L")
        grayscale = _adjust_grayscale(
            raw_grayscale,
            exposure_percent=sanitized.exposure_percent,
            contrast_percent=sanitized.contrast_percent,
            blur_radius=sanitized.blur_radius,
        )
        source_width_px, source_height_px = grayscale.size

        source_width_mm = (source_width_px / sanitized.dpi) * 25.4
        source_height_mm = (source_height_px / sanitized.dpi) * 25.4

        target_width_mm = sanitized.target_width_mm if sanitized.target_width_mm > 0.0 else source_width_mm
        target_height_mm = sanitized.target_height_mm if sanitized.target_height_mm > 0.0 else source_height_mm

        target_width_px = max(1, int((target_width_mm / 25.4) * sanitized.dpi))
        target_height_px = max(1, int((target_height_mm / 25.4) * sanitized.dpi))
        if (
            target_width_px > MAX_RENDER_SIDE_PX
            or target_height_px > MAX_RENDER_SIDE_PX
            or (target_width_px * target_height_px) > MAX_RENDER_PIXELS
        ):
            raise ValueError(
                "Requested prep render is too large. "
                f"Current target resolves to {target_width_px}x{target_height_px}px. "
                "Reduce dimensions or DPI."
            )
        if (target_width_px, target_height_px) != grayscale.size:
            grayscale = grayscale.resize((target_width_px, target_height_px), Image.Resampling.BILINEAR)
            raw_grayscale = raw_grayscale.resize((target_width_px, target_height_px), Image.Resampling.BILINEAR)

        grayscale = _apply_local_masks(
            base_image=grayscale,
            source_image=raw_grayscale,
            masks=sanitized.local_masks,
            global_exposure_percent=sanitized.exposure_percent,
            global_contrast_percent=sanitized.contrast_percent,
            global_blur_radius=sanitized.blur_radius,
        )

        width, height = grayscale.size
        values = list(grayscale.tobytes())

    levels = len(thresholds) + 1
    shades = [int(round((255.0 * idx) / max(levels - 1, 1))) for idx in range(levels)]
    level_indices: list[int] = []
    tonal_pixels = bytearray(width * height)
    for idx, value in enumerate(values):
        level_index = _level_index_for_value(value, thresholds)
        level_indices.append(level_index)
        tonal_pixels[idx] = shades[level_index]

    tonal_preview = Image.frombytes("L", (width, height), bytes(tonal_pixels))
    halftone_pixels = _build_line_halftone_pixels(
        width=width,
        height=height,
        level_indices=level_indices,
        layer_count=len(thresholds),
        strategy=sanitized.strategy,
    )
    halftone_preview = Image.frombytes("L", (width, height), bytes(halftone_pixels))
    export_bmp = halftone_preview.convert("RGB")

    width_mm = (width / sanitized.dpi) * 25.4
    height_mm = (height / sanitized.dpi) * 25.4
    sanitized.target_width_mm = target_width_mm
    sanitized.target_height_mm = target_height_mm

    if not sanitized.auto_thresholds:
        sanitized.manual_thresholds = list(thresholds)

    artifacts = ImagePrepArtifacts(
        tonal_preview_image=tonal_preview,
        halftone_preview_image=halftone_preview,
        export_bmp_image=export_bmp,
        effective_thresholds=list(thresholds),
        image_width_px=width,
        image_height_px=height,
        image_width_mm=width_mm,
        image_height_mm=height_mm,
    )
    return sanitized, artifacts


def save_processed_bmp(*, output_path: Path, image: Image.Image, dpi: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_dpi = max(1, int(dpi))
    image.save(output_path, format="BMP", dpi=(output_dpi, output_dpi))


def write_sidecar(
    *,
    sidecar_path: Path,
    source_image_path: Path,
    settings: ImagePrepSettings,
    effective_thresholds: list[int],
    export_bmp_path: Path | None,
) -> None:
    resolved_source = _safe_resolved_path(source_image_path)
    resolved_export = _safe_resolved_path(export_bmp_path) if export_bmp_path is not None else None
    payload: dict[str, Any] = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "saved_at_utc": _utc_now_iso(),
        "source_image_path": str(resolved_source),
        "source_image_name": source_image_path.name,
        "export_bmp_path": str(resolved_export) if resolved_export is not None else None,
        "settings": settings.to_dict(),
        "effective_thresholds": [int(value) for value in effective_thresholds],
    }
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_sidecar(
    sidecar_path: Path,
) -> tuple[Path, ImagePrepSettings, Path | None]:
    raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    source_raw = str(raw.get("source_image_path", "")).strip()
    if not source_raw:
        raise ValueError("Sidecar is missing source_image_path.")
    source_image_path = Path(source_raw)
    if not source_image_path.is_absolute():
        source_image_path = (sidecar_path.parent / source_image_path).resolve()
    settings_payload = raw.get("settings", {})
    if not isinstance(settings_payload, dict):
        settings_payload = {}
    settings = ImagePrepSettings.from_dict(settings_payload)
    export_raw = str(raw.get("export_bmp_path", "")).strip()
    export_bmp_path: Path | None = None
    if export_raw:
        candidate = Path(export_raw)
        if not candidate.is_absolute():
            candidate = (sidecar_path.parent / candidate).resolve()
        export_bmp_path = candidate
    return source_image_path, settings, export_bmp_path
