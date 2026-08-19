"""Per-call spend, estimated from the usage the Live API reports on the wire.

Google's billing dashboard lags by hours, which is useless while tuning a bot
that is charged by the second of audio. Gemini sends `usageMetadata` with every
response, broken down by modality, so the cost of a call is known the moment it
ends -- as an estimate, at published list prices.
"""

from __future__ import annotations

import json
import os

#: USD per million tokens, gemini-3.1-flash-live-preview list price.
#: https://ai.google.dev/gemini-api/docs/pricing
DEFAULT_RATES: dict[str, dict[str, float]] = {
    "input": {"TEXT": 0.75, "AUDIO": 3.00, "IMAGE": 1.00, "VIDEO": 1.00},
    "output": {"TEXT": 4.50, "AUDIO": 12.00},
}


def _load_rates() -> dict[str, dict[str, float]]:
    """Prices change; GEMINI_PRICING_JSON overrides any subset of them."""
    override = os.getenv("GEMINI_PRICING_JSON", "").strip()
    rates = {side: dict(table) for side, table in DEFAULT_RATES.items()}
    if not override:
        return rates
    try:
        parsed = json.loads(override)
    except ValueError:
        return rates
    for side in ("input", "output"):
        for modality, price in (parsed.get(side) or {}).items():
            rates[side][str(modality).upper()] = float(price)
    return rates


RATES = _load_rates()


class UsageMeter:
    """Accumulates the per-response usage reports of one call."""

    def __init__(self) -> None:
        self.input_tokens: dict[str, int] = {}
        self.output_tokens: dict[str, int] = {}

    def add(self, usage: dict[str, object]) -> None:
        self._add_side(
            self.input_tokens, usage.get("promptTokensDetails"), usage.get("promptTokenCount")
        )
        self._add_side(
            self.output_tokens,
            usage.get("responseTokensDetails"),
            usage.get("responseTokenCount"),
        )

    @staticmethod
    def _add_side(bucket: dict[str, int], details: object, total: object) -> None:
        counted = 0
        if isinstance(details, list):
            for entry in details:
                if not isinstance(entry, dict):
                    continue
                modality = str(entry.get("modality") or "AUDIO").upper()
                tokens = int(entry.get("tokenCount") or 0)
                bucket[modality] = bucket.get(modality, 0) + tokens
                counted += tokens
        # The per-modality breakdown does not always add up to the reported
        # total. Charge the remainder as audio: it is both the likeliest source
        # and the dearest rate, so the estimate errs high rather than low.
        remainder = int(total or 0) - counted
        if remainder > 0:
            bucket["AUDIO"] = bucket.get("AUDIO", 0) + remainder

    def cost_usd(self) -> float:
        total = 0.0
        for side, bucket in (("input", self.input_tokens), ("output", self.output_tokens)):
            for modality, tokens in bucket.items():
                rate = RATES[side].get(modality, RATES[side].get("AUDIO", 0.0))
                total += tokens * rate / 1_000_000
        return round(total, 6)

    def snapshot(self) -> dict[str, object]:
        return {
            "input_tokens": dict(self.input_tokens),
            "output_tokens": dict(self.output_tokens),
            "cost_usd": self.cost_usd(),
        }
