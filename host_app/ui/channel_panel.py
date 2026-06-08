from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ..telemetry.store import TelemetryStore


class ChannelPanel(QtWidgets.QWidget):
    def __init__(self, store: TelemetryStore) -> None:
        super().__init__()
        self.store = store
        self._build_ui()
        self.store.channels_changed.connect(self.refresh)
        self.store.changed.connect(self.refresh_values)

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["", "Channel", "Value", "Min", "Max", "Drop"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(self._edit_color)
        layout.addWidget(self.tree, 1)
        hint = QtWidgets.QLabel("Double-click a row to change color. Check boxes bind channels to the wave panel.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8191a3")
        layout.addWidget(hint)

    def refresh(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for channel in self.store.channels.values():
            item = QtWidgets.QTreeWidgetItem([
                "",
                channel.name,
                self._format_value(channel.stats.latest, channel.unit),
                self._format_value(channel.stats.min_value, channel.unit),
                self._format_value(channel.stats.max_value, channel.unit),
                str(channel.stats.dropped),
            ])
            item.setData(0, QtCore.Qt.UserRole, channel.key)
            item.setCheckState(0, QtCore.Qt.Checked if channel.enabled else QtCore.Qt.Unchecked)
            item.setForeground(1, QtGui.QBrush(QtGui.QColor(channel.color)))
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)
        self.tree.blockSignals(False)

    def refresh_values(self) -> None:
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            key = item.data(0, QtCore.Qt.UserRole)
            channel = self.store.channels.get(key)
            if channel is None:
                continue
            item.setText(2, self._format_value(channel.stats.latest, channel.unit))
            item.setText(3, self._format_value(channel.stats.min_value, channel.unit))
            item.setText(4, self._format_value(channel.stats.max_value, channel.unit))
            item.setText(5, str(channel.stats.dropped))

    def _item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        key = item.data(0, QtCore.Qt.UserRole)
        self.store.set_channel_enabled(key, item.checkState(0) == QtCore.Qt.Checked)

    def _edit_color(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        key = item.data(0, QtCore.Qt.UserRole)
        channel = self.store.channels.get(key)
        if channel is None:
            return
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(channel.color), self, "Channel Color")
        if color.isValid():
            self.store.set_channel_color(key, color.name())

    @staticmethod
    def _format_value(value: float, unit: str) -> str:
        suffix = f" {unit}" if unit else ""
        return f"{value:.3f}{suffix}"
