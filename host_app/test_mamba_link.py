import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from .audio_tools import TARGET_RATE, convert_to_mamba_wav
from .mamba_link import (
    STREAM_JUSTFLOAT,
    TYPE_HELLO,
    FrameParser,
    crc16_ccitt,
    decode_udp_packet,
    encode_frame,
    parse_justfloat_payload,
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


if __name__ == "__main__":
    unittest.main()
