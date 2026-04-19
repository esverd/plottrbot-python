from __future__ import annotations

import math
from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QLabel, QWidget

from plottrbot.core.image_prep import ImagePrepMask


class PrepPreviewWidget(QLabel):
    maskSelected = Signal(int)
    maskMoved = Signal(int, float, float)

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._masks: list[ImagePrepMask] = []
        self._selected_index = -1
        self._drag_index = -1
        self.setMouseTracking(True)

    def set_masks(self, masks: list[ImagePrepMask], selected_index: int) -> None:
        self._masks = [mask.sanitized() for mask in masks]
        self._selected_index = selected_index if 0 <= selected_index < len(self._masks) else -1
        self.update()

    def clear_masks(self) -> None:
        self._masks = []
        self._selected_index = -1
        self._drag_index = -1
        self.update()

    def _pixmap_rect(self) -> QRectF:
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            return QRectF()
        size = pixmap.size()
        x = (self.width() - size.width()) / 2.0
        y = (self.height() - size.height()) / 2.0
        return QRectF(x, y, size.width(), size.height())

    def _mask_geometry(self, mask: ImagePrepMask) -> tuple[QPointF, float]:
        rect = self._pixmap_rect()
        span = max(1.0, min(rect.width(), rect.height()))
        center = QPointF(
            rect.left() + (mask.center_x * rect.width()),
            rect.top() + (mask.center_y * rect.height()),
        )
        return center, max(4.0, mask.radius * span)

    def _mask_at(self, point: QPointF) -> int:
        if self._pixmap_rect().isEmpty():
            return -1
        for index in range(len(self._masks) - 1, -1, -1):
            center, radius = self._mask_geometry(self._masks[index])
            distance = math.hypot(point.x() - center.x(), point.y() - center.y())
            if distance <= radius + 6.0:
                return index
        return -1

    def _normalized_point(self, point: QPointF) -> tuple[float, float]:
        rect = self._pixmap_rect()
        if rect.isEmpty():
            return 0.5, 0.5
        x = (point.x() - rect.left()) / max(rect.width(), 1.0)
        y = (point.y() - rect.top()) / max(rect.height(), 1.0)
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    def paintEvent(self, event: object) -> None:  # noqa: N802 (Qt API)
        super().paintEvent(event)
        if not self._masks:
            return
        rect = self._pixmap_rect()
        if rect.isEmpty():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for index, mask in enumerate(self._masks):
            center, radius = self._mask_geometry(mask)
            selected = index == self._selected_index
            color = QColor(21, 111, 191, 230) if selected else QColor(70, 86, 106, 170)
            painter.setPen(QPen(color, 3 if selected else 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius, radius)
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(center.x() - 7, center.y()), QPointF(center.x() + 7, center.y()))
            painter.drawLine(QPointF(center.x(), center.y() - 7), QPointF(center.x(), center.y() + 7))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        point = event.position()
        index = self._mask_at(point)
        if index >= 0 and event.button() == Qt.MouseButton.LeftButton:
            self._selected_index = index
            self._drag_index = index
            self.maskSelected.emit(index)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if self._drag_index >= 0 and event.buttons() & Qt.MouseButton.LeftButton:
            center_x, center_y = self._normalized_point(event.position())
            self._masks[self._drag_index] = replace(
                self._masks[self._drag_index],
                center_x=center_x,
                center_y=center_y,
            )
            self.maskMoved.emit(self._drag_index, center_x, center_y)
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if self._drag_index >= 0 and event.button() == Qt.MouseButton.LeftButton:
            self._drag_index = -1
            event.accept()
            return
        super().mouseReleaseEvent(event)
