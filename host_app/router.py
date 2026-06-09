from __future__ import annotations

from dataclasses import dataclass, field
import json
import socket
import struct
import time
from pathlib import Path


VOFA_TAIL = b"\x00\x00\x80\x7f"
DEFAULT_PROJECT = {
    "project_version": 2,
    "vofa_host": "127.0.0.1",
    "vofa_remote_port": 1347,
    "vofa_local_port": 1346,
    "firmware_channels": [],
    "can_ids": [],
    "vofa_channels": [],
    "theme": "light",
}


@dataclass
class SourceValue:
    key: str
    name: str
    unit: str = ""
    value: float = 0.0
    rate_hz: int = 0
    updated_at: float = field(default_factory=time.monotonic)


@dataclass
class VofaChannel:
    index: int
    source: str
    label: str = ""
    enabled: bool = True


@dataclass
class CanFrame:
    can_id: int
    dlc: int
    data: bytes
    timestamp_us: int
    seen_at: float = field(default_factory=time.monotonic)

    @property
    def data_hex(self) -> str:
        return " ".join(f"{byte:02X}" for byte in self.data[:self.dlc])


def encode_justfloat(values: list[float]) -> bytes:
    return struct.pack("<" + "f" * len(values), *values) + VOFA_TAIL if values else VOFA_TAIL


def load_project(path: Path) -> dict:
    if not path.exists():
        return dict(DEFAULT_PROJECT)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    project = dict(DEFAULT_PROJECT)
    project.update(data)
    if int(data.get("project_version") or 0) < 2:
        if int(project.get("vofa_remote_port") or 0) == 1346 and int(project.get("vofa_local_port") or 0) == 1347:
            project["vofa_remote_port"] = 1347
            project["vofa_local_port"] = 1346
    project["project_version"] = DEFAULT_PROJECT["project_version"]
    return project


def save_project(path: Path, project: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_catalog(payload: bytes) -> list[SourceValue]:
    data = json.loads(payload.decode(errors="replace"))
    out: list[SourceValue] = []
    for item in data.get("sources", []):
        try:
            out.append(SourceValue(
                key=str(item["key"]),
                name=str(item.get("name") or item["key"]),
                unit=str(item.get("unit") or ""),
                value=float(item.get("value") or 0.0),
                rate_hz=int(item.get("rate_hz") or 0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_selected_values(payload: bytes) -> list[float]:
    batch = parse_selected_batch(payload)
    return batch[-1] if batch else []


def parse_selected_batch(payload: bytes) -> list[list[float]]:
    if len(payload) < 4:
        raise ValueError("selected-values payload too short")
    count = payload[0]
    sample_count = payload[1]
    if count > 16:
        raise ValueError("bad selected-values count")
    if count == 0:
        return []
    if sample_count == 0:
        if len(payload) < 4 + count * 4:
            raise ValueError("bad selected-values count")
        return [list(struct.unpack_from("<" + "f" * count, payload, 4))]
    if sample_count > 32:
        raise ValueError("bad selected-values sample count")
    expected = 4 + sample_count * count * 4
    if len(payload) < expected:
        raise ValueError("bad selected-values batch size")
    out: list[list[float]] = []
    offset = 4
    for _sample in range(sample_count):
        out.append(list(struct.unpack_from("<" + "f" * count, payload, offset)))
        offset += count * 4
    return out


def parse_can_last(payload: bytes) -> list[CanFrame]:
    if len(payload) < 4:
        raise ValueError("CAN-last payload too short")
    count = struct.unpack_from("<H", payload, 0)[0]
    if count > 16 or len(payload) < 4 + count * 24:
        raise ValueError("bad CAN-last count")
    frames: list[CanFrame] = []
    for i in range(count):
        off = 4 + i * 24
        can_id = struct.unpack_from("<I", payload, off)[0]
        dlc = min(payload[off + 4], 8)
        timestamp_us = struct.unpack_from("<Q", payload, off + 8)[0]
        frames.append(CanFrame(can_id, dlc, payload[off + 16:off + 16 + dlc], timestamp_us))
    return frames


def parse_dji_motor(can_id: int, data: bytes) -> dict[str, float]:
    if can_id < 0x201 or can_id > 0x208 or len(data) < 8:
        return {}
    angle = struct.unpack_from(">H", data, 0)[0]
    rpm = struct.unpack_from(">h", data, 2)[0]
    torque = struct.unpack_from(">h", data, 4)[0]
    return {
        "angle": float(angle),
        "rpm": float(rpm),
        "torque_current": float(torque),
        "temperature": float(data[6]),
        "error": float(data[7]),
    }


class VofaUdpSender:
    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._bound_port: int | None = None

    def configure(self, local_port: int) -> None:
        if self._sock and self._bound_port == local_port:
            return
        self.close()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", int(local_port)))
        self._bound_port = int(local_port)

    def send(self, host: str, port: int, values: list[float]) -> None:
        if not self._sock:
            self.configure(DEFAULT_PROJECT["vofa_local_port"])
        assert self._sock is not None
        self._sock.sendto(encode_justfloat(values), (host, int(port)))

    def close(self) -> None:
        if self._sock:
            self._sock.close()
        self._sock = None
        self._bound_port = None
