from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QTransform
from PySide6.QtWidgets import QLabel, QWidget

from plottrbot.core.image_prep import ImagePrepCrop, ImagePrepMask


class PrepPreviewWidget(QLabel):
    maskSelected = Signal(int)
    maskMoved = Signal(int, float, float)
    cropMoved = Signal(float, float)

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._crop = ImagePrepCrop()
        self._crop_edit_enabled = False
        self._crop_dragging = False
        self._masks: list[ImagePrepMask] = []
        self._selected_index = -1
        self._drag_index = -1
        self.setMouseTracking(True)

    def set_crop(self, crop: ImagePrepCrop, edit_enabled: bool) -> None:
        self._crop = crop.sanitized()
        self._crop_edit_enabled = edit_enabled and self._crop.enabled
        if not self._crop_edit_enabled:
            self._crop_dragging = False
        self.update()

    def clear_crop(self) -> None:
        self._crop = ImagePrepCrop()
        self._crop_edit_enabled = False
        self._crop_dragging = False
        self.update()

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

    def _crop_geometry(self) -> QRectF:
        rect = self._pixmap_rect()
        crop_width = max(4.0, self._crop.width * rect.width())
        crop_height = max(4.0, self._crop.height * rect.height())
        center = QPointF(
            rect.left() + (self._crop.center_x * rect.width()),
            rect.top() + (self._crop.center_y * rect.height()),
        )
        return QRectF(
            center.x() - (crop_width / 2.0),
            center.y() - (crop_height / 2.0),
            crop_width,
            crop_height,
        )

    def _crop_at(self, point: QPointF) -> bool:
        if not self._crop.enabled or self._pixmap_rect().isEmpty():
            return False
        return self._crop_geometry().adjusted(-6.0, -6.0, 6.0, 6.0).contains(point)

    def _mask_geometry(self, mask: ImagePrepMask) -> tuple[QPointF, QRectF, float]:
        rect = self._pixmap_rect()
        center = QPointF(
            rect.left() + (mask.center_x * rect.width()),
            rect.top() + (mask.center_y * rect.height()),
        )
        mask_width = max(8.0, mask.width * rect.width())
        mask_height = max(8.0, mask.height * rect.height())
        mask_rect = QRectF(
            center.x() - (mask_width / 2.0),
            center.y() - (mask_height / 2.0),
            mask_width,
            mask_height,
        )
        corner_radius = (min(mask_width, mask_height) / 2.0) * (mask.roundness_percent / 100.0)
        return center, mask_rect, max(0.0, corner_radius)

    def _mask_at(self, point: QPointF) -> int:
        if self._pixmap_rect().isEmpty():
            return -1
        for index in range(len(self._masks) - 1, -1, -1):
            center, mask_rect, _corner_radius = self._mask_geometry(self._masks[index])
            transform = QTransform()
            transform.translate(center.x(), center.y())
            transform.rotate(self._masks[index].rotation_degrees)
            transform.translate(-center.x(), -center.y())
            inverse, invertible = transform.inverted()
            test_point = inverse.map(point) if invertible else point
            if mask_rect.adjusted(-6.0, -6.0, 6.0, 6.0).contains(test_point):
                return index
        return -1

    def _normalized_point(self, point: QPointF) -> tuple[float, float]:
        rect = self._pixmap_rect()
        if rect.isEmpty():
            return 0.5, 0.5
        x = (point.x() - rect.left()) / max(rect.width(), 1.0)
        y = (point.y() - rect.top()) / max(rect.height(), 1.0)
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    def _normalized_crop_center(self, point: QPointF) -> tuple[float, float]:
        x, y = self._normalized_point(point)
        half_width = self._crop.width / 2.0
        half_height = self._crop.height / 2.0
        if half_width >= 0.5:
            x = 0.5
        else:
            x = max(half_width, min(1.0 - half_width, x))
        if half_height >= 0.5:
            y = 0.5
        else:
            y = max(half_height, min(1.0 - half_height, y))
        return x, y

    def paintEvent(self, event: object) -> None:  # noqa: N802 (Qt API)
        super().paintEvent(event)
        if not self._masks and not self._crop.enabled:
            return
        rect = self._pixmap_rect()
        if rect.isEmpty():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._crop.enabled:
            crop_color = QColor(214, 107, 0, 230) if self._crop_edit_enabled else QColor(214, 107, 0, 150)
            painter.setPen(QPen(crop_color, 3 if self._crop_edit_enabled else 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._crop_geometry())
        for index, mask in enumerate(self._masks):
            center, mask_rect, corner_radius = self._mask_geometry(mask)
            selected = index == self._selected_index
            color = QColor(21, 111, 191, 230) if selected else QColor(70, 86, 106, 170)
            painter.setPen(QPen(color, 3 if selected else 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.save()
            painter.translate(center)
            painter.rotate(mask.rotation_degrees)
            painter.translate(-center.x(), -center.y())
            painter.drawRoundedRect(mask_rect, corner_radius, corner_radius)
            painter.restore()
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(center.x() - 7, center.y()), QPointF(center.x() + 7, center.y()))
            painter.drawLine(QPointF(center.x(), center.y() - 7), QPointF(center.x(), center.y() + 7))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        point = event.position()
        if self._crop_edit_enabled and event.button() == Qt.MouseButton.LeftButton:
            if self._crop_at(point):
                self._crop_dragging = True
                event.accept()
                return
            event.accept()
            return
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
        if self._crop_dragging and event.buttons() & Qt.MouseButton.LeftButton:
            center_x, center_y = self._normalized_crop_center(event.position())
            self._crop = replace(self._crop, center_x=center_x, center_y=center_y).sanitized()
            self.cropMoved.emit(center_x, center_y)
            self.update()
            event.accept()
            return
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
        if self._crop_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._crop_dragging = False
            event.accept()
            return
        if self._drag_index >= 0 and event.button() == Qt.MouseButton.LeftButton:
            self._drag_index = -1
            event.accept()
            return
        super().mouseReleaseEvent(event)
