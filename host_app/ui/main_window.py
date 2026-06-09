from __future__ import annotations

import json
import math
import queue
import socket
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtWidgets
import numpy as np

try:
    import serial
except Exception:  # pragma: no cover
    serial = None

from ..audio_tools import BYTES_PER_SECOND, POWER_ON_MAX_SECONDS, STORAGE_PARTITION_BYTES, convert_to_mamba_wav
from ..mamba_link import (
    STREAM_CAN_LAST,
    STREAM_CATALOG,
    STREAM_SELECTED_VALUES,
    TYPE_ACK,
    TYPE_AUDIO_BEGIN,
    TYPE_AUDIO_CHUNK,
    TYPE_AUDIO_END,
    TYPE_AUDIO_STREAM_START,
    TYPE_AUDIO_STREAM_STOP,
    TYPE_AUDIO_TEST,
    TYPE_ERROR,
    TYPE_GET_STATUS,
    TYPE_HELLO,
    TYPE_SET_CAN_FORWARD_IDS,
    TYPE_SET_STREAM_CHANNELS,
    TYPE_STATUS,
    TYPE_TELEMETRY,
    TYPE_WIFI_CONFIG,
    FrameParser,
    decode_udp_packet,
    encode_speaker_udp_packet,
    encode_frame,
    SPEAKER_UDP_PAYLOAD_SAMPLES,
    SPEAKER_UDP_PORT,
)
from ..router import (
    DEFAULT_PROJECT,
    CanFrame,
    SourceValue,
    VofaUdpSender,
    load_project,
    parse_can_last,
    parse_catalog,
    parse_dji_motor,
    parse_selected_batch,
    save_project,
)


TCP_PORT = 37210
UDP_HELLO_PORT = 37211
UDP_TELEMETRY_PORT = 37212
STATE_PATH = Path(__file__).resolve().parents[1] / "runtime" / "state.json"
PROJECT_PATH = Path(__file__).resolve().parents[1] / "runtime" / "last_project.mamba.json"
AUDIO_CHUNK_SIZE = 384
AUDIO_HEADER_ALLOWANCE_BYTES = 4096
AUDIO_STORAGE_SAFETY_BYTES = 32 * 1024
DEFAULT_SPIFFS_TOTAL_BYTES = STORAGE_PARTITION_BYTES


class TcpServer(QtCore.QThread):
    frame_received = QtCore.Signal(int, bytes)
    client_changed = QtCore.Signal(str)
    protocol_warning = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._sock: socket.socket | None = None
        self._client: socket.socket | None = None
        self._running = True
        self._parser = FrameParser()
        self._ack_cond = threading.Condition()
        self._acks: dict[int, tuple[int, bytes]] = {}
        self._connected_cond = threading.Condition()
        self._seq = 1
        self._send_lock = threading.Lock()
        self._peer_ip: str | None = None

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
                    self._peer_ip = str(addr[0])
                    self._client.settimeout(0.2)
                    with self._connected_cond:
                        self._connected_cond.notify_all()
                    self.client_changed.emit(f"{addr[0]}:{addr[1]}")
                    self.send(TYPE_HELLO, b'{"host":"mamba-v2"}')
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
                if self._parser.last_bad_version is not None:
                    self.protocol_warning.emit(
                        f"TCP received MambaLink v{self._parser.last_bad_version}; host expects v2. Reflash current firmware."
                    )
                    self._parser.last_bad_version = None
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
        seq = self._next_seq()
        with self._ack_cond:
            self._acks.pop(seq, None)
        frame = encode_frame(msg_type, payload, seq)
        deadline = time.monotonic() + timeout
        sent = False
        while not sent:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timeout waiting for TCP connection seq={seq}")
            if not self._wait_connected(min(remaining, 0.5)):
                continue
            try:
                with self._send_lock:
                    if self._client is None:
                        continue
                    self._client.sendall(frame)
                sent = True
            except OSError:
                self._close_client()
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

    def peer_ip(self) -> str | None:
        return self._peer_ip if self._client is not None else None

    def _wait_connected(self, timeout: float) -> bool:
        if self._client is not None:
            return True
        deadline = time.monotonic() + timeout
        with self._connected_cond:
            while self._client is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._connected_cond.wait(remaining)
        return True

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq or 1

    def _close_client(self) -> None:
        if self._client:
            try:
                self._client.close()
            except OSError:
                pass
        self._client = None
        self._peer_ip = None
        self.client_changed.emit("disconnected")

    def stop(self) -> None:
        self._running = False
        self._close_client()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


class UdpListener(QtCore.QThread):
    telemetry = QtCore.Signal(dict)
    selected_batch = QtCore.Signal(list)
    hello = QtCore.Signal(str)
    warning = QtCore.Signal(str)

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
                    if item["stream_id"] == STREAM_SELECTED_VALUES:
                        try:
                            self.selected_batch.emit(parse_selected_batch(item["payload"]))
                            continue
                        except ValueError:
                            pass
                    self.telemetry.emit(item)
                except ValueError as exc:
                    if data[:2] == b"MT" and len(data) >= 3:
                        self.warning.emit(f"UDP telemetry version mismatch or bad packet: v{data[2]} ({exc})")
                    continue
        sock.close()

    def stop(self) -> None:
        self._running = False


