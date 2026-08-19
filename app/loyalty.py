"""The passenger club: points earned, points spent, and the rules that keep
both honest.

Two invariants drive the whole module. Points are only ever earned by a ride
that actually happened — the trigger is the order reaching ``done``, never the
order being taken — and every movement is one append-only row, so a balance is
a sum and a correction is another row rather than an edit.

Idempotency is enforced on ``(order_id, reason)``: replaying a completion, a
dispatcher clicking twice, and the nightly reconciliation all produce the same
ledger.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import db

log = logging.getLogger("loyalty")

#: A ride earns points once, under this reason; the welcome gift and the
#: referral bonus are separate reasons so a phone can hold all three.
REASON_RIDE = "ride"
REASON_GIFT = "first_ride_gift"
REASON_REFERRAL = "referral"
REASON_REDEEM = "redeem"
REASON_REVERSAL = "reversal"
REASON_MANUAL = "manual"


def balance(session: Session, phone: str) -> int:
    phone = db.normalize_phone(phone)
    total = session.scalar(
        select(func.coalesce(func.sum(db.PointsEntry.delta), 0)).where(
            db.PointsEntry.phone == phone
        )
    )
    return int(total or 0)


def history(session: Session, phone: str, limit: int = 100) -> list[db.PointsEntry]:
    phone = db.normalize_phone(phone)
    return list(
        session.scalars(
            select(db.PointsEntry)
            .where(db.PointsEntry.phone == phone)
            .order_by(db.PointsEntry.created_at.desc())
            .limit(limit)
        ).all()
    )


def _has_entry(session: Session, *, phone: str, reason: str, order_id: int | None) -> bool:
    stmt = select(db.PointsEntry.id).where(
        db.PointsEntry.phone == phone, db.PointsEntry.reason == reason
    )
    if order_id is not None:
        stmt = stmt.where(db.PointsEntry.order_id == order_id)
    return session.scalars(stmt.limit(1)).first() is not None


def _add(
    session: Session,
    *,
    phone: str,
    delta: int,
    reason: str,
    order_id: int | None = None,
    referral_id: int | None = None,
    actor: str = "system",
    note: str | None = None,
) -> db.PointsEntry:
    entry = db.PointsEntry(
        phone=db.normalize_phone(phone),
        delta=delta,
        reason=reason,
        order_id=order_id,
        referral_id=referral_id,
        actor=actor,
        note=note,
    )
    session.add(entry)
    session.flush()
    db.log_action(
        session,
        "points",
        actor=actor,
        entity="phone",
        entity_id=entry.phone,
        detail=f"{delta:+d} ({reason})" + (f" order {order_id}" if order_id else ""),
    )
    return entry


def grant(
    session: Session,
    *,
    phone: str,
    delta: int,
    reason: str,
    order_id: int | None = None,
    referral_id: int | None = None,
    actor: str = "system",
    note: str | None = None,
) -> db.PointsEntry:
    """Write one ledger row. The way other modules (referrals, the console)
    move points, so every movement goes through the same logging."""
    return _add(
        session,
        phone=phone,
        delta=delta,
        reason=reason,
        order_id=order_id,
        referral_id=referral_id,
        actor=actor,
        note=note,
    )


def points_for_order(order: db.Order) -> int:
    """A ride bought with points earns none — otherwise a free ride would
    partly pay for the next one."""
    if order.points_spent:
        return 0
    price = float(order.price or 0.0)
    if price <= 0:
        return 0
    return int(round(price * db.setting_float("points_per_shekel")))


def award_for_order(session: Session, order: db.Order, *, actor: str = "system") -> dict:
    """Called when an order reaches ``done``. Grants the ride points, the
    one-per-phone welcome gift, and any referral bonus the ride triggers."""
    if order.status != "done":
        return {"awarded": 0, "reason": "order not done"}

    phone = db.normalize_phone(order.phone)
    granted: dict[str, int] = {}

    if not _has_entry(session, phone=phone, reason=REASON_RIDE, order_id=order.id):
        amount = points_for_order(order)
        if amount > 0:
            _add(
                session,
                phone=phone,
                delta=amount,
                reason=REASON_RIDE,
                order_id=order.id,
                actor=actor,
            )
            granted[REASON_RIDE] = amount

    # The gift is keyed on the phone alone, so a second ride never repeats it.
    if not _has_entry(session, phone=phone, reason=REASON_GIFT, order_id=None):
        gift = db.setting_int("first_ride_gift")
        if gift > 0:
            _add(
                session,
                phone=phone,
                delta=gift,
                reason=REASON_GIFT,
                order_id=order.id,
                actor=actor,
                note="מתנת הצטרפות",
            )
            granted[REASON_GIFT] = gift
            customer = session.scalars(
                select(db.Customer).where(db.Customer.phone == phone)
            ).first()
            if customer is not None and customer.club_joined_at is None:
                customer.club_joined_at = datetime.utcnow()

    # Imported late: referrals credit the referrer through this same ledger.
    from app import referrals

    referral_points = referrals.credit_for_order(session, order, actor=actor)
    if referral_points:
        granted[REASON_REFERRAL] = referral_points

    return {"awarded": sum(granted.values()), "breakdown": granted}


def reverse_for_order(session: Session, order: db.Order, *, actor: str = "system") -> int:
    """An order that was marked done and then cancelled gives the points back.
    Redemptions are refunded too — the passenger never got the ride."""
    reversed_total = 0
    entries = session.scalars(
        select(db.PointsEntry).where(
            db.PointsEntry.order_id == order.id,
            db.PointsEntry.reason != REASON_REVERSAL,
        )
    ).all()
    already = session.scalar(
        select(func.coalesce(func.sum(db.PointsEntry.delta), 0)).where(
            db.PointsEntry.order_id == order.id, db.PointsEntry.reason == REASON_REVERSAL
        )
    )
    outstanding = int(sum(e.delta for e in entries) + (already or 0))
    if outstanding:
        _add(
            session,
            phone=order.phone,
            delta=-outstanding,
            reason=REASON_REVERSAL,
            order_id=order.id,
            actor=actor,
            note=f"ביטול הזמנה {order.id}",
        )
        reversed_total = outstanding
    if order.points_spent:
        order.points_spent = 0
    return reversed_total


def can_redeem(session: Session, phone: str) -> bool:
    return balance(session, phone) >= db.setting_int("redeem_points")


def redeem_ride(
    session: Session, order: db.Order, *, actor: str = "system"
) -> dict:
    """Spend the points that buy a free ride. Fails loudly rather than letting
    a balance go negative: the caller is told there are not enough points."""
    cost = db.setting_int("redeem_points")
    if order.points_spent:
        return {"redeemed": False, "error": "כבר מומשו נקודות בהזמנה זו"}
    current = balance(session, order.phone)
    if current < cost:
        return {"redeemed": False, "error": "אין מספיק נקודות", "balance": current}
    _add(
        session,
        phone=order.phone,
        delta=-cost,
        reason=REASON_REDEEM,
        order_id=order.id,
        actor=actor,
        note="נסיעת חינם",
    )
    order.points_spent = cost
    order.price = 0.0
    return {"redeemed": True, "spent": cost, "balance": current - cost}


def adjust(
    session: Session, phone: str, delta: int, *, actor: str, note: str | None = None
) -> int:
    """Dispatcher correction. Deliberately allowed to go negative only down to
    zero, so a mistake cannot leave a passenger in debt to the club."""
    current = balance(session, phone)
    if delta < 0 and current + delta < 0:
        delta = -current
    if delta:
        _add(session, phone=phone, delta=delta, reason=REASON_MANUAL, actor=actor, note=note)
    return balance(session, phone)


def club_members(session: Session, limit: int = 500) -> list[dict]:
    rows = session.execute(
        select(
            db.PointsEntry.phone,
            func.sum(db.PointsEntry.delta).label("balance"),
            func.max(db.PointsEntry.created_at).label("last_at"),
        )
        .group_by(db.PointsEntry.phone)
        .order_by(func.sum(db.PointsEntry.delta).desc())
        .limit(limit)
    ).all()
    names = {
        c.phone: c.name
        for c in session.scalars(select(db.Customer)).all()
    }
    return [
        {
            "phone": row.phone,
            "name": names.get(row.phone),
            "balance": int(row.balance or 0),
            "last_at": row.last_at.isoformat() if row.last_at else None,
        }
        for row in rows
    ]
