from __future__ import annotations

import json
import random
import socket
import struct
import sys
import time

from PySide6 import QtCore, QtWidgets

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover
    pg = None

try:
    import serial
    import serial.tools.list_ports
except Exception:  # pragma: no cover
    serial = None

try:
    from .mamba_link import (
        STREAM_BATTERY,
        STREAM_CAN_RAW,
        STREAM_JUSTFLOAT,
        TYPE_GET_STATUS,
        TYPE_HELLO,
        TYPE_STATUS,
        FrameParser,
        decode_udp_packet,
        encode_frame,
        parse_can_payload,
        parse_justfloat_payload,
    )
except ImportError:
    from mamba_link import (
        STREAM_BATTERY,
        STREAM_CAN_RAW,
        STREAM_JUSTFLOAT,
        TYPE_GET_STATUS,
        TYPE_HELLO,
        TYPE_STATUS,
        FrameParser,
        decode_udp_packet,
        encode_frame,
        parse_can_payload,
        parse_justfloat_payload,
    )


TCP_PORT = 37210
UDP_HELLO_PORT = 37211
UDP_TELEMETRY_PORT = 37212


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
        client = self._client
        if client is None:
            return
        try:
            client.sendall(encode_frame(msg_type, payload, int(time.time() * 10) & 0xFFFF))
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
            self._sock.close()


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
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Mamba Host")
        self.resize(1180, 760)
        self.tcp = TcpServer()
        self.udp_hello = UdpListener(UDP_HELLO_PORT)
        self.udp_tel = UdpListener(UDP_TELEMETRY_PORT)
        self.serial_worker: SerialWorker | None = None
        self.mock_timer = QtCore.QTimer(self)
        self.mock_timer.timeout.connect(self._mock_tick)
        self.points: list[float] = []
        self.float_points: list[list[float]] = [[] for _ in range(16)]
        self.last_seq: dict[int, int] = {}
        self.drop_counts: dict[int, int] = {}
        self._build_ui()
        self._wire()
        self.tcp.start()
        self.udp_hello.start()
        self.udp_tel.start()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(root)
        self.setCentralWidget(root)

        side = QtWidgets.QWidget()
        side.setFixedWidth(280)
        form = QtWidgets.QVBoxLayout(side)
        self.conn_label = QtWidgets.QLabel("TCP: waiting")
        self.hello_label = QtWidgets.QLabel("UDP hello: none")
        self.serial_combo = QtWidgets.QComboBox()
        self.refresh_ports()
        self.serial_btn = QtWidgets.QPushButton("Connect USB")
        self.status_btn = QtWidgets.QPushButton("Get Status")
        self.mock_btn = QtWidgets.QPushButton("Start Mock")
        self.wifi_ssid = QtWidgets.QLineEdit()
        self.wifi_pass = QtWidgets.QLineEdit()
        self.wifi_pass.setEchoMode(QtWidgets.QLineEdit.Password)
        self.wifi_btn = QtWidgets.QPushButton("Send Wi-Fi Config")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        for w in [
            QtWidgets.QLabel("Connection"),
            self.conn_label,
            self.hello_label,
            self.serial_combo,
            self.serial_btn,
            self.status_btn,
            self.mock_btn,
            QtWidgets.QLabel("Computer Hotspot SSID"),
            self.wifi_ssid,
            QtWidgets.QLabel("Password"),
            self.wifi_pass,
            self.wifi_btn,
            QtWidgets.QLabel("Log"),
            self.log,
        ]:
            form.addWidget(w)
        layout.addWidget(side)

        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs, 1)

        self.plot_widget = pg.PlotWidget() if pg else QtWidgets.QPlainTextEdit("pyqtgraph missing")
        if pg:
            self.plot_widget.setBackground("w")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
            self.battery_curve = self.plot_widget.plot(pen=pg.mkPen("#176b5c", width=2), name="battery_mv")
            self.float_curves = [
                self.plot_widget.plot(pen=pg.intColor(i, hues=16), name=f"jf{i}") for i in range(8)
            ]
        tabs.addTab(self.plot_widget, "Scope")

        self.can_table = QtWidgets.QTableWidget(0, 4)
        self.can_table.setHorizontalHeaderLabels(["Time us", "ID", "DLC", "Data"])
        self.can_table.horizontalHeader().setStretchLastSection(True)
        tabs.addTab(self.can_table, "CAN")

        self.status_text = QtWidgets.QPlainTextEdit()
        self.status_text.setReadOnly(True)
        tabs.addTab(self.status_text, "Status")

    def _wire(self) -> None:
        self.tcp.frame_received.connect(self._handle_frame)
        self.tcp.client_changed.connect(lambda text: self.conn_label.setText(f"TCP: {text}"))
        self.udp_hello.hello.connect(lambda text: self.hello_label.setText(f"UDP hello: {text}"))
        self.udp_tel.telemetry.connect(self._handle_telemetry)
        self.serial_btn.clicked.connect(self._connect_serial)
        self.status_btn.clicked.connect(lambda: self._send(TYPE_GET_STATUS, b"{}"))
        self.wifi_btn.clicked.connect(self._send_wifi)
        self.mock_btn.clicked.connect(self._toggle_mock)

    def refresh_ports(self) -> None:
        self.serial_combo.clear()
        if serial is None:
            return
        for port in serial.tools.list_ports.comports():
            self.serial_combo.addItem(f"{port.device} {port.description}", port.device)

    def _send(self, msg_type: int, payload: bytes) -> None:
        self.tcp.send(msg_type, payload)
        if self.serial_worker:
            self.serial_worker.send(msg_type, payload)

    def _send_wifi(self) -> None:
        payload = json.dumps({"ssid": self.wifi_ssid.text(), "password": self.wifi_pass.text()}).encode()
        try:
            from .mamba_link import TYPE_WIFI_CONFIG
        except ImportError:
            from mamba_link import TYPE_WIFI_CONFIG

        self._send(TYPE_WIFI_CONFIG, payload)

    def _connect_serial(self) -> None:
        port = self.serial_combo.currentData()
        if not port:
            self.refresh_ports()
            return
        if self.serial_worker:
            self.serial_worker.stop()
        self.serial_worker = SerialWorker(port)
        self.serial_worker.frame_received.connect(self._handle_frame)
        self.serial_worker.state.connect(lambda text: self._log(f"USB: {text}"))
        self.serial_worker.start()

    def _handle_frame(self, msg_type: int, payload: bytes) -> None:
        if msg_type == TYPE_STATUS:
            self.status_text.setPlainText(payload.decode(errors="replace"))
        else:
            self._log(f"frame type={msg_type} len={len(payload)} {payload[:80]!r}")

    def _handle_telemetry(self, item: dict) -> None:
        stream = item["stream_id"]
        seq = item["seq"]
        last = self.last_seq.get(stream)
        if last is not None and seq != last + 1:
            self.drop_counts[stream] = self.drop_counts.get(stream, 0) + max(0, seq - last - 1)
        self.last_seq[stream] = seq
        payload = item["payload"]
        if stream == STREAM_BATTERY:
            try:
                data = json.loads(payload.decode())
                self.points.append(float(data.get("fused_mv", 0)))
                self.points = self.points[-500:]
                self._redraw()
            except Exception:
                pass
        elif stream == STREAM_CAN_RAW:
            try:
                can = parse_can_payload(payload)
                self._append_can(can)
            except ValueError:
                pass
        elif stream == STREAM_JUSTFLOAT:
            try:
                jf = parse_justfloat_payload(payload)
                for i, value in enumerate(jf["values"][:8]):
                    self.float_points[i].append(float(value))
                    self.float_points[i] = self.float_points[i][-500:]
                self._redraw()
            except ValueError:
                pass

    def _append_can(self, can: dict) -> None:
        row = 0
        self.can_table.insertRow(row)
        values = [
            str(can["timestamp_us"]),
            f"0x{can['id']:03X}",
            str(can["dlc"]),
            " ".join(f"{b:02X}" for b in can["data"]),
        ]
        for col, value in enumerate(values):
            self.can_table.setItem(row, col, QtWidgets.QTableWidgetItem(value))
        if self.can_table.rowCount() > 300:
            self.can_table.removeRow(300)

    def _redraw(self) -> None:
        if not pg:
            return
        self.battery_curve.setData(self.points)
        for curve, values in zip(self.float_curves, self.float_points):
            curve.setData(values)

    def _toggle_mock(self) -> None:
        if self.mock_timer.isActive():
            self.mock_timer.stop()
            self.mock_btn.setText("Start Mock")
        else:
            self.mock_timer.start(50)
            self.mock_btn.setText("Stop Mock")

    def _mock_tick(self) -> None:
        t = time.time()
        battery = {"stream_id": STREAM_BATTERY, "seq": int(t * 20), "payload": json.dumps({"fused_mv": 22000 + random.random() * 400}).encode()}
        self._handle_telemetry(battery)
        values = [random.uniform(-1, 1), random.uniform(-0.4, 0.4), random.uniform(0, 1)]
        payload = struct.pack("<BBHI", len(values), 0, 0, 0) + struct.pack("<" + "f" * len(values), *values)
        self._handle_telemetry({"stream_id": STREAM_JUSTFLOAT, "seq": int(t * 20), "payload": payload})

    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def closeEvent(self, event) -> None:
        self.tcp.stop()
        self.udp_hello.stop()
        self.udp_tel.stop()
        if self.serial_worker:
            self.serial_worker.stop()
        super().closeEvent(event)


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
