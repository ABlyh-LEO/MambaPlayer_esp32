import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtWidgets

from .telemetry.store import TelemetryStore
from .ui.main_window import MainWindow
from .ui.theme import apply_dark_theme


class TelemetryStoreTests(unittest.TestCase):
    def test_ring_buffer_crops_old_samples(self):
        store = TelemetryStore(capacity=3)
        for i in range(5):
            store.append_sample("x", "X", float(i), timestamp_s=float(i))
        channel = store.channels["x"]
        t, v = channel.arrays()
        np.testing.assert_array_equal(t, np.array([2.0, 3.0, 4.0]))
        np.testing.assert_array_equal(v, np.array([2.0, 3.0, 4.0], dtype=np.float32))
        self.assertEqual(channel.count, 3)
        self.assertEqual(channel.stats.samples, 5)

    def test_scale_offset_applies_before_storage(self):
        store = TelemetryStore(capacity=8)
        store.ensure_channel("adc", "ADC")
        store.set_channel_scale("adc", 2.0, -1.0)
        store.append_sample("adc", "ADC", 3.5, timestamp_s=1.0)
        _, values = store.channels["adc"].arrays()
        self.assertAlmostEqual(float(values[-1]), 6.0)
        self.assertEqual(store.channels["adc"].snapshot()["scale"], 2.0)

    def test_tail_arrays_returns_visible_window_after_wrap(self):
        store = TelemetryStore(capacity=5)
        for i in range(8):
            store.append_sample("adc", "ADC", float(i), timestamp_s=float(i))
        times, values = store.channels["adc"].tail_arrays(2.5)
        np.testing.assert_array_equal(times, np.array([5.0, 6.0, 7.0]))
        np.testing.assert_array_equal(values, np.array([5.0, 6.0, 7.0], dtype=np.float32))

    def test_channel_enable_and_udp_drop_count(self):
        store = TelemetryStore(capacity=8)
        store.append_sample("jf0", "JF0", 1.0, timestamp_s=1.0)
        store.set_channel_enabled("jf0", False)
        self.assertFalse(store.channels["jf0"].enabled)
        store.note_udp_seq(3, 10)
        store.note_udp_seq(3, 14)
        self.assertEqual(store.state.total_udp_dropped, 3)
        self.assertEqual(store.channels["jf0"].stats.dropped, 3)


class HostGuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        apply_dark_theme(cls.app)

    def test_main_window_renders_mock_waveform(self):
        window = MainWindow(start_workers=False)
        try:
            window._mock_tick()
            self.app.processEvents()
            window.wave.refresh()
            self.app.processEvents()
            self.assertGreater(len(window.store.channels), 0)
            self.assertGreater(sum(ch.count for ch in window.store.channels.values()), 0)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "wave.png"
                image = window.wave.grab().toImage()
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(path)))
                self.assertGreater(path.stat().st_size, 0)
        finally:
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
