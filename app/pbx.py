"""Client for the Technoline Interaction API — our server calling the PBX.

Three surfaces, three trust models, and they are not interchangeable:

* ``ivrFilesApi.php?action=makeCall`` places the flash call ("צינתוק"): it rings
  the recipient with a caller ID we choose and hangs up the moment they answer.
  No audio, no cost to the driver, and the number stays in their missed calls.
  Authenticated by IP whitelist only — no apiKey.
* ``campaignApi.php`` broadcasts a recorded offer to a list of numbers. apiKey
  *and* IP whitelist. This is the paid path, used for drivers who bought spoken
  offers.
* ``ivrFilesApi.php`` (everything else) manages extensions and audio files with
  an apiKey.

The documented rate limit on ``makeCall`` is one call per number per two
minutes, and the docs are explicit that hitting it returns an error rather than
queueing, so the debounce lives here, in our own ledger, before the request is
made.
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db

log = logging.getLogger("pbx")

BASE_URL = os.getenv("PBX_BASE_URL", "https://app.ipsales.co.il").rstrip("/")
API_KEY = os.getenv("PBX_API_KEY", "")


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


#: Dialling is opt-in. `makeCall` needs no apiKey — only a whitelisted IP — so
#: an unconfigured deployment would happily ring real drivers; the client
#: therefore logs what it would have sent unless the operator says otherwise.
#: `PBX_LIVE=1` enables flash calls on an IP-whitelisted host with no apiKey.
DRY_RUN = (
    True
    if _flag("PBX_DRY_RUN")
    else not (_flag("PBX_LIVE") or bool(os.getenv("PBX_API_KEY")))
)
TIMEOUT_S = float(os.getenv("PBX_TIMEOUT_S", "10"))
#: The PBX's own limit; we stay one second clear of it.
DEBOUNCE_SECONDS = int(os.getenv("PBX_FLASH_DEBOUNCE_S", "125"))


class PbxError(RuntimeError):
    pass


def _ok(payload: dict) -> bool:
    """Older endpoints answer `Ok`, newer ones `OK`; both mean success."""
    return str(payload.get("status", "")).strip().lower() == "ok"


def _request(action: str, params: dict, *, endpoint: str = "ivrFilesApi.php") -> dict:
    url = f"{BASE_URL}/{endpoint}"
    body = {"action": action, **{k: v for k, v in params.items() if v is not None}}
    # makeCall is authenticated by IP alone; sending a key there is at best
    # noise, and the endpoint is documented as rejecting on IP, not on key.
    if API_KEY and action != "makeCall":
        body.setdefault("apiKey", API_KEY)
    if DRY_RUN:
        redacted = {k: ("***" if k == "apiKey" else v) for k, v in body.items()}
        log.info("pbx dry-run %s %s", url, redacted)
        return {"status": "OK", "dry_run": True}
    try:
        response = httpx.post(url, data=body, timeout=TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PbxError(f"{action}: {exc}") from exc
    if not _ok(payload):
        raise PbxError(f"{action}: {payload.get('note') or payload}")
    return payload


def new_cid(seed: int | None = None) -> str:
    """The six digits the driver's phone displays. Derived from the tender id
    when there is one, so the missed call itself says which ride it was."""
    if seed is not None:
        return f"{seed % 1_000_000:06d}"
    return f"{random.randint(0, 999_999):06d}"


def recently_called(session: Session, phone: str, seconds: int = DEBOUNCE_SECONDS) -> bool:
    cutoff = datetime.utcnow() - timedelta(seconds=seconds)
    return bool(
        session.scalars(
            select(db.FlashCall.id).where(
                db.FlashCall.phone == db.normalize_phone(phone),
                db.FlashCall.created_at >= cutoff,
                db.FlashCall.status.in_(("sent", "dry_run")),
            )
        ).first()
    )


def flash_call(
    session: Session,
    phone: str,
    *,
    cid: str | None = None,
    driver_id: int | None = None,
    tender_id: int | None = None,
    kind: str = "tender",
) -> dict:
    """One ring on the recipient's phone showing `cid`, then silence.

    Returns the ledger row's outcome rather than raising, because a blast of
    fifty drivers must not stop at the first number that is in cooldown.
    """
    phone = db.normalize_phone(phone)
    cid = cid or new_cid(tender_id)
    if recently_called(session, phone):
        session.add(
            db.FlashCall(
                phone=phone,
                driver_id=driver_id,
                tender_id=tender_id,
                cid=cid,
                kind=kind,
                status="debounced",
                note="נקרא לאחרונה לפני פחות משתי דקות",
            )
        )
        return {"sent": False, "status": "debounced", "phone": phone}

    status, note = "sent", None
    try:
        payload = _request("makeCall", {"phone": phone, "cid": cid})
        if payload.get("dry_run"):
            status = "dry_run"
    except PbxError as exc:
        status, note = "failed", str(exc)
        log.warning("flash call to %s failed: %s", phone, exc)

    session.add(
        db.FlashCall(
            phone=phone,
            driver_id=driver_id,
            tender_id=tender_id,
            cid=cid,
            kind=kind,
            status=status,
            note=note,
        )
    )
    return {"sent": status in {"sent", "dry_run"}, "status": status, "cid": cid, "phone": phone}


def voice_broadcast(
    phones: list[str], *, name: str, file_name: str | None = None, module_url: str | None = None
) -> dict:
    """The paid alternative to a flash call: the driver's phone is answered by
    a recorded offer and a keypress hands the call to our Module API tree.

    The documentation names the action but not its parameters, so these field
    names are provisional; a failure here downgrades the driver to a flash
    call rather than dropping them from the tender.
    """
    if not phones:
        return {"started": False, "note": "אין נמענים"}
    params: dict[str, object] = {
        "campaignName": name,
        "phones": ",".join(db.normalize_phone(p) for p in phones),
    }
    if file_name:
        params["fileName"] = file_name
    if module_url:
        params["moduleUrl"] = module_url
    payload = _request("campaignRun", params, endpoint="campaignApi.php")
    return {"started": True, "response": payload}


def campaign_report(campaign_id: str) -> dict:
    return _request(
        "campaignReport", {"campaignId": campaign_id}, endpoint="campaignApi.php"
    )


def stop_campaign(campaign_id: str) -> dict:
    return _request("campaignStop", {"campaignId": campaign_id}, endpoint="campaignApi.php")
