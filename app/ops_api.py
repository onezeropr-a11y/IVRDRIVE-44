"""JSON API for the three back-office screens: dispatch, the passenger club,
and the books.

Kept apart from ``api.py`` — which serves the original order/call board — only
because it is a different surface with a different audience; it mounts on the
same ``/api`` prefix behind the same token, so the console sees one API.

Money-moving endpoints take an actor from the ``X-Actor`` header and write it
to the action log. It is not authentication (the shared admin token is that),
it is attribution: "who gave this passenger 500 points" has to be answerable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import accounting, db, dispatch, drivers, loyalty, notify, pbx, ratings, referrals
from app.api import require_token

router = APIRouter(prefix="/api", tags=["ops"], dependencies=[Depends(require_token)])


def actor_of(x_actor: Annotated[str | None, Header()] = None) -> str:
    return (x_actor or "console").strip()[:64]


Actor = Annotated[str, Depends(actor_of)]
Payload = Annotated[dict, Body()]


def _int(payload: dict, key: str) -> int | None:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{key} must be a number") from exc


def _bool(payload: dict, key: str) -> bool | None:
    value = payload.get(key)
    return None if value is None else bool(value)


# ------------------------------------------------------------------ drivers


@router.get("/drivers")
def list_drivers(status: str | None = None) -> dict:
    with db.session_scope() as session:
        stmt = select(db.Driver).order_by(db.Driver.phone)
        if status:
            stmt = stmt.where(db.Driver.status == status)
        rows = session.scalars(stmt).all()
        return {"drivers": [drivers.to_json(session, d) for d in rows]}


def _save_driver(session: Session, phone: str, payload: dict, actor: str) -> dict:
    driver = drivers.register(
        session,
        phone,
        name=payload.get("name"),
        home_area=payload.get("home_area"),
        car_model=payload.get("car_model"),
        car_year=_int(payload, "car_year"),
        seats=_int(payload, "seats"),
        birth_year=_int(payload, "birth_year"),
        smartphone=_bool(payload, "smartphone"),
        voice_offers=_bool(payload, "voice_offers"),
        quiet_from=_int(payload, "quiet_from"),
        quiet_to=_int(payload, "quiet_to"),
        status=payload.get("status"),
        notes=payload.get("notes"),
    )
    if isinstance(payload.get("areas"), list):
        drivers.set_areas(session, driver, [str(a) for a in payload["areas"]])
    db.log_action(session, "driver_edited", actor=actor, entity="driver", entity_id=driver.id)
    return drivers.to_json(session, driver)


@router.post("/drivers")
def create_driver(payload: Payload, actor: Actor) -> dict:
    phone = str(payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=422, detail="phone required")
    with db.session_scope() as session:
        return _save_driver(session, phone, payload, actor)


@router.patch("/drivers/{driver_id}")
def update_driver(driver_id: int, payload: Payload, actor: Actor) -> dict:
    with db.session_scope() as session:
        driver = session.get(db.Driver, driver_id)
        if driver is None:
            raise HTTPException(status_code=404, detail="no such driver")
        return _save_driver(session, driver.phone, payload, actor)


@router.delete("/drivers/{driver_id}")
def remove_driver(driver_id: int, actor: Actor) -> dict:
    """Removal is a status change, never a delete: the driver's finished rides
    are still on the books and still owe commission."""
    with db.session_scope() as session:
        driver = session.get(db.Driver, driver_id)
        if driver is None:
            raise HTTPException(status_code=404, detail="no such driver")
        driver.status = "removed"
        db.log_action(
            session, "driver_removed", actor=actor, entity="driver", entity_id=driver.id
        )
        return {"ok": True, "id": driver_id, "status": driver.status}


@router.get("/drivers/board")
def board() -> dict:
    with db.session_scope() as session:
        return {"areas": drivers.area_board(session)}


@router.post("/drivers/{driver_id}/location")
def driver_location(driver_id: int, payload: Payload) -> dict:
    with db.session_scope() as session:
        driver = session.get(db.Driver, driver_id)
        if driver is None:
            raise HTTPException(status_code=404, detail="no such driver")
        area = str(payload.get("area") or "").strip()
        if not area:
            raise HTTPException(status_code=422, detail="area required")
        return drivers.report_location(
            session, driver, area, source=str(payload.get("source") or "declared")
        )


@router.post("/drivers/{driver_id}/flash")
def driver_flash(driver_id: int, actor: Actor) -> dict:
    with db.session_scope() as session:
        driver = session.get(db.Driver, driver_id)
        if driver is None:
            raise HTTPException(status_code=404, detail="no such driver")
        result = pbx.flash_call(session, driver.phone, driver_id=driver.id, kind="manual")
        db.log_action(
            session,
            "flash_manual",
            actor=actor,
            entity="driver",
            entity_id=driver.id,
            detail=result["status"],
        )
        return result


# -------------------------------------------------------------------- areas


@router.get("/areas")
def list_areas() -> dict:
    with db.session_scope() as session:
        rows = session.scalars(select(db.Area).order_by(db.Area.id)).all()
        return {
            "areas": [
                {
                    "id": a.id,
                    "name": a.name,
                    "callback_number": a.callback_number,
                    "flash_cid": a.flash_cid,
                    "active": a.active,
                }
                for a in rows
            ]
        }


@router.post("/areas")
def create_area(payload: Payload, actor: Actor) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name required")
    with db.session_scope() as session:
        row = session.scalars(select(db.Area).where(db.Area.name == name)).first()
        if row is None:
            row = db.Area(name=name)
            session.add(row)
        row.callback_number = payload.get("callback_number") or row.callback_number
        row.flash_cid = payload.get("flash_cid") or row.flash_cid
        if payload.get("active") is not None:
            row.active = bool(payload["active"])
        session.flush()
        db.log_action(session, "area_saved", actor=actor, entity="area", entity_id=row.id)
        return {"id": row.id, "name": row.name}


# ------------------------------------------------------------------ tenders


@router.post("/orders/{order_id}/tender")
def open_tender(order_id: int, payload: Payload, actor: Actor) -> dict:
    """Ring the area's drivers about this ride and open the bidding window."""
    with db.session_scope() as session:
        order = session.get(db.Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="no such order")
        result = dispatch.open_tender(
            session,
            order,
            area=payload.get("area"),
            filters=payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
            window_seconds=_int(payload, "window_seconds"),
            actor=actor,
        )
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error"))
        return result


