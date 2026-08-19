"""Minimal operator console: edit the prompt, manage prices, export orders.

Deliberately server-rendered HTML with no build step — the dispatcher needs to
change the bot's wording without a deploy, nothing more.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from hmac import compare_digest
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from openpyxl import Workbook
from sqlalchemy import select

from app import db
from app.api import ADMIN_TOKEN

_basic = HTTPBasic(auto_error=False)


def require_admin(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)] = None,
) -> None:
    """These pages are plain browser navigation, so the token travels as a Basic
    password (any user name) rather than the header the JSON API uses."""
    if not ADMIN_TOKEN:
        return
    if credentials is None or not compare_digest(credentials.password, ADMIN_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="admin token required",
            headers={"WWW-Authenticate": "Basic"},
        )


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_PAGE = """<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<title>מוקד דרייברים — ניהול</title>
<style>
 body{{font-family:system-ui,Arial;margin:2rem auto;max-width:900px;line-height:1.6}}
 textarea{{width:100%;height:16rem;font-family:inherit;font-size:1rem}}
 table{{border-collapse:collapse;width:100%;margin-bottom:1rem}}
 td,th{{border:1px solid #ddd;padding:.4rem .6rem;text-align:right}}
 nav a{{margin-left:1rem}} button{{padding:.4rem 1rem;font-size:1rem}}
</style></head><body>
<nav><a href="/admin">פרומפט</a><a href="/admin/prices">מחירים</a>
<a href="/admin/customers">לקוחות</a><a href="/admin/orders">הזמנות</a>
<a href="/admin/calls">שיחות</a><a href="/">דיבאג</a></nav>
<h1>{title}</h1>
{body}
</body></html>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=escape(title), body=body))


@router.get("", response_class=HTMLResponse)
def prompt_form() -> HTMLResponse:
    content = escape(db.get_prompt("system"))
    return _page(
        "עריכת פרומפט",
        f"""<form method="post" action="/admin/prompt">
        <textarea name="content">{content}</textarea>
        <p><button type="submit">שמור</button>
        השינוי נכנס לתוקף בשיחה הבאה.</p></form>""",
    )


@router.post("/prompt")
def prompt_save(content: str = Form(...)) -> RedirectResponse:
    db.set_prompt("system", content)
    return RedirectResponse("/admin", status_code=303)


@router.get("/prices", response_class=HTMLResponse)
def prices_page() -> HTMLResponse:
    with db.session_scope() as session:
        rows = session.scalars(select(db.Price).order_by(db.Price.origin)).all()
        listed = "".join(
            f"<tr><td>{escape(r.origin)}</td><td>{escape(r.destination)}</td>"
            f"<td>{r.price:.0f} ₪</td>"
            f'<td><form method="post" action="/admin/prices/{r.id}/delete">'
            f'<button type="submit">מחק</button></form></td></tr>'
            for r in rows
        )
    return _page(
        "מחירון",
        f"""<table><tr><th>מוצא</th><th>יעד</th><th>מחיר</th><th></th></tr>
        {listed}</table>
        <form method="post" action="/admin/prices">
        <input name="origin" placeholder="מוצא" required>
        <input name="destination" placeholder="יעד" required>
        <input name="price" type="number" step="1" placeholder="מחיר" required>
        <button type="submit">הוסף</button></form>""",
    )


@router.post("/prices")
def prices_add(
    origin: str = Form(...), destination: str = Form(...), price: float = Form(...)
) -> RedirectResponse:
    with db.session_scope() as session:
        session.add(
            db.Price(
                origin=db.normalize_place(origin),
                destination=db.normalize_place(destination),
                price=price,
            )
        )
    return RedirectResponse("/admin/prices", status_code=303)


@router.post("/prices/{price_id}/delete")
def prices_delete(price_id: int) -> RedirectResponse:
    with db.session_scope() as session:
        if (row := session.get(db.Price, price_id)) is not None:
            session.delete(row)
    return RedirectResponse("/admin/prices", status_code=303)


@router.get("/customers", response_class=HTMLResponse)
def customers_page() -> HTMLResponse:
    with db.session_scope() as session:
        rows = session.scalars(select(db.Customer).order_by(db.Customer.phone)).all()
        listed = "".join(
            f"<tr><td>{escape(r.phone)}</td><td>{escape(r.name or '')}</td>"
            f"<td>{escape(r.default_pickup or '')}</td><td>{escape(r.notes or '')}</td>"
            f'<td><form method="post" action="/admin/customers/{r.id}/delete">'
            f'<button type="submit">מחק</button></form></td></tr>'
            for r in rows
        )
    return _page(
        "לקוחות",
        f"""<table><tr><th>טלפון</th><th>שם</th><th>כתובת איסוף</th><th>הערות</th>
        <th></th></tr>{listed}</table>
        <form method="post" action="/admin/customers">
        <input name="phone" placeholder="טלפון" required>
        <input name="name" placeholder="שם">
        <input name="default_pickup" placeholder="כתובת איסוף">
        <input name="notes" placeholder="הערות">
        <button type="submit">שמור</button></form>
        <p>שמירה על מספר קיים מעדכנת אותו.</p>""",
    )


