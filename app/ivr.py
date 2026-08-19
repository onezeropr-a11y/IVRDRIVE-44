"""Module API handler — the PBX asking our server what to do next in a call.

The PBX drives one module at a time: it GETs this handler, we answer with a
single JSON object describing one action (play, menu, capture digits, route,
hang up), it runs that action and comes back with the result. There is no
session on the PBX side, so the few digits already collected live in
``ivr_sessions``, keyed by the call id.

Two rules from the PBX documentation shape everything here:

* an unrecognised ``type`` or non-JSON body silently sends the caller back to
  the previous menu, which is invisible from our side, so every path — including
  every error path — must end in a valid module. That is why the handler catches
  its own exceptions and answers with an apology message instead of a 500;
* the request is not authenticated: the URL is the secret. So the caller's
  identity is only ever the phone number the PBX reports, and nothing
  destructive happens without it matching a record we already hold.

Audio file names are settings (``audio_*``), because the recordings live in the
PBX account's library and the office re-records them without a deploy.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db, dispatch, drivers, loyalty, pbx, ratings, referrals

log = logging.getLogger("ivr")

router = APIRouter(prefix="/ivr", tags=["ivr"])

#: No phone menu step takes longer than this, so anything older belongs to a
#: call that has already ended.
STALE_CALL = timedelta(minutes=15)

#: Logical name -> default file in the PBX audio library. The office uploads
#: recordings under these names, or renames them through the settings table.
DEFAULT_AUDIO = {
    "driver_menu": "drivers_menu",
    "driver_offer": "drivers_offer",
    "driver_wait": "drivers_wait",
    "driver_taken": "drivers_taken",
    "driver_no_offer": "drivers_no_offer",
    "driver_connecting": "drivers_connecting",
    "driver_register": "drivers_register",
    "driver_pending": "drivers_pending",
    "driver_saved": "drivers_saved",
    "driver_reputation": "drivers_reputation",
    "driver_area_prompt": "drivers_area_prompt",
    "driver_quiet_prompt": "drivers_quiet_prompt",
    "driver_location_prompt": "drivers_location_prompt",
    "driver_location_done": "drivers_location_done",
    "driver_finish_done": "drivers_finish_done",
    "passenger_menu": "club_menu",
    "passenger_balance": "club_balance",
    "passenger_redeem_ok": "club_redeem_ok",
    "passenger_redeem_no": "club_redeem_no",
    "passenger_refer_prompt": "club_refer_prompt",
    "passenger_refer_ok": "club_refer_ok",
    "passenger_refer_no": "club_refer_no",
    "passenger_prefs": "club_prefs",
    "rating_prompt": "rating_prompt",
    "rating_thanks": "rating_thanks",
    "error": "system_error",
}


def audio(key: str) -> str:
    return db.get_setting(f"audio_{key}") or DEFAULT_AUDIO.get(key, key)


# --------------------------------------------------------------- module JSON


def message(key: str, **extra: Any) -> dict:
    return {"type": "simpleMessage", "fileName": audio(key), **extra}


def menu(key: str, *, digits: int = 1, tries: int = 3, timeout: int = 5) -> dict:
    return {
        "type": "simpleMenu",
        "fileName": audio(key),
        "min_digits": 1,
        "max_digits": digits,
        "tries": tries,
        "timeout": timeout,
    }


def get_digits(*, min_digits: int = 1, max_digits: int = 10, timeout: int = 8) -> dict:
    return {
        "type": "getDTMF",
        "min_digits": min_digits,
        "max_digits": max_digits,
        "timeout": timeout,
    }


def route(phone: str) -> dict:
    return {"type": "simpleRouting", "dialPhone": phone}


def hangup() -> dict:
    return {"type": "hangup"}


# ------------------------------------------------------------ call plumbing


def call_params(request: Request) -> dict[str, str]:
    """The PBX names its parameters differently between modules and versions,
    so every field is read through its known aliases."""
    raw = {k.lower(): v for k, v in request.query_params.items()}

    def pick(*names: str) -> str:
        for name in names:
            value = raw.get(name.lower())
            if value:
                return str(value)
        return ""

    return {
        "call_id": pick("callId", "call_id", "uniqueid", "id"),
        "caller": pick("caller", "phone", "callerid", "did_caller", "from"),
        "extension": pick("extension", "ext", "did"),
        "dtmf": pick("dtmf", "digits", "input", "value"),
        "area": pick("area"),
        "tender": pick("tender"),
        "rating": pick("rating"),
    }


def _session_row(session: Session, call_id: str, caller: str) -> db.IvrSession:
    row = session.scalars(
        select(db.IvrSession).where(db.IvrSession.call_id == call_id)
    ).first()
    if row is None:
        row = db.IvrSession(call_id=call_id, phone=db.normalize_phone(caller), step="start")
        session.add(row)
        session.flush()
        return row
    # Some extensions report no call id, so the row is keyed by the caller and
    # outlives the call. A finished or stale row therefore means a new call,
    # not a caller stuck at the end of the previous one.
    if row.step == "done" or row.updated_at < datetime.utcnow() - STALE_CALL:
        row.step = "start"
        row.data = "{}"
    return row


def _state(row: db.IvrSession) -> dict:
    try:
        value = json.loads(row.data or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _save(row: db.IvrSession, step: str, state: dict) -> None:
    row.step = step
    row.data = json.dumps(state, ensure_ascii=False)
    row.updated_at = datetime.utcnow()


# ------------------------------------------------------------- driver line


@router.api_route("/driver", methods=["GET", "POST"])
async def driver_line(request: Request) -> JSONResponse:
    params = call_params(request)
    try:
        with db.session_scope() as session:
            body = _driver_step(session, params)
    except Exception:
        log.exception("driver IVR failed for %s", params)
        body = message("error")
    return JSONResponse(body)


def _driver_step(session: Session, params: dict[str, str]) -> dict:
    caller = db.normalize_phone(params["caller"])
    row = _session_row(session, params["call_id"] or caller, caller)
    state = _state(row)
    dtmf = params["dtmf"]
    driver = drivers.get_by_phone(session, caller)

    # A driver who was rung about a ride gets the offer immediately; the
    # callback is the answer to the flash call, not a visit to the menu.
    if row.step == "start":
        if driver is None:
            _save(row, "register_car_year", state)
            return menu("driver_register")
        if driver.status != "active":
            return message("driver_pending")
        tender = dispatch.latest_tender_for_driver(session, driver)
        if tender is not None:
            order = session.get(db.Order, tender.order_id)
            state.update({"tender": tender.id, "order": order.id if order else None})
            _save(row, "offer", state)
            return menu("driver_offer")
        _save(row, "menu", state)
        return menu("driver_menu")

    if row.step == "offer":
        tender = session.get(db.Tender, int(state.get("tender") or 0))
        if tender is None:
            _save(row, "menu", state)
            return menu("driver_menu")
        if dtmf != "1":
            _save(row, "menu", state)
            return menu("driver_menu")
        result = dispatch.place_bid(session, tender, driver)
        if not result.get("ok"):
            _save(row, "done", state)
            return message("driver_taken")
        # The hold message runs for the rest of the window; the PBX comes back
        # when it ends, which is when the auction is decided.
        _save(row, "await_result", state)
        return message("driver_wait")

    if row.step == "await_result":
        tender = session.get(db.Tender, int(state.get("tender") or 0))
        if tender is None:
            return message("driver_taken")
        outcome = dispatch.result_for_driver(session, tender, driver)
        _save(row, "done", state)
        if outcome.get("won") and outcome.get("passenger_phone"):
            return route(outcome["passenger_phone"])
        return message("driver_taken")

    if row.step == "menu":
        return _driver_menu_choice(session, row, state, driver, dtmf)

    if row.step == "register_car_year":
        if dtmf == "1":
            _save(row, "register_year_digits", state)
            return get_digits(min_digits=4, max_digits=4)
        _save(row, "done", state)
        return hangup()

    if row.step == "register_year_digits":
        state["car_year"] = dtmf
        _save(row, "register_seats", state)
        return get_digits(min_digits=1, max_digits=1)

    if row.step == "register_seats":
        state["seats"] = dtmf
        drivers.register(
            session,
            caller,
            car_year=int(state.get("car_year") or 0) or None,
            seats=int(state.get("seats") or 0) or None,
        )
        _save(row, "done", state)
        return message("driver_pending")

    if row.step == "area_choice":
        area = _area_by_index(session, dtmf)
        if area is not None and driver is not None:
            drivers.set_areas(session, driver, [area.name])
        _save(row, "done", state)
        return message("driver_saved")

    if row.step == "quiet_from":
        state["quiet_from"] = dtmf
        _save(row, "quiet_to", state)
        return get_digits(min_digits=2, max_digits=2)

    if row.step == "quiet_to":
        if driver is not None:
            driver.quiet_from = _hour(state.get("quiet_from"))
            driver.quiet_to = _hour(dtmf)
        _save(row, "done", state)
        return message("driver_saved")

    if row.step == "location_choice":
        area = _area_by_index(session, dtmf)
        if area is None or driver is None:
            _save(row, "done", state)
            return message("error")
        result = drivers.report_location(session, driver, area.name, source="declared")
        _save(row, "done", state)
        return message("driver_location_done" if result["ok"] else "error")

    _save(row, "menu", state)
    return menu("driver_menu")


def _driver_menu_choice(
    session: Session, row: db.IvrSession, state: dict, driver: db.Driver | None, dtmf: str
) -> dict:
    if driver is None:
        return message("error")
    if dtmf == "1":  # current offer
        tender = dispatch.open_tender_for_area(session, driver.last_area or driver.home_area)
        if tender is None:
            _save(row, "done", state)
            return message("driver_no_offer")
        state["tender"] = tender.id
        _save(row, "offer", state)
        return menu("driver_offer")
    if dtmf == "2":  # reputation
        _save(row, "done", state)
        return message("driver_reputation")
    if dtmf == "3":  # preferred areas
        _save(row, "area_choice", state)
        return menu("driver_area_prompt", digits=2)
    if dtmf == "4":  # quiet hours
        _save(row, "quiet_from", state)
        return get_digits(min_digits=2, max_digits=2)
    if dtmf == "5":  # location update
        _save(row, "location_choice", state)
        return menu("driver_location_prompt", digits=2)
    if dtmf == "6":  # ride finished
        order = session.scalars(
            select(db.Order)
            .where(db.Order.driver_id == driver.id, db.Order.status.in_(("assigned", "on_route")))
            .order_by(db.Order.created_at.desc())
            .limit(1)
        ).first()
        if order is None:
            _save(row, "done", state)
            return message("error")
        dispatch.finish_ride(session, order)
        _save(row, "done", state)
        return message("driver_finish_done")
    return menu("driver_menu")


def _hour(value: object) -> int | None:
    try:
        hour = int(str(value))
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= 23 else None


def _area_by_index(session: Session, dtmf: str) -> db.Area | None:
    """Menu digits map onto the active areas in display order, so adding an
    area never means re-recording the menu's numbering by hand."""
    try:
        index = int(dtmf)
    except (TypeError, ValueError):
        return None
    rows = session.scalars(
        select(db.Area).where(db.Area.active.is_(True)).order_by(db.Area.id)
    ).all()
    if 1 <= index <= len(rows):
        return rows[index - 1]
    return None


