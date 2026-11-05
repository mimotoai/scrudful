"""Django request/response glue shared by the DRF and django-ninja adapters.

Private. Imports Django only (neither DRF nor django-ninja), so either adapter
can be used without the other.
"""

from __future__ import annotations

import logging
import math
import types
import warnings
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, TypedDict
from urllib.parse import urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import DisallowedHost, ImproperlyConfigured
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
    DEFAULT_HSR_CACHE_CONTROL,
    HsrConfigError,
    check_base_url,
    check_hsr_cache_control,
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
    "SETTING_ACCESSORS",
    "ScrudfulSettings",
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
    "setting_base_url",
    "setting_error",
    "setting_hsr_cache_control",
    "setting_hsr_route",
    "setting_hsr_url",
    "slice_page",
]


class ScrudfulSettings(TypedDict, total=False):
    """The shape of the optional ``SCRUDFUL`` Django setting.

    Each key's value is checked when it is read, by its accessor below
    (``ImproperlyConfigured`` when it is invalid). The per-router and
    per-viewset arguments override them.
    """

    #: The absolute URL relative link URLs resolve against
    #: (:func:`setting_base_url`).
    BASE_URL: str
    #: The API-wide error entity, of a JSON media type
    #: (:func:`setting_error`). DRF only: django-ninja accepts the key and
    #: ignores it (``add_error_links`` is its API-wide entity).
    ERROR: Entity
    #: The ``Cache-Control`` of the HTTP Schema Resource routes
    #: (:func:`setting_hsr_cache_control`).
    HSR_CACHE_CONTROL: str
    #: Whether scrudful serves the HTTP Schema Resource routes
    #: (:func:`setting_hsr_route`).
    HSR_ROUTE: bool
    #: Where the HTTP Schema Resources are served instead
    #: (:func:`setting_hsr_url`); each adapter says what it is called with.
    HSR_URL: Callable[[str], str]


logger = logging.getLogger(__name__)

#: The keys of the ``SCRUDFUL`` setting.
SETTING_KEYS = frozenset(
    ScrudfulSettings.__required_keys__ | ScrudfulSettings.__optional_keys__
)


