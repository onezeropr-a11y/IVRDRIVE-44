"""Test wiring.

The database URL and the PBX dry-run flag are read at import time by
``app.db`` and ``app.pbx``, so they are set here — before any application
module is imported — rather than in a fixture.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ivrdrive-tests-"))
os.environ["BOT_DB_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["PBX_DRY_RUN"] = "1"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["CAPTURE_DIR"] = str(_TMP / "captures")

from app import db  # noqa: E402  (must follow the env setup above)


@pytest.fixture(autouse=True)
def clean_db() -> Iterator[None]:
    db.Base.metadata.drop_all(db.engine)
    db.Base.metadata.create_all(db.engine)
    yield


@pytest.fixture()
def session() -> Iterator[db.Session]:
    with db.session_scope() as active:
        yield active
