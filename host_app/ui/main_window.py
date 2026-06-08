from __future__ import annotations

import json
import math
import random
import socket
import struct
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtWidgets
import numpy as np

try:
    import serial
except Exception:  # pragma: no cover
    serial = None

from ..mamba_link import (
    STREAM_BATTERY,
    STREAM_ADC_BATCH,
    STREAM_CAN_RAW,
    STREAM_RM_MOTOR,
    STREAM_JUSTFLOAT,
    TYPE_ACK,
    TYPE_AUDIO_BEGIN,
    TYPE_AUDIO_CHUNK,
    TYPE_AUDIO_END,
    TYPE_AUDIO_TEST,
    TYPE_AUDIO_STREAM_PCM,
    TYPE_AUDIO_STREAM_START,
    TYPE_AUDIO_STREAM_STOP,
    TYPE_ERROR,
    TYPE_GET_STATUS,
    TYPE_HELLO,
    TYPE_SET_CONFIG,
    TYPE_STATUS,
    TYPE_WIFI_CONFIG,
    FrameParser,
    decode_udp_packet,
    encode_frame,
    parse_can_payload,
    parse_adc_batch_payload,
    parse_justfloat_payload,
    parse_rm_motor_payload,
)
from ..audio_tools import BYTES_PER_SECOND, POWER_ON_MAX_SECONDS, STORAGE_PARTITION_BYTES, convert_to_mamba_wav
from ..telemetry.store import TelemetryStore
from .channel_panel import ChannelPanel
from .property_panel import PropertyPanel
from .wave_panel import WavePanel


TCP_PORT = 37210
UDP_HELLO_PORT = 37211
UDP_TELEMETRY_PORT = 37212
STATE_PATH = Path(__file__).resolve().parents[1] / "runtime" / "state.json"
AUDIO_CHUNK_SIZE = 384
AUDIO_HEADER_ALLOWANCE_BYTES = 4096
AUDIO_STORAGE_SAFETY_BYTES = 32 * 1024
DEFAULT_SPIFFS_TOTAL_BYTES = STORAGE_PARTITION_BYTES


class TcpServer(QtCore.QThread):
    frame_received = QtCore.Signal(int, bytes)
    client_changed = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._sock: socket.socket | None = None
        self._client: socket.socket | None = None
        self._running = True
        self._parser = FrameParser()
        self._ack_cond = threading.Condition()
        self._acks: dict[int, tuple[int, bytes]] = {}
        self._seq = 1
        self._send_lock = threading.Lock()

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
                    if frame.msg_type in (TYPE_ACK, TYPE_ERROR):
                        with self._ack_cond:
                            self._acks[frame.seq] = (frame.msg_type, frame.payload)
                            self._ack_cond.notify_all()
                    self.frame_received.emit(frame.msg_type, frame.payload)
            except socket.timeout:
                continue
            except OSError:
                self._close_client()

    def send(self, msg_type: int, payload: bytes = b"") -> None:
        if self._client is None:
            return
        try:
            with self._send_lock:
                self._client.sendall(encode_frame(msg_type, payload, self._next_seq()))
        except OSError:
            self._close_client()

    def send_wait(self, msg_type: int, payload: bytes = b"", timeout: float = 5.0) -> bytes:
        if self._client is None:
            raise RuntimeError("TCP is not connected")
        seq = self._next_seq()
        with self._ack_cond:
            self._acks.pop(seq, None)
        with self._send_lock:
            self._client.sendall(encode_frame(msg_type, payload, seq))
        deadline = time.monotonic() + timeout
        with self._ack_cond:
            while seq not in self._acks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timeout waiting for ACK seq={seq}")
                self._ack_cond.wait(remaining)
            msg_type, response = self._acks.pop(seq)
        if msg_type == TYPE_ERROR:
            raise RuntimeError(response.decode(errors="replace"))
        return response

    def is_connected(self) -> bool:
        return self._client is not None

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        if self._seq == 0:
            self._seq = 1
        return self._seq

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


