"""'Share and ride' — a passenger brings numbers, and earns on their rides.

The rules exist to stop the obvious abuse of a referral scheme, so they are
enforced here rather than in the UI:

* only a number the system has never seen can be referred, which keeps existing
  passengers from being re-claimed;
* the invited number confirms by ringing in itself within 24 hours, so nobody
  can enrol a stranger;
* the referrer earns on that number's rides for 30 days from confirmation;
* a number can be referred exactly once, ever.

The confirmation call is nudged along by a flash call to the invited number:
one ring, no cost, and the office number is left in the missed-calls list for
them to ring back.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db

log = logging.getLogger("referrals")

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_EXPIRED = "expired"


def _known_number(session: Session, phone: str) -> bool:
    """"Not in the list" means the number is unknown to the business: no
    customer record, no order, and not a driver."""
    if session.scalars(select(db.Customer.id).where(db.Customer.phone == phone)).first():
        return True
    if session.scalars(select(db.Order.id).where(db.Order.phone == phone)).first():
        return True
    return bool(session.scalars(select(db.Driver.id).where(db.Driver.phone == phone)).first())


def assign(
    session: Session, referrer_phone: str, invited_phone: str, *, actor: str = "ivr"
) -> dict:
    referrer = db.normalize_phone(referrer_phone)
    invited = db.normalize_phone(invited_phone)
    if not invited or len(invited) < 9:
        return {"ok": False, "error": "מספר לא תקין"}
    if invited == referrer:
        return {"ok": False, "error": "אי אפשר לשייך את המספר של עצמך"}

    existing = session.scalars(
        select(db.Referral).where(db.Referral.invited_phone == invited)
    ).first()
    if existing is not None:
        return {"ok": False, "error": "המספר כבר שויך", "status": existing.status}
    if _known_number(session, invited):
        return {"ok": False, "error": "המספר כבר קיים במערכת"}

    hours = db.setting_int("referral_confirm_hours")
    referral = db.Referral(
        referrer_phone=referrer,
        invited_phone=invited,
        status=STATUS_PENDING,
        expires_at=datetime.utcnow() + timedelta(hours=hours),
    )
    session.add(referral)
    session.flush()
    db.log_action(
        session,
        "referral_assigned",
        actor=actor,
        entity="referral",
        entity_id=referral.id,
        detail=f"{referrer} -> {invited}",
    )
    return {"ok": True, "referral_id": referral.id, "expires_hours": hours}


def confirm_by_call(session: Session, phone: str) -> db.Referral | None:
    """Called on every inbound call: if this number owes a confirmation and the
    window is still open, the call itself is the confirmation."""
    invited = db.normalize_phone(phone)
    referral = session.scalars(
        select(db.Referral).where(
            db.Referral.invited_phone == invited, db.Referral.status == STATUS_PENDING
        )
    ).first()
    if referral is None:
        return None
    now = datetime.utcnow()
    if referral.expires_at < now:
        referral.status = STATUS_EXPIRED
        db.log_action(
            session, "referral_expired", entity="referral", entity_id=referral.id
        )
        return None
    referral.status = STATUS_CONFIRMED
    referral.confirmed_at = now
    referral.credit_until = now + timedelta(days=db.setting_int("referral_credit_days"))
    db.log_action(
        session,
        "referral_confirmed",
        entity="referral",
        entity_id=referral.id,
        detail=f"{referral.referrer_phone} <- {invited}",
    )
    return referral


def credit_for_order(session: Session, order: db.Order, *, actor: str = "system") -> int:
    """Pay the referrer for a completed ride of a number they brought, once per
    order and only inside the credit window."""
    from app import loyalty

    invited = db.normalize_phone(order.phone)
    referral = session.scalars(
        select(db.Referral).where(
            db.Referral.invited_phone == invited, db.Referral.status == STATUS_CONFIRMED
        )
    ).first()
    if referral is None or referral.credit_until is None:
        return 0
    when = order.finished_at or order.created_at or datetime.utcnow()
    if when > referral.credit_until:
        return 0
    already = session.scalars(
        select(db.PointsEntry.id).where(
            db.PointsEntry.order_id == order.id,
            db.PointsEntry.reason == loyalty.REASON_REFERRAL,
        )
    ).first()
    if already:
        return 0
    points = db.setting_int("referral_points")
    if points <= 0:
        return 0
    loyalty.grant(
        session,
        phone=referral.referrer_phone,
        delta=points,
        reason=loyalty.REASON_REFERRAL,
        order_id=order.id,
        referral_id=referral.id,
        actor=actor,
        note=f"נסיעה של {invited}",
    )
    referral.rewarded_orders += 1
    return points


def expire_stale(session: Session) -> int:
    """Housekeeping for windows that closed without a confirming call."""
    now = datetime.utcnow()
    rows = session.scalars(
        select(db.Referral).where(
            db.Referral.status == STATUS_PENDING, db.Referral.expires_at < now
        )
    ).all()
    for row in rows:
        row.status = STATUS_EXPIRED
    return len(rows)


def for_referrer(session: Session, phone: str) -> list[db.Referral]:
    return list(
        session.scalars(
            select(db.Referral)
            .where(db.Referral.referrer_phone == db.normalize_phone(phone))
            .order_by(db.Referral.created_at.desc())
        ).all()
    )
