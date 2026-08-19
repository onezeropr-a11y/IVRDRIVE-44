"""'Share and ride': who may be referred, and for how long it pays."""

from __future__ import annotations

from datetime import datetime, timedelta

from app import db, loyalty, referrals

REFERRER = "0521111111"
INVITED = "0522222222"


def test_assign_rejects_a_number_already_known(session):
    session.add(db.Customer(phone=INVITED))
    session.flush()
    assert referrals.assign(session, REFERRER, INVITED)["ok"] is False


def test_assign_rejects_self_and_garbage(session):
    assert referrals.assign(session, REFERRER, REFERRER)["ok"] is False
    assert referrals.assign(session, REFERRER, "123")["ok"] is False


def test_a_number_can_only_be_referred_once(session):
    assert referrals.assign(session, REFERRER, INVITED)["ok"] is True
    assert referrals.assign(session, "0523333333", INVITED)["ok"] is False


def test_the_invited_number_confirms_by_calling(session):
    referrals.assign(session, REFERRER, INVITED)
    row = referrals.confirm_by_call(session, INVITED)
    assert row is not None
    assert row.status == referrals.STATUS_CONFIRMED
    assert row.credit_until > datetime.utcnow() + timedelta(days=29)


def test_a_late_call_does_not_confirm(session):
    referrals.assign(session, REFERRER, INVITED)
    row = session.scalars(db.select(db.Referral)).first()
    row.expires_at = datetime.utcnow() - timedelta(minutes=1)
    session.flush()
    assert referrals.confirm_by_call(session, INVITED) is None
    assert row.status == referrals.STATUS_EXPIRED


def test_the_referrer_earns_on_rides_inside_the_window(session):
    referrals.assign(session, REFERRER, INVITED)
    referrals.confirm_by_call(session, INVITED)
    order = db.Order(
        call_id="c1",
        phone=INVITED,
        origin="a",
        destination="b",
        price=50.0,
        status="done",
    )
    session.add(order)
    session.flush()

    loyalty.award_for_order(session, order)
    assert loyalty.balance(session, REFERRER) == 30
    # And never twice for the same ride.
    loyalty.award_for_order(session, order)
    assert loyalty.balance(session, REFERRER) == 30


def test_no_credit_after_the_window_closes(session):
    referrals.assign(session, REFERRER, INVITED)
    row = referrals.confirm_by_call(session, INVITED)
    row.credit_until = datetime.utcnow() - timedelta(days=1)
    order = db.Order(
        call_id="c1",
        phone=INVITED,
        origin="a",
        destination="b",
        price=50.0,
        status="done",
    )
    session.add(order)
    session.flush()
    loyalty.award_for_order(session, order)
    assert loyalty.balance(session, REFERRER) == 0


def test_expire_stale_closes_unconfirmed_windows(session):
    referrals.assign(session, REFERRER, INVITED)
    row = session.scalars(db.select(db.Referral)).first()
    row.expires_at = datetime.utcnow() - timedelta(hours=1)
    session.flush()
    assert referrals.expire_stale(session) == 1
