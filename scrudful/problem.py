"""RFC 9457 problem details for scrudful's own 400, 412 and 428 responses.

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

__all__ = ["PROBLEM_JSON", "problem_details"]

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
