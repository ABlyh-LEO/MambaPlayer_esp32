import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from .audio_tools import BYTES_PER_SECOND, POWER_ON_MAX_SECONDS, STORAGE_PARTITION_BYTES, TARGET_RATE, convert_to_mamba_wav, normalize_peak
from .mamba_link import (
    STREAM_SELECTED_VALUES,
    TYPE_HELLO,
    FrameParser,
    crc16_ccitt,
    decode_udp_packet,
    encode_frame,
)
from .router import encode_justfloat, parse_can_last, parse_dji_motor, parse_selected_batch, parse_selected_values, save_project, load_project


class ProtocolTests(unittest.TestCase):
    def test_crc_reference(self):
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)

    def test_frame_parser_handles_split_frames(self):
        frame = encode_frame(TYPE_HELLO, b'{"ok":true}', 42)
        parser = FrameParser()
        self.assertEqual(parser.feed(frame[:3]), [])
        out = parser.feed(frame[3:])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].seq, 42)
        self.assertEqual(out[0].payload, b'{"ok":true}')

    def test_udp_selected_values_payload(self):
        payload = struct.pack("<BBHff", 2, 0, 0, 1.25, -2.5)
        packet = b"MT" + bytes([2, STREAM_SELECTED_VALUES]) + struct.pack("<IQHBB", 9, 1234, len(payload), 0, 0) + payload
        decoded = decode_udp_packet(packet)
        parsed = parse_selected_values(decoded["payload"])
        self.assertAlmostEqual(parsed[0], 1.25)
        self.assertAlmostEqual(parsed[1], -2.5)

    def test_udp_selected_values_batch_payload(self):
        payload = struct.pack("<BBHffff", 2, 2, 1000, 1.0, 2.0, 3.0, 4.0)
        batch = parse_selected_batch(payload)
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0], [1.0, 2.0])
        self.assertEqual(batch[1], [3.0, 4.0])
        self.assertEqual(parse_selected_values(payload), [3.0, 4.0])

    def test_can_last_payload_and_dji_parser(self):
        payload = bytearray(28)
        struct.pack_into("<HH", payload, 0, 1, 0)
        data = bytes([0x12, 0x34, 0xFF, 0x9C, 0x00, 0x2A, 55, 1])
        struct.pack_into("<IBBBQ", payload, 4, 0x201, 8, 0, 0, 1234)
        payload[20:28] = data
        frames = parse_can_last(bytes(payload))
        self.assertEqual(frames[0].can_id, 0x201)
        parsed = parse_dji_motor(frames[0].can_id, frames[0].data)
        self.assertEqual(parsed["angle"], 0x1234)
        self.assertEqual(parsed["rpm"], -100)
        self.assertEqual(parsed["temperature"], 55)

    def test_vofa_justfloat_encoder(self):
        data = encode_justfloat([1.0, -2.0])
        self.assertEqual(data[-4:], b"\x00\x00\x80\x7f")
        self.assertEqual(struct.unpack_from("<ff", data), (1.0, -2.0))

    def test_project_json_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.mamba.json"
            save_project(path, {"firmware_channels": ["adc.raw"], "vofa_channels": [{"index": 5, "source": "adc.raw"}]})
            loaded = load_project(path)
            self.assertEqual(loaded["firmware_channels"], ["adc.raw"])
            self.assertEqual(loaded["vofa_channels"][0]["index"], 5)

    def test_legacy_vofa_port_defaults_are_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.mamba.json"
            path.write_text('{"vofa_remote_port":1346,"vofa_local_port":1347}', encoding="utf-8")
            loaded = load_project(path)
            self.assertEqual(loaded["vofa_remote_port"], 1347)
            self.assertEqual(loaded["vofa_local_port"], 1346)


class AudioTests(unittest.TestCase):
    def test_adpcm_wav_header(self):
        seconds = 0.05
        t = np.arange(int(TARGET_RATE * seconds)) / TARGET_RATE
        pcm = (np.sin(2 * math.pi * 440 * t) * 0.5)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            with wave.open(str(tmp_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(TARGET_RATE)
                wav.writeframes(np.round(pcm * 32767).astype("<i2").tobytes())
            data = convert_to_mamba_wav(tmp_path, max_seconds=1.0)
        finally:
            tmp_path.unlink(missing_ok=True)
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(data[8:12], b"WAVE")
        self.assertEqual(struct.unpack_from("<H", data, 20)[0], 0x0011)
        self.assertEqual(struct.unpack_from("<I", data, 24)[0], TARGET_RATE)
        self.assertEqual(data[40:44], b"fact")

    def test_peak_normalize(self):
        samples = np.array([0.0, 0.1, -0.2], dtype=np.float32)
        normalized, gain = normalize_peak(samples)
        self.assertGreater(gain, 1.0)
        self.assertAlmostEqual(float(np.max(np.abs(normalized))), 0.98, places=5)

    def test_alarm_budget_tracks_storage_partition(self):
        power_on_budget = BYTES_PER_SECOND * POWER_ON_MAX_SECONDS + 4096
        alarm_budget = STORAGE_PARTITION_BYTES - power_on_budget - 32 * 1024
        self.assertGreater(alarm_budget / BYTES_PER_SECOND, 240.0)


if __name__ == "__main__":
    unittest.main()
