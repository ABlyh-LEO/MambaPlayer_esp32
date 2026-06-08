from __future__ import annotations

from pathlib import Path
import math
import struct
import wave

import numpy as np


TARGET_RATE = 16000
BLOCK_ALIGN = 256
SAMPLES_PER_BLOCK = (BLOCK_ALIGN - 4) * 2 + 1
BYTES_PER_SECOND = math.ceil(TARGET_RATE * BLOCK_ALIGN / SAMPLES_PER_BLOCK)
POWER_ON_MAX_SECONDS = 10.0
NORMALIZE_TARGET_PEAK = 0.98
STORAGE_PARTITION_BYTES = 0x250000

IMA_INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
IMA_STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]


def _load_audio(path: str | Path) -> tuple[np.ndarray, int]:
    path = Path(path)
    try:
        import soundfile as sf

        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        return mono.astype(np.float32), int(rate)
    except Exception:
        pass

    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    else:
        raise ValueError("only 8-bit or 16-bit PCM WAV is supported without soundfile")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data.astype(np.float32), rate


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    if len(samples) == 0:
        return samples
    duration = len(samples) / float(src_rate)
    dst_len = max(1, int(round(duration * dst_rate)))
    src_x = np.linspace(0.0, duration, num=len(samples), endpoint=False)
    dst_x = np.linspace(0.0, duration, num=dst_len, endpoint=False)
    return np.interp(dst_x, src_x, samples).astype(np.float32)


def normalize_peak(samples: np.ndarray, target_peak: float = NORMALIZE_TARGET_PEAK) -> tuple[np.ndarray, float]:
    if len(samples) == 0:
        return samples, 1.0
    peak = float(np.max(np.abs(samples)))
    if not np.isfinite(peak) or peak <= 1e-6:
        return samples, 1.0
    gain = min(target_peak / peak, 64.0)
    if gain <= 1.001:
        return samples, 1.0
    return np.clip(samples * gain, -target_peak, target_peak).astype(np.float32), gain


def _encode_nibble(sample: int, state: dict[str, int]) -> int:
    step = IMA_STEP_TABLE[state["index"]]
    diff = sample - state["predictor"]
    nibble = 0
    if diff < 0:
        nibble = 8
        diff = -diff
    delta = step >> 3
    if diff >= step:
        nibble |= 4
        diff -= step
        delta += step
    if diff >= step >> 1:
        nibble |= 2
        diff -= step >> 1
        delta += step >> 1
    if diff >= step >> 2:
        nibble |= 1
        delta += step >> 2
    state["predictor"] += -delta if nibble & 8 else delta
    state["predictor"] = max(-32768, min(32767, state["predictor"]))
    state["index"] = max(0, min(88, state["index"] + IMA_INDEX_TABLE[nibble & 0x0F]))
    return nibble & 0x0F


def _encode_adpcm_blocks(samples: np.ndarray) -> bytes:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = np.round(pcm * 32767.0).astype("<i2")
    blocks = math.ceil(len(pcm16) / SAMPLES_PER_BLOCK)
    out = bytearray(blocks * BLOCK_ALIGN)
    si = 0
    di = 0
    for _ in range(blocks):
        first = int(pcm16[si]) if si < len(pcm16) else 0
        state = {"predictor": first, "index": 0}
        struct.pack_into("<hBB", out, di, first, 0, 0)
        di += 4
        si += 1
        for _ in range(BLOCK_ALIGN - 4):
            s1 = int(pcm16[si]) if si < len(pcm16) else 0
            si += 1
            s2 = int(pcm16[si]) if si < len(pcm16) else 0
            si += 1
            out[di] = _encode_nibble(s1, state) | (_encode_nibble(s2, state) << 4)
            di += 1
    return bytes(out)


def convert_to_mamba_wav(path: str | Path, max_seconds: float, trim: bool = False,
                         normalize: bool = True, return_info: bool = False):
    samples, rate = _load_audio(path)
    duration = len(samples) / float(rate)
    if duration > max_seconds:
        if not trim:
            raise ValueError(f"audio too long: {duration:.1f}s > {max_seconds:.1f}s")
        samples = samples[:int(max_seconds * rate)]
    samples = _resample_linear(samples, rate, TARGET_RATE)
    gain = 1.0
    if normalize:
        samples, gain = normalize_peak(samples)
    data = _encode_adpcm_blocks(samples)
    file_size = 60 + len(data)
    header = bytearray()
    header += b"RIFF"
    header += struct.pack("<I", file_size - 8)
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<IHHIIHHHH", 20, 0x0011, 1, TARGET_RATE, BYTES_PER_SECOND, BLOCK_ALIGN, 4, 2, SAMPLES_PER_BLOCK)
    header += b"fact"
    header += struct.pack("<II", 4, len(samples))
    header += b"data"
    header += struct.pack("<I", len(data))
    wav = bytes(header) + data
    if return_info:
        gain_db = 20.0 * math.log10(gain) if gain > 0 else 0.0
        return wav, {"gain": gain, "gain_db": gain_db, "samples": len(samples)}
    return wav