# ---------------------------------------------------------- passenger line


@router.api_route("/passenger", methods=["GET", "POST"])
async def passenger_line(request: Request) -> JSONResponse:
    params = call_params(request)
    try:
        with db.session_scope() as session:
            body = _passenger_step(session, params)
    except Exception:
        log.exception("passenger IVR failed for %s", params)
        body = message("error")
    return JSONResponse(body)


def _passenger_step(session: Session, params: dict[str, str]) -> dict:
    caller = db.normalize_phone(params["caller"])
    row = _session_row(session, params["call_id"] or caller, caller)
    state = _state(row)
    dtmf = params["dtmf"]

    if row.step == "start":
        # Ringing in is how an invited number confirms its referral, so this
        # happens before any menu and regardless of what the caller wanted.
        referrals.confirm_by_call(session, caller)
        _save(row, "menu", state)
        return menu("passenger_menu")

    if row.step == "menu":
        if dtmf == "1":
            _save(row, "done", state)
            return message("passenger_balance")
        if dtmf == "2":
            order = session.scalars(
                select(db.Order)
                .where(db.Order.phone == caller, db.Order.status.in_(("new", "assigned")))
                .order_by(db.Order.created_at.desc())
                .limit(1)
            ).first()
            if order is None or not loyalty.can_redeem(session, caller):
                _save(row, "done", state)
                return message("passenger_redeem_no")
            result = loyalty.redeem_ride(session, order, actor=f"ivr:{caller}")
            _save(row, "done", state)
            return message("passenger_redeem_ok" if result["redeemed"] else "passenger_redeem_no")
        if dtmf == "3":
            _save(row, "refer_number", state)
            return get_digits(min_digits=9, max_digits=10, timeout=12)
        if dtmf == "4":
            _save(row, "done", state)
            return message("passenger_prefs")
        return menu("passenger_menu")

    if row.step == "refer_number":
        result = referrals.assign(session, caller, dtmf, actor=f"ivr:{caller}")
        _save(row, "done", state)
        if not result.get("ok"):
            return message("passenger_refer_no")
        # A single ring on the invited phone leaves our number in their missed
        # calls, which is all the confirmation call needs.
        pbx.flash_call(session, dtmf, kind="referral")
        return message("passenger_refer_ok")

    _save(row, "menu", state)
    return menu("passenger_menu")


