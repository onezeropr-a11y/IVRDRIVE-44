"""Driver records, the reputation score, and who is eligible for an offer.

The score is deliberately split in two, because the two halves answer different
questions and are used at different moments:

* the **general** score is who this driver is — passenger ratings, car, years
  with us, age, volume. It is stable, it is what the driver hears when they ask
  for their reputation, and it decides the tier;
* the **situational** score is where they are right now — how recently they
  reported being in the area, and whether that report was a finished ride
  (trustworthy, free) or a self declaration (cheap talk, once a day).

The blast uses both to decide who gets rung, and the auction uses both again on
whoever answered. Weights live in the settings table so the office can retune
priorities without a deploy.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db

log = logging.getLogger("drivers")

#: Tier thresholds on the general score (0-100). The names are what the driver
#: hears in the IVR, so they are fixed rather than configurable.
TIERS: tuple[tuple[str, str, float], ...] = (
    ("premium", "פרימיום", 85.0),
    ("pro_plus", "פרו פלוס", 70.0),
    ("pro", "פרו", 55.0),
    ("standard", "סטנדרט", 0.0),
)

#: A location report decays to nothing over this many hours; a finished ride
#: counts for more than a self declaration because the system saw it happen.
SOURCE_WEIGHT = {"ride_finished": 1.0, "declared": 0.7}


def get_by_phone(session: Session, phone: str) -> db.Driver | None:
    return session.scalars(
        select(db.Driver).where(db.Driver.phone == db.normalize_phone(phone))
    ).first()


def register(
    session: Session,
    phone: str,
    *,
    name: str | None = None,
    home_area: str | None = None,
    car_model: str | None = None,
    car_year: int | None = None,
    seats: int | None = None,
    birth_year: int | None = None,
    smartphone: bool | None = None,
    voice_offers: bool | None = None,
    quiet_from: int | None = None,
    quiet_to: int | None = None,
    status: str | None = None,
    notes: str | None = None,
) -> db.Driver:
    """Self-registration from the phone menu, or dispatcher entry. A new driver
    starts `pending` — they get no offers until the office approves them.

    Only fields actually supplied are touched, so the phone menu changing one
    setting cannot blank out everything the office typed in."""
    phone = db.normalize_phone(phone)
    driver = get_by_phone(session, phone)
    if driver is None:
        driver = db.Driver(phone=phone, status="pending")
        session.add(driver)
    if name is not None:
        driver.name = name
    if home_area is not None:
        driver.home_area = home_area
    if car_model is not None:
        driver.car_model = car_model
    if car_year is not None:
        driver.car_year = car_year
    if seats is not None:
        driver.seats = seats
    if birth_year is not None:
        driver.birth_year = birth_year
    if smartphone is not None:
        driver.smartphone = smartphone
    if voice_offers is not None:
        driver.voice_offers = voice_offers
    if quiet_from is not None:
        driver.quiet_from = quiet_from
    if quiet_to is not None:
        driver.quiet_to = quiet_to
    if status is not None:
        driver.status = status
    if notes is not None:
        driver.notes = notes
    session.flush()
    db.log_action(
        session, "driver_saved", entity="driver", entity_id=driver.id, detail=phone
    )
    return driver


def set_areas(session: Session, driver: db.Driver, areas: list[str]) -> None:
    session.query(db.DriverArea).filter(db.DriverArea.driver_id == driver.id).delete()
    for area in areas:
        clean = (area or "").strip()
        if clean:
            session.add(db.DriverArea(driver_id=driver.id, area=clean))


def areas_of(session: Session, driver: db.Driver) -> list[str]:
    return list(
        session.scalars(
            select(db.DriverArea.area).where(db.DriverArea.driver_id == driver.id)
        ).all()
    )


def average_rating(driver: db.Driver) -> float | None:
    if not driver.rating_count:
        return None
    return round(driver.rating_sum / driver.rating_count, 2)


def record_rating(session: Session, driver: db.Driver, score: int) -> None:
    driver.rating_sum += float(score)
    driver.rating_count += 1
    db.log_action(
        session, "driver_rated", entity="driver", entity_id=driver.id, detail=str(score)
    )


def general_score(driver: db.Driver, *, now: datetime | None = None) -> float:
    """0-100. An unrated driver sits at the middle of the rating band rather
    than at zero, so a new driver is not frozen out before their first ride."""
    now = now or datetime.utcnow()

    rating = average_rating(driver)
    rating_part = ((rating if rating is not None else 4.0) / 5.0) * 100.0

    if driver.car_year:
        age_of_car = max(0, now.year - int(driver.car_year))
        car_part = max(0.0, 100.0 - age_of_car * 7.0)
    else:
        car_part = 50.0

    years = max(0.0, (now - (driver.joined_at or now)).days / 365.25)
    seniority_part = min(100.0, years * 25.0)

    if driver.birth_year:
        age = max(18, now.year - int(driver.birth_year))
        # Experience without the very top of the age range: peaks around 45.
        age_part = max(0.0, 100.0 - abs(age - 45) * 2.5)
    else:
        age_part = 50.0

    volume_part = min(100.0, (driver.rides_done or 0) * 2.0)

    weights = {
        "rating": db.setting_float("score_weight_rating") or 0.40,
        "car": db.setting_float("score_weight_car") or 0.15,
        "seniority": db.setting_float("score_weight_seniority") or 0.15,
        "age": db.setting_float("score_weight_age") or 0.10,
        "volume": db.setting_float("score_weight_volume") or 0.20,
    }
    total = sum(weights.values()) or 1.0
    score = (
        rating_part * weights["rating"]
        + car_part * weights["car"]
        + seniority_part * weights["seniority"]
        + age_part * weights["age"]
        + volume_part * weights["volume"]
    ) / total
    return round(score, 2)


def situational_score(
    session: Session, driver: db.Driver, area: str | None, *, now: datetime | None = None
) -> float:
    """0-100 for 'is this driver near the ride, and do we believe it'."""
    now = now or datetime.utcnow()
    fresh_hours = db.setting_int("location_fresh_hours") or 10
    cutoff = now - timedelta(hours=fresh_hours)
    update = session.scalars(
        select(db.LocationUpdate)
        .where(db.LocationUpdate.driver_id == driver.id, db.LocationUpdate.created_at >= cutoff)
        .order_by(db.LocationUpdate.created_at.desc())
        .limit(1)
    ).first()
    if update is None:
        return 0.0
    if area and update.area.strip() != area.strip():
        return 0.0
    hours_old = max(0.0, (now - update.created_at).total_seconds() / 3600.0)
    freshness = max(0.0, 1.0 - hours_old / fresh_hours)
    return round(100.0 * freshness * SOURCE_WEIGHT.get(update.source, 0.7), 2)


def total_score(
    session: Session, driver: db.Driver, area: str | None, *, now: datetime | None = None
) -> float:
    general = general_score(driver, now=now)
    situational = situational_score(session, driver, area, now=now)
    share = db.setting_float("score_situational_share") or 0.40
    return round(general * (1 - share) + situational * share, 2)


def tier_of(driver: db.Driver) -> tuple[str, str]:
    score = general_score(driver)
    for key, label, threshold in TIERS:
        if score >= threshold:
            return key, label
    return "standard", "סטנדרט"


def in_quiet_hours(driver: db.Driver, *, now: datetime | None = None) -> bool:
    """Quiet hours may wrap midnight (22 → 6), which the naive comparison
    would get backwards."""
    if driver.quiet_from is None or driver.quiet_to is None:
        return False
    hour = (now or datetime.utcnow()).hour
    start, end = int(driver.quiet_from), int(driver.quiet_to)
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def matches_filters(driver: db.Driver, filters: dict, *, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if (min_year := filters.get("min_car_year")) and (
        not driver.car_year or int(driver.car_year) < int(min_year)
    ):
        return False
    if (min_seats := filters.get("min_seats")) and int(driver.seats or 0) < int(min_seats):
        return False
    if (want := filters.get("smartphone")) is not None and bool(driver.smartphone) != bool(want):
        return False
    if (want := filters.get("voice_offers")) is not None and bool(
        driver.voice_offers
    ) != bool(want):
        return False
    if (min_age := filters.get("min_age")) and (
        not driver.birth_year or (now.year - int(driver.birth_year)) < int(min_age)
    ):
        return False
    if (tiers := filters.get("tiers")) and tier_of(driver)[0] not in tiers:
        return False
    if (min_rating := filters.get("min_rating")) is not None:
        rating = average_rating(driver)
        if rating is None or rating < float(min_rating):
            return False
    return True


def candidates(
    session: Session,
    area: str | None,
    filters: dict | None = None,
    *,
    now: datetime | None = None,
    ignore_quiet_hours: bool = False,
) -> list[tuple[db.Driver, float]]:
    """Everyone who may be rung for this ride, best first.

    Area matching is by preference list, with an empty list meaning "anywhere":
    a driver who never set preferences should still get work.
    """
    now = now or datetime.utcnow()
    filters = filters or {}
    rows = session.scalars(select(db.Driver).where(db.Driver.status == "active")).all()
    prefs: dict[int, list[str]] = {}
    for driver_id, pref_area in session.execute(
        select(db.DriverArea.driver_id, db.DriverArea.area)
    ).all():
        prefs.setdefault(driver_id, []).append(pref_area)

    picked: list[tuple[db.Driver, float]] = []
    for driver in rows:
        if not ignore_quiet_hours and in_quiet_hours(driver, now=now):
            continue
        wanted = prefs.get(driver.id, [])
        if area and wanted and area.strip() not in {a.strip() for a in wanted}:
            continue
        if not matches_filters(driver, filters, now=now):
            continue
        picked.append((driver, total_score(session, driver, area, now=now)))
    picked.sort(key=lambda pair: pair[1], reverse=True)
    return picked


def report_location(
    session: Session, driver: db.Driver, area: str, *, source: str = "declared"
) -> dict:
    """A finished ride may update the location at any time; the self report is
    once a day, which is the whole point of it being cheap to trust."""
    now = datetime.utcnow()
    if source == "declared":
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        existing = session.scalars(
            select(db.LocationUpdate).where(
                db.LocationUpdate.driver_id == driver.id,
                db.LocationUpdate.source == "declared",
                db.LocationUpdate.created_at >= today,
            )
        ).first()
        if existing is not None:
            return {"ok": False, "error": "עדכון מיקום יומי כבר בוצע היום"}
    session.add(db.LocationUpdate(driver_id=driver.id, area=area, source=source))
    driver.last_area = area
    driver.last_area_at = now
    return {"ok": True, "area": area, "source": source}


def area_board(session: Session, *, now: datetime | None = None) -> list[dict]:
    """What the dispatcher's map shows: who is where, and how stale it is."""
    now = now or datetime.utcnow()
    fresh_hours = db.setting_int("location_fresh_hours") or 10
    cutoff = now - timedelta(hours=fresh_hours)
    rows = session.scalars(
        select(db.Driver).where(db.Driver.status == "active", db.Driver.last_area_at >= cutoff)
    ).all()
    board: dict[str, list[dict]] = {}
    for driver in rows:
        entry = {
            "id": driver.id,
            "name": driver.name,
            "phone": driver.phone,
            "tier": tier_of(driver)[1],
            "minutes_ago": int((now - driver.last_area_at).total_seconds() // 60),
            "score": general_score(driver, now=now),
        }
        board.setdefault(driver.last_area or "לא ידוע", []).append(entry)
    return [
        {"area": area, "drivers": sorted(items, key=lambda d: -d["score"])}
        for area, items in sorted(board.items())
    ]


def to_json(session: Session, driver: db.Driver) -> dict:
    key, label = tier_of(driver)
    return {
        "id": driver.id,
        "phone": driver.phone,
        "name": driver.name,
        "home_area": driver.home_area,
        "areas": areas_of(session, driver),
        "car_model": driver.car_model,
        "car_year": driver.car_year,
        "seats": driver.seats,
        "birth_year": driver.birth_year,
        "smartphone": driver.smartphone,
        "voice_offers": driver.voice_offers,
        "quiet_from": driver.quiet_from,
        "quiet_to": driver.quiet_to,
        "status": driver.status,
        "rating": average_rating(driver),
        "rating_count": driver.rating_count,
        "rides_done": driver.rides_done,
        "score": general_score(driver),
        "tier": key,
        "tier_label": label,
        "last_area": driver.last_area,
        "last_area_at": driver.last_area_at.isoformat() if driver.last_area_at else None,
        "notes": driver.notes,
    }


def parse_filters(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