@router.get("/tenders")
def list_tenders(limit: int = 50) -> dict:
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.Tender).order_by(db.Tender.opened_at.desc()).limit(limit)
        ).all()
        bids: dict[int, int] = {}
        for tender_id, in session.execute(
            select(db.TenderBid.tender_id).where(
                db.TenderBid.tender_id.in_([t.id for t in rows] or [0])
            )
        ).all():
            bids[tender_id] = bids.get(tender_id, 0) + 1
        return {
            "tenders": [
                {
                    "id": t.id,
                    "order_id": t.order_id,
                    "area": t.area,
                    "status": t.status,
                    "opened_at": t.opened_at.isoformat(),
                    "closes_at": t.closes_at.isoformat(),
                    "notified": t.notified,
                    "bids": bids.get(t.id, 0),
                    "awarded_driver_id": t.awarded_driver_id,
                    "filters": drivers.parse_filters(t.filters_json),
                }
                for t in rows
            ]
        }


@router.post("/tenders/{tender_id}/bid")
def bid(tender_id: int, payload: Payload) -> dict:
    """A bid placed for a driver by the dispatcher — the phone path goes
    through the IVR, but a driver who called the office still competes."""
    with db.session_scope() as session:
        tender = session.get(db.Tender, tender_id)
        driver = session.get(db.Driver, _int(payload, "driver_id") or 0)
        if tender is None or driver is None:
            raise HTTPException(status_code=404, detail="no such tender or driver")
        return dispatch.place_bid(session, tender, driver)


@router.post("/tenders/{tender_id}/close")
def close_tender(tender_id: int, actor: Actor) -> dict:
    with db.session_scope() as session:
        tender = session.get(db.Tender, tender_id)
        if tender is None:
            raise HTTPException(status_code=404, detail="no such tender")
        return dispatch.close_tender(session, tender, actor=actor)


