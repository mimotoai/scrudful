"""Django request/response glue shared by the DRF and django-ninja adapters.

Private. Imports Django only (neither DRF nor django-ninja), so either adapter
can be used without the other.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from django.core.paginator import UnorderedObjectListWarning
from django.db.models import QuerySet
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseNotModified,
    JsonResponse,
    QueryDict,
)
from django.utils import timezone

from scrudful import _adapter
from scrudful.collection import InvalidPage
from scrudful.conditional import ConditionalResult

__all__ = [
    "aware",
    "conditional_response",
    "json_response",
    "page_url",
    "precondition_headers",
    "slice_page",
]


def precondition_headers(request: HttpRequest) -> dict[str, str]:
    """Collect the precondition fields from a Django (or DRF) request."""

    # WSGI/ASGI servers already combine repeated field lines with ",", so each
    # field has at most one value.
    def getlist(name: str) -> list[str]:
        value = request.headers.get(name)
        return [] if value is None else [value]

    return _adapter.precondition_headers(getlist)


#: Writes a JSON body: ``render(data, status)`` returns its bytes.
Render = Callable[[Any, int], bytes]


def conditional_response(
    result: ConditionalResult,
    link: str | None = None,
    content_type: str | None = None,
    render: Render | None = None,
) -> HttpResponseBase:
    """Render a failed precondition: a bare 304, or a JSON body and status.

    ``link`` is the Link field value of a response with a body (the adapter's
    error Links) and ``content_type`` its Content-Type (default
    ``application/json``; the media type of the adapter's error entity); a
    304 gets neither. The body is written by ``render`` (the ninja adapter
    passes the API's renderer), else as Django's ``JsonResponse`` writes it.
    """
    response: HttpResponseBase
    if result.body is None:
        response = HttpResponseNotModified()
    else:
        response = json_response(
            result.body, result.status, content_type or "application/json", render
        )
        if link:
            response["Link"] = link
    for name, value in result.headers.items():
        response[name] = value
    _adapter.expose_headers(response)
    return response


def json_response(
    data: Any, status: int, content_type: str, render: Render | None = None
) -> HttpResponse:
    """A response with the JSON body ``data``, written by ``render`` if given,
    else by Django's ``JsonResponse``, and this status and Content-Type."""
    if render is None:
        return JsonResponse(data, status=status, content_type=content_type)
    return HttpResponse(render(data, status), status=status, content_type=content_type)


def aware(value: Any) -> Any:
    """Make a naive ``datetime`` aware in the default time zone."""
    if isinstance(value, datetime) and timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_default_timezone())
    return value


def slice_page(items: Any, number: int, size: int) -> tuple[int, list[Any]]:
    """The item count of ``items`` and the items of page ``number``.

    ``count()`` for a ``QuerySet``, else ``len()``. An unordered ``QuerySet``
    gets Django's ``UnorderedObjectListWarning``, as with its ``Paginator``.

    Raises
    ------
    InvalidPage
        For a page past the last one (page 1 always exists).

    Notes
    -----
    The unordered warning is attributed to the caller's caller (stacklevel 3),
    as the adapter wraps this function.
    """
    if isinstance(items, QuerySet):
        if not items.ordered:
            warnings.warn(
                "Pagination may yield inconsistent results with an unordered "
                f"object_list: {items.model} {items.__class__.__name__}.",
                UnorderedObjectListWarning,
                stacklevel=3,
            )
        count = items.count()
    else:
        count = len(items)
    if number > max(math.ceil(count / size), 1):
        raise InvalidPage("Invalid page.")
    start = (number - 1) * size
    return count, list(items[start : start + size])


def page_url(url: str, query: QueryDict, number: int) -> str:
    """``url`` with the query ``query`` (a ``QueryDict``) and ``page=number``.

    The parameters are sorted by name, as DRF's ``replace_query_param`` does.
    The query comes from ``request.GET``, not from ``url``: they are the same
    in Django, but ninja's ``TestClient`` builds URLs without a query.
    """
    query = query.copy()
    query.setlist("page", [str(number)])
    scheme, netloc, path, _, _ = urlsplit(url)
    encoded = urlencode(sorted(query.lists()), doseq=True)
    return urlunsplit((scheme, netloc, path, encoded, ""))
