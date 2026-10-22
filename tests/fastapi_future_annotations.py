"""Endpoints defined in a module with ``from __future__ import annotations``.

All annotations here are strings that name this module's globals, which
FastAPI < 0.124 resolves against the *wrapper's* globals (scrudful.fastapi's).
Used by ``test_fastapi_adapter.py``.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from pydantic import BaseModel

from scrudful.fastapi import scrudful


class Body(BaseModel):
    name: str


def etag(item_id: int, **kwargs: object) -> str:
    return f"v{item_id}"


@scrudful(etag_func=etag)
async def replace_item(request: Request, item_id: int, body: Body) -> dict[str, str]:
    return {"name": body.name, "method": request.method}


def make_app() -> FastAPI:
    app = FastAPI()
    app.put("/items/{item_id}")(replace_item)
    return app
