from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPixmap

from plottrbot.core.image_prep import ImagePrepCrop, ImagePrepMask
from plottrbot.ui.prep_preview_widget import PrepPreviewWidget


def test_prep_preview_widget_drags_mask_in_normalized_image_space(qtbot) -> None:
    widget = PrepPreviewWidget()
    pixmap = QPixmap(100, 100)
    pixmap.fill(QColor("white"))
    widget.setPixmap(pixmap)
    widget.setFixedSize(100, 100)
    widget.set_masks([ImagePrepMask(center_x=0.5, center_y=0.5, radius=0.2)], 0)
    qtbot.addWidget(widget)
    widget.show()

    moved: list[tuple[int, float, float]] = []
    selected: list[int] = []
    widget.maskMoved.connect(lambda index, x, y: moved.append((index, x, y)))
    widget.maskSelected.connect(selected.append)

    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
    qtbot.mouseMove(widget, QPoint(75, 25))
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(75, 25))

    assert selected == [0]
    assert moved
    index, center_x, center_y = moved[-1]
    assert index == 0
    assert center_x == 0.75
    assert center_y == 0.25


def test_prep_preview_widget_crop_edit_mode_drags_crop_without_selecting_masks(qtbot) -> None:
    widget = PrepPreviewWidget()
    pixmap = QPixmap(100, 100)
    pixmap.fill(QColor("white"))
    widget.setPixmap(pixmap)
    widget.setFixedSize(100, 100)
    widget.set_crop(ImagePrepCrop(enabled=True, center_x=0.5, center_y=0.5, width=0.5, height=0.5), True)
    widget.set_masks([ImagePrepMask(center_x=0.5, center_y=0.5, width=0.5, height=0.5)], 0)
    qtbot.addWidget(widget)
    widget.show()

    moved: list[tuple[float, float]] = []
    selected: list[int] = []
    widget.cropMoved.connect(lambda x, y: moved.append((x, y)))
    widget.maskSelected.connect(selected.append)

    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
    qtbot.mouseMove(widget, QPoint(75, 25))
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(75, 25))

    assert selected == []
    assert moved
    center_x, center_y = moved[-1]
    assert center_x == 0.75
    assert center_y == 0.25
