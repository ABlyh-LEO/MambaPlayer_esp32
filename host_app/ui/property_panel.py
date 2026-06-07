from __future__ import annotations

import json

from PySide6 import QtCore, QtWidgets

from ..telemetry.store import TelemetryStore
from .wave_panel import WavePanel


class PropertyPanel(QtWidgets.QWidget):
    wifi_requested = QtCore.Signal(str, str)
    status_requested = QtCore.Signal()
    mock_toggled = QtCore.Signal(bool)
    serial_connect_requested = QtCore.Signal(str)
    audio_upload_requested = QtCore.Signal(str, str)
    audio_test_requested = QtCore.Signal(str)

    def __init__(self, store: TelemetryStore, wave: WavePanel) -> None:
        super().__init__()
        self.store = store
        self.wave = wave
        self._build_ui()
        self.store.state_changed.connect(self.refresh)
        self.wave.snapshot_requested.connect(self.refresh_measurements)

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.connection = QtWidgets.QGroupBox("Connection")
        conn = QtWidgets.QFormLayout(self.connection)
        self.tcp_label = QtWidgets.QLabel("waiting")
        self.udp_label = QtWidgets.QLabel("none")
        self.usb_label = QtWidgets.QLabel("disconnected")
        self.serial_combo = QtWidgets.QComboBox()
        self.refresh_ports()
        self.serial_button = QtWidgets.QPushButton("Connect USB")
        self.serial_button.clicked.connect(lambda: self.serial_connect_requested.emit(self.serial_combo.currentData() or ""))
        self.status_button = QtWidgets.QPushButton("Request Status")
        self.status_button.clicked.connect(self.status_requested.emit)
        conn.addRow("TCP", self.tcp_label)
        conn.addRow("UDP Hello", self.udp_label)
        conn.addRow("USB", self.usb_label)
        conn.addRow("Port", self.serial_combo)
        conn.addRow("", self.serial_button)
        conn.addRow("", self.status_button)
        layout.addWidget(self.connection)

        self.wifi = QtWidgets.QGroupBox("Wi-Fi Provisioning")
        wifi = QtWidgets.QFormLayout(self.wifi)
        self.ssid = QtWidgets.QLineEdit()
        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.wifi_button = QtWidgets.QPushButton("Send to Device")
        self.wifi_button.clicked.connect(lambda: self.wifi_requested.emit(self.ssid.text(), self.password.text()))
        wifi.addRow("SSID", self.ssid)
        wifi.addRow("Password", self.password)
        wifi.addRow("", self.wifi_button)
        layout.addWidget(self.wifi)

        self.audio = QtWidgets.QGroupBox("Audio")
        audio = QtWidgets.QVBoxLayout(self.audio)
        upload_row = QtWidgets.QHBoxLayout()
        self.power_upload_button = QtWidgets.QPushButton("Upload Power-On")
        self.alarm_upload_button = QtWidgets.QPushButton("Upload Alarm")
        self.power_upload_button.clicked.connect(lambda: self._pick_audio("poweron"))
        self.alarm_upload_button.clicked.connect(lambda: self._pick_audio("alarm"))
        upload_row.addWidget(self.power_upload_button)
        upload_row.addWidget(self.alarm_upload_button)
        test_row = QtWidgets.QHBoxLayout()
        self.power_test_button = QtWidgets.QPushButton("Play Power-On")
        self.alarm_test_button = QtWidgets.QPushButton("Play Alarm")
        self.audio_stop_button = QtWidgets.QPushButton("Stop")
        self.power_test_button.clicked.connect(lambda: self.audio_test_requested.emit("power"))
        self.alarm_test_button.clicked.connect(lambda: self.audio_test_requested.emit("alarm"))
        self.audio_stop_button.clicked.connect(lambda: self.audio_test_requested.emit("stop"))
        test_row.addWidget(self.power_test_button)
        test_row.addWidget(self.alarm_test_button)
        test_row.addWidget(self.audio_stop_button)
        self.audio_status = QtWidgets.QLabel("16 kHz mono IMA ADPCM on device")
        audio.addLayout(upload_row)
        audio.addLayout(test_row)
        audio.addWidget(self.audio_status)
        layout.addWidget(self.audio)

        self.measure_box = QtWidgets.QGroupBox("Cursor Measurement")
        measure_layout = QtWidgets.QVBoxLayout(self.measure_box)
        self.measure_text = QtWidgets.QPlainTextEdit()
        self.measure_text.setReadOnly(True)
        self.measure_text.setMaximumHeight(180)
        measure_layout.addWidget(self.measure_text)
        layout.addWidget(self.measure_box)

        self.mock_button = QtWidgets.QPushButton("Start Mock")
        self.mock_button.setCheckable(True)
        self.mock_button.toggled.connect(self._mock_changed)
        layout.addWidget(self.mock_button)
        layout.addStretch(1)

    def refresh_ports(self) -> None:
        self.serial_combo.clear()
        try:
            import serial.tools.list_ports

            for port in serial.tools.list_ports.comports():
                self.serial_combo.addItem(f"{port.device} {port.description}", port.device)
        except Exception:
            self.serial_combo.addItem("pyserial unavailable", "")

    def select_serial_port(self, port_name: str) -> None:
        for index in range(self.serial_combo.count()):
            if self.serial_combo.itemData(index) == port_name:
                self.serial_combo.setCurrentIndex(index)
                return

    def refresh(self) -> None:
        self.tcp_label.setText(self.store.state.tcp)
        self.udp_label.setText(self.store.state.udp_hello)
        self.usb_label.setText(self.store.state.usb)
        if self.store.state.last_status:
            try:
                status = json.loads(self.store.state.last_status)
                audio = status.get("audio", {})
                storage = status.get("storage", {})
                used = int(storage.get("used", 0))
                total = int(storage.get("total", 0))
                current = audio.get("current", "")
                playing = "playing" if audio.get("playing") else "idle"
                self.audio_status.setText(f"{playing} {current} | storage {used}/{total} bytes")
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

    def refresh_measurements(self) -> None:
        data = self.wave.measurements()
        if not data:
            self.measure_text.setPlainText("Enable cursors and double-click the wave area twice.")
            return
        lines = [f"Δt = {data['dt']:.6f} s"]
        for row in data["channels"]:
            lines.append(f"{row['name']}: ΔY={row['dy']:.5g}  A={row['a']:.5g}  B={row['b']:.5g}")
        self.measure_text.setPlainText("\n".join(lines))

    def _mock_changed(self, checked: bool) -> None:
        self.mock_button.setText("Stop Mock" if checked else "Start Mock")
        self.mock_toggled.emit(checked)

    def _pick_audio(self, kind: str) -> None:
        title = "Upload Alarm Audio" if kind == "alarm" else "Upload Power-On Audio"
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            title,
            "",
            "Audio Files (*.wav *.flac *.ogg *.aiff *.aif);;All Files (*)",
        )
        if path:
            self.audio_upload_requested.emit(kind, path)
