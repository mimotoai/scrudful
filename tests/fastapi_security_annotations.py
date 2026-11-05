"""Security dependencies declared in a module with ``from __future__ import
annotations`` and a name imported only under ``TYPE_CHECKING``.

Every annotation here is a string, and ``Session`` can't be evaluated: the
other annotations of the same function must still be read, so that a
``Security(...)`` in an ``Annotated`` is found. One that can't be evaluated
itself leaves OPTIONS unable to tell what it declares. Used by
``test_fastapi_hsr.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Security

from scrudful.fastapi import add_options, scrudful

if TYPE_CHECKING:
    from decimal import Decimal as Session

LINKS = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
BASE = "https://api.example/"
WRITER = {"Authorization": "Bearer writer"}
#: The names of the dependencies OPTIONS ran.
CALLS: list[str] = []


def get_db() -> object:
    CALLS.append("get_db")
    return object()


def get_session() -> object:
    CALLS.append("get_session")
    return object()


def writer(authorization: Annotated[str | None, Header()] = None) -> str:
    """A security dependency with no security scheme: it reads the header
    itself, so only its ``Security(...)`` marks it."""
    CALLS.append("writer")
    if authorization != "Bearer writer":
        raise HTTPException(403, "writers only")
    return "writer"


def get_user(
    user: Annotated[str, Security(writer)], db: Session = Depends(get_session)
) -> str:
    """A plain dependency whose own ``Security(...)`` sits beside an
    annotation that can't be evaluated (it is a security dependency, so it
    runs whole on OPTIONS, ``get_session`` included)."""
    return user


def unreadable_user(user: Annotated[Session, Security(writer)]) -> Any:
    """A plain dependency whose ``Security(...)`` annotation can't be
    evaluated."""
    return user  # pragma: no cover


def readable_app(**options: Any) -> FastAPI:
    """``/notes/{id}``: GET for anyone; PUT for writers, through a
    ``Security(...)`` parameter of the endpoint; POST for writers, through
    one of a ``Depends``. Each function also has an annotation that can't be
    evaluated."""
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**LINKS)
    async def read(id: int):
        return {"id": id}

    @app.put("/notes/{id}")
    @scrudful(**LINKS)
    async def write(
        id: int,
        user: Annotated[str, Security(writer)],
        db: Session = Depends(get_db),
    ):
        return {"id": id}

    @app.post("/notes/{id}")
    @scrudful(**LINKS)
    async def act(id: int, user: str = Depends(get_user)):
        return {"id": id}

    add_options(app, base_url=BASE, **options)
    return app


def unreadable_app() -> FastAPI:
    """``/notes/{id}``: GET for anyone; PUT and PATCH with a ``Security(...)``
    whose annotation can't be evaluated (of the endpoint, and of a
    dependency of it). OPTIONS is not given ``add_options`` here."""
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**LINKS)
    async def read(id: int):
        return {"id": id}

    @app.put("/notes/{id}")
    @scrudful(**LINKS)
    async def write(id: int, user: Annotated[Session, Security(writer)]):
        return {"id": id}  # pragma: no cover

    @app.patch("/notes/{id}")
    @scrudful(**LINKS)
    async def patch(id: int, user: Any = Depends(unreadable_user)):
        return {"id": id}  # pragma: no cover

    return app
