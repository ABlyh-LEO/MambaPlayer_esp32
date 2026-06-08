import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from .audio_tools import BYTES_PER_SECOND, POWER_ON_MAX_SECONDS, STORAGE_PARTITION_BYTES, TARGET_RATE, convert_to_mamba_wav, normalize_peak
from .mamba_link import (
    STREAM_JUSTFLOAT,
    TYPE_HELLO,
    FrameParser,
    crc16_ccitt,
    decode_udp_packet,
    encode_frame,
    parse_justfloat_payload,
    parse_adc_batch_payload,
    parse_rm_motor_payload,
)


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

    def test_udp_justfloat_payload(self):
        payload = struct.pack("<BBHIff", 2, 0, 0, 3, 1.25, -2.5)
        packet = b"MT" + bytes([1, STREAM_JUSTFLOAT]) + struct.pack("<IQHBB", 9, 1234, len(payload), 0, 0) + payload
        decoded = decode_udp_packet(packet)
        parsed = parse_justfloat_payload(decoded["payload"])
        self.assertEqual(parsed["dropped"], 3)
        self.assertAlmostEqual(parsed["values"][0], 1.25)
        self.assertAlmostEqual(parsed["values"][1], -2.5)

    def test_batched_adc_payload(self):
        payload = struct.pack("<IIHHIHHIHH", 10, 2000, 2, 1, 22000, 2000, 1234, 22100, 2010, 1235)
        parsed = parse_adc_batch_payload(payload)
        self.assertEqual(parsed["samples"][0]["sample"], 10)
        self.assertEqual(parsed["samples"][1]["battery_mv"], 22100)
        self.assertEqual(parsed["dropped"], 1)

    def test_batched_justfloat_payload(self):
        payload = struct.pack("<HHQBBHff", 1, 0, 1234, 2, 0, 0, 1.0, -1.0)
        parsed = parse_justfloat_payload(payload)
        self.assertEqual(len(parsed["frames"]), 1)
        self.assertEqual(parsed["frames"][0]["values"][0], 1.0)

    def test_batched_rm_motor_payload(self):
        payload = bytearray(28)
        struct.pack_into("<HH", payload, 0, 1, 0)
        struct.pack_into("<QBBBBHhhhI", payload, 4, 1000, 1, 55, 0, 0, 123, -100, 42, -9, 0)
        parsed = parse_rm_motor_payload(bytes(payload))
        self.assertEqual(parsed["motors"][0]["motor_id"], 1)
        self.assertEqual(parsed["motors"][0]["rpm"], -100)


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
