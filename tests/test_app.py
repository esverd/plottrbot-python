from __future__ import annotations

from plottrbot.app import _parse_launch_args


def test_parse_dummy_serial_preserves_qt_args() -> None:
    launch_args, qt_args = _parse_launch_args(["--dummy-serial", "--style", "Fusion"])

    assert launch_args.dummy_serial is True
    assert qt_args == ["--style", "Fusion"]


def test_parse_default_launch_args() -> None:
    launch_args, qt_args = _parse_launch_args([])

    assert launch_args.dummy_serial is False
    assert qt_args == []
