"""Per-call capture of everything that crosses the raw channel."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from app import codecs

CAPTURE_ROOT = Path(os.getenv("PROBE_CAPTURE_DIR", "captures"))
HEX_HEAD_BYTES = 48
MAX_FRAMES_LOGGED = int(os.getenv("PROBE_MAX_FRAMES_LOGGED", "4000"))


def _ascii_preview(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data[:HEX_HEAD_BYTES])


def _find_base64_audio(node: Any, path: str = "") -> list[tuple[dict[str, Any], bytes]]:
    """Locate base64-looking string leaves, the way Twilio wraps media."""
    found: list[tuple[dict[str, Any], bytes]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _find_base64_audio(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _find_base64_audio(value, f"{path}[{index}]")
    elif isinstance(node, str) and len(node) >= 32:
        try:
            raw = base64.b64decode(node, validate=True)
        except Exception:
            return found
        # Identifiers (hex call ids, tokens) decode as "valid" base64 too. Only
        # an even-length payload of at least one 5ms frame can be audio.
        if len(raw) < 80 or len(raw) % 2:
            return found
        found.append(
            (
                {
                    "json_path": path,
                    "b64_chars": len(node),
                    "decoded_bytes": len(raw),
                    "decoded_hex_head": raw[:HEX_HEAD_BYTES].hex(),
                    "codec_candidates": [
                        c.__dict__ for c in codecs.score_candidates(raw)
                    ],
                },
                raw,
            )
        )
    return found


class CallCapture:
    """Writes a forensic record of one WebSocket connection to disk."""

    def __init__(self, call_id: str, handshake: dict[str, Any]) -> None:
        self.call_id = call_id
        self.dir = CAPTURE_ROOT / call_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        self.handshake = handshake
        self.frame_count = 0
        self.inbound_frames = 0
        self.outbound_frames = 0
        self.inbound_bytes = 0
        self.outbound_bytes = 0
        self.frame_sizes: dict[int, int] = {}
        self.kinds: dict[str, int] = {}
        self.first_text_frames: list[str] = []
        self.closed_reason: str | None = None
        #: Anything a caller wants recorded alongside the capture, e.g. AI stats.
        self.extra: dict[str, Any] = {}
        self._last_inbound_ms: float | None = None
        self._frames_file = (self.dir / "frames.jsonl").open("w", encoding="utf-8")
        self._inbound_raw = (self.dir / "inbound.bin").open("wb")
        #: JSON-wrapped audio is kept apart from raw binary frames so a stray
        #: base64 field can never shift the byte alignment of the real stream.
        self._inbound_json_raw = (self.dir / "inbound_json.bin").open("wb")
        self._outbound_raw = (self.dir / "outbound.bin").open("wb")
        self._write_meta()

    # ---------------------------------------------------------------- record

    def _elapsed_ms(self) -> float:
        return round((time.monotonic() - self.started_monotonic) * 1000, 1)

    def record(self, direction: str, kind: str, payload: bytes | str) -> dict[str, Any]:
        self.frame_count += 1
        now_ms = self._elapsed_ms()
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload

        entry: dict[str, Any] = {
            "seq": self.frame_count,
            "dir": direction,
            "kind": kind,
            "t_ms": now_ms,
            "bytes": len(raw),
        }

        if direction == "in":
            self.inbound_frames += 1
            self.inbound_bytes += len(raw)
            self._inbound_raw.write(raw if kind == "binary" else b"")
            self.frame_sizes[len(raw)] = self.frame_sizes.get(len(raw), 0) + 1
            self.kinds[kind] = self.kinds.get(kind, 0) + 1
            if self._last_inbound_ms is not None:
                entry["dt_ms"] = round(now_ms - self._last_inbound_ms, 1)
            self._last_inbound_ms = now_ms
        else:
            self.outbound_frames += 1
            self.outbound_bytes += len(raw)
            self._outbound_raw.write(raw if kind == "binary" else b"")

        if kind == "binary":
            entry["hex_head"] = raw[:HEX_HEAD_BYTES].hex()
            entry["ascii_head"] = _ascii_preview(raw)
            if direction == "in" and self.inbound_frames <= 20:
                entry["codec_candidates"] = [
                    c.__dict__ for c in codecs.score_candidates(raw)
                ]
        else:
            text = payload if isinstance(payload, str) else raw.decode("utf-8", "replace")
            entry["text"] = text[:2000]
            try:
                parsed = json.loads(text)
            except Exception:
                entry["json"] = None
            else:
                entry["json_keys"] = sorted(parsed) if isinstance(parsed, dict) else None
                embedded = _find_base64_audio(parsed)
                if embedded:
                    entry["embedded_base64"] = [meta for meta, _ in embedded]
                    if direction == "in":
                        # Feed the decoded audio into the same analysis path as
                        # binary frames, so JSON-wrapped media still yields a
                        # codec verdict and WAV renderings.
                        largest = max(embedded, key=lambda item: len(item[1]))[1]
                        self._inbound_json_raw.write(largest)
            if direction == "in" and len(self.first_text_frames) < 10:
                self.first_text_frames.append(text[:2000])

        if self.frame_count <= MAX_FRAMES_LOGGED:
            self._frames_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._frames_file.flush()
        return entry

    # ----------------------------------------------------------------- close

    def close(self, reason: str) -> dict[str, Any]:
        self.closed_reason = reason
        for handle in (
            self._frames_file,
            self._inbound_raw,
            self._inbound_json_raw,
            self._outbound_raw,
        ):
            try:
                handle.close()
            except Exception:
                pass
        self._render_audio_candidates()
        return self._write_meta()

    def _audio_source(self) -> Path:
        """Raw binary frames if any arrived, else audio unwrapped from JSON."""
        binary = self.dir / "inbound.bin"
        if binary.exists() and binary.stat().st_size:
            return binary
        return self.dir / "inbound_json.bin"

    def _render_audio_candidates(self) -> None:
        """Write one WAV per candidate decoding so a human can just listen."""
        source = self._audio_source()
        raw = source.read_bytes() if source.exists() else b""
        if not raw:
            return
        renderers = {
            "pcm16le": codecs.pcm16le_to_samples,
            "pcm16be": codecs.pcm16be_to_samples,
            "mulaw": codecs.ulaw_to_pcm16,
            "alaw": codecs.alaw_to_pcm16,
        }
        for name, decode in renderers.items():
            samples = decode(raw)
            if samples:
                (self.dir / f"inbound_as_{name}.wav").write_bytes(codecs.wav_bytes(samples))

    def summary(self) -> dict[str, Any]:
        duration = round(time.monotonic() - self.started_monotonic, 2)
        common_sizes = sorted(self.frame_sizes.items(), key=lambda kv: -kv[1])[:5]
        verdict = None
        raw_path = self._audio_source()
        if raw_path.exists() and raw_path.stat().st_size:
            head = raw_path.read_bytes()[: 8000 * 4]
            ranked = codecs.score_candidates(head)
            verdict = {
                "best_guess": ranked[0].name if ranked[0].plausible else "inconclusive",
                "ranked": [c.__dict__ for c in ranked],
            }
        return {
            "call_id": self.call_id,
            "started_at": self.started_at,
            "duration_s": duration,
            "handshake": self.handshake,
            "frames_total": self.frame_count,
            "inbound_frames": self.inbound_frames,
            "outbound_frames": self.outbound_frames,
            "inbound_bytes": self.inbound_bytes,
            "outbound_bytes": self.outbound_bytes,
            "frame_kinds": self.kinds,
            "common_frame_sizes": [{"bytes": s, "count": n} for s, n in common_sizes],
            "inferred_frame_ms": self._inferred_frame_ms(common_sizes, duration),
            "first_text_frames": self.first_text_frames,
            "closed_reason": self.closed_reason,
            "codec_verdict": verdict,
            **self.extra,
        }

    def _inferred_frame_ms(
        self, common_sizes: list[tuple[int, int]], duration: float
    ) -> dict[str, Any] | None:
        if not common_sizes or duration <= 0:
            return None
        size, count = common_sizes[0]
        if count < 5:
            return None
        rate = count / duration
        return {
            "dominant_frame_bytes": size,
            "frames_per_second": round(rate, 1),
            "frame_ms_if_pcm16_8k": round(size / 16, 2),
            "frame_ms_if_g711_8k": round(size / 8, 2),
            "wall_clock_frame_ms": round(1000 / rate, 2) if rate else None,
        }

    def _write_meta(self) -> dict[str, Any]:
        data = self.summary()
        (self.dir / "meta.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return data


def list_captures() -> list[dict[str, Any]]:
    if not CAPTURE_ROOT.exists():
        return []
    out = []
    for meta in CAPTURE_ROOT.glob("*/meta.json"):
        try:
            out.append(json.loads(meta.read_text(encoding="utf-8")))
        except Exception:
            continue
    return sorted(out, key=lambda m: m.get("started_at", 0), reverse=True)


def load_capture(call_id: str) -> dict[str, Any] | None:
    meta = CAPTURE_ROOT / call_id / "meta.json"
    if not meta.exists():
        return None
    return json.loads(meta.read_text(encoding="utf-8"))


def load_frames(call_id: str, limit: int = 500) -> list[dict[str, Any]]:
    path = CAPTURE_ROOT / call_id / "frames.jsonl"
    if not path.exists():
        return []
    frames = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if len(frames) >= limit:
                break
            try:
                frames.append(json.loads(line))
            except Exception:
                continue
    return frames
