"""The automatic driver rating call.

One call per finished ride, ever. The guarantee is structural rather than
careful bookkeeping: the request row is unique on ``order_id``, and the row's
status is what the dialler reads, so a retrying worker, a dispatcher pressing
"complete" twice and the scheduler waking up late all converge on the same
single call.

Timing follows the two triggers the business asked for: if the driver reported
the ride finished, the passenger is called right away while it is fresh;
otherwise the ride is assumed over an hour and a half after it was ordered.

The flash call cannot carry audio, so the rating uses the campaign endpoint
with a single recipient pointed at our Module API tree — the passenger answers,
hears the question and presses a digit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db, drivers, pbx

log = logging.getLogger("ratings")

STATUS_SCHEDULED = "scheduled"
STATUS_CALLING = "calling"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

MAX_ATTEMPTS = 2


def schedule_for_order(session: Session, order: db.Order) -> dict:
    """Queue the call. Returns the existing row untouched if there is one."""
    existing = session.scalars(
        select(db.RatingRequest).where(db.RatingRequest.order_id == order.id)
    ).first()
    if existing is not None:
        return {"scheduled": False, "status": existing.status, "id": existing.id}

    customer = session.scalars(
        select(db.Customer).where(db.Customer.phone == db.normalize_phone(order.phone))
    ).first()
    if customer is not None and customer.no_marketing:
        session.add(
            db.RatingRequest(
                order_id=order.id,
                driver_id=order.driver_id,
                phone=db.normalize_phone(order.phone),
                due_at=datetime.utcnow(),
                status=STATUS_SKIPPED,
            )
        )
        return {"scheduled": False, "status": STATUS_SKIPPED}

    delay = db.setting_int("rating_delay_minutes")
    due = (
        order.finished_at
        if order.finished_at is not None
        else (order.created_at or datetime.utcnow()) + timedelta(minutes=delay)
    )
    row = db.RatingRequest(
        order_id=order.id,
        driver_id=order.driver_id,
        phone=db.normalize_phone(order.phone),
        due_at=due,
        status=STATUS_SCHEDULED,
    )
    session.add(row)
    session.flush()
    db.log_action(
        session,
        "rating_scheduled",
        entity="rating",
        entity_id=row.id,
        detail=f"order {order.id} due {due.isoformat()}",
    )
    return {"scheduled": True, "id": row.id, "due_at": due.isoformat()}


def due_requests(session: Session, *, now: datetime | None = None) -> list[db.RatingRequest]:
    now = now or datetime.utcnow()
    return list(
        session.scalars(
            select(db.RatingRequest).where(
                db.RatingRequest.status == STATUS_SCHEDULED, db.RatingRequest.due_at <= now
            )
        ).all()
    )


def module_url(request: db.RatingRequest) -> str:
    base = (db.get_setting("public_base_url") or "").rstrip("/")
    return f"{base}/ivr/rating?rating={request.id}" if base else ""


def place_call(session: Session, request: db.RatingRequest) -> dict:
    """Dial the passenger. Marked `calling` before the request goes out, so a
    crash mid-dial never turns into a second call."""
    if request.status != STATUS_SCHEDULED:
        return {"called": False, "status": request.status}
    request.status = STATUS_CALLING
    request.attempts += 1
    session.flush()
    try:
        pbx.voice_broadcast(
            [request.phone], name=f"rating-{request.id}", module_url=module_url(request)
        )
    except pbx.PbxError as exc:
        request.status = (
            STATUS_SCHEDULED if request.attempts < MAX_ATTEMPTS else STATUS_FAILED
        )
        log.warning("rating call %s failed: %s", request.id, exc)
        return {"called": False, "error": str(exc), "status": request.status}
    return {"called": True, "status": request.status}


def record_score(session: Session, request: db.RatingRequest, score: int) -> dict:
    """Digits outside 1-5 are the caller mistyping, not a rating."""
    if score < 1 or score > 5:
        return {"ok": False, "error": "דירוג חייב להיות בין 1 ל-5"}
    if request.status == STATUS_DONE:
        return {"ok": False, "error": "כבר דורג"}
    request.score = score
    request.status = STATUS_DONE
    request.answered_at = datetime.utcnow()
    driver = session.get(db.Driver, request.driver_id) if request.driver_id else None
    if driver is not None:
        drivers.record_rating(session, driver, score)
    db.log_action(
        session,
        "rating_recorded",
        entity="rating",
        entity_id=request.id,
        detail=f"order {request.order_id} score {score}",
    )
    return {"ok": True, "score": score}


def run_due(session: Session, *, now: datetime | None = None) -> int:
    placed = 0
    for request in due_requests(session, now=now):
        if place_call(session, request).get("called"):
            placed += 1
    return placed
