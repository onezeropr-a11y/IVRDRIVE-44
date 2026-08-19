"""Offering a ride to drivers, and choosing between the ones who want it.

The flow the drivers experience:

1. the dispatcher (or the bot, on a fresh order) opens a **tender** for an area;
2. every eligible driver gets a flash call — one ring, free, showing that
   area's caller ID. Drivers who pay for spoken offers get a voice broadcast
   instead, where pressing 1 bids without a callback;
3. a driver rings the area number back, hears the ride, and presses 1 to bid;
4. the window stays open for a few more seconds so slower drivers still get a
   fair shot — this is what makes it an auction rather than a race;
5. when it closes, the algorithm picks the best bidder and only then is anyone
   connected to the passenger.

Nobody is told they won until the window closes, so the first driver to press 1
is not automatically the driver who gets the ride.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db, drivers, loyalty, pbx, ratings

log = logging.getLogger("dispatch")

STATUS_OPEN = "open"
STATUS_AWARDED = "awarded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


def open_tender(
    session: Session,
    order: db.Order,
    *,
    area: str | None = None,
    filters: dict | None = None,
    window_seconds: int | None = None,
    actor: str = "dispatcher",
    blast: bool = True,
) -> dict:
    """Open the bidding on one order and ring the drivers who qualify."""
    area = area or order.area or order.origin
    window = window_seconds or db.setting_int("tender_window_seconds") or 10
    now = datetime.utcnow()

    existing = session.scalars(
        select(db.Tender).where(
            db.Tender.order_id == order.id, db.Tender.status == STATUS_OPEN
        )
    ).first()
    if existing is not None:
        return {"ok": False, "error": "כבר פתוח מכרז להזמנה זו", "tender_id": existing.id}

    tender = db.Tender(
        order_id=order.id,
        area=area,
        status=STATUS_OPEN,
        opened_at=now,
        closes_at=now + timedelta(seconds=window),
        filters_json=json.dumps(filters or {}, ensure_ascii=False),
    )
    session.add(tender)
    session.flush()
    order.area = area

    notified = blast_tender(session, tender, filters or {}) if blast else {"flash": 0, "voice": 0}
    tender.notified = int(notified.get("flash", 0)) + int(notified.get("voice", 0))
    db.log_action(
        session,
        "tender_opened",
        actor=actor,
        entity="tender",
        entity_id=tender.id,
        detail=f"order {order.id} area {area} notified {tender.notified}",
    )
    return {
        "ok": True,
        "tender_id": tender.id,
        "closes_at": tender.closes_at.isoformat(),
        "window_seconds": window,
        **notified,
    }


def blast_tender(session: Session, tender: db.Tender, filters: dict) -> dict:
    """Ring everyone eligible. Flash calls for most, a spoken campaign for the
    drivers who pay for one; a driver never gets both."""
    ranked = drivers.candidates(session, tender.area, filters)
    area_row = session.scalars(
        select(db.Area).where(db.Area.name == (tender.area or ""))
    ).first()
    cid = area_row.flash_cid if area_row and area_row.flash_cid else pbx.new_cid(tender.id)

    flash_targets = [d for d, _ in ranked if not d.voice_offers]
    voice_targets = [d for d, _ in ranked if d.voice_offers]

    sent = 0
    for driver in flash_targets:
        result = pbx.flash_call(
            session,
            driver.phone,
            cid=cid,
            driver_id=driver.id,
            tender_id=tender.id,
            kind="tender",
        )
        sent += 1 if result["sent"] else 0

    voice_sent = 0
    if voice_targets:
        try:
            pbx.voice_broadcast(
                [d.phone for d in voice_targets],
                name=f"tender-{tender.id}",
                module_url=voice_module_url(tender),
            )
            voice_sent = len(voice_targets)
        except pbx.PbxError as exc:
            # A campaign that will not start must not leave the drivers who
            # paid for it worse off than the ones who did not.
            log.warning("voice broadcast for tender %s failed: %s", tender.id, exc)
            for driver in voice_targets:
                result = pbx.flash_call(
                    session,
                    driver.phone,
                    cid=cid,
                    driver_id=driver.id,
                    tender_id=tender.id,
                    kind="tender",
                )
                sent += 1 if result["sent"] else 0

    return {"eligible": len(ranked), "flash": sent, "voice": voice_sent}


def voice_module_url(tender: db.Tender) -> str:
    base = (
        db.get_setting("public_base_url")
        or ""
    ).rstrip("/")
    return f"{base}/ivr/driver?tender={tender.id}" if base else ""


def open_tender_for_area(session: Session, area: str | None) -> db.Tender | None:
    """What a driver ringing an area number is being offered right now."""
    stmt = (
        select(db.Tender)
        .where(db.Tender.status == STATUS_OPEN)
        .order_by(db.Tender.opened_at.desc())
    )
    if area:
        stmt = stmt.where(db.Tender.area == area)
    return session.scalars(stmt.limit(1)).first()


def latest_tender_for_driver(session: Session, driver: db.Driver) -> db.Tender | None:
    """The tender this driver was actually rung about, so a callback lands on
    the right ride even after the dispatcher opened a newer one."""
    flash = session.scalars(
        select(db.FlashCall)
        .where(db.FlashCall.driver_id == driver.id, db.FlashCall.tender_id.is_not(None))
        .order_by(db.FlashCall.created_at.desc())
        .limit(1)
    ).first()
    if flash is not None and flash.tender_id:
        tender = session.get(db.Tender, flash.tender_id)
        if tender is not None and tender.status == STATUS_OPEN:
            return tender
    return open_tender_for_area(session, driver.last_area or driver.home_area)


def place_bid(session: Session, tender: db.Tender, driver: db.Driver) -> dict:
    """Register interest. The answer is deliberately 'wait', not 'yours'."""
    if tender.status != STATUS_OPEN:
        return {"ok": False, "state": tender.status, "error": "הנסיעה כבר נתפסה"}
    if datetime.utcnow() >= tender.closes_at:
        close_tender(session, tender)
        return {"ok": False, "state": tender.status, "error": "חלון ההצעות נסגר"}
    existing = session.scalars(
        select(db.TenderBid).where(
            db.TenderBid.tender_id == tender.id, db.TenderBid.driver_id == driver.id
        )
    ).first()
    if existing is None:
        session.add(
            db.TenderBid(
                tender_id=tender.id,
                driver_id=driver.id,
                score=drivers.total_score(session, driver, tender.area),
            )
        )
        session.flush()
    wait = max(0.0, (tender.closes_at - datetime.utcnow()).total_seconds())
    return {"ok": True, "wait_seconds": round(wait, 1), "tender_id": tender.id}


def close_tender(session: Session, tender: db.Tender, *, actor: str = "system") -> dict:
    """Pick the winner. Re-scored at close rather than reusing the bid's score,
    because a location update during the window is exactly the kind of fresh
    information the selection should honour."""
    if tender.status != STATUS_OPEN:
        return {"status": tender.status, "driver_id": tender.awarded_driver_id}

    bids = session.scalars(
        select(db.TenderBid).where(db.TenderBid.tender_id == tender.id)
    ).all()
    if not bids:
        tender.status = STATUS_FAILED
        db.log_action(
            session, "tender_failed", actor=actor, entity="tender", entity_id=tender.id
        )
        return {"status": STATUS_FAILED, "driver_id": None}

    scored: list[tuple[float, db.TenderBid, db.Driver]] = []
    for bid in bids:
        driver = session.get(db.Driver, bid.driver_id)
        if driver is None or driver.status != "active":
            continue
        score = drivers.total_score(session, driver, tender.area)
        bid.score = score
        scored.append((score, bid, driver))
    if not scored:
        tender.status = STATUS_FAILED
        return {"status": STATUS_FAILED, "driver_id": None}

    # Ties go to whoever pressed 1 first — the only fair tiebreak left.
    scored.sort(key=lambda item: (-item[0], item[1].created_at))
    score, winning_bid, winner = scored[0]
    winning_bid.won = True
    tender.status = STATUS_AWARDED
    tender.awarded_driver_id = winner.id
    tender.awarded_at = datetime.utcnow()

    order = session.get(db.Order, tender.order_id)
    if order is not None:
        order.driver_id = winner.id
        order.driver_name = winner.name
        order.driver_phone = winner.phone
        if order.status in {"new", "assigned"}:
            order.status = "assigned"
    db.log_action(
        session,
        "tender_awarded",
        actor=actor,
        entity="tender",
        entity_id=tender.id,
        detail=f"driver {winner.id} score {score} of {len(scored)} bids",
    )
    return {"status": STATUS_AWARDED, "driver_id": winner.id, "score": score}


def result_for_driver(session: Session, tender: db.Tender, driver: db.Driver) -> dict:
    """What the driver on hold should hear when the window closes."""
    if tender.status == STATUS_OPEN and datetime.utcnow() >= tender.closes_at:
        close_tender(session, tender)
    if tender.status == STATUS_AWARDED and tender.awarded_driver_id == driver.id:
        order = session.get(db.Order, tender.order_id)
        return {
            "won": True,
            "order_id": tender.order_id,
            "passenger_phone": order.phone if order else None,
        }
    return {"won": False, "state": tender.status}


def reap(session: Session) -> int:
    """Close every window that has run out. Called by the scheduler, and also
    defensively from the IVR, so a stalled worker cannot strand a ride."""
    due = session.scalars(
        select(db.Tender).where(
            db.Tender.status == STATUS_OPEN, db.Tender.closes_at <= datetime.utcnow()
        )
    ).all()
    for tender in due:
        close_tender(session, tender)
    return len(due)


def cancel(session: Session, tender: db.Tender, *, actor: str = "dispatcher") -> None:
    tender.status = STATUS_CANCELLED
    db.log_action(
        session, "tender_cancelled", actor=actor, entity="tender", entity_id=tender.id
    )


def finish_ride(session: Session, order: db.Order, *, area: str | None = None) -> dict:
    """The driver's 'ride finished': completes the order, which is what makes
    the points real, refreshes their location for free, and queues the rating
    call."""
    order.status = "done"
    order.finished_at = datetime.utcnow()
    rate = db.setting_float("commission_rate")
    order.commission = round(float(order.price or 0.0) * rate, 2)

    driver = session.get(db.Driver, order.driver_id) if order.driver_id else None
    if driver is not None:
        driver.rides_done += 1
        drivers.report_location(
            session, driver, area or order.destination or order.area or "", source="ride_finished"
        )

    awarded = loyalty.award_for_order(session, order)
    rating = ratings.schedule_for_order(session, order)
    return {"order_id": order.id, "points": awarded, "rating": rating}
