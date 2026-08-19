"""Club rules: points are earned by rides that happened, and only once."""

from __future__ import annotations

from app import db, loyalty


def _order(session, phone="0521234567", price=100.0, status="new") -> db.Order:
    order = db.Order(
        call_id="c1",
        phone=phone,
        origin="ירושלים",
        destination="בני ברק",
        price=price,
        status=status,
    )
    session.add(order)
    session.flush()
    return order


def test_open_order_earns_nothing(session):
    order = _order(session)
    assert loyalty.award_for_order(session, order)["awarded"] == 0
    assert loyalty.balance(session, order.phone) == 0


def test_done_order_earns_ride_points_and_the_welcome_gift(session):
    order = _order(session, status="done")
    result = loyalty.award_for_order(session, order)
    # 100₪ at one point per shekel, plus the 50 point joining gift.
    assert result["breakdown"] == {"ride": 100, "first_ride_gift": 50}
    assert loyalty.balance(session, order.phone) == 150


def test_award_is_idempotent(session):
    order = _order(session, status="done")
    loyalty.award_for_order(session, order)
    loyalty.award_for_order(session, order)
    assert loyalty.balance(session, order.phone) == 150


def test_gift_is_once_per_phone(session):
    first = _order(session, status="done")
    loyalty.award_for_order(session, first)
    second = _order(session, status="done")
    result = loyalty.award_for_order(session, second)
    assert "first_ride_gift" not in result["breakdown"]
    assert loyalty.balance(session, first.phone) == 250


def test_redeem_requires_enough_points(session):
    order = _order(session)
    assert loyalty.redeem_ride(session, order)["redeemed"] is False
    loyalty.grant(session, phone=order.phone, delta=500, reason="manual")
    result = loyalty.redeem_ride(session, order)
    assert result["redeemed"] is True
    assert loyalty.balance(session, order.phone) == 0
    assert order.points_spent == 500
    assert order.price == 0.0


def test_a_ride_paid_with_points_earns_none(session):
    order = _order(session)
    loyalty.grant(session, phone=order.phone, delta=500, reason="manual")
    loyalty.redeem_ride(session, order)
    order.status = "done"
    result = loyalty.award_for_order(session, order)
    assert result["breakdown"].get("ride") is None


def test_cancelling_a_completed_ride_takes_the_points_back(session):
    order = _order(session, status="done")
    loyalty.award_for_order(session, order)
    reversed_points = loyalty.reverse_for_order(session, order)
    assert reversed_points == 150
    assert loyalty.balance(session, order.phone) == 0


def test_manual_adjustment_cannot_go_negative(session):
    loyalty.grant(session, phone="0521234567", delta=30, reason="manual")
    assert loyalty.adjust(session, "0521234567", -100, actor="dispatcher") == 0
