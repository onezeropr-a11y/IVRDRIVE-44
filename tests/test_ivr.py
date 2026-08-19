"""The Module API contract.

The PBX drops back to the previous menu on any JSON it cannot parse, so the
one thing every branch — including the failure branches — must guarantee is a
module of a documented type.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, dispatch, drivers, ivr, loyalty, ratings, referrals
from app.main import app

#: The subset of the documented module list this service emits.
KNOWN_TYPES = {"simpleMessage", "simpleMenu", "getDTMF", "simpleRouting", "hangup"}

DRIVER = "0521111111"
PASSENGER = "0529999999"


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as active:
        yield active


def call(client: TestClient, path: str, **params) -> dict:
    response = client.get(path, params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["type"] in KNOWN_TYPES, body
    return body


def test_an_unknown_driver_is_offered_registration(client):
    body = call(client, "/ivr/driver", callId="c1", caller=DRIVER)
    assert body["fileName"] == ivr.DEFAULT_AUDIO["driver_register"]


def test_registration_collects_the_car_year_and_seats(client):
    call(client, "/ivr/driver", callId="c1", caller=DRIVER)
    year = call(client, "/ivr/driver", callId="c1", caller=DRIVER, dtmf="1")
    assert year["type"] == "getDTMF"
    call(client, "/ivr/driver", callId="c1", caller=DRIVER, dtmf="2021")
    done = call(client, "/ivr/driver", callId="c1", caller=DRIVER, dtmf="4")
    assert done["fileName"] == ivr.DEFAULT_AUDIO["driver_pending"]

    with db.session_scope() as session:
        driver = drivers.get_by_phone(session, DRIVER)
        assert (driver.car_year, driver.seats, driver.status) == (2021, 4, "pending")


def test_a_second_call_on_a_reused_id_starts_from_the_top(client):
    """Extensions that report no call id key the row by the caller, so the row
    survives the call and must not strand them mid-menu."""
    call(client, "/ivr/driver", callId="", caller=DRIVER)
    call(client, "/ivr/driver", callId="", caller=DRIVER, dtmf="9")

    again = call(client, "/ivr/driver", callId="", caller=DRIVER)

    assert again["fileName"] == ivr.DEFAULT_AUDIO["driver_register"]


def test_registration_never_demotes_a_driver_the_office_approved(client):
    with db.session_scope() as session:
        drivers.register(session, DRIVER, status="active")

    with db.session_scope() as session:
        drivers.register(session, DRIVER, car_year=2021)
        assert drivers.get_by_phone(session, DRIVER).status == "active"


def test_a_pending_driver_hears_that_and_nothing_else(client):
    with db.session_scope() as session:
        drivers.register(session, DRIVER)
    body = call(client, "/ivr/driver", callId="c2", caller=DRIVER)
    assert body["fileName"] == ivr.DEFAULT_AUDIO["driver_pending"]


def test_the_ride_offer_holds_the_driver_until_the_window_closes(client):
    with db.session_scope() as session:
        drivers.register(session, DRIVER, status="active")
        order = db.Order(
            call_id="x", phone=PASSENGER, origin="ירושלים", destination="בני ברק", price=80.0
        )
        session.add(order)
        session.flush()
        dispatch.open_tender(session, order, area="ירושלים")

    offer = call(client, "/ivr/driver", callId="c3", caller=DRIVER)
    assert offer["fileName"] == ivr.DEFAULT_AUDIO["driver_offer"]

    waiting = call(client, "/ivr/driver", callId="c3", caller=DRIVER, dtmf="1")
    assert waiting["fileName"] == ivr.DEFAULT_AUDIO["driver_wait"]

    with db.session_scope() as session:
        tender = session.scalars(db.select(db.Tender)).first()
        tender.closes_at = tender.opened_at

    connected = call(client, "/ivr/driver", callId="c3", caller=DRIVER)
    assert connected["type"] == "simpleRouting"
    assert connected["dialPhone"] == PASSENGER


def test_ringing_in_confirms_a_pending_referral(client):
    with db.session_scope() as session:
        referrals.assign(session, "0523333333", PASSENGER)

    call(client, "/ivr/passenger", callId="c4", caller=PASSENGER)

    with db.session_scope() as session:
        row = session.scalars(db.select(db.Referral)).first()
        assert row.status == referrals.STATUS_CONFIRMED


def test_redeeming_without_points_says_so(client):
    with db.session_scope() as session:
        session.add(
            db.Order(call_id="x", phone=PASSENGER, origin="a", destination="b", price=90.0)
        )
    call(client, "/ivr/passenger", callId="c5", caller=PASSENGER)
    body = call(client, "/ivr/passenger", callId="c5", caller=PASSENGER, dtmf="2")
    assert body["fileName"] == ivr.DEFAULT_AUDIO["passenger_redeem_no"]


def test_redeeming_with_points_zeroes_the_fare(client):
    with db.session_scope() as session:
        session.add(
            db.Order(call_id="x", phone=PASSENGER, origin="a", destination="b", price=90.0)
        )
        loyalty.grant(session, phone=PASSENGER, delta=500, reason="manual")
    call(client, "/ivr/passenger", callId="c6", caller=PASSENGER)
    body = call(client, "/ivr/passenger", callId="c6", caller=PASSENGER, dtmf="2")
    assert body["fileName"] == ivr.DEFAULT_AUDIO["passenger_redeem_ok"]
    with db.session_scope() as session:
        order = session.scalars(db.select(db.Order)).first()
        assert (order.price, order.points_spent) == (0.0, 500)


def test_the_rating_call_records_one_score(client):
    with db.session_scope() as session:
        driver = drivers.register(session, DRIVER, status="active")
        order = db.Order(
            call_id="x",
            phone=PASSENGER,
            origin="a",
            destination="b",
            price=50.0,
            status="done",
            driver_id=driver.id,
        )
        session.add(order)
        session.flush()
        ratings.schedule_for_order(session, order)
        rating_id = session.scalars(db.select(db.RatingRequest)).first().id

    call(client, "/ivr/rating", callId="c7", caller=PASSENGER, rating=rating_id)
    body = call(client, "/ivr/rating", callId="c7", caller=PASSENGER, rating=rating_id, dtmf="5")
    assert body["fileName"] == ivr.DEFAULT_AUDIO["rating_thanks"]

    with db.session_scope() as session:
        request = session.get(db.RatingRequest, rating_id)
        assert (request.score, request.status) == (5, ratings.STATUS_DONE)
        assert drivers.get_by_phone(session, DRIVER).rating_count == 1


def test_a_broken_step_still_returns_a_module(client):
    # No caller at all: the PBX must still get valid JSON rather than a 500.
    body = call(client, "/ivr/driver", callId="c8")
    assert body["type"] in KNOWN_TYPES