class SerialWorker(QtCore.QThread):
    frame_received = QtCore.Signal(int, bytes)
    state = QtCore.Signal(str)
    ready = QtCore.Signal()
    protocol_warning = QtCore.Signal(str)

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
        self._send_lock = threading.Lock()

    def run(self) -> None:
        if serial is None:
            self.state.emit("pyserial missing")
            return
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.1, write_timeout=8)
            self.state.emit(self.port)
            self._ready.set()
            self._ready_emitted = True
            self.ready.emit()
        except Exception as exc:
            self.state.emit(str(exc))
            return
        while self._running:
            data = self._ser.read(512)
            if not data:
                continue
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
            if self._parser.last_bad_version is not None:
                self.protocol_warning.emit(
                    f"USB received MambaLink v{self._parser.last_bad_version}; host expects v2. Reflash current firmware."
                )
                self._parser.last_bad_version = None
        if self._ser:
            self._ser.close()

    def send(self, msg_type: int, payload: bytes = b"") -> None:
        if self._ser:
            self._ready.wait(timeout=2.0)
            with self._send_lock:
                self._ser.write(encode_frame(msg_type, payload, self._next_seq()))

    def send_wait(self, msg_type: int, payload: bytes = b"", timeout: float = 3.0) -> bytes:
        if not self._ser:
            raise RuntimeError("USB serial is not connected")
        self._ready.wait(timeout=2.0)
        seq = self._next_seq()
        with self._ack_cond:
            self._acks.pop(seq, None)
        with self._send_lock:
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
        return self._seq or 1

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
            self.transport.send_wait(TYPE_AUDIO_BEGIN, json.dumps({"kind": self.kind, "size": len(wav)}).encode(), timeout=8.0)
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


class SpeakerControlWorker(QtCore.QThread):
    started = QtCore.Signal(str)
    stopped = QtCore.Signal()
    failed = QtCore.Signal(str, str)

    def __init__(self, transport, action: str, peer_ip: str = "", sample_rate: int = 16000) -> None:
        super().__init__()
        self.transport = transport
        self.action = action
        self.peer_ip = peer_ip
        self.sample_rate = sample_rate

    def run(self) -> None:
        try:
            if self.action == "start":
                payload = json.dumps({"sample_rate": self.sample_rate}).encode()
                self.transport.send_wait(TYPE_AUDIO_STREAM_START, payload, timeout=8.0)
                self.started.emit(self.peer_ip)
            else:
                self.transport.send_wait(TYPE_AUDIO_STREAM_STOP, b"{}", timeout=3.0)
                self.stopped.emit()
        except Exception as exc:
            self.failed.emit(self.action, str(exc))