# -------------------------------------------------------------- rating line


@router.api_route("/rating", methods=["GET", "POST"])
async def rating_line(request: Request) -> JSONResponse:
    params = call_params(request)
    try:
        with db.session_scope() as session:
            body = _rating_step(session, params)
    except Exception:
        log.exception("rating IVR failed for %s", params)
        body = message("error")
    return JSONResponse(body)


def _rating_step(session: Session, params: dict[str, str]) -> dict:
    caller = db.normalize_phone(params["caller"])
    row = _session_row(session, params["call_id"] or caller, caller)
    state = _state(row)
    dtmf = params["dtmf"]

    request_id = params["rating"] or state.get("rating")
    if request_id:
        state["rating"] = str(request_id)

    if row.step == "start" or not dtmf:
        _save(row, "score", state)
        return menu("rating_prompt")

    rating = (
        session.get(db.RatingRequest, int(state["rating"]))
        if str(state.get("rating") or "").isdigit()
        else None
    )
    if rating is None:
        rating = session.scalars(
            select(db.RatingRequest)
            .where(
                db.RatingRequest.phone == caller,
                db.RatingRequest.status.in_((ratings.STATUS_CALLING, ratings.STATUS_SCHEDULED)),
            )
            .order_by(db.RatingRequest.due_at.desc())
            .limit(1)
        ).first()
    if rating is None:
        _save(row, "done", state)
        return message("rating_thanks")

    try:
        score = int(dtmf[:1])
    except ValueError:
        return menu("rating_prompt")
    ratings.record_score(session, rating, score)
    _save(row, "done", state)
    return message("rating_thanks")
