from __future__ import annotations

from enum import Enum


class UiState(str, Enum):
    BLANK = "blank"
    BMP_LOADED = "bmp_loaded"
    BMP_SLICED = "bmp_sliced"
    USB_CONNECTED = "usb_connected"
    BMP_LOADED_USB_CONNECTED = "bmp_loaded_usb_connected"
    BMP_SLICED_USB_CONNECTED = "bmp_sliced_usb_connected"
    BMP_DRAWING = "bmp_drawing"


def derive_ui_state(
    *,
    has_image: bool,
    is_sliced: bool,
    usb_connected: bool,
    is_drawing: bool,
) -> UiState:
    if usb_connected and is_drawing and has_image and is_sliced:
        return UiState.BMP_DRAWING
    if usb_connected and has_image and is_sliced:
        return UiState.BMP_SLICED_USB_CONNECTED
    if usb_connected and has_image:
        return UiState.BMP_LOADED_USB_CONNECTED
    if usb_connected:
        return UiState.USB_CONNECTED
    if has_image and is_sliced:
        return UiState.BMP_SLICED
    if has_image:
        return UiState.BMP_LOADED
    return UiState.BLANK
