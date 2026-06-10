import json
import os
import struct
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtWidgets

from .telemetry.store import TelemetryStore
from .mamba_link import SPEAKER_SAMPLE_RATE, TYPE_AUDIO_STREAM_START, TYPE_AUDIO_STREAM_STOP, TYPE_STATUS, TYPE_TELEMETRY
from .ui.main_window import MainWindow, SpeakerControlWorker
from .ui.theme import apply_light_theme


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
        apply_light_theme(cls.app)

    def test_main_window_renders_mock_data_hub(self):
        window = MainWindow(start_workers=False)
        try:
            window._mock_tick()
            self.app.processEvents()
            self.assertGreater(len(window.sources), 0)
            self.assertGreater(window.sources_table.rowCount(), 0)
            self.assertGreater(window.can_table.rowCount(), 0)
            image = window.grab().toImage()
            self.assertFalse(image.isNull())
            self.assertGreater(image.width() * image.height(), 0)
        finally:
            window.close()
            self.app.processEvents()

    def test_source_selection_survives_mock_refresh(self):
        window = MainWindow(start_workers=False)
        try:
            window._mock_tick()
            self.app.processEvents()
            index = window.source_combo.findData("justfloat.1")
            self.assertGreaterEqual(index, 0)
            window.source_combo.setCurrentIndex(index)
            window._mock_tick()
            self.app.processEvents()
            self.assertEqual(window.source_combo.currentData(), "justfloat.1")
        finally:
            window.close()
            self.app.processEvents()

    def test_telemetry_catalog_frame_populates_sources(self):
        window = MainWindow(start_workers=False)
        try:
            payload = b'{"type":"catalog","sources":[{"key":"adc.raw","name":"ADC Raw","unit":"","value":123,"rate_hz":500}]}'
            window._handle_frame(TYPE_TELEMETRY, payload)
            self.app.processEvents()
            self.assertIn("adc.raw", window.sources)
            self.assertEqual(window.sources_table.rowCount(), 1)
        finally:
            window.close()
            self.app.processEvents()

    def test_status_frame_populates_fallback_sources(self):
        window = MainWindow(start_workers=False)
        try:
            payload = b'{"battery":{"adc_mv":22000,"capacity":42,"fused_mv":22100,"current_ma":-120,"temp_decic":265}}'
            window._handle_frame(TYPE_STATUS, payload)
            self.app.processEvents()
            self.assertEqual(window.sources["adc.battery_mv"].value, 22000)
            self.assertEqual(window.sources["i2c.capacity"].value, 42)
        finally:
            window.close()
            self.app.processEvents()

    def test_selected_batch_enqueues_1khz_vofa_frames(self):
        class FakeForwarder:
            def __init__(self):
                self.items = []

            def enqueue(self, host, remote_port, local_port, values):
                self.items.append(values)

            def stop(self):
                pass

            def wait(self, _ms):
                pass

        window = MainWindow(start_workers=False)
        fake = FakeForwarder()
        window.vofa_forwarder = fake
        try:
            window.firmware_channels = ["adc.raw", "adc.battery_mv"]
            window.vofa_channels = [
                {"index": 0, "source": "adc.raw", "enabled": True},
                {"index": 1, "source": "adc.battery_mv", "enabled": True},
            ]
            payload = struct.pack("<BBHffffffff", 2, 4, 1000, 1.0, 10.0, 2.0, 20.0, 3.0, 30.0, 4.0, 40.0)
            window._handle_selected(payload)
            self.assertEqual(fake.items, [[1.0, 10.0], [2.0, 20.0], [3.0, 30.0], [4.0, 40.0]])
            self.assertEqual(window.selected_values["adc.raw"], 4.0)
        finally:
            window.close()
            self.app.processEvents()

    def test_speaker_control_worker_start_sequence(self):
        class RecordingTransport:
            def __init__(self):
                self.calls = []

            def send_wait(self, msg_type, payload=b"", timeout=3.0):
                self.calls.append((msg_type, payload, timeout))
                return b"ok"

        transport = RecordingTransport()
        worker = SpeakerControlWorker(transport, "start", "192.168.4.2")
        worker.run()
        self.assertEqual([call[0] for call in transport.calls], [TYPE_AUDIO_STREAM_START])
        self.assertEqual(transport.calls[-1][2], 8.0)
        payload = json.loads(transport.calls[-1][1].decode())
        self.assertEqual(payload["sample_rate"], SPEAKER_SAMPLE_RATE)
        self.assertEqual(payload["latency_mode"], "balanced")


if __name__ == "__main__":
    unittest.main()
