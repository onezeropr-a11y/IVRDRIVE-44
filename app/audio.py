"""Sample-rate conversion between the PBX (8kHz) and Gemini Live (16k in / 24k out).

Everything is PCM 16-bit little-endian mono, which is what both sides speak.
The filters are deliberately cheap: at telephone bandwidth the audible gain of a
proper polyphase resampler does not justify the added per-frame latency.
"""

from __future__ import annotations

import array


def _samples(data: bytes) -> array.array:
    out = array.array("h")
    out.frombytes(data[: len(data) - (len(data) % 2)])
    return out


def upsample_8k_to_16k(data: bytes) -> bytes:
    """Linear interpolation; each input sample becomes two output samples."""
    src = _samples(data)
    if not src:
        return b""
    out = array.array("h", bytes(4 * len(src)))
    previous = src[0]
    for i, sample in enumerate(src):
        out[2 * i] = (previous + sample) // 2
        out[2 * i + 1] = sample
        previous = sample
    return out.tobytes()


def downsample_24k_to_8k(data: bytes, carry: bytes = b"") -> tuple[bytes, bytes]:
    """Average every 3 samples. Returns (pcm8k, leftover) so streaming chunks
    that do not divide by 3 do not lose or duplicate samples."""
    src = _samples(carry + data)
    usable = len(src) - (len(src) % 3)
    out = array.array("h", bytes(2 * (usable // 3)))
    for i in range(0, usable, 3):
        out[i // 3] = (src[i] + src[i + 1] + src[i + 2]) // 3
    leftover = src[usable:].tobytes()
    return out.tobytes(), leftover


def rms(data: bytes) -> float:
    src = _samples(data)
    if not src:
        return 0.0
    return (sum(s * s for s in src) / len(src)) ** 0.5
