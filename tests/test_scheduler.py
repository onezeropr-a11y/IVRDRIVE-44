"""The background tick closes what the phone path would otherwise leave open."""

from __future__ import annotations

from datetime import datetime, timedelta

from app import db, dispatch, drivers, ratings, referrals, scheduler


def test_one_tick_closes_tenders_dials_ratings_and_expires_referrals(session):
    driver = drivers.register(session, "0521111111", status="active")
    order = db.Order(
        call_id="c1", phone="0529999999", origin="ירושלים", destination="בני ברק", price=60.0
    )
    session.add(order)
    session.flush()
    dispatch.open_tender(session, order, area="ירושלים")
    tender = session.scalars(db.select(db.Tender)).first()
    dispatch.place_bid(session, tender, driver)
    tender.closes_at = datetime.utcnow() - timedelta(seconds=1)

    ratings.schedule_for_order(session, order)
    request = session.scalars(db.select(db.RatingRequest)).first()
    request.due_at = datetime.utcnow() - timedelta(minutes=1)

    referrals.assign(session, "0523333333", "0524444444")
    referral = session.scalars(db.select(db.Referral)).first()
    referral.expires_at = datetime.utcnow() - timedelta(hours=1)
    session.commit()

    result = scheduler.tick()

    assert result == {"tenders_closed": 1, "ratings_called": 1, "referrals_expired": 1}
    session.expire_all()
    assert tender.status == dispatch.STATUS_AWARDED
    assert tender.awarded_driver_id == driver.id
    assert request.status == ratings.STATUS_CALLING
    assert referral.status == referrals.STATUS_EXPIRED
