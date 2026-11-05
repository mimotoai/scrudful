"""The functions whose signatures the callables of
``tests/annotation_globals_uses.py`` take, in another module than theirs.

Written with ``from __future__ import annotations``, and each signature has
one annotation that can't be evaluated (``Session``, imported only under
``TYPE_CHECKING``), so each of the others is evaluated on its own, in the
globals ``annotation_globals`` finds. ``writer`` is a name of this module's
only: read in the other module's globals, ``Security(writer)`` can't be
evaluated. Used by ``test_fastapi_hsr.py``.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, Security

from tests.fastapi_security_annotations import get_db, writer

if TYPE_CHECKING:
    from decimal import Decimal as Session

#: The module the callables are exported from, as their ``__module__``.
USES = "tests.annotation_globals_uses"


def exported_from(module: str) -> Any:
    """Set a function's ``__module__`` to the module it is exported from (as
    numpy's ``set_module`` does): its globals stay this module's."""

    def decorate(func: Any) -> Any:
        func.__module__ = module
        return func

    return decorate


class Checks:
    """Its method is a security dependency, bound."""

    @exported_from(USES)
    def check(
        self, user: Annotated[str, Security(writer)], db: Session = Depends(get_db)
    ) -> str:
        return user


class UserBase:
    """Its subclasses' ``__init__``."""

    def __init__(
        self, user: Annotated[str, Security(writer)], db: Session = Depends(get_db)
    ) -> None:
        self.user = user  # pragma: no cover


class CheckerBase:
    """Its subclasses' instances' ``__call__``."""

    def __call__(
        self, user: Annotated[str, Security(writer)], db: Session = Depends(get_db)
    ) -> str:
        return user  # pragma: no cover


def logged(func: Any) -> Any:
    """A decorator of this module's: the wrapper's globals are this module's,
    its ``__wrapped__``'s those of the module the function is in."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)  # pragma: no cover

    return wrapper
