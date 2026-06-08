from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtWidgets

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_app.ui.main_window import MainWindow
from host_app.ui.theme import apply_light_theme


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    apply_light_theme(app)
    window = MainWindow()
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
