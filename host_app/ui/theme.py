from __future__ import annotations

from PySide6 import QtWidgets


LIGHT_STYLE = """
QMainWindow, QWidget {
    background: #f5f7fb;
    color: #172033;
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 12px;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #c8d1df;
    border-radius: 5px;
    min-height: 26px;
    padding: 4px 10px;
}
QPushButton:hover { background: #eef4ff; border-color: #7aa7e8; }
QPushButton:checked { background: #2563eb; color: white; border-color: #2563eb; }
QLineEdit, QComboBox, QSpinBox {
    background: #ffffff;
    border: 1px solid #c8d1df;
    border-radius: 5px;
    min-height: 24px;
    padding: 2px 6px;
}
QTableWidget, QPlainTextEdit {
    background: #ffffff;
    alternate-background-color: #f0f4fa;
    border: 1px solid #d8e0ec;
    gridline-color: #e1e7f0;
    selection-background-color: #cfe2ff;
}
QHeaderView::section {
    background: #e9eef6;
    color: #46566f;
    border: 0;
    border-right: 1px solid #d8e0ec;
    border-bottom: 1px solid #d8e0ec;
    padding: 5px;
}
QLabel { color: #2b384f; }
"""

DARK_STYLE = """
QMainWindow, QWidget {
    background: #11161d;
    color: #d7e3ef;
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 12px;
}
QPushButton {
    background: #202b37;
    border: 1px solid #344457;
    border-radius: 5px;
    color: #d7e3ef;
    min-height: 26px;
    padding: 4px 10px;
}
QPushButton:hover { background: #2a3848; }
QPushButton:checked { background: #0d6efd; border-color: #3b92ff; }
QLineEdit, QComboBox, QSpinBox {
    background: #0c1117;
    border: 1px solid #344457;
    border-radius: 5px;
    color: #d7e3ef;
    min-height: 24px;
    padding: 2px 6px;
}
QTableWidget, QPlainTextEdit {
    background: #0c1117;
    alternate-background-color: #121a23;
    border: 1px solid #263241;
    color: #d7e3ef;
    gridline-color: #263241;
    selection-background-color: #25496e;
}
QHeaderView::section {
    background: #18202a;
    color: #a9bbcc;
    border: 0;
    border-right: 1px solid #263241;
    border-bottom: 1px solid #263241;
    padding: 5px;
}
QLabel { color: #d7e3ef; }
"""


def apply_light_theme(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(LIGHT_STYLE)


def apply_dark_theme(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)
