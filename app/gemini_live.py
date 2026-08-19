"""Thin client for the Gemini Live (bidiGenerateContent) WebSocket API.

Deliberately dependency-free: the official SDK pulls in a large stack and hides
the wire timing, which is the one thing this spike exists to measure.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import websockets

ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

#: Fastest model with native audio in/out at the time of the bake-off.
DEFAULT_MODEL = os.getenv("GEMINI_LIVE_MODEL", "models/gemini-3.1-flash-live-preview")
DEFAULT_VOICE = os.getenv("GEMINI_LIVE_VOICE", "Aoede")

INPUT_RATE = 16000
OUTPUT_RATE = 24000

#: End-of-turn detection dominates perceived latency: the model cannot start
#: answering until its VAD decides the caller stopped, and the default wait is
#: long enough that a mid-sentence pause reads as the end of the turn. 300ms is
#: short enough to feel conversational and still longer than the pauses inside
#: a spoken Hebrew sentence.
SILENCE_MS = int(os.getenv("GEMINI_VAD_SILENCE_MS", "300"))
#: Audio kept from before speech onset, so a clipped first syllable does not
#: cost a whole turn in misunderstanding.
PREFIX_PADDING_MS = int(os.getenv("GEMINI_VAD_PREFIX_MS", "120"))
START_SENSITIVITY = os.getenv("GEMINI_VAD_START", "START_SENSITIVITY_HIGH")
END_SENSITIVITY = os.getenv("GEMINI_VAD_END", "END_SENSITIVITY_HIGH")


class GeminiLiveSession:
    def __init__(
        self,
        api_key: str,
        system_prompt: str,
        model: str = DEFAULT_MODEL,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self._api_key = api_key
        self._system_prompt = system_prompt
        self._model = model
        self._tools = tools or []
        self._ws: Any = None

    async def __aenter__(self) -> GeminiLiveSession:
        self._ws = await websockets.connect(
            f"{ENDPOINT}?key={self._api_key}", max_size=None, ping_interval=20
        )
        await self._ws.send(
            json.dumps(
                {
                    "setup": {
                        "model": self._model,
                        "generationConfig": {
                            "responseModalities": ["AUDIO"],
                            # Thinking triples time-to-first-audio; a phone call
                            # cannot wait three seconds for a greeting.
                            "thinkingConfig": {"thinkingBudget": 0},
                            "speechConfig": {
                                "languageCode": "he-IL",
                                "voiceConfig": {
                                    "prebuiltVoiceConfig": {"voiceName": DEFAULT_VOICE}
                                },
                            },
                        },
                        "systemInstruction": {"parts": [{"text": self._system_prompt}]},
                        "inputAudioTranscription": {},
                        "outputAudioTranscription": {},
                        "tools": (
                            [{"functionDeclarations": self._tools}] if self._tools else []
                        ),
                        "realtimeInputConfig": {
                            "automaticActivityDetection": {
                                "startOfSpeechSensitivity": START_SENSITIVITY,
                                "endOfSpeechSensitivity": END_SENSITIVITY,
                                "prefixPaddingMs": PREFIX_PADDING_MS,
                                "silenceDurationMs": SILENCE_MS,
                            },
                            "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                        },
                    }
                }
            )
        )
        await self._ws.recv()  # setupComplete
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send_audio(self, pcm16k: bytes) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "realtimeInput": {
                        "audio": {
                            "mimeType": f"audio/pcm;rate={INPUT_RATE}",
                            "data": base64.b64encode(pcm16k).decode(),
                        }
                    }
                }
            )
        )

    async def send_text(self, text: str) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text": text}]}],
                        "turnComplete": True,
                    }
                }
            )
        )

    async def send_tool_responses(self, responses: list[dict[str, Any]]) -> None:
        await self._ws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Normalised stream of {type: audio|interrupted|turn_complete|transcript}."""
        async for message in self._ws:
            raw = message if isinstance(message, str) else message.decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except ValueError:
                continue

            if calls := (data.get("toolCall") or {}).get("functionCalls"):
                yield {"type": "tool_call", "calls": calls}
                continue

            if usage := data.get("usageMetadata"):
                yield {"type": "usage", "usage": usage}

            server = data.get("serverContent")
            if server is None:
                if "goAway" in data:
                    yield {"type": "go_away", "detail": data["goAway"]}
                continue

            if server.get("interrupted"):
                yield {"type": "interrupted"}
            for key, who in (("inputTranscription", "user"), ("outputTranscription", "bot")):
                if (text := (server.get(key) or {}).get("text")):
                    yield {"type": "transcript", "who": who, "text": text}
            for part in (server.get("modelTurn") or {}).get("parts", []):
                if inline := part.get("inlineData"):
                    yield {"type": "audio", "pcm24k": base64.b64decode(inline["data"])}
                elif text := part.get("text"):
                    yield {"type": "transcript", "who": "bot", "text": text}
            if server.get("turnComplete"):
                yield {"type": "turn_complete"}
