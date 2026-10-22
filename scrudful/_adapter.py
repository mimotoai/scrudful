"""Framework-neutral glue shared by the FastAPI and DRF adapters.

Private, and stdlib-only like :mod:`scrudful.conditional`: the adapters keep
everything that touches their framework's request and response objects.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from scrudful.conditional import SAFE_METHODS, validator_headers
from scrudful.utils import EXPOSABLE_HEADERS, merge_expose_headers

DEFAULT_SCHEMA_REL = "describedBy"
DEFAULT_SCHEMA_TYPE = "application/json"
DEFAULT_CONTEXT_REL = "http://www.w3.org/ns/json-ld#context"
DEFAULT_CONTEXT_TYPE = "application/ld+json"

PRECONDITION_FIELDS = (
    "if-match",
    "if-none-match",
    "if-modified-since",
    "if-unmodified-since",
)
#: Methods whose 2xx responses get freshly computed validators (RFC 9110 §9.3.4).
FRESH_VALIDATOR_METHODS = frozenset({"PUT", "PATCH"})

_MALFORMED = "Malformed If-Match or If-None-Match header."
_PRECONDITION_FAILED = "Precondition Failed."
#: Descriptions of the responses a conditional GET/HEAD can get early, by status.
READ_DESCRIPTIONS = {
    304: "Not Modified: the representation matches the request's "
    "If-None-Match or If-Modified-Since validators.",
    400: _MALFORMED,
    412: _PRECONDITION_FAILED,
}
#: Descriptions of the responses a conditional write can get early, by status.
WRITE_DESCRIPTIONS = {
    400: _MALFORMED,
    412: _PRECONDITION_FAILED,
    428: "Precondition Required: this request must be conditional "
    "(send If-Match or If-Unmodified-Since).",
}


class Headers(Protocol):
    """Case-insensitive response headers (Starlette's, or a Django response)."""

    def __contains__(self, name: str, /) -> bool: ...
    def __setitem__(self, name: str, value: str, /) -> None: ...
    def get(self, name: str, /) -> str | None: ...


def check_validators(etag_func: Any, last_modified_func: Any) -> None:
    """Raise ``TypeError`` unless each validator callback is ``None`` or callable."""
    for name, func in (
        ("etag_func", etag_func),
        ("last_modified_func", last_modified_func),
    ):
        if func is not None and not callable(func):
            raise TypeError(f"{name} must be callable, not {type(func).__name__}")


def precondition_headers(getlist: Callable[[str], Sequence[str]]) -> dict[str, str]:
    """Collect the precondition fields, joining repeated lines (RFC 9110 §5.3)."""
    headers = {}
    for name in PRECONDITION_FIELDS:
        values = getlist(name)
        if values:
            headers[name] = ", ".join(values)
    return headers


def validator_policy(method: str, status: int) -> tuple[bool, bool]:
    """``(refresh, send)``: recompute the validators / send them after the view."""
    if not 200 <= status < 300:
        return False, False
    refresh = method in FRESH_VALIDATOR_METHODS
    return refresh, refresh or method in SAFE_METHODS


def set_validator_headers(
    headers: Headers, etag: str | None, last_modified: int | None
) -> None:
    """Set ETag / Last-Modified from the validators unless already set."""
    for name, value in validator_headers(etag, last_modified).items():
        if name not in headers:
            headers[name] = value


def expose_headers(headers: Headers) -> None:
    """Add the exposable headers present to Access-Control-Expose-Headers."""
    names = [name for name in EXPOSABLE_HEADERS if name in headers]
    if names:
        headers["Access-Control-Expose-Headers"] = merge_expose_headers(
            headers.get("Access-Control-Expose-Headers"), names
        )
