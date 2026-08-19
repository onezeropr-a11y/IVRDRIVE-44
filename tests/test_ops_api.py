"""The back-office API, exercised the way the console uses it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, drivers, loyalty
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as active:
        yield active


def make_order(price: float = 200.0, phone: str = "0529999999") -> int:
    with db.session_scope() as session:
        order = db.Order(
            call_id="c1",
            phone=phone,
            origin="ירושלים",
            destination="בני ברק",
            price=price,
            area="ירושלים",
        )
        session.add(order)
        session.flush()
        return order.id


def test_driver_crud_and_removal_keeps_the_record(client):
    created = client.post(
        "/api/drivers",
        json={"phone": "0521111111", "name": "דוד", "car_year": 2022, "areas": ["ירושלים"]},
        headers={"X-Actor": "sara"},
    ).json()
    assert created["areas"] == ["ירושלים"]

    updated = client.patch(
        f"/api/drivers/{created['id']}", json={"status": "active", "seats": 7}
    ).json()
    assert (updated["status"], updated["seats"], updated["name"]) == ("active", 7, "דוד")

    removed = client.delete(f"/api/drivers/{created['id']}").json()
    assert removed["status"] == "removed"
    assert client.get("/api/drivers").json()["drivers"][0]["status"] == "removed"


def test_the_dispatcher_can_open_a_filtered_tender(client):
    with db.session_scope() as session:
        drivers.register(session, "0521111111", status="active", car_year=2012)
        drivers.register(session, "0522222222", status="active", car_year=2024)
    order_id = make_order()

    result = client.post(
        f"/api/orders/{order_id}/tender",
        json={"area": "ירושלים", "filters": {"min_car_year": 2020}, "window_seconds": 30},
    ).json()

    assert (result["eligible"], result["flash"]) == (1, 1)
    listing = client.get("/api/tenders").json()["tenders"][0]
    assert listing["filters"] == {"min_car_year": 2020}
    assert listing["notified"] == 1


def test_an_order_taken_at_the_desk_can_ring_the_drivers_at_once(client):
    with db.session_scope() as session:
        drivers.register(session, "0521111111", status="active")

    created = client.post(
        "/api/orders",
        json={
            "phone": "0529999999",
            "origin": "ירושלים",
            "destination": "בני ברק",
            "price": 180,
            "tender": True,
        },
        headers={"X-Actor": "sara"},
    ).json()

    assert created["tender"]["flash"] == 1
    assert client.post("/api/orders", json={"phone": "0529999999"}).status_code == 422


def test_a_second_tender_on_the_same_order_is_refused(client):
    order_id = make_order()
    client.post(f"/api/orders/{order_id}/tender", json={})
    assert client.post(f"/api/orders/{order_id}/tender", json={}).status_code == 409


def test_finishing_then_cancelling_an_order_undoes_the_points(client):
    order_id = make_order()
    client.post(f"/api/orders/{order_id}/finish", json={})
    assert client.get("/api/club/0529999999").json()["balance"] == 250

    reversal = client.post(f"/api/orders/{order_id}/cancel").json()
    assert reversal["points_reversed"] == 250
    assert client.get("/api/club/0529999999").json()["balance"] == 0


def test_manual_points_adjustments_are_attributed(client):
    client.post(
        "/api/club/0529999999/adjust",
        json={"delta": 120, "note": "פיצוי"},
        headers={"X-Actor": "sara"},
    )
    assert client.get("/api/club/0529999999").json()["balance"] == 120
    log = client.get("/api/logs", params={"action": "points"}).json()["logs"][0]
    assert (log["actor"], log["detail"]) == ("sara", "+120 (manual)")


def test_preferences_round_trip(client):
    client.patch(
        "/api/club/0529999999/preferences",
        json={"name": "רבקה", "no_marketing": True, "preferred_driver_phone": "0521111111"},
    )
    member = client.get("/api/club/0529999999").json()
    assert member["name"] == "רבקה"
    assert member["preferences"]["no_marketing"] is True


def test_the_books_add_up(client):
    order_id = make_order(price=200.0)
    client.post(f"/api/orders/{order_id}/finish", json={})
    client.post("/api/expenses", json={"category": "דלק", "amount": 10.0})

    summary = client.get("/api/accounting/summary").json()
    assert summary["rides_done"] == 1
    assert summary["fares"] == 200.0
    # Income is the office's cut, not the fare.
    assert summary["commission_income"] == 30.0
    assert summary["expenses"] == 10.0
    assert summary["profit"] == 20.0
    # Points granted and unspent are a liability the office should see.
    assert summary["points_outstanding"] == 250


def test_a_driver_statement_lists_the_commission_owed(client):
    with db.session_scope() as session:
        driver = drivers.register(session, "0521111111", status="active", name="דוד")
        driver_id = driver.id
    order_id = make_order(price=100.0)
    with db.session_scope() as session:
        session.get(db.Order, order_id).driver_id = driver_id
    client.post(f"/api/orders/{order_id}/finish", json={})

    statement = client.post(
        f"/api/accounting/drivers/{driver_id}/send", json={"days": 30}
    ).json()
    assert statement["total_commission"] == 15.0
    assert "עמלה 15.0₪" in statement["text"]


def test_settings_survive_a_round_trip(client):
    client.put("/api/settings", json={"tender_window_seconds": "20"})
    assert client.get("/api/settings").json()["settings"]["tender_window_seconds"] == "20"
    assert db.setting_int("tender_window_seconds") == 20


def test_a_manual_flash_call_is_logged(client):
    with db.session_scope() as session:
        driver = drivers.register(session, "0521111111", status="active")
        driver_id = driver.id
    result = client.post(f"/api/drivers/{driver_id}/flash", headers={"X-Actor": "sara"}).json()
    assert result["status"] == "dry_run"
    assert client.get("/api/logs", params={"action": "flash_manual"}).json()["logs"]


def test_the_area_board_shows_who_reported_in(client):
    with db.session_scope() as session:
        driver = drivers.register(session, "0521111111", status="active")
        driver_id = driver.id
    client.post(f"/api/drivers/{driver_id}/location", json={"area": "ירושלים"})
    board = client.get("/api/drivers/board").json()["areas"]
    assert board[0]["area"] == "ירושלים"
    assert board[0]["drivers"][0]["phone"] == "0521111111"


def test_redeeming_a_ride_needs_the_points(client):
    order_id = make_order()
    assert client.post(f"/api/orders/{order_id}/redeem").status_code == 409
    with db.session_scope() as session:
        loyalty.grant(session, phone="0529999999", delta=500, reason="manual")
    assert client.post(f"/api/orders/{order_id}/redeem").json()["spent"] == 500
