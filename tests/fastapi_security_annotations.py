"""Security dependencies declared in a module with ``from __future__ import
annotations`` and a name imported only under ``TYPE_CHECKING``.

Every annotation here is a string, and ``Session`` can't be evaluated: the
other annotations of the same function must still be read, so that a
``Security(...)`` in an ``Annotated`` is found. One that can't be evaluated
itself leaves OPTIONS unable to tell what it declares. Used by
``test_fastapi_hsr.py``.
"""

from __future__ import annotations

import decimal
import functools
import inspect
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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


# --- Dependencies that are not functions ------------------------------------
# Their annotations are evaluated in the globals of the function their
# signature comes from (``__call__``, ``__init__``, a partial's ``func``), as
# ``inspect.signature(eval_str=True)`` and FastAPI evaluate them.


class Paginator:
    """A callable-instance dependency with a plain ``Depends``: no security."""

    def __init__(self, limit: int) -> None:
        self.limit = limit

    def __call__(self, db: Annotated[object, Depends(get_db)], page: int = 1) -> int:
        return page


class Checker:
    """A callable-instance security dependency."""

    def __call__(self, user: Annotated[str, Security(writer)]) -> str:
        return user


def with_role(role: str, user: Annotated[str, Security(writer)]) -> str:
    """A security dependency given as a ``functools.partial``."""
    return user


class ClassUser:
    """A class security dependency (its ``__init__`` declares it)."""

    def __init__(self, user: Annotated[str, Security(writer)]) -> None:
        self.user = user


class ClassPage:
    """A class dependency with a plain ``Depends``: no security."""

    def __init__(self, db: Annotated[object, Depends(get_db)], page: int = 1) -> None:
        self.page = page


class MixedChecker:
    """A callable instance with a ``Security(...)`` beside an annotation
    that can't be evaluated."""

    def __call__(
        self, user: Annotated[str, Security(writer)], db: Session = Depends(get_db)
    ) -> str:
        return user  # pragma: no cover


def mixed_role(
    role: str, user: Annotated[str, Security(writer)], db: Session = Depends(get_db)
) -> str:
    """The same, for a ``functools.partial``."""
    return user  # pragma: no cover


class MixedUser:
    """The same, for a class."""

    def __init__(
        self, user: Annotated[str, Security(writer)], db: Session = Depends(get_db)
    ) -> None:
        self.user = user  # pragma: no cover


def missing_attribute(a: int, b: decimal.Nope = None) -> None:  # type: ignore
    """An annotation whose evaluation raises ``AttributeError``."""


def lookup(user: str = "") -> str:
    """A plain dependency; its annotation is replaced after the route is
    built (see :func:`missing_attribute_app`)."""
    return user  # pragma: no cover


def missing_attribute_app() -> FastAPI:
    """``/notes/{id}``: GET for anyone; PUT with ``Depends(lookup)``, whose
    ``user`` annotation then becomes ``Annotated[decimal.Nope,
    Security(writer)]``, which raises ``AttributeError`` when evaluated
    (FastAPI itself refuses such an annotation when it builds a route, so
    it is set after). OPTIONS is not given ``add_options`` here."""
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**LINKS)
    async def read(id: int):
        return {"id": id}

    @app.put("/notes/{id}")
    @scrudful(**LINKS)
    async def write(id: int, user: str = Depends(lookup)):
        return {"id": id}  # pragma: no cover

    lookup.__annotations__["user"] = "Annotated[decimal.Nope, Security(writer)]"
    return app


def callables_app() -> FastAPI:
    """``/notes/{id}``: GET and DELETE for anyone (a callable instance and a
    class, each with a plain ``Depends``); PUT, POST and PATCH for writers (a
    callable instance, a ``functools.partial`` and a class, each with a
    ``Security(...)``)."""
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**LINKS)
    async def read(id: int, page: int = Depends(Paginator(10))):
        return {"id": id}

    @app.put("/notes/{id}")
    @scrudful(**LINKS)
    async def write(id: int, user: Annotated[str, Depends(Checker())]):
        return {"id": id}

    @app.post("/notes/{id}")
    @scrudful(**LINKS)
    async def act(id: int, user: str = Depends(functools.partial(with_role, "x"))):
        return {"id": id}

    @app.delete("/notes/{id}")
    @scrudful(**LINKS)
    async def remove(id: int, page: Annotated[ClassPage, Depends(ClassPage)]):
        return {"id": id}

    @app.patch("/notes/{id}")
    @scrudful(**LINKS)
    async def patch(id: int, user: ClassUser = Depends(ClassUser)):
        return {"id": id}

    add_options(app, base_url=BASE)
    return app


