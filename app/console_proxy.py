"""Serve the dispatcher console from the backend's own domain.

The console has its own Render service and domain, but that domain is a fresh
one and some filtered networks block it until it is approved. Proxying it under
the backend's already-approved domain avoids that without merging the two
builds: this module only forwards bytes, and deleting it restores the previous
setup.
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse, Response

CONSOLE_UPSTREAM = os.getenv(
    "CONSOLE_UPSTREAM", "https://drivers-console.onrender.com"
).rstrip("/")

#: Hop-by-hop and length headers belong to our own response, not the upstream's.
DROP_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
}

router = APIRouter(tags=["console"])


@router.get("/console")
async def console_root() -> RedirectResponse:
    """The bundle uses relative asset paths, so the trailing slash matters."""
    return RedirectResponse("/console/")


@router.get("/console/{path:path}")
async def console(path: str) -> Response:
    url = f"{CONSOLE_UPSTREAM}/{path}" if path else f"{CONSOLE_UPSTREAM}/"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            upstream = await client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"console upstream: {exc}") from exc

    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in DROP_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )
