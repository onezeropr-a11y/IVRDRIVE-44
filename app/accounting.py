"""The books: what came in, what went out, and what each driver owes.

Revenue is the office's commission on completed rides, not the fare — the fare
belongs to the driver, and counting it as income would overstate the business
several times over. Rides paid for with points earn no commission but still
cost the club its liability, so they are reported separately rather than
quietly dropped.

Outstanding points are a real liability: every unspent point is a future free
ride, so the report values them at the current redemption rate.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import db


def _window(days: int) -> datetime:
    return datetime.utcnow() - timedelta(days=days)


def profit_and_loss(session: Session, days: int = 30) -> dict:
    since = _window(days)
    done = session.scalars(
        select(db.Order).where(db.Order.status == "done", db.Order.created_at >= since)
    ).all()
    rate = db.setting_float("commission_rate")

    fares = sum(float(o.price or 0.0) for o in done)
    commission = sum(
        float(o.commission if o.commission is not None else float(o.price or 0.0) * rate)
        for o in done
    )
    point_rides = [o for o in done if o.points_spent]
    expenses = session.scalars(
        select(db.Expense).where(db.Expense.spent_on >= since)
    ).all()
    expense_total = sum(float(e.amount or 0.0) for e in expenses)

    outstanding = int(
        session.scalar(select(func.coalesce(func.sum(db.PointsEntry.delta), 0))) or 0
    )
    redeem_cost = db.setting_int("redeem_points") or 1

    by_category: dict[str, float] = {}
    for row in expenses:
        by_category[row.category] = by_category.get(row.category, 0.0) + float(row.amount or 0.0)

    return {
        "days": days,
        "rides_done": len(done),
        "fares": round(fares, 2),
        "commission_income": round(commission, 2),
        "expenses": round(expense_total, 2),
        "profit": round(commission - expense_total, 2),
        "expenses_by_category": {k: round(v, 2) for k, v in sorted(by_category.items())},
        "point_rides": len(point_rides),
        "points_outstanding": outstanding,
        "points_liability_rides": round(outstanding / redeem_cost, 2),
    }


def driver_statement(session: Session, driver_id: int, days: int = 30) -> dict:
    """What the office bills one driver: their completed rides and the cut."""
    since = _window(days)
    driver = session.get(db.Driver, driver_id)
    if driver is None:
        return {}
    rows = session.scalars(
        select(db.Order)
        .where(
            db.Order.driver_id == driver_id,
            db.Order.status == "done",
            db.Order.created_at >= since,
        )
        .order_by(db.Order.created_at)
    ).all()
    rate = db.setting_float("commission_rate")
    rides = [
        {
            "order_id": o.id,
            "date": (o.finished_at or o.created_at).isoformat(),
            "origin": o.origin,
            "destination": o.destination,
            "price": float(o.price or 0.0),
            "paid_with_points": bool(o.points_spent),
            "commission": round(
                float(o.commission if o.commission is not None else float(o.price or 0.0) * rate),
                2,
            ),
        }
        for o in rows
    ]
    return {
        "driver": {"id": driver.id, "name": driver.name, "phone": driver.phone},
        "days": days,
        "rides": rides,
        "total_fares": round(sum(r["price"] for r in rides), 2),
        "total_commission": round(sum(r["commission"] for r in rides), 2),
    }


def rides_by_driver(session: Session, days: int = 30) -> list[dict]:
    since = _window(days)
    rows = session.execute(
        select(
            db.Order.driver_id,
            func.count(db.Order.id),
            func.coalesce(func.sum(db.Order.price), 0.0),
            func.coalesce(func.sum(db.Order.commission), 0.0),
        )
        .where(
            db.Order.status == "done",
            db.Order.created_at >= since,
            db.Order.driver_id.is_not(None),
        )
        .group_by(db.Order.driver_id)
    ).all()
    names = {d.id: (d.name, d.phone) for d in session.scalars(select(db.Driver)).all()}
    out = []
    for driver_id, count, fares, commission in rows:
        name, phone = names.get(driver_id, (None, None))
        out.append(
            {
                "driver_id": driver_id,
                "name": name,
                "phone": phone,
                "rides": int(count),
                "fares": round(float(fares), 2),
                "commission": round(float(commission), 2),
            }
        )
    return sorted(out, key=lambda row: -row["rides"])


def add_expense(
    session: Session, *, category: str, amount: float, note: str | None, actor: str
) -> db.Expense:
    row = db.Expense(category=category, amount=amount, note=note)
    session.add(row)
    session.flush()
    db.log_action(
        session,
        "expense_added",
        actor=actor,
        entity="expense",
        entity_id=row.id,
        detail=f"{category} {amount}",
    )
    return row
