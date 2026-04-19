from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from plottrbot.ui.main_window import MainWindow


def _parse_launch_args(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="warhol-slicer",
        description="Launch the Warhol Slicer desktop app.",
    )
    parser.add_argument(
        "--dummy-serial",
        action="store_true",
        help="show a fake serial port that acknowledges commands without hardware",
    )
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parsed_args, qt_args = parser.parse_known_args(raw_args)
    return parsed_args, qt_args


def main(argv: Sequence[str] | None = None) -> int:
    launch_args, qt_args = _parse_launch_args(argv)
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Warhol Slicer")
    window = MainWindow(dummy_serial=launch_args.dummy_serial)
    window.show()
    return app.exec()


def dummy_serial_main() -> int:
    return main(["--dummy-serial"])
