from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

try:
    import pyqtgraph as pg
    import pyqtgraph.exporters
except Exception:  # pragma: no cover
    pg = None

from ..telemetry.store import TelemetryStore


class WavePanel(QtWidgets.QWidget):
    snapshot_requested = QtCore.Signal()

    def __init__(self, store: TelemetryStore) -> None:
        super().__init__()
        self.store = store
        self.running = True
        self.follow_tail = True
        self.cursor_enabled = False
        self.cursor_a = None
        self.cursor_b = None
        self.curves: dict[str, object] = {}
        self.window_seconds = 10.0
        self._dirty = False
        self._last_snapshot_emit = 0
        self._build_ui()
        self.store.changed.connect(self.mark_dirty)
        self.store.channels_changed.connect(self.rebuild_curves)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(16)

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar = QtWidgets.QToolBar()
        self.toolbar.setIconSize(QtCore.QSize(16, 16))
        layout.addWidget(self.toolbar)

        self.run_action = self.toolbar.addAction("Pause")
        self.run_action.setCheckable(True)
        self.run_action.triggered.connect(self.toggle_run)
        self.toolbar.addAction("Clear", self.store.clear)
        self.toolbar.addSeparator()
        self.toolbar.addAction("Auto Y", self.auto_y)
        self.toolbar.addAction("Auto X/Y", self.auto_xy)
        self.follow_action = self.toolbar.addAction("Follow Tail")
        self.follow_action.setCheckable(True)
        self.follow_action.setChecked(True)
        self.follow_action.toggled.connect(lambda value: setattr(self, "follow_tail", value))
        self.toolbar.addSeparator()
        self.toolbar.addWidget(QtWidgets.QLabel("dt"))
        self.window_spin = QtWidgets.QDoubleSpinBox()
        self.window_spin.setRange(0.1, 3600)
        self.window_spin.setValue(self.window_seconds)
        self.window_spin.setSuffix(" s")
        self.window_spin.setDecimals(1)
        self.window_spin.valueChanged.connect(lambda value: setattr(self, "window_seconds", float(value)))
        self.toolbar.addWidget(self.window_spin)
        self.cursor_action = self.toolbar.addAction("Cursors")
        self.cursor_action.setCheckable(True)
        self.cursor_action.toggled.connect(self.set_cursors_enabled)
        self.toolbar.addAction("Export PNG", self.export_png)

        if pg is None:
            self.plot = QtWidgets.QPlainTextEdit("pyqtgraph missing")
            self.plot.setReadOnly(True)
            layout.addWidget(self.plot, 1)
            return

        pg.setConfigOptions(antialias=False, useOpenGL=False)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#070b10")
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setLabel("bottom", "dt", units="s")
        self.plot.setLabel("left", "Value")
        self.plot.addLegend(offset=(10, 10))
        self.plot.setMenuEnabled(True)
        self.plot.scene().sigMouseClicked.connect(self._mouse_clicked)
        layout.addWidget(self.plot, 1)

    def toggle_run(self, checked: bool) -> None:
        self.running = not checked
        self.run_action.setText("Run" if checked else "Pause")

    def set_cursors_enabled(self, enabled: bool) -> None:
        self.cursor_enabled = enabled
        if not enabled:
            self._remove_cursors()

    def rebuild_curves(self) -> None:
        if pg is None:
            return
        existing = set(self.curves)
        for key in existing - set(self.store.channels):
            self.plot.removeItem(self.curves.pop(key))
        for key, channel in self.store.channels.items():
            if key not in self.curves:
                curve = self.plot.plot(
                    pen=pg.mkPen(channel.color, width=1.4),
                    name=channel.name,
                    skipFiniteCheck=True,
                )
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self.curves[key] = curve
            else:
                self.curves[key].setPen(pg.mkPen(channel.color, width=1.4))
                self.curves[key].opts["name"] = channel.name
        self._dirty = True

    def mark_dirty(self) -> None:
        self._dirty = True

    def refresh(self) -> None:
        if not self.running or pg is None or not self._dirty:
            return
        self._dirty = False
        latest = None
        for key, channel in self.store.channels.items():
            curve = self.curves.get(key)
            if curve is None:
                continue
            if not channel.enabled:
                curve.setData([], [])
                continue
            t, v = channel.tail_arrays(self.window_seconds if self.follow_tail else None)
            if t.size:
                latest = max(float(t[-1]), latest or float(t[-1]))
                curve.setData(t, v)
            else:
                curve.setData([], [])
        if self.follow_tail and latest is not None:
            self.plot.setXRange(max(0.0, latest - self.window_seconds), latest, padding=0)
        now = QtCore.QDateTime.currentMSecsSinceEpoch()
        if now - self._last_snapshot_emit >= 1000:
            self._last_snapshot_emit = now
            self.snapshot_requested.emit()

    def auto_y(self) -> None:
        if pg is None:
            return
        view = self.plot.getViewBox()
        xmin, xmax = view.viewRange()[0]
        ymin = None
        ymax = None
        for channel in self.store.channels.values():
            if not channel.enabled:
                continue
            _, values = channel.visible_arrays(xmin, xmax)
            if values.size == 0:
                continue
            cmin = float(values.min())
            cmax = float(values.max())
            ymin = cmin if ymin is None else min(ymin, cmin)
            ymax = cmax if ymax is None else max(ymax, cmax)
        if ymin is not None and ymax is not None:
            if ymin == ymax:
                ymin -= 1
                ymax += 1
            pad = (ymax - ymin) * 0.08
            self.plot.setYRange(ymin - pad, ymax + pad, padding=0)

    def auto_xy(self) -> None:
        latest = None
        for channel in self.store.channels.values():
            t, _ = channel.arrays()
            if t.size:
                latest = max(float(t[-1]), latest or float(t[-1]))
        if latest is not None and pg is not None:
            self.plot.setXRange(max(0.0, latest - self.window_seconds), latest, padding=0)
            self.auto_y()

    def export_png(self) -> None:
        if pg is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export Waveform", "mamba_wave.png", "PNG (*.png)")
        if not path:
            return
        exporter = pg.exporters.ImageExporter(self.plot.getPlotItem())
        exporter.export(path)

    def _mouse_clicked(self, event) -> None:
        if pg is None or not self.cursor_enabled or event.button() != QtCore.Qt.LeftButton or not event.double():
            return
        pos = self.plot.getPlotItem().vb.mapSceneToView(event.scenePos())
        if self.cursor_a is None:
            self.cursor_a = pg.InfiniteLine(pos.x(), angle=90, movable=True, pen=pg.mkPen("#ffcc00", width=1))
            self.plot.addItem(self.cursor_a)
        elif self.cursor_b is None:
            self.cursor_b = pg.InfiniteLine(pos.x(), angle=90, movable=True, pen=pg.mkPen("#ff4f81", width=1))
            self.plot.addItem(self.cursor_b)
        else:
            self._remove_cursors()

    def _remove_cursors(self) -> None:
        if pg is None:
            return
        for cursor in (self.cursor_a, self.cursor_b):
            if cursor is not None:
                self.plot.removeItem(cursor)
        self.cursor_a = None
        self.cursor_b = None

    def measurements(self) -> dict:
        if self.cursor_a is None or self.cursor_b is None:
            return {}
        xa = float(self.cursor_a.value())
        xb = float(self.cursor_b.value())
        result = {"xa": xa, "xb": xb, "dt": abs(xb - xa), "channels": []}
        for channel in self.store.channels.values():
            if not channel.enabled:
                continue
            t, v = channel.arrays()
            if t.size == 0:
                continue
            ia = int(abs(t - xa).argmin())
            ib = int(abs(t - xb).argmin())
            result["channels"].append({
                "name": channel.name,
                "dy": float(v[ib] - v[ia]),
                "a": float(v[ia]),
                "b": float(v[ib]),
            })
        return result

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "follow_tail": self.follow_tail,
            "window_seconds": self.window_seconds,
            "cursor_enabled": self.cursor_enabled,
            "measurements": self.measurements(),
        }
