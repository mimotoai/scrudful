"""Framework-neutral glue shared by the FastAPI, DRF and django-ninja adapters.

Private, and stdlib-only like :mod:`scrudful.conditional`: the adapters keep
everything that touches their framework's request and response objects.
"""

from __future__ import annotations

import copy
import inspect
import sys
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

#: :data:`READ_DESCRIPTIONS` as OPTIONS responses (string status keys).
READ_RESPONSES = {
    str(status): {"description": text} for status, text in READ_DESCRIPTIONS.items()
}
#: :data:`WRITE_DESCRIPTIONS` as OPTIONS responses (string status keys).
WRITE_RESPONSES = {
    str(status): {"description": text} for status, text in WRITE_DESCRIPTIONS.items()
}
#: The request body description and success status of each write method.
_WRITES = {
    "post": ("The content for the resource to be created.", "201"),
    "put": ("The new content of the resource.", "200"),
    "patch": ("The changes to apply to the resource.", "200"),
    "delete": (None, "204"),
}
_STATUS_TEXT = {"200": "OK", "201": "Created", "204": "No Content"}


class Headers(Protocol):
    """Case-insensitive response headers (Starlette's, or a Django response).

    ``get`` returns a single value and ``[]=`` replaces every existing line of
    that name, so a field repeated on several lines must be joined by the
    caller first.
    """

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
    """Collect the precondition fields, joining repeated lines (RFC 9110 §5.3).

    Empty (or blank) lines are dropped: an empty field is the same as an absent
    one, as in :func:`scrudful.conditional.evaluate_preconditions`.
    """
    headers = {}
    for name in PRECONDITION_FIELDS:
        values = [value for value in getlist(name) if value.strip(" \t")]
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


def evaluated_signature(func: Callable[..., Any]) -> inspect.Signature:
    """``func``'s signature with string annotations evaluated in its module.

    For endpoints written with ``from __future__ import annotations``: a
    framework reading the strings off an adapter's wrapper would evaluate them
    in the wrapper's globals (the adapter module's), where the endpoint's names
    are undefined. If an annotation can't be evaluated, the unevaluated
    signature is returned.
    """
    try:
        return inspect.signature(func, eval_str=True)
    except NameError:
        pass
    try:
        return inspect.signature(func)
    except NameError:
        # Python 3.14+: annotations naming objects imported only under
        # TYPE_CHECKING can't be evaluated; FastAPI and ninja do the same.
        if sys.version_info < (3, 14):
            raise
        from annotationlib import Format

        return inspect.signature(func, annotation_format=Format.FORWARDREF)


def resolve_annotation(annotation: Any, globalns: dict[str, Any]) -> Any:
    """Evaluate a string or forward-reference annotation in ``globalns``.

    Other annotations are returned unchanged; ``None`` if evaluation fails.
    """
    if not isinstance(annotation, (str, type)):
        annotation = getattr(annotation, "__forward_arg__", annotation)
    if isinstance(annotation, str):
        try:
            return eval(annotation, globalns)  # same as typing/inspect
        except Exception:
            return None
    return annotation


# --- OPTIONS descriptions ----------------------------------------------------
#
# The operations of an OPTIONS body (an OpenAPI-like Path Item), shared by the
# DRF and django-ninja adapters. ``content`` is a Media Type map such as
# ``{"application/json": {"schema": <url>, "context": <url>}}``; falsy means
# none. ``conditional``: whether the method's preconditions are evaluated.


def describe_read(content: dict[str, Any] | None, conditional: bool) -> dict[str, Any]:
    """Describe a GET: its 200 and, when conditional, the 304, 400 and 412."""
    ok: dict[str, Any] = {"description": "OK"}
    if content:
        ok["content"] = content
    responses = {"200": ok}
    if conditional:
        responses.update(copy.deepcopy(READ_RESPONSES))
    return {"responses": responses}


def describe_write(
    method: str,
    content: dict[str, Any] | None,
    conditional: bool,
    has_etag: bool,
    has_last_modified: bool,
) -> dict[str, Any]:
    """Describe a ``post``, ``put``, ``patch`` or ``delete``.

    ``content`` describes the request body (DELETE has none). A conditional
    POST gets the 400 and 412, since it is evaluated but never required to be
    conditional; the other methods also get the 428 and the precondition
    header ``parameters`` (see :func:`conditional_header_parameters`).

    Raises
    ------
    ValueError
        If ``method`` is not one of the four.
    """
    if method not in _WRITES:
        raise ValueError(f"not a write method: {method!r}")
    body_description, status = _WRITES[method]
    metadata: dict[str, Any] = {}
    if body_description is not None:
        request_body: dict[str, Any] = {
            "description": body_description,
            "required": True,
        }
        if content:
            request_body["content"] = content
        metadata["requestBody"] = request_body
    metadata["responses"] = {status: {"description": _STATUS_TEXT[status]}}
    if conditional:
        if method == "post":
            metadata["responses"].update(
                {code: copy.deepcopy(WRITE_RESPONSES[code]) for code in ("400", "412")}
            )
        else:
            metadata["responses"].update(copy.deepcopy(WRITE_RESPONSES))
            metadata["parameters"] = conditional_header_parameters(
                has_etag, has_last_modified
            )
    return metadata


def conditional_header_parameters(
    has_etag: bool, has_last_modified: bool
) -> list[dict[str, Any]] | None:
    """Describe the precondition headers a conditional write accepts.

    One of them is required; a header is marked ``required`` when it is the
    only one accepted. ``None`` without validators.
    """
    headers = []
    if has_etag:
        headers.append(("If-Match", "The entity-tag of the current representation."))
    if has_last_modified:
        headers.append(
            (
                "If-Unmodified-Since",
                "The Last-Modified date of the current representation.",
            )
        )
    if not headers:
        return None
    either = " Either If-Match or If-Unmodified-Since is required."
    return [
        {
            "in": "header",
            "name": name,
            "schema": {"type": "string"},
            "required": len(headers) == 1,
            "description": description if len(headers) == 1 else description + either,
        }
        for name, description in headers
    ]
