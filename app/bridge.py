"""Feasibility spike: bridge one PBX call to Gemini Live and time everything.

The point of this module is not to be the product. It answers three questions a
real call cannot answer on paper: how fast the bot replies, whether it handles
silence sensibly, and whether the caller can interrupt it mid-sentence.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from datetime import datetime

from app import audio, capture, cost, db, tools
from app.gemini_live import GeminiLiveSession

log = logging.getLogger("bridge")

#: PBX framing, discovered empirically: 320 bytes = 160 samples = 20ms @ 8kHz.
FRAME_BYTES = 320
FRAME_S = 0.02
SILENCE_FRAME = b"\x00" * FRAME_BYTES

#: Gemini's server-side VAD needs a steady stream; batching 5 frames keeps the
#: request rate at 10/s instead of 50/s without adding meaningful latency.
INPUT_BATCH_FRAMES = 5

GREETING = os.getenv("BOT_GREETING", "ברוך הבא למוקד דרייברים, איך אפשר לעזור?")


class CallBridge:
    """Owns one call: PBX audio in, Gemini audio out, at a paced 20ms cadence."""

    def __init__(
        self, ws, cap: capture.CallCapture, api_key: str, caller: str = ""
    ) -> None:
        self._ws = ws
        self._cap = cap
        self._api_key = api_key
        self._tools = tools.ToolContext(cap.call_id, caller)
        self._out: collections.deque[bytes] = collections.deque()
        self._carry = b""
        self._partial = b""
        self._inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self._session: GeminiLiveSession | None = None
        self._last_user_audio: float | None = None
        self._speaking = False
        self._meter = cost.UsageMeter()
        self.stats: dict[str, object] = {
            "caller": self._tools.caller,
            "turns": 0,
            "interruptions": 0,
            "reply_latency_ms": [],
            "tool_calls": [],
            "transcript": [],
            "usage": self._meter.snapshot(),
        }

    # ------------------------------------------------------------------ input

    def feed(self, pcm8k: bytes) -> None:
        """Called for every inbound PBX frame. Never blocks the receive loop."""
        self._inbox.put_nowait(pcm8k)

    async def _pump_input(self) -> None:
        batch: list[bytes] = []
        while True:
            batch.append(await self._inbox.get())
            if len(batch) < INPUT_BATCH_FRAMES:
                continue
            chunk = b"".join(batch)
            batch.clear()
            if audio.rms(chunk) > 200:  # ignore the PBX's constant-8 silence
                self._last_user_audio = time.monotonic()
            assert self._session is not None
            await self._session.send_audio(audio.upsample_8k_to_16k(chunk))

    # ----------------------------------------------------------------- output

    async def _pump_output(self) -> None:
        """Emit exactly one 20ms frame per tick, silence when we have nothing.

        A continuous stream keeps the PBX jitter buffer fed and makes barge-in a
        matter of dropping queued frames.
        """
        next_tick = time.monotonic()
        while True:
            next_tick += FRAME_S
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
            frame = self._out.popleft() if self._out else SILENCE_FRAME
            try:
                await self._ws.send_bytes(frame)
            except Exception:
                return
            self._cap.record("out", "binary", frame)

    def _enqueue(self, pcm8k: bytes) -> None:
        data = self._partial + pcm8k
        whole = len(data) - (len(data) % FRAME_BYTES)
        for i in range(0, whole, FRAME_BYTES):
            self._out.append(data[i : i + FRAME_BYTES])
        self._partial = data[whole:]

    # ------------------------------------------------------------------ model

    async def _pump_model(self) -> None:
        assert self._session is not None
        async for event in self._session.events():
            kind = event["type"]
            if kind == "audio":
                pcm8k, self._carry = audio.downsample_24k_to_8k(event["pcm24k"], self._carry)
                if not self._speaking:
                    self._speaking = True
                    if self._last_user_audio is not None:
                        latency = round((time.monotonic() - self._last_user_audio) * 1000)
                        self.stats["reply_latency_ms"].append(latency)
                        log.info("[%s] reply latency %d ms", self._cap.call_id, latency)
                self._enqueue(pcm8k)
            elif kind == "interrupted":
                self.stats["interruptions"] += 1
                self._out.clear()
                self._carry = b""
                self._partial = b""
                self._speaking = False
                log.info("[%s] barge-in: caller interrupted the bot", self._cap.call_id)
            elif kind == "turn_complete":
                self.stats["turns"] += 1
                self._speaking = False
            elif kind == "transcript":
                self.stats["transcript"].append(f"{event['who']}: {event['text']}")
                log.info("[%s] %s: %s", self._cap.call_id, event["who"], event["text"])
            elif kind == "tool_call":
                responses = []
                for call in event["calls"]:
                    result = self._tools.run(call["name"], call.get("args") or {})
                    self.stats["tool_calls"].append({"name": call["name"], "result": result})
                    responses.append(
                        {"id": call.get("id"), "name": call["name"], "response": result}
                    )
                await self._session.send_tool_responses(responses)
            elif kind == "usage":
                self._meter.add(event["usage"])
                self.stats["usage"] = self._meter.snapshot()
            elif kind == "go_away":
                log.warning("[%s] gemini going away: %s", self._cap.call_id, event["detail"])

    # ------------------------------------------------------------------- life

    async def run(self) -> None:
        prompt = db.get_prompt("system")
        if self._tools.caller:
            prompt += f"\nמספר הטלפון של המתקשר הנוכחי הוא {self._tools.caller}."
        async with GeminiLiveSession(
            self._api_key, prompt, tools=tools.DECLARATIONS
        ) as session:
            self._session = session
            if GREETING:
                await session.send_text(f"אמור עכשיו בדיוק את המשפט הזה: {GREETING}")
            tasks = [
                asyncio.create_task(self._pump_input()),
                asyncio.create_task(self._pump_output()),
                asyncio.create_task(self._pump_model()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()

    def finish(self) -> None:
        """Persist the call so a redial within the memory window can resume it."""
        transcript = "\n".join(self.stats["transcript"])
        log.info("[%s] estimated cost $%.4f", self._cap.call_id, self._meter.cost_usd())
        with db.session_scope() as session:
            session.add(
                db.CallLog(
                    call_id=self._cap.call_id,
                    phone=self._tools.caller,
                    ended_at=datetime.utcnow(),
                    transcript=transcript,
                    stats_json=json.dumps(self.stats, ensure_ascii=False),
                    summary=(
                        f"order #{self._tools.saved_order_id}"
                        if self._tools.saved_order_id
                        else "no order saved"
                    ),
                )
            )
