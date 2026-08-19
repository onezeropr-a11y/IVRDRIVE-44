"""The one background loop: closing auctions, dialling ratings, expiring
referrals.

Everything it does is also reachable synchronously from the request path, so
the loop is an optimisation rather than a dependency — a tender whose window
expired is closed by the next driver who asks about it even if this worker is
dead. That keeps a single-instance timer from becoming a single point of
failure for the phone line.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app import db, dispatch, ratings, referrals

log = logging.getLogger("scheduler")

INTERVAL_S = float(os.getenv("SCHEDULER_INTERVAL_S", "2"))
ENABLED = os.getenv("SCHEDULER_ENABLED", "1").lower() not in {"0", "false", "no"}

_task: asyncio.Task | None = None


def tick() -> dict:
    with db.session_scope() as session:
        closed = dispatch.reap(session)
        called = ratings.run_due(session)
        expired = referrals.expire_stale(session)
    return {"tenders_closed": closed, "ratings_called": called, "referrals_expired": expired}


async def _loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(tick)
            if any(result.values()):
                log.info("scheduler %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(INTERVAL_S)


def start() -> None:
    global _task
    if not ENABLED or _task is not None:
        return
    _task = asyncio.create_task(_loop())
    log.info("scheduler started, every %ss", INTERVAL_S)


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
