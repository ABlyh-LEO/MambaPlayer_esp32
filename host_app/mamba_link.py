from __future__ import annotations

from dataclasses import dataclass
import struct


SYNC = b"ML"
VERSION = 1
MAX_PAYLOAD = 1024

TYPE_HELLO = 1
TYPE_GET_STATUS = 2
TYPE_STATUS = 3
TYPE_SET_CONFIG = 4
TYPE_AUDIO_BEGIN = 5
TYPE_AUDIO_CHUNK = 6
TYPE_AUDIO_END = 7
TYPE_ACK = 8
TYPE_AUDIO_TEST = 9
TYPE_ERROR = 10
TYPE_WIFI_CONFIG = 11
TYPE_TELEMETRY = 64

STREAM_BATTERY = 1
STREAM_CAN_RAW = 2
STREAM_RM_MOTOR = 3
STREAM_JUSTFLOAT = 4
STREAM_ALARM = 5


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass
class Frame:
    msg_type: int
    seq: int
    payload: bytes = b""


def encode_frame(msg_type: int, payload: bytes = b"", seq: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    header = bytearray(12)
    header[0:2] = SYNC
    header[2] = VERSION
    header[3] = msg_type
    struct.pack_into("<HHH", header, 6, seq & 0xFFFF, len(payload), crc16_ccitt(payload))
    return bytes(header) + payload


class FrameParser:
    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        out: list[Frame] = []
        self._buf.extend(data)
        while True:
            start = self._buf.find(SYNC)
            if start < 0:
                self._buf.clear()
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < 12:
                break
            if self._buf[2] != VERSION:
                del self._buf[:2]
                continue
            msg_type = self._buf[3]
            seq, length, crc = struct.unpack_from("<HHH", self._buf, 6)
            if length > MAX_PAYLOAD:
                del self._buf[:2]
                continue
            total = 12 + length
            if len(self._buf) < total:
                break
            payload = bytes(self._buf[12:total])
            del self._buf[:total]
            if crc16_ccitt(payload) == crc:
                out.append(Frame(msg_type, seq, payload))
        return out


def decode_udp_packet(packet: bytes) -> dict:
    if len(packet) < 20 or packet[:2] != b"MT":
        raise ValueError("not a Mamba telemetry packet")
    version = packet[2]
    stream_id = packet[3]
    seq = struct.unpack_from("<I", packet, 4)[0]
    timestamp_us = struct.unpack_from("<Q", packet, 8)[0]
    length = struct.unpack_from("<H", packet, 16)[0]
    payload = packet[20:20 + length]
    if version != VERSION or len(payload) != length:
        raise ValueError("bad telemetry packet")
    return {
        "stream_id": stream_id,
        "seq": seq,
        "timestamp_us": timestamp_us,
        "payload": payload,
    }


def parse_can_payload(payload: bytes) -> dict:
    if len(payload) < 24:
        raise ValueError("CAN payload too short")
    can_id = struct.unpack_from("<I", payload, 0)[0]
    dlc = payload[4]
    timestamp_us = struct.unpack_from("<Q", payload, 8)[0]
    data = payload[16:16 + min(dlc, 8)]
    return {"id": can_id, "dlc": dlc, "timestamp_us": timestamp_us, "data": data}


def parse_justfloat_payload(payload: bytes) -> dict:
    if len(payload) < 8:
        raise ValueError("JustFloat payload too short")
    count = payload[0]
    dropped = struct.unpack_from("<I", payload, 4)[0]
    if count > 16 or len(payload) < 8 + count * 4:
        raise ValueError("bad JustFloat count")
    values = struct.unpack_from("<" + "f" * count, payload, 8)
    return {"count": count, "dropped": dropped, "values": values}