class SpeakerCapture(QtCore.QThread):
    pcm_ready = QtCore.Signal(bytes)
    state = QtCore.Signal(str)

    def __init__(self, target_rate: int = 16000) -> None:
        super().__init__()
        self.target_rate = target_rate
        self._running = True

    def run(self) -> None:
        try:
            self._run_wasapi()
        except Exception as exc:
            self.state.emit(f"WASAPI unavailable, using mock: {exc}")
            self._run_mock()

    def _run_wasapi(self) -> None:
        import sounddevice as sd

        device = sd.query_devices(kind="output")
        source_rate = int(device.get("default_samplerate") or 48000)
        channels = int(device.get("max_output_channels") or 2)
        extra = sd.WasapiSettings(loopback=True)
        self.state.emit(f"WASAPI loopback {source_rate}Hz {channels}ch")

        def callback(indata, frames, time_info, status):
            if not self._running:
                raise sd.CallbackStop()
            mono = np.asarray(indata, dtype=np.float32).mean(axis=1)
            pcm = self._to_pcm16(mono, source_rate)
            self.pcm_ready.emit(pcm.tobytes())

        with sd.InputStream(samplerate=source_rate, channels=channels, dtype="float32",
                            extra_settings=extra, callback=callback):
            while self._running:
                self.msleep(50)

    def _run_mock(self) -> None:
        phase = 0
        chunk = 320
        while self._running:
            t = (np.arange(chunk, dtype=np.float32) + phase) / float(self.target_rate)
            phase += chunk
            wave = np.sin(2 * math.pi * 440 * t).astype(np.float32) * 0.2
            self.pcm_ready.emit(np.round(wave * 32767).astype("<i2").tobytes())
            self.msleep(20)

    def _to_pcm16(self, mono: np.ndarray, source_rate: int) -> np.ndarray:
        if source_rate != self.target_rate and len(mono) > 0:
            duration = len(mono) / float(source_rate)
            dst_len = max(1, int(round(duration * self.target_rate)))
            src_x = np.linspace(0.0, duration, num=len(mono), endpoint=False)
            dst_x = np.linspace(0.0, duration, num=dst_len, endpoint=False)
            mono = np.interp(dst_x, src_x, mono).astype(np.float32)
        return np.round(np.clip(mono, -1.0, 1.0) * 32767).astype("<i2")

    def stop(self) -> None:
        self._running = False


class SerialWorker(QtCore.QThread):
    frame_received = QtCore.Signal(int, bytes)
    state = QtCore.Signal(str)
    ready = QtCore.Signal()

    def __init__(self, port: str, baud: int = 115200) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self._running = True
        self._ser = None
        self._parser = FrameParser()
        self._ready = threading.Event()
        self._ready_emitted = False
        self._ack_cond = threading.Condition()
        self._acks: dict[int, tuple[int, bytes]] = {}
        self._seq = 1

    def run(self) -> None:
        if serial is None:
            self.state.emit("pyserial missing")
            return
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.1, write_timeout=8)
            self.state.emit(self.port)
        except Exception as exc:
            self.state.emit(str(exc))
            return
        while self._running:
            data = self._ser.read(512)
            if data:
                for frame in self._parser.feed(data):
                    if not self._ready.is_set():
                        self._ready.set()
                    if not self._ready_emitted:
                        self._ready_emitted = True
                        self.ready.emit()
                    if frame.msg_type in (TYPE_ACK, TYPE_ERROR):
                        with self._ack_cond:
                            self._acks[frame.seq] = (frame.msg_type, frame.payload)
                            self._ack_cond.notify_all()
                    self.frame_received.emit(frame.msg_type, frame.payload)
        if self._ser:
            self._ser.close()

    def send(self, msg_type: int, payload: bytes = b"") -> None:
        if self._ser:
            self._ready.wait(timeout=2.0)
            self._ser.write(encode_frame(msg_type, payload, self._next_seq()))

    def send_wait(self, msg_type: int, payload: bytes = b"", timeout: float = 3.0) -> bytes:
        if not self._ser:
            raise RuntimeError("USB serial is not connected")
        self._ready.wait(timeout=2.0)
        seq = self._next_seq()
        with self._ack_cond:
            self._acks.pop(seq, None)
        self._ser.write(encode_frame(msg_type, payload, seq))
        self._ser.flush()
        deadline = time.monotonic() + timeout
        with self._ack_cond:
            while seq not in self._acks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timeout waiting for ACK seq={seq}")
                self._ack_cond.wait(remaining)
            msg_type, response = self._acks.pop(seq)
        if msg_type == TYPE_ERROR:
            raise RuntimeError(response.decode(errors="replace"))
        return response

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        if self._seq == 0:
            self._seq = 1
        return self._seq

    def stop(self) -> None:
        self._running = False
        if self._ser:
            try:
                self._ser.cancel_read()
            except Exception:
                pass


