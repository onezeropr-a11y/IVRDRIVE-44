"""Tools the bot may call mid-call, plus their Gemini function declarations.

Everything here is lazy on purpose: nothing is loaded when a call starts. The
customer record and the previous call are fetched only if the conversation
actually needs them, which keeps the first response fast.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app import db, dispatch, loyalty, notify

log = logging.getLogger("tools")

#: A caller who redials within this window is treated as continuing one errand.
RECENT_CALL_MINUTES = 10

DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_customer",
        "description": "פרטי לקוח לפי מספר טלפון: שם, כתובת איסוף מועדפת והערות.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "מספר טלפון. השאר ריק למתקשר הנוכחי."}
            },
        },
    },
    {
        "name": "get_recent_call",
        "description": (
            "השיחה הקודמת של אותו מתקשר בעשר הדקות האחרונות, אם הייתה. "
            "השתמש בזה כשנראה שהלקוח ממשיך שיחה קודמת."
        ),
        "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}},
    },
    {
        "name": "lookup_price",
        "description": "מחיר נסיעה לפי מסלול. המקור היחיד למחירים — אין להמציא מחיר.",
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "כתובת או עיר מוצא"},
                "destination": {"type": "string", "description": "כתובת או עיר יעד"},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "get_points",
        "description": (
            "מצב הניקוד של המתקשר במועדון הנוסעים, וכמה נקודות חסרות לנסיעת חינם."
        ),
        "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}},
    },
    {
        "name": "save_order",
        "description": "שמירת ההזמנה בסיום. קרא לזה רק אחרי שהלקוח אישר את הפרטים.",
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "passengers": {"type": "integer"},
                "pickup_time": {"type": "string", "description": "מועד הנסיעה כפי שנמסר"},
                "price": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["origin", "destination", "passengers"],
        },
    },
]


class ToolContext:
    """Binds tool calls to one call: caller id, call id, and a per-call cache."""

    def __init__(self, call_id: str, caller: str) -> None:
        self.call_id = call_id
        self.caller = db.normalize_phone(caller)
        self._cache: dict[str, Any] = {}
        self.saved_order_id: int | None = None

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "get_customer": self._get_customer,
            "get_recent_call": self._get_recent_call,
            "lookup_price": self._lookup_price,
            "get_points": self._get_points,
            "save_order": self._save_order,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        key = f"{name}:{sorted(args.items())}"
        if name != "save_order" and key in self._cache:
            return self._cache[key]
        try:
            result = handler(args)
        except Exception as exc:
            log.exception("[%s] tool %s failed", self.call_id, name)
            return {"error": f"{type(exc).__name__}: {exc}"}
        self._cache[key] = result
        log.info("[%s] tool %s(%s) -> %s", self.call_id, name, args, result)
        return result

    # -------------------------------------------------------------- handlers

    def _get_customer(self, args: dict[str, Any]) -> dict[str, Any]:
        phone = db.normalize_phone(args.get("phone") or self.caller)
        with db.session_scope() as session:
            row = session.scalars(
                select(db.Customer).where(db.Customer.phone == phone)
            ).first()
            if row is None:
                return {"found": False, "phone": phone}
            return {
                "found": True,
                "phone": row.phone,
                "name": row.name,
                "default_pickup": row.default_pickup,
                "notes": row.notes,
            }

    def _get_recent_call(self, args: dict[str, Any]) -> dict[str, Any]:
        phone = db.normalize_phone(args.get("phone") or self.caller)
        with db.session_scope() as session:
            row = db.recent_call(session, phone, RECENT_CALL_MINUTES)
            if row is None:
                return {"found": False}
            last_order = session.scalars(
                select(db.Order)
                .where(db.Order.phone == phone)
                .order_by(db.Order.created_at.desc())
                .limit(1)
            ).first()
            return {
                "found": True,
                "minutes_ago": round(
                    (datetime.utcnow() - row.started_at).total_seconds() / 60, 1
                ),
                "summary": row.summary,
                "transcript_tail": (row.transcript or "")[-1500:],
                "last_order": (
                    {
                        "origin": last_order.origin,
                        "destination": last_order.destination,
                        "passengers": last_order.passengers,
                        "pickup_time": last_order.pickup_time,
                        "price": last_order.price,
                    }
                    if last_order
                    else None
                ),
            }

    def _lookup_price(self, args: dict[str, Any]) -> dict[str, Any]:
        origin, destination = args.get("origin", ""), args.get("destination", "")
        with db.session_scope() as session:
            hit = db.find_price(session, origin, destination)
            if hit is None:
                return {
                    "found": False,
                    "message": "אין מחיר במערכת למסלול הזה; נציג יחזור עם הצעת מחיר.",
                }
            return {
                "found": True,
                "price": hit.price,
                "currency": "ILS",
                "origin": hit.origin,
                "destination": hit.destination,
            }

    def _get_points(self, args: dict[str, Any]) -> dict[str, Any]:
        phone = db.normalize_phone(args.get("phone") or self.caller)
        with db.session_scope() as session:
            balance = loyalty.balance(session, phone)
        cost = db.setting_int("redeem_points")
        return {
            "balance": balance,
            "free_ride_cost": cost,
            "can_redeem": balance >= cost,
            "missing": max(0, cost - balance),
        }

    def _save_order(self, args: dict[str, Any]) -> dict[str, Any]:
        with db.session_scope() as session:
            order = db.Order(
                call_id=self.call_id,
                phone=self.caller,
                origin=args.get("origin", ""),
                destination=args.get("destination", ""),
                passengers=int(args.get("passengers") or 1),
                pickup_time=args.get("pickup_time"),
                price=args.get("price"),
                notes=args.get("notes"),
                area=args.get("origin", ""),
            )
            session.add(order)
            session.flush()
            self.saved_order_id = order.id
            # Ringing the drivers straight off the call is opt-in: most offices
            # want a dispatcher to see the order first.
            if db.setting_int("auto_tender"):
                dispatch.open_tender(session, order, actor="bot")
            payload = {
                "order_id": order.id,
                "phone": order.phone,
                "origin": order.origin,
                "destination": order.destination,
                "passengers": order.passengers,
                "pickup_time": order.pickup_time,
                "price": order.price,
            }
        # Off the call's thread: the caller must not wait on a webhook.
        threading.Thread(target=notify.send_order, args=(payload,), daemon=True).start()
        return {"saved": True, "order_id": payload["order_id"]}