# --- What FastAPI built, read otherwise -------------------------------------
# FastAPI reads a dependency's signature once, when it builds the route: an
# annotation or a signature made unreadable after is read so by OPTIONS only.

#: The writers' bearer scheme.
bearer = HTTPBearer()


def bearer_writer(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    """A user resolved from a bearer token: a plain ``Depends`` with a
    security scheme below it."""
    if credentials.credentials != "writer":
        raise HTTPException(403, "writers only")
    return "writer"


def unread_annotation_app() -> FastAPI:
    """``/notes/{id}``: GET for anyone; PUT, POST and PATCH each through a
    dependency FastAPI read as plain, whose annotation was then made one
    that can't be evaluated and names ``Depends(`` or ``Security(``:

    - PUT: ``lookup``'s ``q`` (a query parameter to FastAPI) names a
      ``Depends(`` of a user resolved from a token;
    - POST: the endpoint's own ``user``, a sub-dependant FastAPI built,
      names ``Security(``;
    - PATCH: the same, of the dependency ``outer``'s ``user``, one level
      down.

    OPTIONS is not given ``add_options`` here."""
    app = FastAPI()

    def lookup(q: str = "") -> str:
        return q  # pragma: no cover

    def plain(q: str = "") -> str:
        return q  # pragma: no cover

    def outer(user: str = Depends(plain)) -> str:
        return user  # pragma: no cover

    @app.get("/notes/{id}")
    @scrudful(**LINKS)
    async def read(id: int):
        return {"id": id}

    @app.put("/notes/{id}")
    @scrudful(**LINKS)
    async def write(id: int, found: str = Depends(lookup)):
        return {"id": id}  # pragma: no cover

    @app.post("/notes/{id}")
    @scrudful(**LINKS)
    async def act(id: int, user: str = Depends(plain)):
        return {"id": id}  # pragma: no cover

    @app.patch("/notes/{id}")
    @scrudful(**LINKS)
    async def patch(id: int, found: str = Depends(outer)):
        return {"id": id}  # pragma: no cover

    lookup.__annotations__["q"] = "Annotated[Missing, Depends(bearer_writer)]"
    # The endpoint's signature is the one scrudful's wrapper gives it.
    signature = inspect.signature(act)
    act.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[
            (
                parameter.replace(annotation="Annotated[Missing, Security(writer)]")
                if parameter.name == "user"
                else parameter
            )
            for parameter in signature.parameters.values()
        ]
    )
    outer.__annotations__["user"] = "Annotated[Missing, Security(writer)]"
    return app


def scheme_below_app() -> FastAPI:
    """``/notes/{id}``: GET for anyone; PUT and POST for bearer writers,
    each through a dependency with a security scheme below it, which still
    decides when a signature above it can't be read (made so after the route
    was built):

    - PUT: the dependency's own signature (``opaque_user``);
    - POST: a sibling dependency's, before the scheme's (``opaque``, of
      ``sibling_user``).
    """
    app = FastAPI()

    def opaque_user(user: str = Depends(bearer_writer)) -> str:
        return user

    def opaque(q: str = "") -> str:
        return q

    def sibling_user(
        other: str = Depends(opaque), user: str = Depends(bearer_writer)
    ) -> str:
        return user

    @app.get("/notes/{id}")
    @scrudful(**LINKS)
    async def read(id: int):
        return {"id": id}

    @app.put("/notes/{id}")
    @scrudful(**LINKS)
    async def write(id: int, user: str = Depends(opaque_user)):
        return {"id": id}

    @app.post("/notes/{id}")
    @scrudful(**LINKS)
    async def act(id: int, user: str = Depends(sibling_user)):
        return {"id": id}

    opaque_user.__signature__ = "not a signature"  # type: ignore[attr-defined]
    opaque.__signature__ = "not a signature"  # type: ignore[attr-defined]
    add_options(app, base_url=BASE)
    return app
