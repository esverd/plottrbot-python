from __future__ import annotations

from plottrbot.core.state_machine import UiState, derive_ui_state


def test_state_blank() -> None:
    state = derive_ui_state(has_image=False, is_sliced=False, usb_connected=False, is_drawing=False)
    assert state == UiState.BLANK


def test_state_bmp_loaded_usb_connected() -> None:
    state = derive_ui_state(has_image=True, is_sliced=False, usb_connected=True, is_drawing=False)
    assert state == UiState.BMP_LOADED_USB_CONNECTED


def test_state_bmp_sliced_usb_connected() -> None:
    state = derive_ui_state(has_image=True, is_sliced=True, usb_connected=True, is_drawing=False)
    assert state == UiState.BMP_SLICED_USB_CONNECTED


def test_state_bmp_drawing() -> None:
    state = derive_ui_state(has_image=True, is_sliced=True, usb_connected=True, is_drawing=True)
    assert state == UiState.BMP_DRAWING
