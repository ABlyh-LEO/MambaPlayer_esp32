from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

try:
    import serial
except Exception:  # pragma: no cover
    serial = None

try:
    from .mamba_link import (
        TYPE_ACK,
        TYPE_AUDIO_BEGIN,
        TYPE_AUDIO_CHUNK,
        TYPE_AUDIO_END,
        TYPE_ERROR,
        TYPE_GET_STATUS,
        TYPE_HELLO,
        TYPE_SET_CAN_FORWARD_IDS,
        TYPE_SET_STREAM_CHANNELS,
        TYPE_STATUS,
        TYPE_WIFI_CONFIG,
        FrameParser,
        decode_udp_packet,
        encode_frame,
    )
    from .audio_tools import BYTES_PER_SECOND, POWER_ON_MAX_SECONDS, STORAGE_PARTITION_BYTES, convert_to_mamba_wav
    from .router import encode_justfloat
except ImportError:
    from mamba_link import (
        TYPE_ACK,
        TYPE_AUDIO_BEGIN,
        TYPE_AUDIO_CHUNK,
        TYPE_AUDIO_END,
        TYPE_ERROR,
        TYPE_GET_STATUS,
        TYPE_HELLO,
        TYPE_SET_CAN_FORWARD_IDS,
        TYPE_SET_STREAM_CHANNELS,
        TYPE_STATUS,
        TYPE_WIFI_CONFIG,
        FrameParser,
        decode_udp_packet,
        encode_frame,
    )
    from audio_tools import BYTES_PER_SECOND, POWER_ON_MAX_SECONDS, STORAGE_PARTITION_BYTES, convert_to_mamba_wav
    from router import encode_justfloat


AUDIO_CHUNK_SIZE = 384
AUDIO_HEADER_ALLOWANCE_BYTES = 4096
AUDIO_STORAGE_SAFETY_BYTES = 32 * 1024
DEFAULT_SPIFFS_TOTAL_BYTES = STORAGE_PARTITION_BYTES


def tcp_status(host: str, port: int, timeout: float) -> int:
    parser = FrameParser()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(encode_frame(TYPE_HELLO, b'{"probe":true}', 1))
        sock.sendall(encode_frame(TYPE_GET_STATUS, b"{}", 2))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = sock.recv(2048)
            for frame in parser.feed(data):
                print(json.dumps({"type": frame.msg_type, "seq": frame.seq, "payload": frame.payload.decode(errors="replace")}, ensure_ascii=False))
                return 0
    return 1


def udp_listen(port: int, timeout: float) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(timeout)
    try:
        data, addr = sock.recvfrom(2048)
    except socket.timeout:
        return 1
    if port == 37212:
        item = decode_udp_packet(data)
        item["payload_hex"] = item.pop("payload").hex()
        item["addr"] = addr[0]
        print(json.dumps(item, ensure_ascii=False))
    else:
        print(json.dumps({"addr": addr[0], "payload": data.decode(errors="replace")}, ensure_ascii=False))
    return 0


def tcp_send_wait(host: str, port: int, msg_type: int, payload: bytes, timeout: float) -> bytes:
    parser = FrameParser()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(encode_frame(msg_type, payload, 1))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = sock.recv(2048)
            for frame in parser.feed(data):
                if frame.seq != 1:
                    continue
                if frame.msg_type == TYPE_ACK:
                    return frame.payload
                if frame.msg_type == TYPE_ERROR:
                    raise RuntimeError(frame.payload.decode(errors="replace"))
    raise TimeoutError("timeout waiting for ACK")


def tcp_set_stream(host: str, port: int, channels: list[str], timeout: float) -> int:
    response = tcp_send_wait(host, port, TYPE_SET_STREAM_CHANNELS, json.dumps({"channels": channels}).encode(), timeout)
    print(response.decode(errors="replace"))
    return 0


def tcp_can_filter(host: str, port: int, ids: list[str], timeout: float) -> int:
    parsed = [int(item, 0) for item in ids]
    response = tcp_send_wait(host, port, TYPE_SET_CAN_FORWARD_IDS, json.dumps({"ids": parsed}).encode(), timeout)
    print(response.decode(errors="replace"))
    return 0


def vofa_test(host: str, remote_port: int, local_port: int, values: list[float]) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", local_port))
    sock.sendto(encode_justfloat(values), (host, remote_port))
    sock.close()
    print(json.dumps({"sent": values, "remote": f"{host}:{remote_port}", "local_port": local_port}))
    return 0


