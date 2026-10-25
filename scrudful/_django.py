"""Django request/response glue shared by the DRF and django-ninja adapters.

Private. Imports Django only (neither DRF nor django-ninja), so either adapter
can be used without the other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.http import (
    HttpRequest,
    HttpResponseBase,
    HttpResponseNotModified,
    JsonResponse,
)
from django.utils import timezone

from scrudful import _adapter
from scrudful.conditional import ConditionalResult

__all__ = ["aware", "conditional_response", "precondition_headers"]


def precondition_headers(request: HttpRequest) -> dict[str, str]:
    """Collect the precondition fields from a Django (or DRF) request."""

    # WSGI/ASGI servers already combine repeated field lines with ",", so each
    # field has at most one value.
    def getlist(name: str) -> list[str]:
        value = request.headers.get(name)
        return [] if value is None else [value]

    return _adapter.precondition_headers(getlist)


def conditional_response(
    result: ConditionalResult,
    link: str | None = None,
    content_type: str | None = None,
) -> HttpResponseBase:
    """Render a failed precondition: a bare 304, or a JSON body and status.

    ``link`` is the Link field value of a response with a body (the adapter's
    error Links) and ``content_type`` its Content-Type (default
    ``application/json``; the media type of the adapter's error entity); a
    304 gets neither.
    """
    response: HttpResponseBase
    if result.body is None:
        response = HttpResponseNotModified()
    else:
        response = JsonResponse(
            result.body,
            status=result.status,
            content_type=content_type or "application/json",
        )
        if link:
            response["Link"] = link
    for name, value in result.headers.items():
        response[name] = value
    _adapter.expose_headers(response)
    return response


def aware(value: Any) -> Any:
    """Make a naive ``datetime`` aware in the default time zone."""
    if isinstance(value, datetime) and timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_default_timezone())
    return value
