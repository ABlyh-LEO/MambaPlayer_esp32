from __future__ import annotations

import json
import random
import socket
import struct
import time
from pathlib import Path

from PySide6 import QtCore, QtWidgets

try:
    import serial
except Exception:  # pragma: no cover
    serial = None

from ..mamba_link import (
    STREAM_BATTERY,
    STREAM_CAN_RAW,
    STREAM_JUSTFLOAT,
    TYPE_GET_STATUS,
    TYPE_HELLO,
    TYPE_STATUS,
    TYPE_WIFI_CONFIG,
    FrameParser,
    decode_udp_packet,
    encode_frame,
    parse_can_payload,
    parse_justfloat_payload,
)
from ..telemetry.store import TelemetryStore
from .channel_panel import ChannelPanel
from .property_panel import PropertyPanel
from .wave_panel import WavePanel


TCP_PORT = 37210
UDP_HELLO_PORT = 37211
UDP_TELEMETRY_PORT = 37212
STATE_PATH = Path(__file__).resolve().parents[1] / "runtime" / "state.json"


class TcpServer(QtCore.QThread):
    frame_received = QtCore.Signal(int, bytes)
    client_changed = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._sock: socket.socket | None = None
        self._client: socket.socket | None = None
        self._running = True
        self._parser = FrameParser()

    def run(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", TCP_PORT))
        self._sock.listen(1)
        self._sock.settimeout(0.5)
        while self._running:
            if self._client is None:
                try:
                    self._client, addr = self._sock.accept()
                    self._client.settimeout(0.2)
                    self.client_changed.emit(f"{addr[0]}:{addr[1]}")
                    self.send(TYPE_HELLO, b'{"host":"mamba"}')
                    self.send(TYPE_GET_STATUS, b"{}")
                except socket.timeout:
                    continue
                except OSError:
                    break
            try:
                data = self._client.recv(4096)
                if not data:
                    raise OSError("closed")
                for frame in self._parser.feed(data):
                    self.frame_received.emit(frame.msg_type, frame.payload)
            except socket.timeout:
                continue
            except OSError:
                self._close_client()

    def send(self, msg_type: int, payload: bytes = b"") -> None:
        if self._client is None:
            return
        try:
            self._client.sendall(encode_frame(msg_type, payload, int(time.time() * 10) & 0xFFFF))
        except OSError:
            self._close_client()

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except OSError:
                pass
        self._client = None
        self.client_changed.emit("disconnected")

    def stop(self) -> None:
        self._running = False
        self._close_client()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


class UdpListener(QtCore.QThread):
    telemetry = QtCore.Signal(dict)
    hello = QtCore.Signal(str)

    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port
        self._running = True

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(0.3)
        while self._running:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.port == UDP_HELLO_PORT:
                self.hello.emit(f"{addr[0]} {data.decode(errors='replace')}")
            else:
                try:
                    item = decode_udp_packet(data)
                    item["addr"] = addr[0]
                    self.telemetry.emit(item)
                except ValueError:
                    continue
        sock.close()

    def stop(self) -> None:
        self._running = False


class SerialWorker(QtCore.QThread):
    frame_received = QtCore.Signal(int, bytes)
    state = QtCore.Signal(str)

    def __init__(self, port: str, baud: int = 115200) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self._running = True
        self._ser = None
        self._parser = FrameParser()

    def run(self) -> None:
        if serial is None:
            self.state.emit("pyserial missing")
            return
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
            self.state.emit(self.port)
        except Exception as exc:
            self.state.emit(str(exc))
            return
        while self._running:
            data = self._ser.read(512)
            if data:
                for frame in self._parser.feed(data):
                    self.frame_received.emit(frame.msg_type, frame.payload)
        self._ser.close()

    def send(self, msg_type: int, payload: bytes = b"") -> None:
        if self._ser:
            self._ser.write(encode_frame(msg_type, payload, int(time.time() * 10) & 0xFFFF))

    def stop(self) -> None:
        self._running = False


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, start_workers: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("Mamba Host")
        self.resize(1360, 860)
        self.setDockOptions(
            QtWidgets.QMainWindow.AllowNestedDocks
            | QtWidgets.QMainWindow.AllowTabbedDocks
            | QtWidgets.QMainWindow.AnimatedDocks
        )
        self.store = TelemetryStore()
        self.tcp = TcpServer()
        self.udp_hello = UdpListener(UDP_HELLO_PORT)
        self.udp_tel = UdpListener(UDP_TELEMETRY_PORT)
        self.serial_worker: SerialWorker | None = None
        self.mock_timer = QtCore.QTimer(self)
        self.mock_timer.timeout.connect(self._mock_tick)
        self._build_ui()
        self._wire()
        if start_workers:
            self.tcp.start()
            self.udp_hello.start()
            self.udp_tel.start()

    def _build_ui(self) -> None:
        self.wave = WavePanel(self.store)
        self.setCentralWidget(self.wave)

        self.channels = ChannelPanel(self.store)
        self.properties = PropertyPanel(self.store, self.wave)
        self.can_table = QtWidgets.QTableWidget(0, 4)
        self.can_table.setHorizontalHeaderLabels(["Time us", "ID", "DLC", "Data"])
        self.can_table.horizontalHeader().setStretchLastSection(True)
        self.raw_status = QtWidgets.QPlainTextEdit()
        self.raw_status.setReadOnly(True)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)

        self._dock("Channels", self.channels, QtCore.Qt.LeftDockWidgetArea)
        self._dock("Properties / Measurement", self.properties, QtCore.Qt.RightDockWidgetArea)
        bottom = QtWidgets.QTabWidget()
        bottom.addTab(self.can_table, "CAN Raw")
        bottom.addTab(self.raw_status, "Status JSON")
        bottom.addTab(self.log, "Log")
        self._dock("Data", bottom, QtCore.Qt.BottomDockWidgetArea)

        self.status = self.statusBar()
        self.status_tcp = QtWidgets.QLabel("TCP waiting")
        self.status_udp = QtWidgets.QLabel("UDP drop 0")
        self.status_channels = QtWidgets.QLabel("0 channels")
        self.status_points = QtWidgets.QLabel("0 points")
        for widget in (self.status_tcp, self.status_udp, self.status_channels, self.status_points):
            self.status.addPermanentWidget(widget)

    def _dock(self, title: str, widget: QtWidgets.QWidget, area: QtCore.Qt.DockWidgetArea) -> QtWidgets.QDockWidget:
        dock = QtWidgets.QDockWidget(title, self)
        dock.setObjectName(title.replace(" ", "_"))
        dock.setWidget(widget)
        self.addDockWidget(area, dock)
        return dock

    def _wire(self) -> None:
        self.tcp.frame_received.connect(self._handle_frame)
        self.tcp.client_changed.connect(self._tcp_changed)
        self.udp_hello.hello.connect(self._hello_changed)
        self.udp_tel.telemetry.connect(self._handle_telemetry)
        self.properties.status_requested.connect(lambda: self._send(TYPE_GET_STATUS, b"{}"))
        self.properties.wifi_requested.connect(self._send_wifi)
        self.properties.mock_toggled.connect(self._toggle_mock)
        self.properties.serial_connect_requested.connect(self._connect_serial)
        self.store.can_changed.connect(self._refresh_can_table)
        self.store.changed.connect(self._refresh_status_bar)
        self.store.channels_changed.connect(self._refresh_status_bar)
        self.store.state_changed.connect(self._write_snapshot)
        self.wave.snapshot_requested.connect(self._write_snapshot)

    def _send(self, msg_type: int, payload: bytes) -> None:
        self.tcp.send(msg_type, payload)
        if self.serial_worker:
            self.serial_worker.send(msg_type, payload)

    def _send_wifi(self, ssid: str, password: str) -> None:
        payload = json.dumps({"ssid": ssid, "password": password}).encode()
        self._send(TYPE_WIFI_CONFIG, payload)
        self._log(f"Wi-Fi config sent for SSID {ssid!r}")

    def _connect_serial(self, port: str) -> None:
        if not port:
            self.properties.refresh_ports()
            return
        if self.serial_worker:
            self.serial_worker.stop()
        self.serial_worker = SerialWorker(port)
        self.serial_worker.frame_received.connect(self._handle_frame)
        self.serial_worker.state.connect(lambda text: self.store.update_state(usb=text))
        self.serial_worker.state.connect(lambda text: self._log(f"USB: {text}"))
        self.serial_worker.start()

    def _tcp_changed(self, text: str) -> None:
        self.store.update_state(tcp=text)
        self.status_tcp.setText(f"TCP {text}")
        self._log(f"TCP {text}")

    def _hello_changed(self, text: str) -> None:
        self.store.update_state(udp_hello=text)
        self._log(f"UDP hello {text}")

    def _handle_frame(self, msg_type: int, payload: bytes) -> None:
        if msg_type == TYPE_STATUS:
            text = payload.decode(errors="replace")
            self.store.update_state(last_status=text)
            self.raw_status.setPlainText(text)
        else:
            self._log(f"frame type={msg_type} len={len(payload)} {payload[:96]!r}")

    def _handle_telemetry(self, item: dict) -> None:
        stream = item["stream_id"]
        self.store.note_udp_seq(stream, item["seq"])
        timestamp_s = item["timestamp_us"] / 1_000_000.0
        payload = item["payload"]
        if stream == STREAM_BATTERY:
            self._handle_battery(payload, timestamp_s)
        elif stream == STREAM_CAN_RAW:
            self._handle_can(payload)
        elif stream == STREAM_JUSTFLOAT:
            self._handle_justfloat(payload, timestamp_s)

    def _handle_battery(self, payload: bytes, timestamp_s: float) -> None:
        try:
            data = json.loads(payload.decode())
        except json.JSONDecodeError:
            return
        samples = []
        if "fused_mv" in data:
            samples.append(("battery.fused_mv", "Battery Fused", float(data["fused_mv"]), "mV"))
        if "adc_mv" in data:
            samples.append(("battery.adc_mv", "ADC Battery", float(data["adc_mv"]), "mV"))
        if "capacity" in data:
            samples.append(("battery.capacity", "Capacity", float(data["capacity"]), "%"))
        if samples:
            self.store.append_samples(samples, timestamp_s)

    def _handle_can(self, payload: bytes) -> None:
        try:
            can = parse_can_payload(payload)
        except ValueError:
            return
        self.store.append_can(can["timestamp_us"], can["id"], can["dlc"], can["data"])
        if can["id"] in range(0x201, 0x209) and len(can["data"]) >= 8:
            data = can["data"]
            motor = can["id"] - 0x200
            rpm = struct.unpack(">h", data[2:4])[0]
            current = struct.unpack(">h", data[4:6])[0]
            temp = data[6]
            self.store.append_samples([
                (f"rm{motor}.rpm", f"RM{motor} RPM", float(rpm), "rpm"),
                (f"rm{motor}.current", f"RM{motor} Current", float(current), ""),
                (f"rm{motor}.temp", f"RM{motor} Temp", float(temp), "C"),
            ])

    def _handle_justfloat(self, payload: bytes, timestamp_s: float) -> None:
        try:
            jf = parse_justfloat_payload(payload)
        except ValueError:
            return
        samples = []
        for i, value in enumerate(jf["values"]):
            samples.append((f"justfloat.{i}", f"JF{i}", float(value), ""))
        self.store.append_samples(samples, timestamp_s)

    def _refresh_can_table(self) -> None:
        self.can_table.setRowCount(0)
        for row_data in self.store.can_rows[:300]:
            row = self.can_table.rowCount()
            self.can_table.insertRow(row)
            values = [
                str(row_data.timestamp_us),
                f"0x{row_data.can_id:03X}",
                str(row_data.dlc),
                row_data.data_hex,
            ]
            for col, value in enumerate(values):
                self.can_table.setItem(row, col, QtWidgets.QTableWidgetItem(value))

    def _refresh_status_bar(self) -> None:
        points = sum(channel.count for channel in self.store.channels.values())
        self.status_udp.setText(f"UDP drop {self.store.state.total_udp_dropped}")
        self.status_channels.setText(f"{len(self.store.channels)} channels")
        self.status_points.setText(f"{points:,} points")

    def _toggle_mock(self, enabled: bool) -> None:
        if enabled:
            self.mock_timer.start(20)
            self._log("mock started")
        else:
            self.mock_timer.stop()
            self._log("mock stopped")

    def _mock_tick(self) -> None:
        t = time.monotonic()
        self.store.append_samples([
            ("battery.fused_mv", "Battery Fused", 22000 + random.random() * 600, "mV"),
            ("battery.adc_mv", "ADC Battery", 21900 + random.random() * 700, "mV"),
            ("battery.capacity", "Capacity", 70 + random.random() * 3, "%"),
        ], t)
        values = [
            ("justfloat.0", "JF0", random.uniform(-1, 1), ""),
            ("justfloat.1", "JF1", random.uniform(-0.5, 0.5), ""),
            ("justfloat.2", "JF2", random.uniform(0, 1), ""),
            ("rm1.rpm", "RM1 RPM", 2500 + random.uniform(-300, 300), "rpm"),
        ]
        self.store.append_samples(values, t)
        if random.random() < 0.1:
            self.store.append_can(int(t * 1_000_000), 0x201, 8, bytes(random.randrange(256) for _ in range(8)))

    def _write_snapshot(self) -> None:
        data = self.store.snapshot()
        data["wave"] = self.wave.snapshot()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def closeEvent(self, event) -> None:
        self.tcp.stop()
        self.udp_hello.stop()
        self.udp_tel.stop()
        if self.serial_worker:
            self.serial_worker.stop()
        for worker in (self.tcp, self.udp_hello, self.udp_tel, self.serial_worker):
            if worker and worker.isRunning():
                worker.wait(800)
        super().closeEvent(event)