class AudioUploadWorker(QtCore.QThread):
    progress = QtCore.Signal(str, int, int)
    finished_ok = QtCore.Signal(str, int, float)
    failed = QtCore.Signal(str)

    def __init__(self, transport, kind: str, path: str, max_seconds: float) -> None:
        super().__init__()
        self.transport = transport
        self.kind = kind
        self.path = path
        self.max_seconds = max_seconds

    def run(self) -> None:
        try:
            wav, info = convert_to_mamba_wav(self.path, max_seconds=self.max_seconds, return_info=True)
            begin = json.dumps({"kind": self.kind, "size": len(wav)}).encode()
            self.transport.send_wait(TYPE_AUDIO_BEGIN, begin, timeout=8.0)
            sent = 0
            while sent < len(wav):
                chunk = wav[sent:sent + AUDIO_CHUNK_SIZE]
                self.transport.send_wait(TYPE_AUDIO_CHUNK, chunk, timeout=12.0)
                sent += len(chunk)
                if sent == len(wav) or sent % (16 * 1024) < AUDIO_CHUNK_SIZE:
                    self.progress.emit(self.kind, sent, len(wav))
            self.transport.send_wait(TYPE_AUDIO_END, b"{}", timeout=8.0)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(self.kind, len(wav), float(info.get("gain_db", 0.0)))


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
        self.speaker_worker: SpeakerCapture | None = None
        self.audio_upload_worker: AudioUploadWorker | None = None
        self._shutdown_done = False
        self.mock_timer = QtCore.QTimer(self)
        self.mock_timer.timeout.connect(self._mock_tick)
        self._build_ui()
        self._wire()
        if start_workers:
            self.tcp.start()
            self.udp_hello.start()
            self.udp_tel.start()
            QtCore.QTimer.singleShot(0, self._auto_connect_serial)

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
        self.properties.audio_upload_requested.connect(self._upload_audio)
        self.properties.audio_test_requested.connect(self._test_audio)
        self.properties.can_config_requested.connect(self._send_can_config)
        self.properties.device_name_requested.connect(self._send_device_name)
        self.properties.speaker_toggled.connect(self._toggle_speaker)
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

    def _send_can_config(self, can_filter: str, raw_enabled: bool, dji_enabled: bool) -> None:
        payload = json.dumps({
            "can_filter": can_filter,
            "can_raw_enabled": raw_enabled,
            "dji_motor_parse_enabled": dji_enabled,
        }).encode()
        self._send(TYPE_SET_CONFIG, payload)
        self._log(f"CAN config sent: filter={can_filter!r}, raw={raw_enabled}, dji={dji_enabled}")

    def _send_device_name(self, name: str) -> None:
        name = name.strip()
        if not name:
            QtWidgets.QMessageBox.warning(self, "Device Name", "Device name cannot be empty.")
            return
        self._send(TYPE_SET_CONFIG, json.dumps({"device_name": name}).encode())
        self._log(f"device name sent: {name!r}")

    def _status_json(self) -> dict:
        try:
            return json.loads(self.store.state.last_status or "{}")
        except json.JSONDecodeError:
            return {}

    def _alarm_max_seconds(self) -> float:
        status = self._status_json()
        storage = status.get("storage", {})
        total = int(storage.get("total") or DEFAULT_SPIFFS_TOTAL_BYTES)
        power_file_max = int(BYTES_PER_SECOND * POWER_ON_MAX_SECONDS) + AUDIO_HEADER_ALLOWANCE_BYTES
        alarm_bytes = max(0, total - power_file_max - AUDIO_STORAGE_SAFETY_BYTES)
        return max(1.0, alarm_bytes / float(BYTES_PER_SECOND))

    def _send_control_only(self, msg_type: int, payload: bytes) -> None:
        if self.serial_worker:
            self.serial_worker.send(msg_type, payload)
        else:
            self.tcp.send(msg_type, payload)

    def _active_transport(self):
        return self.serial_worker if self.serial_worker else (self.tcp if self.tcp.is_connected() else None)

    def _upload_audio(self, kind: str, path: str) -> None:
        transport = self.serial_worker if self.serial_worker else (self.tcp if self.tcp.is_connected() else None)
        if not transport:
            QtWidgets.QMessageBox.warning(self, "Audio Upload", "Connect USB or TCP before uploading audio.")
            return
        if self.audio_upload_worker and self.audio_upload_worker.isRunning():
            QtWidgets.QMessageBox.information(self, "Audio Upload", "An audio upload is already running.")
            return
        max_seconds = POWER_ON_MAX_SECONDS if kind == "poweron" else self._alarm_max_seconds()
        self.audio_upload_worker = AudioUploadWorker(transport, kind, path, max_seconds)
        self.audio_upload_worker.progress.connect(self._audio_upload_progress)
        self.audio_upload_worker.finished_ok.connect(lambda upload_kind, size, gain_db: self._audio_upload_done(upload_kind, path, size, gain_db))
        self.audio_upload_worker.failed.connect(self._audio_upload_failed)
        self.audio_upload_worker.finished.connect(lambda: setattr(self, "audio_upload_worker", None))
        self.properties.audio_status.setText(f"preparing {kind} upload...")
        self._log(f"audio upload started: {kind} from {path}")
        self.audio_upload_worker.start()

    def _audio_upload_progress(self, kind: str, sent: int, total: int) -> None:
        percent = 100.0 * sent / max(1, total)
        self.properties.audio_status.setText(f"uploading {kind}: {sent}/{total} bytes ({percent:.0f}%)")

    def _audio_upload_done(self, kind: str, path: str, size: int, gain_db: float) -> None:
        self.properties.audio_status.setText(f"uploaded {kind}: {size} bytes")
        self._log(f"uploaded {kind} audio: {size} bytes from {path}, gain {gain_db:.1f} dB")
        self._send(TYPE_GET_STATUS, b"{}")

    def _audio_upload_failed(self, message: str) -> None:
        self.properties.audio_status.setText("audio upload failed")
        self._log(f"audio upload failed: {message}")
        QtWidgets.QMessageBox.warning(self, "Audio Upload", message)

    def _test_audio(self, command: str) -> None:
        self._send_control_only(TYPE_AUDIO_TEST, json.dumps({"cmd": command}).encode())
        self._log(f"audio test command: {command}")

    def _toggle_speaker(self, enabled: bool) -> None:
        if enabled:
            transport = self._active_transport()
            if not transport:
                QtWidgets.QMessageBox.warning(self, "Speaker Mode", "Connect USB or TCP before starting speaker mode.")
                self.properties.speaker_button.setChecked(False)
                return
            try:
                transport.send_wait(TYPE_AUDIO_STREAM_START, json.dumps({"sample_rate": 16000}).encode(), timeout=3.0)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Speaker Mode", str(exc))
                self.properties.speaker_button.setChecked(False)
                return
            self.speaker_worker = SpeakerCapture()
            self.speaker_worker.pcm_ready.connect(self._send_speaker_pcm)
            self.speaker_worker.state.connect(self._log)
            self.speaker_worker.start()
            self._log("speaker mode started")
        else:
            if self.speaker_worker:
                self._stop_worker(self.speaker_worker)
                self.speaker_worker = None
            transport = self._active_transport()
            if transport:
                try:
                    transport.send_wait(TYPE_AUDIO_STREAM_STOP, b"{}", timeout=3.0)
                except Exception as exc:
                    self._log(f"speaker stop failed: {exc}")
            self._log("speaker mode stopped")

    def _send_speaker_pcm(self, payload: bytes) -> None:
        transport = self._active_transport()
        if not transport:
            return
        for offset in range(0, len(payload), 960):
            transport.send(TYPE_AUDIO_STREAM_PCM, payload[offset:offset + 960])

    def _auto_connect_serial(self) -> None:
        try:
            import serial.tools.list_ports
        except Exception:
            return
        self.properties.refresh_ports()
        for port in serial.tools.list_ports.comports():
            hwid = (port.hwid or "").upper()
            if "VID:PID=303A:1001" in hwid:
                self.properties.select_serial_port(port.device)
                self._connect_serial(port.device)
                return

    def _connect_serial(self, port: str) -> None:
        if not port:
            self.properties.refresh_ports()
            return
        if self.serial_worker:
            self._stop_worker(self.serial_worker)
        self.serial_worker = SerialWorker(port)
        self.serial_worker.frame_received.connect(self._handle_frame)
        self.serial_worker.state.connect(lambda text: self.store.update_state(usb=text))
        self.serial_worker.state.connect(lambda text: self._log(f"USB: {text}"))
        self.serial_worker.ready.connect(lambda: self._send(TYPE_GET_STATUS, b"{}"))
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
        elif stream == STREAM_ADC_BATCH:
            self._handle_adc_batch(payload, timestamp_s)
        elif stream == STREAM_CAN_RAW:
            self._handle_can(payload)
        elif stream == STREAM_RM_MOTOR:
            self._handle_rm_motor(payload)
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
        if "frames" in can:
            for frame in can["frames"]:
                self._append_can_frame(frame)
            return
        self._append_can_frame(can)

    def _append_can_frame(self, can: dict) -> None:
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

    def _handle_adc_batch(self, payload: bytes, timestamp_s: float) -> None:
        try:
            batch = parse_adc_batch_payload(payload)
        except ValueError:
            return
        base = timestamp_s
        interval_s = batch["interval_us"] / 1_000_000.0
        for i, sample in enumerate(batch["samples"]):
            t = base + i * interval_s
            self.store.append_samples([
                ("battery.adc_mv", "ADC Battery", float(sample["battery_mv"]), "mV"),
                ("adc.pin_mv", "ADC Pin", float(sample["pin_mv"]), "mV"),
                ("adc.raw", "ADC Raw", float(sample["raw"]), ""),
            ], t)

    def _handle_rm_motor(self, payload: bytes) -> None:
        try:
            batch = parse_rm_motor_payload(payload)
        except ValueError:
            return
        for motor in batch["motors"]:
            t = motor["timestamp_us"] / 1_000_000.0
            motor_id = motor["motor_id"]
            self.store.append_samples([
                (f"rm{motor_id}.rpm", f"RM{motor_id} RPM", float(motor["rpm"]), "rpm"),
                (f"rm{motor_id}.current", f"RM{motor_id} Current", float(motor["torque_current"]), ""),
                (f"rm{motor_id}.temp", f"RM{motor_id} Temp", float(motor["temperature"]), "C"),
                (f"rm{motor_id}.cmd", f"RM{motor_id} Cmd", float(motor["commanded_current"]), ""),
            ], t)

    def _handle_justfloat(self, payload: bytes, timestamp_s: float) -> None:
        try:
            jf = parse_justfloat_payload(payload)
        except ValueError:
            return
        if "frames" in jf:
            for frame in jf["frames"]:
                t = frame["timestamp_us"] / 1_000_000.0 if frame["timestamp_us"] else timestamp_s
                self.store.append_samples([
                    (f"justfloat.{i}", f"JF{i}", float(value), "")
                    for i, value in enumerate(frame["values"])
                ], t)
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

    def _stop_worker(self, worker: QtCore.QThread | None) -> None:
        if not worker:
            return
        stop = getattr(worker, "stop", None)
        if callable(stop):
            stop()
        if worker.isRunning() and not worker.wait(1500):
            worker.terminate()
            worker.wait(500)

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.mock_timer.stop()
        if self.speaker_worker:
            self._stop_worker(self.speaker_worker)
            self.speaker_worker = None
        if self.audio_upload_worker:
            self._stop_worker(self.audio_upload_worker)
            self.audio_upload_worker = None
        for worker in (self.serial_worker, self.tcp, self.udp_hello, self.udp_tel):
            self._stop_worker(worker)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
