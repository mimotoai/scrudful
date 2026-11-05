"""Callables whose signatures come from functions of another module,
``tests/annotation_globals_base.py``, which has the names their annotations
use: this module doesn't (no ``writer``), so its globals can't evaluate
them. ``editor`` is the other way round: a name of this module's only. Used
by ``test_fastapi_hsr.py``.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Security

from scrudful.fastapi import add_options, scrudful
from tests import annotation_globals_base as base
from tests.fastapi_security_annotations import BASE, LINKS, get_db

if TYPE_CHECKING:
    from decimal import Decimal as Session


def editor(authorization: Annotated[str | None, Header()] = None) -> str:
    """A security dependency of this module's only."""
    if authorization != "Bearer writer":
        raise HTTPException(403, "editors only")
    return "editor"


class User(base.UserBase):
    """A class whose ``__init__`` is its base's."""


class Checker(base.CheckerBase):
    """A class whose instances' ``__call__`` is its base's."""


#: A callable instance.
CHECKER = Checker()
#: A bound method of the other module's function, whose ``__module__`` says
#: this one.
CHECK = base.Checks().check


@base.logged
def logged_user(
    user: Annotated[str, Security(editor)], db: Session = Depends(get_db)
) -> str:
    """Wrapped by the other module's decorator."""
    return user  # pragma: no cover


class Signed:
    """A callable with a ``__signature__`` (as a framework's object may
    have) and a ``__call__`` that is no Python function: its annotations
    are read in the globals of its module."""

    __call__ = staticmethod(print)


SIGNED = Signed()
SIGNED.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
    [
        inspect.Parameter(
            "user",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation="Annotated[str, Security(editor)]",
        ),
        inspect.Parameter(
            "db", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation="Session"
        ),
    ]
)


def bound_method_app() -> FastAPI:
    """``/notes/{id}``: GET for anyone; PUT for writers, through ``CHECK``,
    a bound method whose function is the other module's."""
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**LINKS)
    async def read(id: int) -> Any:
        return {"id": id}

    @app.put("/notes/{id}")
    @scrudful(**LINKS)
    async def write(id: int, user: Annotated[str, Depends(CHECK)]) -> Any:
        return {"id": id}

    add_options(app, base_url=BASE)
    return app
