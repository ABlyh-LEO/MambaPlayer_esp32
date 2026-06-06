from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtWidgets

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_app.ui.main_window import MainWindow
from host_app.ui.theme import apply_dark_theme


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
