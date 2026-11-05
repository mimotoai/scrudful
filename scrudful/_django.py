"""Django request/response glue shared by the DRF and django-ninja adapters.

Private. Imports Django only (neither DRF nor django-ninja), so either adapter
can be used without the other.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import UnorderedObjectListWarning
from django.db.models import QuerySet
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseNotAllowed,
    HttpResponseNotModified,
    JsonResponse,
    QueryDict,
)
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.views import defaults

from scrudful import _adapter
from scrudful._hsr import (
    HsrConfigError,
    check_base_url,
    hsr_etag,
    hsr_response_links,
    serialize_hsr,
)
from scrudful.collection import InvalidPage
from scrudful.conditional import ConditionalResult, evaluate_preconditions
from scrudful.entity import Entity
from scrudful.problem import PROBLEM_JSON, problem_from_errors
from scrudful.utils import join_links, link_content, resolve_uri

__all__ = [
    "aware",
    "conditional_response",
    "error_handler",
    "hsr_not_allowed",
    "hsr_response",
    "json_response",
    "no_content",
    "page_url",
    "precondition_headers",
    "scrudful_setting",
    "slice_page",
]

#: The keys of the ``SCRUDFUL`` setting.
SETTING_KEYS = frozenset(
    {"BASE_URL", "ERROR", "HSR_CACHE_CONTROL", "HSR_ROUTE", "HSR_URL"}
)


def scrudful_setting(name: str, default: Any) -> Any:
    """``settings.SCRUDFUL[name]``, or ``default`` when unset.

    ``SCRUDFUL`` is an optional dict of the keys :data:`SETTING_KEYS`; the
    per-router and per-viewset arguments override it. It is read on every
    call, so ``override_settings`` applies.

    Raises
    ------
    ImproperlyConfigured
        If ``SCRUDFUL`` is not a dict, or has a key scrudful doesn't know.
    """
    values = getattr(settings, "SCRUDFUL", None)
    if values is None:
        return default
    if not isinstance(values, Mapping):
        raise ImproperlyConfigured(
            f"SCRUDFUL must be a dict, not {type(values).__name__}"
        )
    unknown = sorted(str(key) for key in values.keys() - SETTING_KEYS)
    if unknown:
        raise ImproperlyConfigured(
            f"SCRUDFUL has unknown keys {unknown}; its keys are "
            f"{sorted(SETTING_KEYS)}"
        )
    return values.get(name, default)


def precondition_headers(request: HttpRequest) -> dict[str, str]:
    """Collect the precondition fields from a Django (or DRF) request."""

    # WSGI/ASGI servers already combine repeated field lines with ",", so each
    # field has at most one value.
    def getlist(name: str) -> list[str]:
        value = request.headers.get(name)
        return [] if value is None else [value]

    return _adapter.precondition_headers(getlist)


#: Writes a JSON body: ``render(data, status, content_type)`` returns the
#: response carrying it, with that status and Content-Type.
Render = Callable[[Any, int, str], HttpResponse]


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


def hsr_response(
    request: HttpRequest, document: Mapping[str, Any], cache_control: str
) -> HttpResponseBase:
    """The answer to a GET or HEAD of the HTTP Schema Resource ``document``.

    A 200 whose body is :func:`~scrudful._hsr.serialize_hsr` of the document
    (the bytes the strong ``ETag`` hashes), with the HSR's own Links and
    ``cache_control`` as ``Cache-Control``; or, for an ``If-None-Match``
    matching the ``ETag`` (weak comparison), a 304 with the ``ETag`` and
    ``Cache-Control``. The same for every adapter and every requester.
    """
    body = serialize_hsr(document)
    etag = hsr_etag(body)
    result = evaluate_preconditions(
        request.method or "GET",
        precondition_headers(request),
        etag=etag,
        last_modified=None,
        require_precondition=False,
    )
    if result is not None:
        early = conditional_response(result)
        if result.status == 304:
            early["Cache-Control"] = cache_control
        return early
    response = HttpResponse(body, content_type="application/json")
    response["Link"] = hsr_response_links()
    response["ETag"] = etag
    response["Cache-Control"] = cache_control
    _adapter.expose_headers(response)
    return response


def hsr_not_allowed() -> HttpResponseBase:
    """The 405 of an HTTP Schema Resource view to a method but GET and HEAD:
    ``Allow: GET, HEAD``, with the expose headers, and neither content nor
    ``Content-Type`` (Django's ``text/html`` would label empty content as
    a media type no HTTP Schema Resource describes)."""
    response = HttpResponseNotAllowed(["GET", "HEAD"])
    del response["Content-Type"]
    _adapter.expose_headers(response)
    return response


def no_content() -> HttpResponse:
    """A 204 without content or ``Content-Type`` (a CORS preflight's answer)."""
    response = HttpResponse(status=204)
    del response["Content-Type"]
    return response


def json_response(
    data: Any, status: int, content_type: str, render: Render | None = None
) -> HttpResponse:
    """A response with the JSON body ``data``, written by ``render`` if given,
    else by Django's ``JsonResponse``, and this status and Content-Type."""
    if render is None:
        return JsonResponse(data, status=status, content_type=content_type)
    return render(data, status, content_type)


def _plain_links(entity: Entity, request: HttpRequest) -> str | None:
    """The Link field value of ``entity``'s plain schema and context links.

    The same rels, types and defaults as the adapters' error Links; a
    relative URL resolves against ``SCRUDFUL["BASE_URL"]`` when it is set,
    as the adapters' Links and HTTP Schema Resources do, else against the
    request.

    Raises
    ------
    ImproperlyConfigured
        If ``SCRUDFUL["BASE_URL"]`` is not an absolute URL.
    """
    base = scrudful_setting("BASE_URL", None)
    if base is None:
        base = request.build_absolute_uri()
    else:
        try:
            check_base_url(base, 'SCRUDFUL["BASE_URL"]')
        except HsrConfigError as exc:
            raise ImproperlyConfigured(str(exc)) from exc
    candidates = [
        (
            entity.schema_link_or_func,
            entity.schema_rel_or_func,
            entity.schema_type_or_func,
            (_adapter.DEFAULT_SCHEMA_REL, _adapter.DEFAULT_SCHEMA_TYPE),
            None,
        )
    ]
    context_defaults = _adapter.context_defaults(entity.media_type)
    if context_defaults != (None, None):
        candidates.append(
            (
                entity.context_link_or_func,
                entity.context_rel_or_func,
                entity.context_type_or_func,
                context_defaults,
                entity.media_type,
            )
        )
    parts = []
    for url, rel, content_type, defaults_, context_of in candidates:
        if not url:
            continue
        if context_of is not None:
            content_type = _adapter.context_link_type(context_of, content_type)
        parts.append(
            link_content(
                resolve_uri(url, base),
                rel or defaults_[0],
                content_type or defaults_[1],
            )
        )
    return join_links(*parts) if parts else None


def error_handler(
    status: Literal[404, 500], error: Entity
) -> Callable[..., HttpResponse]:
    """A Django ``handler404`` / ``handler500`` that honours ``Accept``.

    The view answers with Django's HTML page when the client prefers
    ``text/html`` to the entity's media type, else with a JSON body (RFC 9457
    problem details for ``application/problem+json``, else
    ``{"detail": <reason phrase>}``) served as ``error.media_type`` with the
    entity's Links. It always sends ``Vary: Accept``. It is
    ``login_required = False``, public under Django's
    ``LoginRequiredMiddleware`` (Django exempts its error handlers anyway).

    Raises
    ------
    TypeError
        If a link of ``error`` is a callable.
    """
    if any(
        callable(value)
        for value in (
            error.schema_link_or_func,
            error.schema_rel_or_func,
            error.schema_type_or_func,
            error.context_link_or_func,
            error.context_rel_or_func,
            error.context_type_or_func,
        )
    ):
        raise TypeError("handler404/handler500 need an Entity with plain link values")

    def view(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
        if _adapter.prefers_html(request.headers.get("Accept"), error.media_type):
            if status == 404:
                response = defaults.page_not_found(
                    request, exception  # type: ignore[arg-type]
                )
            else:
                response = defaults.server_error(request)
        else:
            if _adapter.essence(error.media_type) == PROBLEM_JSON:
                body = problem_from_errors(status, None)
            else:
                body = {"detail": _adapter.reason_phrase(status)}
            response = json_response(body, status, error.media_type)
            link = _plain_links(error, request)
            if link:
                response["Link"] = link
        patch_vary_headers(response, ["Accept"])
        _adapter.expose_headers(response)
        return response

    # Public under LoginRequiredMiddleware (Django >= 5.1), as Django's own
    # error handlers are.
    view.login_required = False  # type: ignore[attr-defined]
    return view


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
