from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from plottrbot.core.models import BoundingBox, MachineProfile, TraceLine


@dataclass(slots=True)
class ImagePlacement:
    image: QImage
    x_mm: int
    y_mm: int
    width_mm: float
    height_mm: float


class PreviewCanvas(QWidget):
    def __init__(self, machine_profile: MachineProfile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.machine_profile = machine_profile
        self.scale = 1.0
        self.render_mode = "image"

        self._primary_image: ImagePlacement | None = None
        self._retained_image: ImagePlacement | None = None
        self._trace_lines: list[TraceLine] = []
        self._selected_line_index = -1
        self._bbox_overlay: BoundingBox | None = None
        self._show_bbox_overlay = False

        self.setAutoFillBackground(True)
        self._sync_canvas_size()

    def _sync_canvas_size(self) -> None:
        width_px = int(round(self.machine_profile.canvas_width_mm * self.scale))
        height_px = int(round(self.machine_profile.canvas_height_mm * self.scale))
        self.setMinimumSize(width_px, height_px)
        self.resize(width_px, height_px)
        self.update()

    def set_machine_profile(self, profile: MachineProfile) -> None:
        self.machine_profile = profile
        self._sync_canvas_size()

    def set_scale(self, scale: float) -> None:
        self.scale = max(0.1, scale)
        self._sync_canvas_size()

    def zoom(self, factor: float) -> None:
        self.set_scale(self.scale * factor)

    def set_render_mode(self, mode: str) -> None:
        self.render_mode = mode
        self.update()

    def set_primary_image(
        self,
        *,
        image_path: str,
        x_mm: int,
        y_mm: int,
        width_mm: float,
        height_mm: float,
    ) -> None:
        image = QImage(image_path)
        if image.isNull():
            self._primary_image = None
        else:
            self._primary_image = ImagePlacement(image, x_mm, y_mm, width_mm, height_mm)
        self.update()

    def clear_primary_image(self) -> None:
        self._primary_image = None
        self.update()

    def set_retained_image(
        self,
        *,
        image_path: str,
        x_mm: int,
        y_mm: int,
        width_mm: float,
        height_mm: float,
    ) -> None:
        image = QImage(image_path)
        if image.isNull():
            self._retained_image = None
        else:
            self._retained_image = ImagePlacement(image, x_mm, y_mm, width_mm, height_mm)
        self.update()

    def clear_retained_image(self) -> None:
        self._retained_image = None
        self.update()

    def set_trace_lines(self, lines: list[TraceLine]) -> None:
        self._trace_lines = list(lines)
        self.update()

    def clear_trace_lines(self) -> None:
        self._trace_lines = []
        self._selected_line_index = -1
        self.update()

    def set_selected_line(self, line_index: int) -> None:
        self._selected_line_index = line_index
        self.update()

    @property
    def selected_line_index(self) -> int:
        return self._selected_line_index

    def set_bbox_overlay(self, bbox: BoundingBox | None, visible: bool = True) -> None:
        self._bbox_overlay = bbox
        self._show_bbox_overlay = visible and bbox is not None
        self.update()

    def clear_bbox_overlay(self) -> None:
        self._show_bbox_overlay = False
        self.update()

    def clear_all(self) -> None:
        self._primary_image = None
        self._retained_image = None
        self._trace_lines = []
        self._selected_line_index = -1
        self._bbox_overlay = None
        self._show_bbox_overlay = False
        self.render_mode = "image"
        self.update()

    def _mm_to_px(self, value: float) -> float:
        return value * self.scale

    def _draw_image(self, painter: QPainter, placement: ImagePlacement) -> None:
        target = QRectF(
            self._mm_to_px(placement.x_mm),
            self._mm_to_px(placement.y_mm),
            self._mm_to_px(placement.width_mm),
            self._mm_to_px(placement.height_mm),
        )
        painter.drawImage(target, placement.image)

    def paintEvent(self, _: object) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.white)

        border_pen = QPen(QColor(90, 90, 90), 2)
        painter.setPen(border_pen)
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)

        if self._retained_image is not None:
            self._draw_image(painter, self._retained_image)

        if self.render_mode == "image" and self._primary_image is not None:
            self._draw_image(painter, self._primary_image)

        if self.render_mode == "lines":
            for line in self._trace_lines:
                pen_color = QColor(0, 0, 0) if line.draw else QColor(230, 230, 230)
                painter.setPen(QPen(pen_color, 1))
                painter.drawLine(
                    QPointF(self._mm_to_px(line.x0), self._mm_to_px(line.y0)),
                    QPointF(self._mm_to_px(line.x1), self._mm_to_px(line.y1)),
                )

            if 0 <= self._selected_line_index < len(self._trace_lines):
                selected_line = self._trace_lines[self._selected_line_index]
                painter.setPen(QPen(QColor(220, 30, 30), 2))
                painter.drawLine(
                    QPointF(self._mm_to_px(selected_line.x0), self._mm_to_px(selected_line.y0)),
                    QPointF(self._mm_to_px(selected_line.x1), self._mm_to_px(selected_line.y1)),
                )

        if self._show_bbox_overlay and self._bbox_overlay is not None:
            painter.setPen(QPen(QColor(220, 30, 30), 2))
            x0 = self._mm_to_px(self._bbox_overlay.min_x)
            y0 = self._mm_to_px(self._bbox_overlay.min_y)
            x1 = self._mm_to_px(self._bbox_overlay.max_x)
            y1 = self._mm_to_px(self._bbox_overlay.max_y)
            painter.drawLine(QPointF(x0, y0), QPointF(x1, y0))
            painter.drawLine(QPointF(x1, y0), QPointF(x1, y1))
            painter.drawLine(QPointF(x1, y1), QPointF(x0, y1))
            painter.drawLine(QPointF(x0, y1), QPointF(x0, y0))
