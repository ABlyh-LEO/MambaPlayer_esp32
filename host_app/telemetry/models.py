from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


DEFAULT_CAPACITY = 1_000_000


@dataclass
class ChannelStats:
    samples: int = 0
    dropped: int = 0
    min_value: float = 0.0
    max_value: float = 0.0
    latest: float = 0.0


@dataclass
class Channel:
    key: str
    name: str
    unit: str = ""
    color: str = "#00d1ff"
    enabled: bool = True
    scale: float = 1.0
    offset: float = 0.0
    capacity: int = DEFAULT_CAPACITY
    stats: ChannelStats = field(default_factory=ChannelStats)

    def __post_init__(self) -> None:
        self._t = np.empty(self.capacity, dtype=np.float64)
        self._v = np.empty(self.capacity, dtype=np.float32)
        self._start = 0
        self._count = 0

    def append(self, t: float, raw_value: float) -> None:
        value = raw_value * self.scale + self.offset
        idx = (self._start + self._count) % self.capacity
        if self._count == self.capacity:
            self._start = (self._start + 1) % self.capacity
        else:
            self._count += 1
        self._t[idx] = t
        self._v[idx] = value
        self.stats.samples += 1
        self.stats.latest = value
        if self.stats.samples == 1:
            self.stats.min_value = value
            self.stats.max_value = value
        else:
            self.stats.min_value = min(self.stats.min_value, value)
            self.stats.max_value = max(self.stats.max_value, value)

    def clear(self) -> None:
        self._start = 0
        self._count = 0
        self.stats = ChannelStats(dropped=self.stats.dropped)

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        if self._count == 0:
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float32)
        if self._start + self._count <= self.capacity:
            return (
                self._t[self._start:self._start + self._count].copy(),
                self._v[self._start:self._start + self._count].copy(),
            )
        first = self.capacity - self._start
        t = np.concatenate((self._t[self._start:], self._t[:self._count - first]))
        v = np.concatenate((self._v[self._start:], self._v[:self._count - first]))
        return t, v

    def tail_arrays(self, window_seconds: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        if self._count == 0:
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float32)
        latest_idx = (self._start + self._count - 1) % self.capacity
        latest = float(self._t[latest_idx])
        if window_seconds is None or window_seconds <= 0:
            return self.arrays()

        cutoff = latest - window_seconds
        if self._start + self._count <= self.capacity:
            t_view = self._t[self._start:self._start + self._count]
            v_view = self._v[self._start:self._start + self._count]
            first = int(np.searchsorted(t_view, cutoff, side="left"))
            return t_view[first:].copy(), v_view[first:].copy()

        first_len = self.capacity - self._start
        first_t = self._t[self._start:]
        if first_t.size and cutoff <= float(first_t[-1]):
            first_idx = int(np.searchsorted(first_t, cutoff, side="left"))
            return (
                np.concatenate((first_t[first_idx:], self._t[:self._count - first_len])),
                np.concatenate((self._v[self._start + first_idx:], self._v[:self._count - first_len])),
            )
        second_t = self._t[:self._count - first_len]
        first_idx = int(np.searchsorted(second_t, cutoff, side="left"))
        return second_t[first_idx:].copy(), self._v[first_idx:self._count - first_len].copy()

    def visible_arrays(self, xmin: float | None = None, xmax: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        t, v = self.arrays()
        if t.size == 0 or xmin is None or xmax is None:
            return t, v
        mask = (t >= xmin) & (t <= xmax)
        return t[mask], v[mask]

    @property
    def count(self) -> int:
        return self._count

    def snapshot(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "unit": self.unit,
            "color": self.color,
            "enabled": self.enabled,
            "scale": self.scale,
            "offset": self.offset,
            "count": self._count,
            "samples": self.stats.samples,
            "dropped": self.stats.dropped,
            "latest": self.stats.latest,
            "min": self.stats.min_value,
            "max": self.stats.max_value,
        }


@dataclass
class CanRow:
    timestamp_us: int
    can_id: int
    dlc: int
    data_hex: str


@dataclass
class AppState:
    tcp: str = "waiting"
    usb: str = "disconnected"
    udp_hello: str = "none"
    last_status: str = ""
    total_udp_dropped: int = 0