@router.post("/tenders/{tender_id}/cancel")
def cancel_tender(tender_id: int, actor: Actor) -> dict:
    with db.session_scope() as session:
        tender = session.get(db.Tender, tender_id)
        if tender is None:
            raise HTTPException(status_code=404, detail="no such tender")
        dispatch.cancel(session, tender, actor=actor)
        return {"ok": True}


# ------------------------------------------------------------------- orders


@router.post("/orders")
def create_order(payload: Payload, actor: Actor) -> dict:
    """A ride taken by a human at the desk rather than by the bot; the rest of
    the pipeline cannot tell the difference."""
    phone = db.normalize_phone(str(payload.get("phone") or ""))
    origin = str(payload.get("origin") or "").strip()
    destination = str(payload.get("destination") or "").strip()
    if not (phone and origin and destination):
        raise HTTPException(status_code=422, detail="phone, origin and destination required")
    with db.session_scope() as session:
        order = db.Order(
            call_id=f"desk-{datetime.utcnow():%Y%m%d%H%M%S}",
            phone=phone,
            origin=origin,
            destination=destination,
            passengers=_int(payload, "passengers") or 1,
            pickup_time=payload.get("pickup_time") or None,
            price=float(payload["price"]) if payload.get("price") not in (None, "") else None,
            notes=payload.get("notes") or None,
            area=payload.get("area") or origin,
        )
        session.add(order)
        session.flush()
        db.log_action(
            session, "order_created", actor=actor, entity="order", entity_id=order.id
        )
        result = {"id": order.id, "status": order.status}
        if payload.get("tender"):
            result["tender"] = dispatch.open_tender(session, order, actor=actor)
        return result


@router.post("/orders/{order_id}/finish")
def finish_order(order_id: int, payload: Payload, actor: Actor) -> dict:
    with db.session_scope() as session:
        order = session.get(db.Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="no such order")
        result = dispatch.finish_ride(session, order, area=payload.get("area"))
        db.log_action(
            session, "order_finished", actor=actor, entity="order", entity_id=order.id
        )
        return result


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, actor: Actor) -> dict:
    """Cancelling a completed ride takes its points back — the ride did not
    happen, so the points were never earned."""
    with db.session_scope() as session:
        order = session.get(db.Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="no such order")
        reversed_points = loyalty.reverse_for_order(session, order, actor=actor)
        order.status = "cancelled"
        return {"ok": True, "points_reversed": reversed_points}


@router.post("/orders/{order_id}/redeem")
def redeem_order(order_id: int, actor: Actor) -> dict:
    with db.session_scope() as session:
        order = session.get(db.Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="no such order")
        result = loyalty.redeem_ride(session, order, actor=actor)
        if not result["redeemed"]:
            raise HTTPException(status_code=409, detail=result["error"])
        return result


# --------------------------------------------------------------------- club


@router.get("/club/members")
def club_members() -> dict:
    with db.session_scope() as session:
        return {"members": loyalty.club_members(session)}


@router.get("/club/{phone}")
def club_member(phone: str) -> dict:
    with db.session_scope() as session:
        clean = db.normalize_phone(phone)
        customer = session.scalars(
            select(db.Customer).where(db.Customer.phone == clean)
        ).first()
        return {
            "phone": clean,
            "name": customer.name if customer else None,
            "balance": loyalty.balance(session, clean),
            "can_redeem": loyalty.can_redeem(session, clean),
            "preferences": {
                "preferred_driver_phone": customer.preferred_driver_phone if customer else None,
                "blocked_driver_phone": customer.blocked_driver_phone if customer else None,
                "no_marketing": customer.no_marketing if customer else False,
                "default_pickup": customer.default_pickup if customer else None,
            },
            "history": [
                {
                    "id": e.id,
                    "delta": e.delta,
                    "reason": e.reason,
                    "order_id": e.order_id,
                    "actor": e.actor,
                    "note": e.note,
                    "created_at": e.created_at.isoformat(),
                }
                for e in loyalty.history(session, clean)
            ],
            "referrals": [
                {
                    "invited_phone": r.invited_phone,
                    "status": r.status,
                    "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
                    "credit_until": r.credit_until.isoformat() if r.credit_until else None,
                    "rewarded_orders": r.rewarded_orders,
                }
                for r in referrals.for_referrer(session, clean)
            ],
        }


