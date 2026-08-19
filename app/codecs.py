"""Pure-python G.711 decoding and PCM candidate scoring.

No stdlib ``audioop`` dependency: it was removed in Python 3.13 and Render
images track new interpreters quickly.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

_ULAW_TABLE: list[int] = []
_ALAW_TABLE: list[int] = []


def _build_ulaw() -> None:
    for byte in range(256):
        value = ~byte & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        _ULAW_TABLE.append(-sample if sign else sample)


def _build_alaw() -> None:
    for byte in range(256):
        value = byte ^ 0x55
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        if exponent:
            sample = (mantissa + 16) << (exponent + 3)
        else:
            sample = (mantissa << 4) + 8
        _ALAW_TABLE.append(-sample if sign else sample)


_build_ulaw()
_build_alaw()


def ulaw_to_pcm16(data: bytes) -> list[int]:
    return [_ULAW_TABLE[b] for b in data]


def alaw_to_pcm16(data: bytes) -> list[int]:
    return [_ALAW_TABLE[b] for b in data]


def pcm16_to_ulaw(samples: list[int]) -> bytes:
    out = bytearray()
    for sample in samples:
        sign = 0x80 if sample < 0 else 0x00
        magnitude = min(abs(sample), 32635) + 0x84
        exponent = 7
        mask = 0x4000
        while exponent > 0 and not magnitude & mask:
            mask >>= 1
            exponent -= 1
        mantissa = (magnitude >> (exponent + 3)) & 0x0F
        out.append(~(sign | (exponent << 4) | mantissa) & 0xFF)
    return bytes(out)


def pcm16le_to_samples(data: bytes) -> list[int]:
    usable = len(data) - (len(data) % 2)
    return list(struct.unpack(f"<{usable // 2}h", data[:usable]))


def pcm16be_to_samples(data: bytes) -> list[int]:
    usable = len(data) - (len(data) % 2)
    return list(struct.unpack(f">{usable // 2}h", data[:usable]))


@dataclass
class CandidateScore:
    """How plausible one interpretation of a raw byte stream is."""

    name: str
    sample_count: int
    rms: float
    #: Mean absolute difference between neighbouring samples, over RMS.
    #: Speech at 8kHz is smooth (well under 1.0); a wrong endianness or a
    #: wrong companding law scrambles the low bits and pushes this high.
    roughness: float
    clipping_ratio: float
    dc_offset: float

    @property
    def plausible(self) -> bool:
        return self.sample_count > 32 and self.rms > 40 and self.roughness < 0.9


def _score(name: str, samples: list[int]) -> CandidateScore:
    count = len(samples)
    if count < 2:
        return CandidateScore(name, count, 0.0, 99.0, 0.0, 0.0)
    mean = sum(samples) / count
    rms = (sum(s * s for s in samples) / count) ** 0.5
    deltas = sum(abs(samples[i] - samples[i - 1]) for i in range(1, count)) / (count - 1)
    clipped = sum(1 for s in samples if abs(s) >= 32000) / count
    return CandidateScore(
        name=name,
        sample_count=count,
        rms=round(rms, 1),
        roughness=round(deltas / rms, 3) if rms else 99.0,
        clipping_ratio=round(clipped, 4),
        dc_offset=round(mean, 1),
    )


def score_candidates(data: bytes) -> list[CandidateScore]:
    """Rank the four realistic 8kHz telephony encodings for a raw payload."""
    scores = [
        _score("pcm16le", pcm16le_to_samples(data)),
        _score("pcm16be", pcm16be_to_samples(data)),
        _score("mulaw", ulaw_to_pcm16(data)),
        _score("alaw", alaw_to_pcm16(data)),
    ]
    return sorted(scores, key=lambda s: s.roughness)


def wav_bytes(samples: list[int], sample_rate: int = 8000) -> bytes:
    body = struct.pack(f"<{len(samples)}h", *samples)
    header = b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(body))
    return header + body


def tone(
    frequency: int,
    milliseconds: int,
    sample_rate: int = 8000,
    amplitude: int = 8000,
) -> list[int]:

    count = int(sample_rate * milliseconds / 1000)
    return [
        int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
        for i in range(count)
    ]
