"""Blasting an offer, bidding on it, and who wins when the window closes."""

from __future__ import annotations

from datetime import datetime, timedelta

from app import db, dispatch, drivers, loyalty, pbx, ratings


def make_driver(session, phone, **fields) -> db.Driver:
    driver = drivers.register(session, phone, status="active", **fields)
    return driver


def make_order(session, price=100.0) -> db.Order:
    order = db.Order(
        call_id="c1",
        phone="0529999999",
        origin="ירושלים",
        destination="בית שמש",
        price=price,
        area="ירושלים",
    )
    session.add(order)
    session.flush()
    return order


def test_blast_rings_every_eligible_driver_once(session):
    make_driver(session, "0521111111", car_year=2022)
    make_driver(session, "0522222222", car_year=2015)
    order = make_order(session)

    result = dispatch.open_tender(session, order, area="ירושלים")

    assert result["ok"] is True
    assert result["eligible"] == 2
    assert result["flash"] == 2
    calls = session.scalars(db.select(db.FlashCall)).all()
    assert {c.status for c in calls} == {"dry_run"}


def test_a_paid_driver_gets_a_campaign_and_not_a_flash_call(session, monkeypatch):
    make_driver(session, "0521111111", voice_offers=True)
    make_driver(session, "0522222222")
    seen: list[list[str]] = []
    monkeypatch.setattr(
        pbx, "voice_broadcast", lambda phones, **kw: seen.append(phones) or {"started": True}
    )

    result = dispatch.open_tender(session, make_order(session), area="ירושלים")

    assert (result["voice"], result["flash"]) == (1, 1)
    assert seen == [["0521111111"]]
    assert [c.phone for c in session.scalars(db.select(db.FlashCall)).all()] == ["0522222222"]


def test_a_campaign_that_will_not_start_falls_back_to_a_flash_call(session, monkeypatch):
    make_driver(session, "0521111111", voice_offers=True)

    def refuse(phones, **kwargs):
        raise pbx.PbxError("access denied")

    monkeypatch.setattr(pbx, "voice_broadcast", refuse)

    result = dispatch.open_tender(session, make_order(session), area="ירושלים")

    assert (result["voice"], result["flash"]) == (0, 1)
    assert [c.phone for c in session.scalars(db.select(db.FlashCall)).all()] == ["0521111111"]


def test_a_second_ring_inside_the_debounce_is_not_sent(session):
    driver = make_driver(session, "0521111111")
    first = pbx.flash_call(session, driver.phone, driver_id=driver.id)
    second = pbx.flash_call(session, driver.phone, driver_id=driver.id)
    assert first["sent"] is True
    assert second["status"] == "debounced"


def test_quiet_hours_and_filters_exclude_drivers(session):
    now = datetime(2026, 1, 1, 23, 0)
    make_driver(session, "0521111111", quiet_from=22, quiet_to=6)
    make_driver(session, "0522222222", car_year=2012)
    make_driver(session, "0523333333", car_year=2024)

    ranked = drivers.candidates(session, None, {"min_car_year": 2020}, now=now)

    assert [d.phone for d, _ in ranked] == ["0523333333"]


def test_the_first_bidder_is_not_told_the_ride_is_theirs(session):
    driver = make_driver(session, "0521111111")
    order = make_order(session)
    dispatch.open_tender(session, order, area="ירושלים")
    tender = session.scalars(db.select(db.Tender)).first()

    result = dispatch.place_bid(session, tender, driver)

    assert result["ok"] is True
    assert result["wait_seconds"] > 0
    assert tender.status == "open"
    assert order.driver_id is None


def test_the_window_picks_the_better_driver_not_the_faster_one(session):
    weak = make_driver(session, "0521111111", car_year=2008, birth_year=1930)
    strong = make_driver(session, "0522222222", car_year=2025, birth_year=1980)
    strong.rating_sum, strong.rating_count, strong.rides_done = 25.0, 5, 40
    order = make_order(session)
    dispatch.open_tender(session, order, area="ירושלים")
    tender = session.scalars(db.select(db.Tender)).first()

    dispatch.place_bid(session, tender, weak)
    dispatch.place_bid(session, tender, strong)
    result = dispatch.close_tender(session, tender)

    assert result["driver_id"] == strong.id
    assert order.driver_id == strong.id
    assert order.status == "assigned"
    assert order.driver_phone == strong.phone


def test_bidding_after_the_window_closes_the_tender(session):
    driver = make_driver(session, "0521111111")
    order = make_order(session)
    dispatch.open_tender(session, order, area="ירושלים")
    tender = session.scalars(db.select(db.Tender)).first()
    tender.closes_at = datetime.utcnow() - timedelta(seconds=1)
    session.flush()

    result = dispatch.place_bid(session, tender, driver)

    assert result["ok"] is False
    assert tender.status == "failed"


def test_no_bids_fails_the_tender_rather_than_hanging(session):
    make_driver(session, "0521111111")
    order = make_order(session)
    dispatch.open_tender(session, order, area="ירושלים")
    tender = session.scalars(db.select(db.Tender)).first()
    tender.closes_at = datetime.utcnow() - timedelta(seconds=1)
    session.flush()

    assert dispatch.reap(session) == 1
    assert tender.status == "failed"


def test_finishing_a_ride_pays_points_refreshes_location_and_queues_the_rating(session):
    driver = make_driver(session, "0521111111")
    order = make_order(session)
    order.driver_id = driver.id
    session.flush()

    result = dispatch.finish_ride(session, order, area="בית שמש")

    assert order.status == "done"
    assert order.commission == 15.0
    assert driver.rides_done == 1
    assert driver.last_area == "בית שמש"
    assert loyalty.balance(session, order.phone) == 150
    assert result["rating"]["scheduled"] is True
    # Finished by the driver, so the call is due now rather than in 90 minutes.
    request = session.scalars(db.select(db.RatingRequest)).first()
    assert request.due_at <= datetime.utcnow()
    assert request.status == ratings.STATUS_SCHEDULED


def test_the_daily_location_report_is_once_a_day(session):
    driver = make_driver(session, "0521111111")
    assert drivers.report_location(session, driver, "ירושלים")["ok"] is True
    assert drivers.report_location(session, driver, "ירושלים")["ok"] is False
    # A finished ride is always allowed to refresh it.
    assert drivers.report_location(
        session, driver, "בני ברק", source="ride_finished"
    )["ok"] is True


def test_tiers_follow_the_general_score(session):
    rookie = make_driver(session, "0521111111", car_year=2005, birth_year=1930)
    star = make_driver(session, "0522222222", car_year=2026, birth_year=1980)
    star.rating_sum, star.rating_count, star.rides_done = 50.0, 10, 60
    assert drivers.tier_of(rookie)[0] == "standard"
    assert drivers.tier_of(star)[0] in {"pro_plus", "premium"}