class SpeakerCapture(QtCore.QThread):
    pcm_ready = QtCore.Signal(bytes)
    state = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, target_rate: int = 16000) -> None:
        super().__init__()
        self.target_rate = target_rate
        self._running = True

    def run(self) -> None:
        try:
            self._run_capture()
        except Exception as exc:
            self.failed.emit(f"speaker capture failed: {exc}")

    def _run_capture(self) -> None:
        import sounddevice as sd

        index, device = self._select_capture(sd)
        rate = int(device.get("default_samplerate") or 48000)
        channels = min(2, int(device.get("max_input_channels") or 0))
        if channels <= 0:
            raise RuntimeError("capture device has no input channels")
        self.state.emit(f"audio capture {device.get('name', index)} {rate}Hz {channels}ch")

        def callback(indata, frames, time_info, status):
            if not self._running:
                raise sd.CallbackStop()
            mono = np.asarray(indata, dtype=np.float32).mean(axis=1)
            pcm = self._to_pcm16(mono, rate)
            self.pcm_ready.emit(pcm.tobytes())

        with sd.InputStream(device=index, samplerate=rate, channels=channels, dtype="float32",
                            blocksize=max(64, rate // 200), callback=callback):
            while self._running:
                self.msleep(10)

    def _select_capture(self, sd):
        hostapis = sd.query_hostapis()
        candidates = []
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels") or 0) <= 0:
                continue
            name = str(device.get("name", ""))
            lower = name.lower()
            hostapi = hostapis[int(device.get("hostapi", 0))]["name"]
            score = 0
            if "cable output" in lower and "vb-audio" in lower:
                score = 1000
            elif "vb-audio" in lower and ("output" in lower or "input" in lower):
                score = 700
            elif "stereo mix" in lower or "立体声混音" in name:
                score = 300
            if score:
                score += 50 if hostapi == "Windows WASAPI" else 20 if hostapi == "Windows DirectSound" else 0
                candidates.append((score, index, device))
        if not candidates:
            raise RuntimeError("no VB-CABLE/Stereo Mix capture device found")
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][1], candidates[0][2]

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


class VofaForwarder(QtCore.QThread):
    warning = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._queue: queue.Queue[tuple[str, int, int, list[float]]] = queue.Queue(maxsize=4096)
        self._running = True
        self._sender = VofaUdpSender()

    def enqueue(self, host: str, remote_port: int, local_port: int, values: list[float]) -> None:
        item = (host, int(remote_port), int(local_port), list(values))
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            pass

    def run(self) -> None:
        timer_period_set = False
        try:
            import ctypes
            timer_period_set = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        except Exception:
            timer_period_set = False
        next_send = time.perf_counter()
        try:
            while self._running:
                try:
                    host, remote_port, local_port, values = self._queue.get(timeout=0.05)
                except queue.Empty:
                    next_send = time.perf_counter()
                    continue
                now = time.perf_counter()
                if now < next_send:
                    time.sleep(next_send - now)
                elif now - next_send > 0.02:
                    next_send = now
                try:
                    self._sender.configure(local_port)
                    self._sender.send(host, remote_port, values)
                except OSError as exc:
                    self.warning.emit(f"VOFA UDP failed: {exc}")
                next_send += 0.001
        finally:
            if timer_period_set:
                try:
                    import ctypes
                    ctypes.windll.winmm.timeEndPeriod(1)
                except Exception:
                    pass

    def stop(self) -> None:
        self._running = False
        self._sender.close()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, start_workers: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("Mamba Data Hub")
        self.resize(1320, 820)
        self.project_path = PROJECT_PATH
        self.project = load_project(self.project_path)
        self.sources: dict[str, SourceValue] = {}
        self.can_frames: dict[int, CanFrame] = {}
        self.firmware_channels: list[str] = list(self.project.get("firmware_channels", []))[:16]
        self.vofa_channels: list[dict] = list(self.project.get("vofa_channels", []))
        self.can_ids: list[int] = [int(v) & 0x7ff for v in self.project.get("can_ids", [])][:16]
        self.selected_values: dict[str, float] = {}
        self.tcp = TcpServer()
        self.udp_hello = UdpListener(UDP_HELLO_PORT)
        self.udp_tel = UdpListener(UDP_TELEMETRY_PORT)
        self.serial_worker: SerialWorker | None = None
        self.audio_upload_worker: AudioUploadWorker | None = None
        self.speaker_control_worker: SpeakerControlWorker | None = None
        self.speaker_worker: SpeakerCapture | None = None
        self.speaker_udp: socket.socket | None = None
        self.speaker_udp_target: tuple[str, int] | None = None
        self.speaker_udp_seq = 0
        self.speaker_pcm_buffer = bytearray()
        self.speaker_udp_packets = 0
        self.speaker_udp_bytes = 0
        self.speaker_udp_last_error = ""
        self._speaker_changing = False
        self.vofa_forwarder = VofaForwarder()
        self.vofa_timer = QtCore.QTimer(self)
        self.vofa_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.vofa_timer.timeout.connect(self._send_vofa_frame)
        self.mock_timer = QtCore.QTimer(self)
        self.mock_timer.timeout.connect(self._mock_tick)
        self.status_poll_timer = QtCore.QTimer(self)
        self.status_poll_timer.timeout.connect(self._poll_status_until_sources)
        self._mock_phase = 0.0
        self._last_selected_ui_refresh = 0.0
        self._shutdown_done = False
        self._build_ui()
        self._wire()
        self._load_project_to_ui()
        self.vofa_timer.start(1)
        if start_workers:
            self.vofa_forwarder.start()
            self.tcp.start()
            self.udp_hello.start()
            self.udp_tel.start()
            self.status_poll_timer.start(2000)
            QtCore.QTimer.singleShot(0, self._auto_connect_serial)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        self.setCentralWidget(central)

        top = QtWidgets.QHBoxLayout()
        self.tcp_label = QtWidgets.QLabel("TCP waiting")
        self.usb_label = QtWidgets.QLabel("USB disconnected")
        self.udp_label = QtWidgets.QLabel("UDP none")
        self.theme_button = QtWidgets.QPushButton("Toggle Theme")
        self.mock_button = QtWidgets.QPushButton("Start Mock")
        self.mock_button.setCheckable(True)
        self.save_button = QtWidgets.QPushButton("Save Project")
        for widget in (self.tcp_label, self.usb_label, self.udp_label):
            top.addWidget(widget)
        top.addStretch(1)
        top.addWidget(self.theme_button)
        top.addWidget(self.mock_button)
        top.addWidget(self.save_button)
        layout.addLayout(top)

        controls = QtWidgets.QHBoxLayout()
        self.vofa_host = QtWidgets.QLineEdit()
        self.vofa_remote = QtWidgets.QSpinBox()
        self.vofa_remote.setRange(1, 65535)
        self.vofa_local = QtWidgets.QSpinBox()
        self.vofa_local.setRange(1, 65535)
        self.serial_combo = QtWidgets.QComboBox()
        self.refresh_ports()
        self.serial_button = QtWidgets.QPushButton("Connect USB")
        self.status_button = QtWidgets.QPushButton("Status")
        controls.addWidget(QtWidgets.QLabel("Send to VOFA"))
        controls.addWidget(self.vofa_host)
        controls.addWidget(self.vofa_remote)
        controls.addWidget(QtWidgets.QLabel("Bind local"))
        controls.addWidget(self.vofa_local)
        controls.addWidget(self.serial_combo)
        controls.addWidget(self.serial_button)
        controls.addWidget(self.status_button)
        layout.addLayout(controls)

        splitter = QtWidgets.QSplitter()
        self.sources_table = self._table(["Key", "Name", "Value", "Unit", "Rate Hz"])
        self.firmware_table = self._table(["Slot", "Source", "Latest"])
        self.channels_table = self._table(["VOFA Ch", "Source", "Label", "Latest"])
        self.can_table = self._table(["ID", "Timestamp us", "DLC", "Data", "Parser fields"])
        middle = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        middle.addWidget(self.firmware_table)
        middle.addWidget(self.channels_table)
        middle.setSizes([260, 360])
        splitter.addWidget(self.sources_table)
        splitter.addWidget(middle)
        splitter.addWidget(self.can_table)
        splitter.setSizes([420, 420, 480])
        layout.addWidget(splitter, 1)

        route = QtWidgets.QHBoxLayout()
        self.source_combo = QtWidgets.QComboBox()
        self.vofa_index = QtWidgets.QSpinBox()
        self.vofa_index.setRange(0, 255)
        self.add_fw_button = QtWidgets.QPushButton("Add Firmware Ch")
        self.remove_fw_button = QtWidgets.QPushButton("Remove Firmware Ch")
        self.apply_fw_button = QtWidgets.QPushButton("Apply Firmware 1kHz")
        self.add_vofa_button = QtWidgets.QPushButton("Add VOFA Ch")
        self.remove_vofa_button = QtWidgets.QPushButton("Remove VOFA Row")
        self.can_ids_edit = QtWidgets.QLineEdit()
        self.apply_can_button = QtWidgets.QPushButton("Apply CAN IDs")
        route.addWidget(QtWidgets.QLabel("Source"))
        route.addWidget(self.source_combo, 2)
        route.addWidget(QtWidgets.QLabel("VOFA Ch"))
        route.addWidget(self.vofa_index)
        route.addWidget(self.add_fw_button)
        route.addWidget(self.remove_fw_button)
        route.addWidget(self.apply_fw_button)
        route.addWidget(self.add_vofa_button)
        route.addWidget(self.remove_vofa_button)
        route.addWidget(QtWidgets.QLabel("CAN IDs"))
        route.addWidget(self.can_ids_edit)
        route.addWidget(self.apply_can_button)
        layout.addLayout(route)

        audio = QtWidgets.QHBoxLayout()
        self.power_upload = QtWidgets.QPushButton("Upload Power-On")
        self.alarm_upload = QtWidgets.QPushButton("Upload Alarm")
        self.power_test = QtWidgets.QPushButton("Play Power-On")
        self.alarm_test = QtWidgets.QPushButton("Play Alarm")
        self.audio_stop = QtWidgets.QPushButton("Stop Audio")
        self.speaker_button = QtWidgets.QPushButton("Start Speaker Mode")
        self.speaker_button.setCheckable(True)
        self.audio_status = QtWidgets.QLabel("audio idle")
        for widget in (self.power_upload, self.alarm_upload, self.power_test, self.alarm_test,
                       self.audio_stop, self.speaker_button, self.audio_status):
            audio.addWidget(widget)
        layout.addLayout(audio)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        layout.addWidget(self.log)

    def _table(self, headers: list[str]) -> QtWidgets.QTableWidget:
        table = QtWidgets.QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        return table

    def _wire(self) -> None:
        self.tcp.frame_received.connect(self._handle_frame)
        self.tcp.client_changed.connect(self._tcp_changed)
        self.tcp.protocol_warning.connect(self._log)
        self.vofa_forwarder.warning.connect(self._log)
        self.udp_hello.hello.connect(self._hello_changed)
        self.udp_tel.telemetry.connect(self._handle_telemetry)
        self.udp_tel.selected_batch.connect(self._handle_selected_batch)
        self.udp_tel.warning.connect(self._log)
        self.theme_button.clicked.connect(self._toggle_theme)
        self.mock_button.toggled.connect(self._toggle_mock)
        self.save_button.clicked.connect(self._save_project)
        self.serial_button.clicked.connect(lambda: self._connect_serial(self.serial_combo.currentData() or ""))
        self.status_button.clicked.connect(lambda: self._send(TYPE_GET_STATUS, b"{}"))
        self.add_fw_button.clicked.connect(self._add_firmware_channel)
        self.remove_fw_button.clicked.connect(self._remove_firmware_channel)
        self.apply_fw_button.clicked.connect(self._apply_firmware_channels)
        self.add_vofa_button.clicked.connect(self._add_vofa_channel)
        self.remove_vofa_button.clicked.connect(self._remove_vofa_channel)
        self.apply_can_button.clicked.connect(self._apply_can_ids)
        self.channels_table.itemChanged.connect(lambda _item: self._sync_vofa_table_edits())
        self.power_upload.clicked.connect(lambda: self._pick_audio("poweron"))
        self.alarm_upload.clicked.connect(lambda: self._pick_audio("alarm"))
        self.power_test.clicked.connect(lambda: self._send_control_only(TYPE_AUDIO_TEST, b'{"cmd":"power"}'))
        self.alarm_test.clicked.connect(lambda: self._send_control_only(TYPE_AUDIO_TEST, b'{"cmd":"alarm"}'))
        self.audio_stop.clicked.connect(lambda: self._send_control_only(TYPE_AUDIO_TEST, b'{"cmd":"stop"}'))
        self.speaker_button.toggled.connect(self._toggle_speaker)

    def _load_project_to_ui(self) -> None:
        self.vofa_host.setText(str(self.project.get("vofa_host", DEFAULT_PROJECT["vofa_host"])))
        self.vofa_remote.setValue(int(self.project.get("vofa_remote_port", DEFAULT_PROJECT["vofa_remote_port"])))
        self.vofa_local.setValue(int(self.project.get("vofa_local_port", DEFAULT_PROJECT["vofa_local_port"])))
        self.can_ids_edit.setText(",".join(f"0x{can_id:03X}" for can_id in self.can_ids))
        self._refresh_firmware_table()
        self._refresh_channels_table()
        self._refresh_sources_table()

    def refresh_ports(self) -> None:
        self.serial_combo.clear()
        try:
            import serial.tools.list_ports
            for port in serial.tools.list_ports.comports():
                self.serial_combo.addItem(f"{port.device} {port.description}", port.device)
        except Exception:
            self.serial_combo.addItem("pyserial unavailable", "")

    def _active_transport(self, prefer_tcp: bool = True):
        if prefer_tcp and self.tcp.is_connected():
            return self.tcp
        if self.serial_worker:
            return self.serial_worker
        return self.tcp if self.tcp.is_connected() else None

    def _speaker_peer_ip(self) -> str | None:
        if self.tcp.is_connected():
            return self.tcp.peer_ip()
        return None

    def _send(self, msg_type: int, payload: bytes) -> None:
        self.tcp.send(msg_type, payload)
        if self.serial_worker:
            self.serial_worker.send(msg_type, payload)

    def _send_control_only(self, msg_type: int, payload: bytes) -> None:
        transport = self._active_transport()
        if transport:
            transport.send(msg_type, payload)

    def _poll_status_until_sources(self) -> None:
        if self.sources:
            return
        self._send(TYPE_GET_STATUS, b"{}")

    def _handle_frame(self, msg_type: int, payload: bytes) -> None:
        if msg_type == TYPE_STATUS:
            self._handle_status(payload)
            self._write_snapshot()
        elif msg_type == TYPE_TELEMETRY:
            self._handle_usb_telemetry(payload)
        elif msg_type == TYPE_ERROR:
            self._log(f"device error: {payload.decode(errors='replace')}")
        elif msg_type not in (TYPE_ACK, TYPE_ERROR):
            self._log(f"frame type={msg_type} len={len(payload)}")

    def _handle_status(self, payload: bytes) -> None:
        text = payload.decode(errors="replace")
        self._log(text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        battery = data.get("battery", {})
        now = time.monotonic()
        fallback = [
            ("adc.battery_mv", "ADC Battery", "mV", battery.get("adc_mv"), 500),
            ("i2c.capacity", "I2C Capacity", "%", battery.get("capacity"), 2),
            ("i2c.voltage_mv", "I2C Voltage", "mV", battery.get("fused_mv"), 2),
            ("i2c.current_ma", "I2C Current", "mA", battery.get("current_ma"), 2),
            ("i2c.temperature_c", "I2C Temp", "C", (float(battery.get("temp_decic") or 0) / 10.0), 2),
        ]
        for key, name, unit, value, rate in fallback:
            try:
                self.sources[key] = SourceValue(key=key, name=name, unit=unit, value=float(value or 0), rate_hz=rate, updated_at=now)
            except (TypeError, ValueError):
                continue
        self._refresh_sources_table()
        self._refresh_firmware_table()
        self._refresh_channels_table()

    def _handle_usb_telemetry(self, payload: bytes) -> None:
        if payload.lstrip().startswith(b"{"):
            try:
                data = json.loads(payload.decode(errors="replace"))
            except json.JSONDecodeError:
                self._log(f"USB telemetry JSON parse failed len={len(payload)}")
                return
            if data.get("type") == "catalog":
                self._handle_catalog(payload)
                return
        self._log(f"USB telemetry len={len(payload)}")

    def _handle_telemetry(self, item: dict) -> None:
        stream = item["stream_id"]
        payload = item["payload"]
        if stream == STREAM_CATALOG:
            self._handle_catalog(payload)
        elif stream == STREAM_SELECTED_VALUES:
            self._handle_selected(payload)
        elif stream == STREAM_CAN_LAST:
            self._handle_can(payload)

    def _handle_catalog(self, payload: bytes) -> None:
        try:
            for source in parse_catalog(payload):
                self.sources[source.key] = source
        except (json.JSONDecodeError, ValueError):
            return
        self._refresh_sources_table()
        self._refresh_firmware_table()
        self._refresh_channels_table()
        self._write_snapshot()

    def _handle_selected(self, payload: bytes) -> None:
        try:
            batch = parse_selected_batch(payload)
        except ValueError:
            return
        self._handle_selected_batch(batch)

    def _handle_selected_batch(self, batch: list) -> None:
        if not batch:
            return
        now = time.monotonic()
        for values in batch:
            sample = {key: float(value) for key, value in zip(self.firmware_channels, values)}
            self._enqueue_vofa_sample(sample)
        latest = batch[-1]
        for key, value in zip(self.firmware_channels, latest):
            self.sources[key] = SourceValue(key=key, name=key, value=float(value), rate_hz=1000, updated_at=now)
            self.selected_values[key] = float(value)
        if now - self._last_selected_ui_refresh >= 0.1:
            self._last_selected_ui_refresh = now
            self._refresh_firmware_table()
            self._refresh_channels_table()

    def _handle_can(self, payload: bytes) -> None:
        try:
            frames = parse_can_last(payload)
        except ValueError:
            return
        for frame in frames:
            self.can_frames[frame.can_id] = frame
            for field, value in parse_dji_motor(frame.can_id, frame.data).items():
                key = f"can.0x{frame.can_id:03X}.{field}"
                self.sources[key] = SourceValue(key=key, name=key, value=value, rate_hz=1000)
        self._refresh_can_table()
        self._refresh_sources_table()
        self._refresh_channels_table()

    def _refresh_sources_table(self) -> None:
        keys = sorted(self.sources)
        selected_key = self._selected_source_key()
        self.sources_table.setRowCount(len(keys))
        selected_row = -1
        for row, key in enumerate(keys):
            source = self.sources[key]
            values = [source.key, source.name, f"{source.value:.6g}", source.unit, str(source.rate_hz)]
            for col, value in enumerate(values):
                self.sources_table.setItem(row, col, QtWidgets.QTableWidgetItem(value))
            if key == selected_key:
                selected_row = row
        if selected_row >= 0:
            self.sources_table.selectRow(selected_row)
        combo_keys = [self.source_combo.itemData(i) for i in range(self.source_combo.count())]
        if combo_keys != keys:
            self.source_combo.blockSignals(True)
            self.source_combo.clear()
            for key in keys:
                self.source_combo.addItem(key, key)
            index = self.source_combo.findData(selected_key)
            if index >= 0:
                self.source_combo.setCurrentIndex(index)
            self.source_combo.blockSignals(False)

    def _selected_source_key(self) -> str:
        rows = self.sources_table.selectionModel().selectedRows() if self.sources_table.selectionModel() else []
        if rows:
            item = self.sources_table.item(rows[0].row(), 0)
            if item:
                return item.text()
        data = self.source_combo.currentData()
        return str(data or self.source_combo.currentText() or "")

    def _refresh_firmware_table(self) -> None:
        self.firmware_table.setRowCount(len(self.firmware_channels))
        for row, source in enumerate(self.firmware_channels):
            latest = self.sources.get(source, SourceValue(source, source)).value
            values = [str(row), source, f"{latest:.6g}"]
            for col, value in enumerate(values):
                self.firmware_table.setItem(row, col, QtWidgets.QTableWidgetItem(value))

    def _refresh_channels_table(self) -> None:
        self.channels_table.blockSignals(True)
        self.channels_table.setRowCount(len(self.vofa_channels))
        for row, mapping in enumerate(self.vofa_channels):
            source = str(mapping.get("source", ""))
            latest = self.sources.get(source, SourceValue(source, source)).value
            values = [str(mapping.get("index", row)), source, str(mapping.get("label", "")), f"{latest:.6g}"]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if col != 3:
                    item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
                self.channels_table.setItem(row, col, item)
        self.channels_table.blockSignals(False)

    def _refresh_can_table(self) -> None:
        ids = sorted(self.can_frames)
        self.can_table.setRowCount(len(ids))
        for row, can_id in enumerate(ids):
            frame = self.can_frames[can_id]
            parsed = parse_dji_motor(can_id, frame.data)
            values = [
                f"0x{can_id:03X}",
                str(frame.timestamp_us),
                str(frame.dlc),
                frame.data_hex,
                ", ".join(f"{k}={v:.0f}" for k, v in parsed.items()),
            ]
            for col, value in enumerate(values):
                self.can_table.setItem(row, col, QtWidgets.QTableWidgetItem(value))

    def _add_firmware_channel(self) -> None:
        key = self._selected_source_key()
        if key and key not in self.firmware_channels and len(self.firmware_channels) < 16:
            self.firmware_channels.append(key)
            self._refresh_firmware_table()
            self._log(f"firmware channel added: {key}")

    def _remove_firmware_channel(self) -> None:
        rows = sorted({item.row() for item in self.firmware_table.selectedItems()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self.firmware_channels):
                removed = self.firmware_channels.pop(row)
                self._log(f"firmware channel removed: {removed}")
        self._refresh_firmware_table()

    def _apply_firmware_channels(self) -> None:
        payload = json.dumps({"channels": self.firmware_channels}).encode()
        transport = self._active_transport()
        if not transport:
            self._log("no USB/TCP transport for firmware channel config")
            return
        try:
            transport.send_wait(TYPE_SET_STREAM_CHANNELS, payload, timeout=3.0)
            self._log(f"applied firmware channels: {self.firmware_channels}")
        except Exception as exc:
            self._log(f"firmware channel apply failed: {exc}")

    def _add_vofa_channel(self) -> None:
        key = self._selected_source_key()
        if not key:
            return
        self.vofa_channels.append({"index": self.vofa_index.value(), "source": key, "label": key, "enabled": True})
        self.vofa_channels.sort(key=lambda item: int(item.get("index", 0)))
        self._refresh_channels_table()

    def _remove_vofa_channel(self) -> None:
        rows = sorted({item.row() for item in self.channels_table.selectedItems()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self.vofa_channels):
                del self.vofa_channels[row]
        self._refresh_channels_table()

    def _apply_can_ids(self) -> None:
        ids: list[int] = []
        for token in self.can_ids_edit.text().replace(";", ",").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                ids.append(int(token, 0) & 0x7ff)
            except ValueError:
                self._log(f"bad CAN id: {token}")
        self.can_ids = ids[:16]
        payload = json.dumps({"ids": self.can_ids}).encode()
        transport = self._active_transport()
        if transport:
            try:
                transport.send_wait(TYPE_SET_CAN_FORWARD_IDS, payload, timeout=3.0)
                self._log(f"applied CAN ids: {[hex(v) for v in self.can_ids]}")
            except Exception as exc:
                self._log(f"CAN apply failed: {exc}")

    def _send_vofa_frame(self) -> None:
        if self.selected_values:
            return
        self._enqueue_vofa_sample({})

    def _enqueue_vofa_sample(self, overrides: dict[str, float]) -> None:
        values: list[float] = []
        for mapping in sorted(self.vofa_channels, key=lambda item: int(item.get("index", 0))):
            if not mapping.get("enabled", True):
                continue
            source = str(mapping.get("source", ""))
            values.append(float(overrides.get(source, self.sources.get(source, SourceValue(source, source)).value)))
        if not values:
            return
        self.vofa_forwarder.enqueue(
            self.vofa_host.text().strip() or "127.0.0.1",
            self.vofa_remote.value(),
            self.vofa_local.value(),
            values,
        )

    def _sync_vofa_table_edits(self) -> None:
        rows = self.channels_table.rowCount()
        if rows != len(self.vofa_channels):
            return
        for row in range(rows):
            try:
                self.vofa_channels[row]["index"] = int(self.channels_table.item(row, 0).text())
                self.vofa_channels[row]["source"] = self.channels_table.item(row, 1).text()
                self.vofa_channels[row]["label"] = self.channels_table.item(row, 2).text()
            except (AttributeError, ValueError):
                continue

    def _mock_tick(self) -> None:
        self._mock_phase += 0.001
        t = self._mock_phase
        mock = [
            SourceValue("adc.battery_mv", "ADC Battery", "mV", 22000 + math.sin(t * 5) * 200, 500),
            SourceValue("adc.raw", "ADC Raw", "", 1800 + math.sin(t * 10) * 50, 500),
            SourceValue("justfloat.0", "JustFloat 0", "", math.sin(t * 20), 1000),
            SourceValue("justfloat.1", "JustFloat 1", "", math.cos(t * 13), 1000),
        ]
        for source in mock:
            self.sources[source.key] = source
        data = bytes([0x12, 0x34, 0x01, 0xF4, 0x00, 0x20, 45, 0])
        self.can_frames[0x201] = CanFrame(0x201, 8, data, int(time.time() * 1_000_000))
        for field, value in parse_dji_motor(0x201, data).items():
            key = f"can.0x201.{field}"
            self.sources[key] = SourceValue(key, key, "", value, 1000)
        self._refresh_sources_table()
        self._refresh_firmware_table()
        self._refresh_channels_table()
        self._refresh_can_table()

    def _toggle_mock(self, enabled: bool) -> None:
        self.mock_button.setText("Stop Mock" if enabled else "Start Mock")
        self.mock_timer.start(20) if enabled else self.mock_timer.stop()

    def _toggle_theme(self) -> None:
        from .theme import apply_dark_theme, apply_light_theme
        app = QtWidgets.QApplication.instance()
        if self.project.get("theme", "light") == "light":
            self.project["theme"] = "dark"
            apply_dark_theme(app)
        else:
            self.project["theme"] = "light"
            apply_light_theme(app)

    def _save_project(self) -> None:
        self._sync_vofa_table_edits()
        self.project.update({
            "vofa_host": self.vofa_host.text().strip() or "127.0.0.1",
            "vofa_remote_port": self.vofa_remote.value(),
            "vofa_local_port": self.vofa_local.value(),
            "firmware_channels": self.firmware_channels[:16],
            "can_ids": self.can_ids[:16],
            "vofa_channels": self.vofa_channels,
        })
        save_project(self.project_path, self.project)
        self._log(f"project saved: {self.project_path}")

    def _write_snapshot(self) -> None:
        data = {
            "tcp": self.tcp_label.text(),
            "usb": self.usb_label.text(),
            "udp": self.udp_label.text(),
            "sources": {key: source.__dict__ for key, source in self.sources.items()},
            "firmware_channels": self.firmware_channels,
            "vofa_channels": self.vofa_channels,
            "can_ids": self.can_ids,
            "can_rows": len(self.can_frames),
            "speaker": {
                "active": self.speaker_worker is not None,
                "udp_target": f"{self.speaker_udp_target[0]}:{self.speaker_udp_target[1]}" if self.speaker_udp_target else "",
                "udp_packets": self.speaker_udp_packets,
                "udp_bytes": self.speaker_udp_bytes,
                "udp_last_error": self.speaker_udp_last_error,
            },
            "updated_at": time.time(),
        }
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _status_json(self) -> dict:
        return {}

    def _alarm_max_seconds(self) -> float:
        total = DEFAULT_SPIFFS_TOTAL_BYTES
        power_file_max = int(BYTES_PER_SECOND * POWER_ON_MAX_SECONDS) + AUDIO_HEADER_ALLOWANCE_BYTES
        return max(1.0, (total - power_file_max - AUDIO_STORAGE_SAFETY_BYTES) / float(BYTES_PER_SECOND))

    def _pick_audio(self, kind: str) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Upload Audio", "", "Audio Files (*.wav *.mp3 *.flac *.ogg *.aiff *.aif *.m4a);;All Files (*)"
        )
        if path:
            self._upload_audio(kind, path)

    def _upload_audio(self, kind: str, path: str) -> None:
        transport = self._active_transport()
        if not transport:
            QtWidgets.QMessageBox.warning(self, "Audio Upload", "Connect USB or TCP before uploading audio.")
            return
        max_seconds = POWER_ON_MAX_SECONDS if kind == "poweron" else self._alarm_max_seconds()
        self.audio_upload_worker = AudioUploadWorker(transport, kind, path, max_seconds)
        self.audio_upload_worker.progress.connect(lambda k, s, t: self.audio_status.setText(f"upload {k}: {s}/{t}"))
        self.audio_upload_worker.finished_ok.connect(lambda k, size, gain: self.audio_status.setText(f"uploaded {k}: {size} bytes"))
        self.audio_upload_worker.failed.connect(lambda msg: QtWidgets.QMessageBox.warning(self, "Audio Upload", msg))
        self.audio_upload_worker.start()

    def _toggle_speaker(self, enabled: bool) -> None:
        if self._speaker_changing:
            return
        transport = self._active_transport(prefer_tcp=True)
        if enabled:
            peer_ip = self._speaker_peer_ip()
            if not transport or not peer_ip:
                QtWidgets.QMessageBox.warning(self, "Speaker Mode", "Connect TCP/Wi-Fi before starting low-latency speaker mode.")
                self._set_speaker_checked(False)
                return
            self._begin_speaker_control("start", transport, peer_ip)
        else:
            self._stop_local_speaker_capture()
            if transport:
                self._begin_speaker_control("stop", transport)
            else:
                self._finish_speaker_stopped()

    def _begin_speaker_control(self, action: str, transport, peer_ip: str = "") -> None:
        if self.speaker_control_worker and self.speaker_control_worker.isRunning():
            return
        self.speaker_button.setEnabled(False)
        self.speaker_button.setText("Starting Speaker Mode..." if action == "start" else "Stopping Speaker Mode...")
        self.audio_status.setText("speaker control pending")
        worker = SpeakerControlWorker(transport, action, peer_ip)
        worker.started.connect(self._finish_speaker_started)
        worker.stopped.connect(self._finish_speaker_stopped)
        worker.failed.connect(self._speaker_control_failed)
        worker.finished.connect(self._speaker_control_finished)
        self.speaker_control_worker = worker
        worker.start()

    def _finish_speaker_started(self, peer_ip: str) -> None:
        self._start_speaker_udp(peer_ip)
        self.speaker_worker = SpeakerCapture()
        self.speaker_worker.pcm_ready.connect(self._send_speaker_pcm, QtCore.Qt.DirectConnection)
        self.speaker_worker.state.connect(self._log)
        self.speaker_worker.failed.connect(self._speaker_capture_failed)
        self.speaker_worker.start()
        self.speaker_button.setText("Stop Speaker Mode")
        self.speaker_button.setEnabled(True)
        self.audio_status.setText(f"speaker UDP {peer_ip}:{SPEAKER_UDP_PORT}")

    def _finish_speaker_stopped(self) -> None:
        self._stop_local_speaker_capture()
        self.speaker_button.setText("Start Speaker Mode")
        self.speaker_button.setEnabled(True)
        self._set_speaker_checked(False)
        self.audio_status.setText("speaker stopped")

    def _speaker_control_failed(self, action: str, message: str) -> None:
        if action == "start":
            try:
                transport = self._active_transport(prefer_tcp=True)
                if transport:
                    transport.send(TYPE_AUDIO_STREAM_STOP, b"{}")
            except Exception:
                pass
            self._stop_local_speaker_capture()
            self._set_speaker_checked(False)
            self.speaker_button.setText("Start Speaker Mode")
        else:
            self.speaker_button.setText("Start Speaker Mode")
            self._set_speaker_checked(False)
        self.speaker_button.setEnabled(True)
        self.audio_status.setText(f"speaker {action} failed")
        QtWidgets.QMessageBox.warning(self, "Speaker Mode", message)

    def _speaker_control_finished(self) -> None:
        self.speaker_control_worker = None

    def _speaker_capture_failed(self, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, "Speaker Mode", message)
        self._stop_local_speaker_capture()
        self._set_speaker_checked(False)
        transport = self._active_transport(prefer_tcp=True)
        if transport:
            self._begin_speaker_control("stop", transport)

    def _set_speaker_checked(self, checked: bool) -> None:
        self._speaker_changing = True
        self.speaker_button.setChecked(checked)
        self._speaker_changing = False

    def _stop_local_speaker_capture(self) -> None:
        if self.speaker_worker:
            self._stop_worker(self.speaker_worker)
            self.speaker_worker = None
        self._close_speaker_udp()

    def _send_speaker_pcm(self, payload: bytes) -> None:
        if not self.speaker_udp or not self.speaker_udp_target:
            return
        self.speaker_pcm_buffer.extend(payload)
        packet_bytes = SPEAKER_UDP_PAYLOAD_SAMPLES * 2
        while len(self.speaker_pcm_buffer) >= packet_bytes:
            chunk = bytes(self.speaker_pcm_buffer[:packet_bytes])
            del self.speaker_pcm_buffer[:packet_bytes]
            packet = encode_speaker_udp_packet(self.speaker_udp_seq, int(time.monotonic() * 1_000_000), chunk)
            self.speaker_udp_seq = (self.speaker_udp_seq + 1) & 0xFFFFFFFF
            try:
                self.speaker_udp.sendto(packet, self.speaker_udp_target)
                self.speaker_udp_packets += 1
                self.speaker_udp_bytes += len(chunk)
            except OSError as exc:
                self.speaker_udp_last_error = str(exc)
                return

    def _start_speaker_udp(self, peer_ip: str) -> None:
        self._close_speaker_udp()
        self.speaker_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.speaker_udp_target = (peer_ip, SPEAKER_UDP_PORT)
        self.speaker_udp_seq = 0
        self.speaker_pcm_buffer.clear()
        self.speaker_udp_packets = 0
        self.speaker_udp_bytes = 0
        self.speaker_udp_last_error = ""

    def _close_speaker_udp(self) -> None:
        if self.speaker_udp:
            try:
                self.speaker_udp.close()
            except OSError:
                pass
        self.speaker_udp = None
        self.speaker_udp_target = None
        self.speaker_pcm_buffer.clear()

    def _auto_connect_serial(self) -> None:
        try:
            import serial.tools.list_ports
        except Exception:
            return
        self.refresh_ports()
        for port in serial.tools.list_ports.comports():
            if "VID:PID=303A:1001" in (port.hwid or "").upper():
                self._connect_serial(port.device)
                return

    def _connect_serial(self, port: str) -> None:
        if not port:
            self.refresh_ports()
            return
        if self.serial_worker:
            self._stop_worker(self.serial_worker)
        self.serial_worker = SerialWorker(port)
        self.serial_worker.frame_received.connect(self._handle_frame)
        self.serial_worker.state.connect(lambda text: self.usb_label.setText(f"USB {text}"))
        self.serial_worker.protocol_warning.connect(self._log)
        self.serial_worker.ready.connect(lambda: self._send(TYPE_GET_STATUS, b"{}"))
        self.serial_worker.start()

    def _tcp_changed(self, text: str) -> None:
        self.tcp_label.setText(f"TCP {text}")
        self._log(f"TCP {text}")

    def _hello_changed(self, text: str) -> None:
        self.udp_label.setText(f"UDP {text}")
        self._log(f"UDP hello {text}")

    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _stop_worker(self, worker: QtCore.QThread) -> None:
        if hasattr(worker, "stop"):
            worker.stop()
        worker.wait(2000)

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._save_project()
        if self.speaker_control_worker and self.speaker_control_worker.isRunning():
            self.speaker_control_worker.wait(9000)
        if self.speaker_worker:
            self._stop_local_speaker_capture()
            transport = self._active_transport(prefer_tcp=True)
            if transport:
                try:
                    transport.send_wait(TYPE_AUDIO_STREAM_STOP, b"{}", timeout=1.0)
                except Exception as exc:
                    self._log(f"speaker stop during shutdown failed: {exc}")
        for worker in (self.speaker_control_worker, self.speaker_worker, self.serial_worker, self.tcp, self.udp_hello, self.udp_tel, self.vofa_forwarder):
            if worker:
                self._stop_worker(worker)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