@router.post("/club/{phone}/adjust")
def club_adjust(phone: str, payload: Payload, actor: Actor) -> dict:
    delta = _int(payload, "delta")
    if not delta:
        raise HTTPException(status_code=422, detail="delta required")
    with db.session_scope() as session:
        balance = loyalty.adjust(
            session, phone, delta, actor=actor, note=payload.get("note")
        )
        return {"phone": db.normalize_phone(phone), "balance": balance}


@router.patch("/club/{phone}/preferences")
def club_preferences(phone: str, payload: Payload, actor: Actor) -> dict:
    clean = db.normalize_phone(phone)
    with db.session_scope() as session:
        customer = session.scalars(
            select(db.Customer).where(db.Customer.phone == clean)
        ).first()
        if customer is None:
            customer = db.Customer(phone=clean)
            session.add(customer)
        if "name" in payload:
            customer.name = payload["name"] or None
        if "default_pickup" in payload:
            customer.default_pickup = payload["default_pickup"] or None
        if "preferred_driver_phone" in payload:
            customer.preferred_driver_phone = payload["preferred_driver_phone"] or None
        if "blocked_driver_phone" in payload:
            customer.blocked_driver_phone = payload["blocked_driver_phone"] or None
        if "no_marketing" in payload:
            customer.no_marketing = bool(payload["no_marketing"])
        session.flush()
        db.log_action(
            session, "customer_prefs", actor=actor, entity="phone", entity_id=clean
        )
        return {"ok": True, "phone": clean}


# ---------------------------------------------------------------- referrals


@router.get("/referrals")
def list_referrals(limit: int = 200) -> dict:
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.Referral).order_by(db.Referral.created_at.desc()).limit(limit)
        ).all()
        return {
            "referrals": [
                {
                    "id": r.id,
                    "referrer_phone": r.referrer_phone,
                    "invited_phone": r.invited_phone,
                    "status": r.status,
                    "created_at": r.created_at.isoformat(),
                    "expires_at": r.expires_at.isoformat(),
                    "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
                    "credit_until": r.credit_until.isoformat() if r.credit_until else None,
                    "rewarded_orders": r.rewarded_orders,
                }
                for r in rows
            ]
        }


@router.post("/referrals")
def create_referral(payload: Payload, actor: Actor) -> dict:
    with db.session_scope() as session:
        result = referrals.assign(
            session,
            str(payload.get("referrer_phone") or ""),
            str(payload.get("invited_phone") or ""),
            actor=actor,
        )
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["error"])
        if payload.get("flash"):
            pbx.flash_call(
                session, str(payload["invited_phone"]), kind="referral"
            )
        return result


# ------------------------------------------------------------------ ratings


@router.get("/ratings")
def list_ratings(limit: int = 200) -> dict:
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.RatingRequest).order_by(db.RatingRequest.due_at.desc()).limit(limit)
        ).all()
        return {
            "ratings": [
                {
                    "id": r.id,
                    "order_id": r.order_id,
                    "driver_id": r.driver_id,
                    "phone": r.phone,
                    "due_at": r.due_at.isoformat(),
                    "status": r.status,
                    "score": r.score,
                    "attempts": r.attempts,
                }
                for r in rows
            ]
        }