def wait_usb_ready(ser, parser: FrameParser, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = ser.read(512)
        if parser.feed(data):
            return


def read_usb_status_frame(ser, parser: FrameParser, seq: int, timeout: float) -> dict:
    ser.write(encode_frame(TYPE_GET_STATUS, b"{}", seq))
    ser.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = ser.read(512)
        for frame in parser.feed(data):
            if frame.msg_type == TYPE_STATUS:
                return json.loads(frame.payload.decode(errors="replace"))
    return {}


def send_wait_usb(ser, parser: FrameParser, msg_type: int, payload: bytes, seq: int, timeout: float) -> bytes:
    ser.write(encode_frame(msg_type, payload, seq))
    ser.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = ser.read(512)
        for frame in parser.feed(data):
            if frame.seq != seq:
                continue
            if frame.msg_type == TYPE_ACK:
                return frame.payload
            if frame.msg_type == TYPE_ERROR:
                raise RuntimeError(frame.payload.decode(errors="replace"))
    raise TimeoutError(f"timeout waiting for ACK seq={seq}")


def usb_status(port: str, baud: int, timeout: float) -> int:
    if serial is None:
        print("pyserial not installed", file=sys.stderr)
        return 2
    parser = FrameParser()
    with serial.Serial(port, baud, timeout=0.1, write_timeout=timeout) as ser:
        wait_usb_ready(ser, parser, timeout)
        ser.write(encode_frame(TYPE_GET_STATUS, b"{}", 1))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = ser.read(512)
            for frame in parser.feed(data):
                if frame.msg_type == TYPE_STATUS:
                    print(json.dumps({"type": frame.msg_type, "seq": frame.seq, "payload": frame.payload.decode(errors="replace")}, ensure_ascii=False))
                    return 0
    return 1


def usb_wifi(port: str, ssid: str, password: str, baud: int, timeout: float) -> int:
    if serial is None:
        print("pyserial not installed", file=sys.stderr)
        return 2
    payload = json.dumps({"ssid": ssid, "password": password}).encode()
    parser = FrameParser()
    with serial.Serial(port, baud, timeout=0.1, write_timeout=timeout) as ser:
        wait_usb_ready(ser, parser, timeout)
        ser.write(encode_frame(TYPE_WIFI_CONFIG, payload, 1))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = ser.read(512)
            for frame in parser.feed(data):
                print(json.dumps({"type": frame.msg_type, "seq": frame.seq, "payload": frame.payload.decode(errors="replace")}, ensure_ascii=False))
                return 0
    return 1


def alarm_max_seconds_from_status(status: dict) -> float:
    storage = status.get("storage", {})
    total = int(storage.get("total") or DEFAULT_SPIFFS_TOTAL_BYTES)
    power_file_max = int(BYTES_PER_SECOND * POWER_ON_MAX_SECONDS) + AUDIO_HEADER_ALLOWANCE_BYTES
    alarm_bytes = max(0, total - power_file_max - AUDIO_STORAGE_SAFETY_BYTES)
    return max(1.0, alarm_bytes / float(BYTES_PER_SECOND))


def usb_upload_audio(port: str, kind: str, path: str, baud: int, timeout: float,
                     max_seconds: float | None, trim: bool) -> int:
    if serial is None:
        print("pyserial not installed", file=sys.stderr)
        return 2
    if kind not in ("alarm", "poweron"):
        print("kind must be alarm or poweron", file=sys.stderr)
        return 2
    parser = FrameParser()
    with serial.Serial(port, baud, timeout=0.1, write_timeout=timeout) as ser:
        wait_usb_ready(ser, parser, timeout)
        status = read_usb_status_frame(ser, parser, 1, timeout)
        limit = max_seconds
        if limit is None:
            limit = POWER_ON_MAX_SECONDS if kind == "poweron" else alarm_max_seconds_from_status(status)
        wav = convert_to_mamba_wav(path, max_seconds=limit, trim=trim)
        begin = json.dumps({"kind": kind, "size": len(wav)}).encode()
        seq = 2
        send_wait_usb(ser, parser, TYPE_AUDIO_BEGIN, begin, seq, timeout)
        seq += 1
        sent = 0
        while sent < len(wav):
            chunk = wav[sent:sent + AUDIO_CHUNK_SIZE]
            send_wait_usb(ser, parser, TYPE_AUDIO_CHUNK, chunk, seq, timeout)
            seq = (seq + 1) & 0xFFFF or 1
            sent += len(chunk)
            if sent == len(wav) or sent % (32 * 1024) < AUDIO_CHUNK_SIZE:
                print(f"\ruploading {kind}: {sent}/{len(wav)} bytes", end="", flush=True)
        print()
        send_wait_usb(ser, parser, TYPE_AUDIO_END, b"{}", seq, timeout)
        status = read_usb_status_frame(ser, parser, (seq + 1) & 0xFFFF or 1, timeout)
        print(json.dumps({"uploaded": kind, "source": path, "bytes": len(wav), "status": status}, ensure_ascii=False))
    return 0


def snapshot(path: str | None = None) -> int:
    state_path = Path(path) if path else Path(__file__).resolve().parent / "runtime" / "state.json"
    if not state_path.exists():
        print(f"snapshot not found: {state_path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        print(f"invalid snapshot JSON: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mamba host diagnostic probe")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_tcp = sub.add_parser("tcp-status")
    p_tcp.add_argument("--host", default="127.0.0.1")
    p_tcp.add_argument("--port", type=int, default=37210)
    p_tcp.add_argument("--timeout", type=float, default=3.0)
    p_udp = sub.add_parser("udp-once")
    p_udp.add_argument("--port", type=int, default=37212)
    p_udp.add_argument("--timeout", type=float, default=5.0)
    p_usb_status = sub.add_parser("usb-status")
    p_usb_status.add_argument("--port", required=True)
    p_usb_status.add_argument("--baud", type=int, default=115200)
    p_usb_status.add_argument("--timeout", type=float, default=5.0)
    p_usb = sub.add_parser("usb-wifi")
    p_usb.add_argument("--port", required=True)
    p_usb.add_argument("--ssid", required=True)
    p_usb.add_argument("--password", default="")
    p_usb.add_argument("--baud", type=int, default=115200)
    p_usb.add_argument("--timeout", type=float, default=3.0)
    p_upload = sub.add_parser("usb-upload-audio")
    p_upload.add_argument("--port", required=True)
    p_upload.add_argument("--kind", choices=("alarm", "poweron"), required=True)
    p_upload.add_argument("--file", required=True)
    p_upload.add_argument("--baud", type=int, default=115200)
    p_upload.add_argument("--timeout", type=float, default=5.0)
    p_upload.add_argument("--max-seconds", type=float)
    p_upload.add_argument("--trim", action="store_true")
    p_snapshot = sub.add_parser("snapshot")
    p_snapshot.add_argument("--path")
    p_stream = sub.add_parser("set-stream")
    p_stream.add_argument("--host", default="127.0.0.1")
    p_stream.add_argument("--port", type=int, default=37210)
    p_stream.add_argument("--timeout", type=float, default=3.0)
    p_stream.add_argument("channels", nargs="*")
    p_can = sub.add_parser("can-filter")
    p_can.add_argument("--host", default="127.0.0.1")
    p_can.add_argument("--port", type=int, default=37210)
    p_can.add_argument("--timeout", type=float, default=3.0)
    p_can.add_argument("ids", nargs="*")
    p_catalog = sub.add_parser("catalog")
    p_catalog.add_argument("--port", type=int, default=37212)
    p_catalog.add_argument("--timeout", type=float, default=5.0)
    p_vofa = sub.add_parser("vofa-test")
    p_vofa.add_argument("--host", default="127.0.0.1")
    p_vofa.add_argument("--remote-port", type=int, default=1347)
    p_vofa.add_argument("--local-port", type=int, default=1346)
    p_vofa.add_argument("values", nargs="*", type=float)
    args = parser.parse_args(argv)
    if args.cmd == "tcp-status":
        return tcp_status(args.host, args.port, args.timeout)
    if args.cmd == "udp-once":
        return udp_listen(args.port, args.timeout)
    if args.cmd == "usb-status":
        return usb_status(args.port, args.baud, args.timeout)
    if args.cmd == "usb-wifi":
        return usb_wifi(args.port, args.ssid, args.password, args.baud, args.timeout)
    if args.cmd == "usb-upload-audio":
        return usb_upload_audio(args.port, args.kind, args.file, args.baud, args.timeout, args.max_seconds, args.trim)
    if args.cmd == "snapshot":
        return snapshot(args.path)
    if args.cmd == "set-stream":
        return tcp_set_stream(args.host, args.port, args.channels, args.timeout)
    if args.cmd == "can-filter":
        return tcp_can_filter(args.host, args.port, args.ids, args.timeout)
    if args.cmd == "catalog":
        return udp_listen(args.port, args.timeout)
    if args.cmd == "vofa-test":
        return vofa_test(args.host, args.remote_port, args.local_port, args.values or [1.0, -1.0])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
