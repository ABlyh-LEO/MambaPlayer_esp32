from __future__ import annotations

from dataclasses import dataclass
import struct


SYNC = b"ML"
VERSION = 2
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
TYPE_AUDIO_STREAM_START = 12
TYPE_AUDIO_STREAM_PCM = 13
TYPE_AUDIO_STREAM_STOP = 14
TYPE_SET_STREAM_CHANNELS = 15
TYPE_SET_CAN_FORWARD_IDS = 16
TYPE_TELEMETRY = 64

STREAM_CATALOG = 1
STREAM_SELECTED_VALUES = 2
STREAM_CAN_LAST = 3


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
    if len(payload) >= 4:
        count = struct.unpack_from("<H", payload, 0)[0]
        if count > 0 and len(payload) >= 4 + count * 24:
            frames = []
            for i in range(count):
                off = 4 + i * 24
                can_id = struct.unpack_from("<I", payload, off)[0]
                dlc = payload[off + 4]
                timestamp_us = struct.unpack_from("<Q", payload, off + 8)[0]
                data = payload[off + 16:off + 16 + min(dlc, 8)]
                frames.append({"id": can_id, "dlc": dlc, "timestamp_us": timestamp_us, "data": data})
            return {"frames": frames, "dropped": struct.unpack_from("<H", payload, 2)[0]}
    if len(payload) < 24:
        raise ValueError("CAN payload too short")
    can_id = struct.unpack_from("<I", payload, 0)[0]
    dlc = payload[4]
    timestamp_us = struct.unpack_from("<Q", payload, 8)[0]
    data = payload[16:16 + min(dlc, 8)]
    return {"id": can_id, "dlc": dlc, "timestamp_us": timestamp_us, "data": data}


def parse_justfloat_payload(payload: bytes) -> dict:
    if len(payload) >= 8:
        count = payload[0]
        if 0 < count <= 16 and len(payload) == 8 + count * 4:
            dropped = struct.unpack_from("<I", payload, 4)[0]
            values = struct.unpack_from("<" + "f" * count, payload, 8)
            return {"count": count, "dropped": dropped, "values": values}
    if len(payload) >= 4:
        frames = struct.unpack_from("<H", payload, 0)[0]
        if frames > 0:
            off = 4
            out = []
            for _ in range(frames):
                if len(payload) < off + 12:
                    raise ValueError("bad JustFloat batch")
                timestamp_us = struct.unpack_from("<Q", payload, off)[0]
                count = payload[off + 8]
                off += 12
                if count > 16 or len(payload) < off + count * 4:
                    raise ValueError("bad JustFloat count")
                values = struct.unpack_from("<" + "f" * count, payload, off)
                off += count * 4
                out.append({"timestamp_us": timestamp_us, "values": values})
            return {"frames": out, "dropped": struct.unpack_from("<H", payload, 2)[0]}
    raise ValueError("bad JustFloat payload")


def parse_adc_batch_payload(payload: bytes) -> dict:
    if len(payload) < 12:
        raise ValueError("ADC payload too short")
    sample_start, interval_us, count, dropped = struct.unpack_from("<IIHH", payload, 0)
    if len(payload) < 12 + count * 8:
        raise ValueError("bad ADC sample count")
    samples = []
    for i in range(count):
        off = 12 + i * 8
        battery_mv, pin_mv, raw = struct.unpack_from("<IHH", payload, off)
        samples.append({
            "sample": sample_start + i,
            "time_offset_us": interval_us * i,
            "battery_mv": battery_mv,
            "pin_mv": pin_mv,
            "raw": raw,
        })
    return {"sample_start": sample_start, "interval_us": interval_us, "dropped": dropped, "samples": samples}


def parse_rm_motor_payload(payload: bytes) -> dict:
    if len(payload) < 4:
        raise ValueError("RM motor payload too short")
    count, dropped = struct.unpack_from("<HH", payload, 0)
    if len(payload) < 4 + count * 24:
        raise ValueError("bad RM motor count")
    motors = []
    for i in range(count):
        off = 4 + i * 24
        timestamp_us = struct.unpack_from("<Q", payload, off)[0]
        motor_id = payload[off + 8]
        temperature = payload[off + 9]
        error = payload[off + 10]
        angle, rpm, torque_current, commanded_current = struct.unpack_from("<Hhhh", payload, off + 12)
        motors.append({
            "timestamp_us": timestamp_us,
            "motor_id": motor_id,
            "angle": angle,
            "rpm": rpm,
            "torque_current": torque_current,
            "commanded_current": commanded_current,
            "temperature": temperature,
            "error": error,
        })
    return {"motors": motors, "dropped": dropped}