@router.post("/ratings/{rating_id}/call")
def call_rating(rating_id: int) -> dict:
    with db.session_scope() as session:
        row = session.get(db.RatingRequest, rating_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such rating request")
        return ratings.place_call(session, row)


@router.post("/ratings/{rating_id}/score")
def score_rating(rating_id: int, payload: Payload) -> dict:
    with db.session_scope() as session:
        row = session.get(db.RatingRequest, rating_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such rating request")
        result = ratings.record_score(session, row, _int(payload, "score") or 0)
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["error"])
        return result


# --------------------------------------------------------------- accounting


@router.get("/accounting/summary")
def accounting_summary(days: int = 30) -> dict:
    with db.session_scope() as session:
        return accounting.profit_and_loss(session, days)


@router.get("/accounting/drivers")
def accounting_drivers(days: int = 30) -> dict:
    with db.session_scope() as session:
        return {"drivers": accounting.rides_by_driver(session, days)}


@router.get("/accounting/drivers/{driver_id}")
def accounting_driver(driver_id: int, days: int = 30) -> dict:
    with db.session_scope() as session:
        statement = accounting.driver_statement(session, driver_id, days)
        if not statement:
            raise HTTPException(status_code=404, detail="no such driver")
        return statement


@router.post("/accounting/drivers/{driver_id}/send")
def send_driver_statement(driver_id: int, payload: Payload, actor: Actor) -> dict:
    """The commission invoice: the driver's rides and what they owe, pushed to
    whatever messaging webhook the office uses."""
    days = _int(payload, "days") or 30
    with db.session_scope() as session:
        statement = accounting.driver_statement(session, driver_id, days)
        if not statement:
            raise HTTPException(status_code=404, detail="no such driver")
        lines = [
            f"פירוט נסיעות ל{statement['driver']['name'] or statement['driver']['phone']}",
            f"תקופה: {days} ימים אחרונים",
            *[
                f"{r['date'][:10]} {r['origin']} → {r['destination']} "
                f"{r['price']}₪ (עמלה {r['commission']}₪)"
                for r in statement["rides"]
            ],
            f"סה\"כ נסיעות: {len(statement['rides'])}",
            f"סה\"כ לתשלום תיווך: {statement['total_commission']}₪",
        ]
        text = "\n".join(lines)
        sent = notify.send_text(
            text, kind="driver_statement", extra={"driver": statement["driver"]}
        )
        db.log_action(
            session,
            "statement_sent" if sent else "statement_prepared",
            actor=actor,
            entity="driver",
            entity_id=driver_id,
            detail=f"{len(statement['rides'])} rides",
        )
        return {"sent": sent, "text": text, **statement}


@router.get("/expenses")
def list_expenses(limit: int = 200) -> dict:
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.Expense).order_by(db.Expense.spent_on.desc()).limit(limit)
        ).all()
        return {
            "expenses": [
                {
                    "id": e.id,
                    "spent_on": e.spent_on.isoformat(),
                    "category": e.category,
                    "amount": e.amount,
                    "note": e.note,
                }
                for e in rows
            ]
        }


@router.post("/expenses")
def create_expense(payload: Payload, actor: Actor) -> dict:
    category = str(payload.get("category") or "").strip()
    try:
        amount = float(payload.get("amount"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="amount must be a number") from exc
    if not category:
        raise HTTPException(status_code=422, detail="category required")
    with db.session_scope() as session:
        row = accounting.add_expense(
            session,
            category=category,
            amount=amount,
            note=payload.get("note"),
            actor=actor,
        )
        if payload.get("spent_on"):
            row.spent_on = datetime.fromisoformat(str(payload["spent_on"]))
        return {"id": row.id}


# ---------------------------------------------------------- log & settings


@router.get("/logs")
def list_logs(limit: int = 200, action: str | None = None) -> dict:
    with db.session_scope() as session:
        stmt = select(db.ActionLog).order_by(db.ActionLog.created_at.desc()).limit(limit)
        if action:
            stmt = stmt.where(db.ActionLog.action == action)
        rows = session.scalars(stmt).all()
        return {
            "logs": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat(),
                    "actor": r.actor,
                    "action": r.action,
                    "entity": r.entity,
                    "entity_id": r.entity_id,
                    "detail": r.detail,
                }
                for r in rows
            ]
        }


@router.get("/settings")
def get_settings() -> dict:
    with db.session_scope() as session:
        stored = {row.key: row.value for row in session.scalars(select(db.Setting)).all()}
    return {"settings": {**db.DEFAULT_SETTINGS, **stored}}


@router.put("/settings")
def put_settings(payload: Payload, actor: Actor) -> dict:
    for key, value in payload.items():
        db.set_setting(str(key), str(value))
    with db.session_scope() as session:
        db.log_action(
            session, "settings_changed", actor=actor, detail=",".join(sorted(payload))
        )
    return get_settings()
