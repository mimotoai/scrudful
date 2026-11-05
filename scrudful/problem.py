"""RFC 9457 problem details.

For scrudful's own 400, 412 and 428 responses (:func:`problem_details`) and for
framework errors (:func:`problem_from_errors`: validation errors and the 404
and 500 bodies of the Django and FastAPI adapters).

Framework-free (standard library and scrudful only). Pass
:func:`problem_details` as ``error_body_func`` and, to serve the body as
``application/problem+json``, an ``error`` entity with media type
:data:`PROBLEM_JSON`::

    @scrudful(error_body_func=problem_details,
              error=Entity(media_type=PROBLEM_JSON))
"""

from __future__ import annotations

from typing import Any

from scrudful._adapter import reason_phrase
from scrudful.conditional import ConditionalResult

__all__ = ["PROBLEM_JSON", "problem_details", "problem_from_errors"]

#: The media type of an RFC 9457 problem details object.
PROBLEM_JSON = "application/problem+json"


def problem_details(request: Any, result: ConditionalResult) -> dict[str, Any]:
    """The RFC 9457 problem details of an early 400, 412 or 428 ``result``.

    ``type`` is ``about:blank``, ``title`` the status's reason phrase,
    ``status`` its code and ``detail`` the default body's detail; every other
    member of the default body (e.g. ``accepted-precondition-headers``) is an
    extension member. ``request`` is unused.
    """
    body = dict(result.body or {})
    detail = body.pop("detail", None)
    problem: dict[str, Any] = {
        "type": "about:blank",
        "title": reason_phrase(result.status),
        "status": result.status,
    }
    if detail is not None:
        problem["detail"] = detail
    problem.update(body)
    return problem


def problem_from_errors(
    status: int, errors: Any, detail: str | None = None
) -> dict[str, Any]:
    """RFC 9457 problem details for a framework validation error.

    ``type`` is ``about:blank``, ``title`` the reason phrase of ``status`` and
    ``detail`` the given ``detail`` or that phrase. ``errors`` (the framework's
    own error list) is an extension member, omitted when ``None``.
    """
    problem: dict[str, Any] = {
        "type": "about:blank",
        "title": reason_phrase(status),
        "status": status,
        "detail": detail or reason_phrase(status),
    }
    if errors is not None:
        problem["errors"] = errors
    return problem