def scrudful_setting(name: str, default: Any) -> Any:
    """``settings.SCRUDFUL[name]``, or ``default`` when unset, unchecked.

    ``SCRUDFUL`` is an optional dict of the keys :data:`SETTING_KEYS` (see
    :class:`ScrudfulSettings`); the per-router and per-viewset arguments
    override it. It is read on every call, so ``override_settings`` applies.
    The accessors below (:func:`setting_base_url` ...) read one key each and
    check its value.

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


def setting_base_url() -> str | None:
    """``SCRUDFUL["BASE_URL"]``, an absolute URL, or ``None`` when unset.

    Raises
    ------
    ImproperlyConfigured
        If it is not an absolute URL (a ``str`` with a scheme and a host).
    """
    value = scrudful_setting("BASE_URL", None)
    if value is not None:
        try:
            check_base_url(value, 'SCRUDFUL["BASE_URL"]')
        except HsrConfigError as exc:
            raise ImproperlyConfigured(str(exc)) from exc
    return value


def setting_hsr_route() -> bool:
    """``SCRUDFUL["HSR_ROUTE"]``, else ``True``.

    Raises
    ------
    ImproperlyConfigured
        If it is not a ``bool``.
    """
    value = scrudful_setting("HSR_ROUTE", True)
    if not isinstance(value, bool):
        raise ImproperlyConfigured(
            f'SCRUDFUL["HSR_ROUTE"] must be a bool, not {type(value).__name__}'
        )
    return value


def setting_hsr_url() -> Callable[[str], str] | None:
    """``SCRUDFUL["HSR_URL"]``, else ``None``.

    Raises
    ------
    ImproperlyConfigured
        If it is not callable.
    """
    value = scrudful_setting("HSR_URL", None)
    if value is not None and not callable(value):
        raise ImproperlyConfigured(
            'SCRUDFUL["HSR_URL"] must be a callable returning the HTTP Schema '
            f"Resource's URL, not {type(value).__name__}"
        )
    return value


def setting_hsr_cache_control() -> str:
    """``SCRUDFUL["HSR_CACHE_CONTROL"]``, else ``public, max-age=300``.

    Raises
    ------
    ImproperlyConfigured
        If it is not a non-empty ``str``.
    """
    value = scrudful_setting("HSR_CACHE_CONTROL", DEFAULT_HSR_CACHE_CONTROL)
    try:
        return check_hsr_cache_control(value, 'SCRUDFUL["HSR_CACHE_CONTROL"]')
    except HsrConfigError as exc:
        raise ImproperlyConfigured(str(exc)) from exc


def setting_error() -> Entity | None:
    """``SCRUDFUL["ERROR"]``, the API-wide error entity (DRF only), or ``None``.

    Raises
    ------
    ImproperlyConfigured
        If it is not an :class:`~scrudful.entity.Entity` of a JSON media type.
    """
    value = scrudful_setting("ERROR", None)
    if value is None:
        return None
    if not isinstance(value, Entity):
        raise ImproperlyConfigured(
            f'SCRUDFUL["ERROR"] must be an Entity, not {type(value).__name__}'
        )
    try:
        _adapter.check_error_media_type(value.media_type)
    except ValueError as exc:
        raise ImproperlyConfigured(f'SCRUDFUL["ERROR"]: {exc}') from exc
    return value


#: The accessor of each key of the ``SCRUDFUL`` setting (read-only): the
#: system check validates each key by calling its accessor.
SETTING_ACCESSORS: Mapping[str, Callable[[], Any]] = types.MappingProxyType(
    {
        "BASE_URL": setting_base_url,
        "ERROR": setting_error,
        "HSR_CACHE_CONTROL": setting_hsr_cache_control,
        "HSR_ROUTE": setting_hsr_route,
        "HSR_URL": setting_hsr_url,
    }
)
# A key without an accessor would be accepted but never checked: fail the
# import instead (not an assert, which -O removes).
if SETTING_ACCESSORS.keys() != SETTING_KEYS:
    raise AssertionError(
        "SETTING_ACCESSORS and ScrudfulSettings differ: "
        f"{sorted(SETTING_ACCESSORS.keys() ^ SETTING_KEYS)}"
    )


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


def _plain_links(entity: Entity, request: HttpRequest | None) -> str | None:
    """The Link field value of ``entity``'s plain schema and context links.

    The same rels, types and defaults as the adapters' error Links; a
    relative URL resolves against ``SCRUDFUL["BASE_URL"]`` when it is set,
    as the adapters' Links and HTTP Schema Resources do, else against the
    request (``build_absolute_uri()``, called only then: an absolute link
    needs no host of the request's). Without a ``request``, each URL is
    taken as it is, the settings unread: a check of the rels and types.

    Raises
    ------
    ImproperlyConfigured
        If ``SCRUDFUL["BASE_URL"]`` is not an absolute URL.
    django.core.exceptions.DisallowedHost
        If a relative link needs the request's host and ``ALLOWED_HOSTS``
        refuses it.
    """
    base = setting_base_url() if request is not None else None

    def resolve(url: Any) -> str:
        nonlocal base
        if request is None:
            return str(url)
        if base is None:
            if urlsplit(str(url)).scheme:
                return str(url)
            base = request.build_absolute_uri()
        return resolve_uri(url, base)

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
                resolve(url),
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

    ``error`` is checked here, when the handler is made: an
    :class:`~scrudful.entity.Entity` of a JSON media type (its bodies are
    JSON), with plain link values whose rels and types a Link can carry.

    It doesn't raise for a Link that depends on the settings or the request:
    Django logs the error it answers only once the handler returns, so a
    Link it can't build for one of those (an invalid ``SCRUDFUL["BASE_URL"]``;
    a relative link without a base URL, on a request whose host
    ``ALLOWED_HOSTS`` refuses) is logged, on the ``scrudful._django`` logger
    with the exception and the request's path, and the error is served
    without the entity's Links. Anything else raised building them
    propagates, to Django's own handling of an error handler that fails.

    Raises
    ------
    TypeError
        If ``error`` is not an :class:`~scrudful.entity.Entity`, or a link
        of it is a callable.
    ValueError
        If ``error``'s media type is not JSON (``application/json`` or a
        ``+json`` type), or a rel or type of its links has characters a
        Link can't carry (control characters, or outside Latin-1).
    """
    if not isinstance(error, Entity):
        raise TypeError(f"error must be an Entity, not {type(error).__name__}")
    _adapter.check_error_media_type(error.media_type)
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
    # Each Link with a URL of the same form: what depends on neither the
    # settings nor the request fails now.
    _plain_links(error, None)

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
            try:
                link = _plain_links(error, request)
            except (ImproperlyConfigured, DisallowedHost, HsrConfigError):
                # Serve the error, and keep Django's own log of what it is
                # answering, rather than fail inside the error handler.
                logger.exception(
                    "scrudful handler%s: the error entity's Links can't be "
                    "built for %s; answering without them",
                    status,
                    request.path,
                )
                link = None
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
