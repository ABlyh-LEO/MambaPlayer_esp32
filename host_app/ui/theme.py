from __future__ import annotations

from PySide6 import QtWidgets


STYLE = """
QMainWindow, QWidget {
    background: #11161d;
    color: #d7e3ef;
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 12px;
}
QDockWidget {
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}
QDockWidget::title {
    background: #18202a;
    padding: 6px 8px;
    border-bottom: 1px solid #263241;
    color: #a9bbcc;
}
QToolBar {
    background: #151c25;
    border: 0;
    border-bottom: 1px solid #263241;
    spacing: 6px;
    padding: 4px;
}
QToolButton, QPushButton {
    background: #202b37;
    border: 1px solid #344457;
    border-radius: 4px;
    color: #d7e3ef;
    min-height: 26px;
    padding: 3px 9px;
}
QToolButton:hover, QPushButton:hover {
    background: #2a3848;
}
QToolButton:checked, QPushButton:checked {
    background: #0d6efd;
    border-color: #3b92ff;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #0c1117;
    border: 1px solid #344457;
    border-radius: 4px;
    color: #d7e3ef;
    min-height: 24px;
    padding: 2px 6px;
}
QTableWidget, QTreeWidget, QPlainTextEdit {
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
QTabWidget::pane {
    border: 1px solid #263241;
}
QTabBar::tab {
    background: #151c25;
    color: #a9bbcc;
    padding: 7px 12px;
}
QTabBar::tab:selected {
    background: #202b37;
    color: #ffffff;
}
QStatusBar {
    background: #0c1117;
    border-top: 1px solid #263241;
    color: #a9bbcc;
}
"""


def apply_dark_theme(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
