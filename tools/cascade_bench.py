"""Measure the cascaded path (STT -> LLM -> TTS) against Gemini Live.

Both paths are timed on the same recorded utterance, so the number that comes
out is the honest cost of the cascade: it cannot start speaking before the
transcript is complete, whereas Live starts generating audio while the caller is
still finishing the sentence.

    python tools/cascade_bench.py --wav utterance.wav

Only GEMINI_API_KEY is needed — the same key serves all three stages, which
keeps the comparison free of a second vendor's network variance.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import time
import urllib.request
import wave

from app import db
from app.gemini_live import GeminiLiveSession

API = "https://generativelanguage.googleapis.com/v1beta/models"
STT_MODEL = os.getenv("BENCH_STT_MODEL", "gemini-3.5-flash-lite")
LLM_MODEL = os.getenv("BENCH_LLM_MODEL", "gemini-3.5-flash-lite")
TTS_MODEL = os.getenv("BENCH_TTS_MODEL", "gemini-3.1-flash-tts-preview")

#: Thinking is held as low as the model allows, mirroring the Live path, or the
#: comparison measures reasoning budget rather than transport. The 3.x models
#: reject the numeric thinkingBudget that Live accepts.
LOW_THINKING = {"thinkingConfig": {"thinkingLevel": "low"}}


def _post(model: str, body: dict, key: str) -> dict:
    request = urllib.request.Request(
        f"{API}/{model}:generateContent?key={key}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _read_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as handle:
        return handle.readframes(handle.getnframes()), handle.getframerate()


def _wav_bytes(pcm: bytes, rate: int) -> bytes:
    """generateContent rejects headerless PCM, so re-wrap the frames as a wav."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def run_cascade(path: str, key: str) -> dict[str, float]:
    pcm, rate = _read_wav(path)
    audio_part = {
        "inlineData": {
            "mimeType": "audio/wav",
            "data": base64.b64encode(_wav_bytes(pcm, rate)).decode(),
        }
    }

    start = time.monotonic()
    stt = _post(
        STT_MODEL,
        {
            "contents": [
                {
                    "parts": [
                        {"text": "תמלל את ההקלטה לעברית. החזר טקסט בלבד."},
                        audio_part,
                    ]
                }
            ],
            "generationConfig": LOW_THINKING,
        },
        key,
    )
    transcript = stt["candidates"][0]["content"]["parts"][0]["text"].strip()
    t_stt = time.monotonic()

    llm = _post(
        LLM_MODEL,
        {
            "systemInstruction": {"parts": [{"text": db.get_prompt("system")}]},
            "contents": [{"parts": [{"text": transcript}]}],
            "generationConfig": LOW_THINKING,
        },
        key,
    )
    reply = llm["candidates"][0]["content"]["parts"][0]["text"].strip()
    t_llm = time.monotonic()

    _post(
        TTS_MODEL,
        {
            "contents": [{"parts": [{"text": reply}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Aoede"}}
                },
            },
        },
        key,
    )
    t_tts = time.monotonic()

    return {
        "transcript": transcript,
        "reply": reply,
        "stt_ms": round((t_stt - start) * 1000),
        "llm_ms": round((t_llm - t_stt) * 1000),
        "tts_ms": round((t_tts - t_llm) * 1000),
        "total_ms": round((t_tts - start) * 1000),
    }


async def run_live(path: str, key: str) -> dict[str, float]:
    """Time to first audio byte when the same utterance is streamed to Live."""
    pcm, rate = _read_wav(path)
    if rate != 16000:
        raise SystemExit(f"live path expects a 16kHz wav, got {rate}")

    async with GeminiLiveSession(key, db.get_prompt("system")) as session:
        start = time.monotonic()
        for i in range(0, len(pcm), 3200):  # 100ms chunks, as the bridge does
            await session.send_audio(pcm[i : i + 3200])
        async for event in session.events():
            if event["type"] == "audio":
                return {"first_audio_ms": round((time.monotonic() - start) * 1000)}
    return {"first_audio_ms": -1}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="16kHz mono PCM16 wav of one utterance")
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args()

    key = os.environ["GEMINI_API_KEY"]
    db.init_db()

    cascade = run_cascade(args.wav, key)
    print(json.dumps({"cascade": cascade}, ensure_ascii=False, indent=2))
    if not args.skip_live:
        live = asyncio.run(run_live(args.wav, key))
        print(json.dumps({"live": live}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
