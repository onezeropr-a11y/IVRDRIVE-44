"""Outbound notification for a saved order.

Provider-agnostic on purpose: WhatsApp gateways (Green API, 360dialog, Twilio,
an internal n8n flow) all accept a JSON POST, so the integration is a URL plus
an optional header rather than a vendor SDK. Unset URL means notifications off.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("notify")

WEBHOOK_URL = os.getenv("ORDER_WEBHOOK_URL", "")
WEBHOOK_HEADER = os.getenv("ORDER_WEBHOOK_HEADER", "")  # e.g. "Authorization: Bearer x"
TIMEOUT_S = 5


def format_order(order: dict[str, object]) -> str:
    price = order.get("price")
    return "\n".join(
        [
            "הזמנה חדשה ממוקד דרייברים",
            f"טלפון: {order.get('phone', '')}",
            f"מוצא: {order.get('origin', '')}",
            f"יעד: {order.get('destination', '')}",
            f"נוסעים: {order.get('passengers', '')}",
            f"מועד: {order.get('pickup_time') or 'לא צוין'}",
            f"מחיר: {price if price is not None else 'לא נקבע'}",
        ]
    )


def send_order(order: dict[str, object]) -> bool:
    return _post({"type": "order", "text": format_order(order), "order": order})


def send_text(text: str, *, kind: str = "message", extra: dict | None = None) -> bool:
    """Free-form outbound message — a driver's ride statement, a receipt."""
    return _post({"type": kind, "text": text, **(extra or {})})


def _post(payload: dict[str, object]) -> bool:
    """Best effort: a failed notification must never fail the call."""
    if not WEBHOOK_URL:
        return False
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        WEBHOOK_URL, data=body, headers={"Content-Type": "application/json"}
    )
    if ":" in WEBHOOK_HEADER:
        name, _, value = WEBHOOK_HEADER.partition(":")
        request.add_header(name.strip(), value.strip())
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            log.info("order notification sent, status %s", response.status)
            return 200 <= response.status < 300
    except Exception as exc:
        log.warning("order notification failed: %s", exc)
        return False
