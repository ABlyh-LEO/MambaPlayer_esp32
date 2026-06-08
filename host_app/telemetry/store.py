from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable

from PySide6 import QtCore

from .models import AppState, CanRow, Channel, DEFAULT_CAPACITY


PALETTE = [
    "#00d1ff", "#ffcc00", "#ff4f81", "#72e06a", "#b178ff", "#ff8c32",
    "#00ffa3", "#ff5ef1", "#91a7ff", "#f4f8ff", "#f75940", "#42f5e6",
]


class TelemetryStore(QtCore.QObject):
    changed = QtCore.Signal()
    channels_changed = QtCore.Signal()
    can_changed = QtCore.Signal()
    state_changed = QtCore.Signal()

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__()
        self.capacity = capacity
        self.channels: dict[str, Channel] = {}
        self.can_rows: list[CanRow] = []
        self.state = AppState()
        self._last_seq: dict[int, int] = {}
        self._last_emit = 0.0
        self._last_can_emit = 0.0

    def ensure_channel(self, key: str, name: str, unit: str = "", color: str | None = None) -> Channel:
        channel = self.channels.get(key)
        if channel is None:
            color = color or PALETTE[len(self.channels) % len(PALETTE)]
            channel = Channel(key=key, name=name, unit=unit, color=color, capacity=self.capacity)
            self.channels[key] = channel
            self.channels_changed.emit()
        return channel

    def append_sample(self, key: str, name: str, value: float, timestamp_s: float | None = None, unit: str = "") -> None:
        channel = self.ensure_channel(key, name, unit)
        channel.append(time.monotonic() if timestamp_s is None else timestamp_s, value)
        self._emit_changed_throttled()

    def append_samples(self, samples: Iterable[tuple[str, str, float, str]], timestamp_s: float | None = None) -> None:
        t = time.monotonic() if timestamp_s is None else timestamp_s
        for key, name, value, unit in samples:
            self.ensure_channel(key, name, unit).append(t, value)
        self._emit_changed_throttled()

    def clear(self) -> None:
        for channel in self.channels.values():
            channel.clear()
        self.can_rows.clear()
        self.can_changed.emit()
        self.changed.emit()
        self.channels_changed.emit()

    def set_channel_enabled(self, key: str, enabled: bool) -> None:
        if key in self.channels:
            self.channels[key].enabled = enabled
            self.channels_changed.emit()
            self.changed.emit()

    def set_channel_scale(self, key: str, scale: float, offset: float) -> None:
        if key in self.channels:
            self.channels[key].scale = scale
            self.channels[key].offset = offset
            self.channels_changed.emit()

    def set_channel_color(self, key: str, color: str) -> None:
        if key in self.channels:
            self.channels[key].color = color
            self.channels_changed.emit()
            self.changed.emit()

    def note_udp_seq(self, stream_id: int, seq: int) -> None:
        last = self._last_seq.get(stream_id)
        if last is not None and seq != last + 1:
            missed = max(0, seq - last - 1)
            self.state.total_udp_dropped += missed
            for channel in self.channels.values():
                channel.stats.dropped += missed
        self._last_seq[stream_id] = seq

    def append_can(self, timestamp_us: int, can_id: int, dlc: int, data: bytes) -> None:
        self.can_rows.insert(0, CanRow(timestamp_us, can_id, dlc, " ".join(f"{b:02X}" for b in data)))
        del self.can_rows[500:]
        now = time.monotonic()
        if now - self._last_can_emit >= 1 / 20:
            self._last_can_emit = now
            self.can_changed.emit()

    def update_state(self, **kwargs: str) -> None:
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        self.state_changed.emit()

    def snapshot(self) -> dict:
        return {
            "state": self.state.__dict__.copy(),
            "channels": [channel.snapshot() for channel in self.channels.values()],
            "can_rows": len(self.can_rows),
            "updated_at": time.time(),
        }

    def write_snapshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")

    def _emit_changed_throttled(self) -> None:
        now = time.monotonic()
        if now - self._last_emit >= 1 / 45:
            self._last_emit = now
            self.changed.emit()