@router.post("/customers")
def customers_save(
    phone: str = Form(...),
    name: str = Form(""),
    default_pickup: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    normalized = db.normalize_phone(phone)
    with db.session_scope() as session:
        row = session.scalars(
            select(db.Customer).where(db.Customer.phone == normalized)
        ).first()
        if row is None:
            row = db.Customer(phone=normalized)
            session.add(row)
        row.name = name or None
        row.default_pickup = default_pickup or None
        row.notes = notes or None
    return RedirectResponse("/admin/customers", status_code=303)


@router.post("/customers/{customer_id}/delete")
def customers_delete(customer_id: int) -> RedirectResponse:
    with db.session_scope() as session:
        if (row := session.get(db.Customer, customer_id)) is not None:
            session.delete(row)
    return RedirectResponse("/admin/customers", status_code=303)


def _call_cost_usd(stats_json: str | None) -> float:
    if not stats_json:
        return 0.0
    try:
        stats = json.loads(stats_json)
    except ValueError:
        return 0.0
    usage = stats.get("usage") if isinstance(stats, dict) else None
    if not isinstance(usage, dict):
        return 0.0
    return float(usage.get("cost_usd") or 0.0)


@router.get("/calls", response_class=HTMLResponse)
def calls_page() -> HTMLResponse:
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.CallLog).order_by(db.CallLog.started_at.desc()).limit(100)
        ).all()
        costs = [_call_cost_usd(r.stats_json) for r in rows]
        listed = "".join(
            f"<tr><td>{r.started_at:%d/%m %H:%M}</td><td>{escape(r.phone or '')}</td>"
            f"<td>{escape(r.summary or '')}</td><td>${c:.4f}</td>"
            f'<td><a href="/admin/calls/{r.id}">תמליל</a></td></tr>'
            for r, c in zip(rows, costs, strict=True)
        )
        total = sum(costs)
    return _page(
        "שיחות",
        f"""<p>עלות 100 השיחות האחרונות: <b>${total:.2f}</b> (הערכה לפי מחירון
        Gemini, מתוך דיווח השימוש של המודל עצמו)</p>
        <table><tr><th>מתי</th><th>מתקשר</th><th>תוצאה</th><th>עלות</th><th></th></tr>
        {listed}</table>""",
    )


def _tokens_text(tokens: object) -> str:
    if not isinstance(tokens, dict) or not tokens:
        return "—"
    return ", ".join(f"{name} {count}" for name, count in sorted(tokens.items()))


@router.get("/calls/{call_pk}", response_class=HTMLResponse)
def call_detail(call_pk: int) -> HTMLResponse:
    with db.session_scope() as session:
        row = session.get(db.CallLog, call_pk)
        if row is None:
            return _page("שיחה לא נמצאה", "<p>אין רשומה כזאת.</p>")
        stats = json.loads(row.stats_json) if row.stats_json else {}
        transcript = escape(row.transcript or "")
        latencies = stats.get("reply_latency_ms") or []
        tool_calls = stats.get("tool_calls") or []
        usage = stats.get("usage") or {}
    listed_tools = "".join(
        f"<li>{escape(t.get('name', ''))} ← "
        f"{escape(json.dumps(t.get('result'), ensure_ascii=False))}</li>"
        for t in tool_calls
    )
    return _page(
        f"שיחה {row.call_id}",
        f"""<p>מתקשר: {escape(row.phone or '')} — תורים: {stats.get('turns', 0)} —
        קטיעות: {stats.get('interruptions', 0)}</p>
        <p>זמני תגובה (ms): {escape(', '.join(str(x) for x in latencies)) or '—'}</p>
        <p>עלות משוערת: <b>${float(usage.get('cost_usd') or 0.0):.4f}</b> —
        טוקנים נכנסים: {escape(_tokens_text(usage.get('input_tokens')))} —
        יוצאים: {escape(_tokens_text(usage.get('output_tokens')))}</p>
        <h3>כלים שהופעלו</h3><ul>{listed_tools or '<li>אין</li>'}</ul>
        <h3>תמליל</h3><pre style="white-space:pre-wrap">{transcript}</pre>""",
    )


@router.get("/orders", response_class=HTMLResponse)
def orders_page() -> HTMLResponse:
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.Order).order_by(db.Order.created_at.desc()).limit(200)
        ).all()
        listed = "".join(
            f"<tr><td>{r.created_at:%d/%m %H:%M}</td><td>{escape(r.phone)}</td>"
            f"<td>{escape(r.origin)}</td><td>{escape(r.destination)}</td>"
            f"<td>{r.passengers}</td><td>{escape(r.pickup_time or '')}</td>"
            f"<td>{'' if r.price is None else f'{r.price:.0f}'}</td></tr>"
            for r in rows
        )
    return _page(
        "הזמנות",
        f"""<p><a href="/admin/orders.xlsx">הורדה כאקסל</a></p>
        <table><tr><th>מועד</th><th>טלפון</th><th>מוצא</th><th>יעד</th>
        <th>נוסעים</th><th>לאיסוף</th><th>מחיר</th></tr>{listed}</table>""",
    )


@router.get("/orders.xlsx")
def orders_export() -> Response:
    book = Workbook()
    sheet = book.active
    sheet.title = "orders"
    sheet.append(
        ["מועד יצירה", "טלפון", "מוצא", "יעד", "נוסעים", "מועד איסוף", "מחיר", "הערות"]
    )
    with db.session_scope() as session:
        for r in session.scalars(select(db.Order).order_by(db.Order.created_at)).all():
            sheet.append(
                [
                    r.created_at.strftime("%d/%m/%Y %H:%M"),
                    r.phone,
                    r.origin,
                    r.destination,
                    r.passengers,
                    r.pickup_time or "",
                    r.price,
                    r.notes or "",
                ]
            )
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    name = f"orders-{datetime.utcnow():%Y%m%d-%H%M}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
