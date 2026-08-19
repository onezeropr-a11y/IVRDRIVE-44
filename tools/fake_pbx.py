"""Simulate the PBX side of the raw channel, to validate the probe locally.

Usage:
    python tools/fake_pbx.py --mode binary-mulaw
    python tools/fake_pbx.py --mode json-base64 --bearer secret
    python tools/fake_pbx.py --mode replay --replay call.raw --out reply.wav

The replay mode speaks the real Technoline dialect (start JSON, then 320-byte
PCM16LE frames every 20ms) and writes whatever comes back to a WAV, which is
how the AI bridge gets exercised without placing a phone call.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import struct

import websockets

SAMPLE_RATE = 8000


def speechlike_pcm16(milliseconds: int, phase: float) -> list[int]:
    """A vowel-ish waveform: smooth enough to score like real speech."""
    count = int(SAMPLE_RATE * milliseconds / 1000)
    out = []
    for i in range(count):
        t = (phase + i) / SAMPLE_RATE
        value = (
            6000 * math.sin(2 * math.pi * 120 * t)
            + 3000 * math.sin(2 * math.pi * 240 * t)
            + 1500 * math.sin(2 * math.pi * 700 * t)
        )
        out.append(int(max(-32000, min(32000, value))))
    return out


def to_mulaw(samples: list[int]) -> bytes:
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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws/ivr")
    parser.add_argument(
        "--mode",
        default="binary-pcm16le",
        choices=["binary-pcm16le", "binary-mulaw", "json-base64", "replay"],
    )
    parser.add_argument("--replay", help="raw PCM16LE 8kHz file to play as the caller")
    parser.add_argument("--out", help="write returned audio to this WAV file")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--bearer", default=None)
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.bearer}"} if args.bearer else {}
    async with websockets.connect(
        args.url, additional_headers=headers, max_size=None
    ) as ws:
        if args.mode == "json-base64":
            await ws.send(
                json.dumps(
                    {
                        "event": "start",
                        "callId": "sim-123",
                        "caller": "0501234567",
                        "extension": "100",
                        "format": {"encoding": "pcm16", "sampleRate": 8000},
                    }
                )
            )

        if args.mode == "replay":
            await ws.send(
                json.dumps(
                    {
                        "type": "start",
                        "callId": "sim-" + "0" * 40,
                        "caller": "0501234567",
                        "system": "0765673575",
                        "token": args.bearer or "sim-token",
                        "format": "pcm16;rate=8000;ch=1",
                    }
                )
            )

        received = 0
        returned = bytearray()

        async def drain() -> None:
            nonlocal received
            try:
                async for message in ws:
                    received += 1
                    if isinstance(message, bytes):
                        returned.extend(message)
            except Exception:
                pass

        reader = asyncio.create_task(drain())

        if args.mode == "replay":
            frames = await _replay(ws, args)
        else:
            frames = await _synthesize(ws, args)

        await asyncio.sleep(3.0 if args.mode == "replay" else 1.5)
        reader.cancel()
        print(f"sent {frames} frames, received {received} frames back from probe")

        if args.out and returned:
            body = bytes(returned)
            header = (
                b"RIFF"
                + struct.pack("<I", 36 + len(body))
                + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
                + b"data"
                + struct.pack("<I", len(body))
            )
            with open(args.out, "wb") as handle:
                handle.write(header + body)
            print(f"wrote {args.out} ({len(body) / (SAMPLE_RATE * 2):.1f}s)")


async def _replay(ws, args) -> int:
    with open(args.replay, "rb") as handle:
        data = handle.read()
    size = int(SAMPLE_RATE * 2 * args.frame_ms / 1000)
    sent = 0
    for offset in range(0, len(data) - size + 1, size):
        await ws.send(data[offset : offset + size])
        sent += 1
        await asyncio.sleep(args.frame_ms / 1000)
    return sent


async def _synthesize(ws, args) -> int:
    frames = int(args.seconds * 1000 / args.frame_ms)
    phase = 0.0
    for _ in range(frames):
        samples = speechlike_pcm16(args.frame_ms, phase)
        phase += len(samples)
        if args.mode == "binary-mulaw":
            await ws.send(to_mulaw(samples))
        else:
            raw = struct.pack(f"<{len(samples)}h", *samples)
            if args.mode == "binary-pcm16le":
                await ws.send(raw)
            else:
                await ws.send(
                    json.dumps(
                        {
                            "event": "media",
                            "media": {"payload": base64.b64encode(raw).decode()},
                        }
                    )
                )
        await asyncio.sleep(args.frame_ms / 1000)
    return frames


if __name__ == "__main__":
    asyncio.run(main())
