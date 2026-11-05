"""SCRUDful conditional requests and Link headers for FastAPI endpoints.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to FastAPI's request/response handling.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import copy
import functools
import inspect
import re
import types
import weakref
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, TypeVar, get_args, get_origin
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi import routing as fastapi_routing
from fastapi import exception_handlers as fastapi_handlers
from fastapi.datastructures import DefaultPlaceholder
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders, URLPath
from starlette.exceptions import HTTPException
from starlette.routing import BaseRoute, Match, Mount, NoMatchFound, Route, Router
from starlette.types import Receive, Scope, Send

from scrudful import _adapter
from scrudful._adapter import (
    BODY_HEADERS,
    DEFAULT_SCHEMA_REL,
    DEFAULT_SCHEMA_TYPE,
    METHOD_KEYS,
    SCHEMA_TYPE,
    allow_header,
    check_error_body_func,
    check_error_media_type,
    check_validators,
    context_defaults,
    context_link_type,
    describe_content,
    describe_read,
    describe_write,
    essence,
    evaluated_signature,
    evaluates_preconditions,
    expose_headers,
    has_default_entity,
    is_json,
    media_type_key,
    merge_links,
    options_links,
    precondition_headers,
    reason_phrase,
    resolve_annotation,
    set_validator_headers,
    validator_policy,
    with_error_body,
)
from scrudful._adapter import DEFAULT_CONTEXT_REL  # noqa: F401  (re-exported)
from scrudful._adapter import DEFAULT_CONTEXT_TYPE  # noqa: F401
from scrudful._hsr import (
    DEFAULT_HSR_CACHE_CONTROL,
    HsrConfigError,
    absolute_for_hsr,
    options_operation,
)
from scrudful.conditional import (
    ConditionalResult,
    evaluate_preconditions,
    http_timestamp,
    quote_etag,
)
from scrudful.entity import Entity
from scrudful.utils import (
    join_links,
    link_content,
    merge_expose_headers,
    resolve_uri,
)

__all__ = [
    "CONDITIONAL_READ_RESPONSES",
    "CONDITIONAL_WRITE_RESPONSES",
    "Entity",
    "add_error_links",
    "add_options",
    "http_schema_resource",
    "scrudful",
]

#: OpenAPI descriptions of the responses ``@scrudful`` can produce for GET/HEAD
#: without calling the endpoint. Pass it to the route decorator, e.g.
#: ``@app.get("/items/{item_id}", responses=CONDITIONAL_READ_RESPONSES)``, or
#: merge it with your own: ``responses={**CONDITIONAL_READ_RESPONSES, 404: ...}``.
CONDITIONAL_READ_RESPONSES: dict[int | str, dict[str, Any]] = (
    _adapter.CONDITIONAL_READ_RESPONSES  # type: ignore[assignment]
)

#: OpenAPI descriptions of the responses ``@scrudful`` can produce for
#: PUT/PATCH/DELETE without calling the endpoint, e.g.
#: ``@app.put("/items/{item_id}", responses=CONDITIONAL_WRITE_RESPONSES)``.
CONDITIONAL_WRITE_RESPONSES: dict[int | str, dict[str, Any]] = (
    _adapter.CONDITIONAL_WRITE_RESPONSES  # type: ignore[assignment]
)

Endpoint = TypeVar("Endpoint", bound=Callable[..., Any])
T = TypeVar("T")


@dataclass(frozen=True)
class _EndpointConfig:
    """The :func:`scrudful` options of a decorated endpoint.

    Carried by the wrapper as ``wrapper._scrudful``, for :func:`add_options`
    and :func:`add_error_links`.
    """

    etag_func: Callable[..., Any] | None
    last_modified_func: Callable[..., Any] | None
    schema_link_or_func: Any
    schema_rel_or_func: Any
    schema_type_or_func: Any
    context_link_or_func: Any
    context_rel_or_func: Any
    context_type_or_func: Any
    error: Entity | None
    #: The name of the endpoint's own ``Request`` parameter, if it declares one.
    request_name: str | None
    #: The names of the endpoint's own parameters: the keyword arguments its
    #: callables get inside it, of which a path parameter is passed outside.
    param_names: frozenset[str]
    #: The ``entity=`` given, whose link fields are the ones above; ``None``
    #: for the link options (an ``application/json`` entity).
    entity: Entity | None = None

    @property
    def media_type(self) -> str:
        """The media type of the endpoint's success content (its entity's)."""
        return _ENDPOINT_MEDIA_TYPE if self.entity is None else self.entity.media_type


def scrudful(
    *,
    etag_func: Callable[..., Any] | None = None,
    last_modified_func: Callable[..., Any] | None = None,
    schema_link_or_func: Any = None,
    schema_rel_or_func: Any = None,
    schema_type_or_func: Any = None,
    context_link_or_func: Any = None,
    context_rel_or_func: Any = None,
    context_type_or_func: Any = None,
    entity: Entity | None = None,
    error: Entity | None = None,
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None = None,
) -> Callable[[Endpoint], Endpoint]:
    """Decorate a FastAPI endpoint to make it SCRUDful.

    The decorator evaluates the RFC 9110 conditional request headers
    (If-Match, If-None-Match, If-Modified-Since, If-Unmodified-Since) before the
    endpoint runs, and adds ``ETag``, ``Last-Modified``, ``Link`` and
    ``Access-Control-Expose-Headers`` response headers. All arguments are
    keyword-only.

    ``@scrudful(...)`` must sit *below* the route decorator::

        @app.get("/items/{item_id}", responses=CONDITIONAL_READ_RESPONSES)
        @scrudful(etag_func=item_etag, last_modified_func=item_modified)
        async def get_item(item_id: int): ...

    Placed above it, FastAPI registers the undecorated endpoint and the
    decorator has no effect (FastAPI cannot detect this).

    Callbacks
        Inside the endpoint, for the request it serves, every ``*_func`` /
        ``*_or_func`` callable (of the endpoint and of its ``error``) is
        called with the keyword arguments FastAPI resolved for the endpoint:
        path, query, body and dependency values, plus the endpoint's own
        ``Request`` / ``Response`` parameters if it declares any (a link
        callback must not depend on them, see below). Accept ``**kwargs`` to ignore
        the ones you don't need. Callbacks may be ``def`` (run in the
        threadpool) or ``async def``; an awaitable result is awaited.

        Outside the endpoint, the link callables (schema and context URL and
        type, of the endpoint and of its ``error``) are also called in
        *describe* mode, ``func()`` with no arguments, to build the path's
        HTTP Schema Resource (see :func:`add_options`), which is the same
        for every requester. They must return the same target in both
        modes, or the description disagrees with the responses: a link
        depending on the request or a path parameter can't be described.
        :func:`add_options` checks, when it is called, that each can be
        called with no arguments, and raises ``ValueError`` otherwise; give
        the parameters it takes inside the endpoint defaults, or accept only
        ``**kwargs``.

        Those of ``error`` are also called by :func:`add_error_links`'s
        exception handlers. There they get only the URL's path parameters
        that the endpoint takes (not those of an ``include_router`` prefix
        it doesn't declare, which FastAPI doesn't pass it either) as
        Starlette matched them (strings, unless the path uses a convertor
        such as ``{id:int}``), plus the request under the name of the
        endpoint's ``Request`` parameter when it declares one: no body,
        query or dependency values. :func:`add_error_links` checks, when it
        is called, that each can be called that way, and raises
        ``TypeError`` otherwise.

        The validator callbacks run before the endpoint, so before any lookup
        or authorization check its body makes: they must apply the same
        scoping (e.g. the current user from a dependency). Otherwise a
        resource the endpoint would hide reveals its validators (304) and its
        existence (412/428 instead of 404).

    Validators
        Without validator callbacks no preconditions are evaluated: the If-*
        headers are ignored and the route behaves like an undecorated one
        (apart from the Link and expose headers). A
        validator callback returning ``None`` means the target resource has
        no current representation. When both validators are ``None`` the
        preconditions are skipped (except for PUT) and the endpoint decides,
        e.g. by raising ``HTTPException(404)``.

        When either validator callback is configured, PUT, PATCH and DELETE on
        an existing resource must be conditional: a request without If-Match
        (or a valid If-Unmodified-Since, when ``last_modified_func`` is set, or
        ``If-None-Match: *``, which then fails with 412) is answered with 428
        Precondition Required. A malformed If-Match or If-None-Match is
        answered with 400, a failed precondition with 412 and a matching
        GET/HEAD with 304 carrying the validators. These responses are JSON
        responses (304 without a body) returned without calling the endpoint,
        so ``response_model`` does not apply to them; describe them in the
        route's OpenAPI ``responses=`` with :data:`CONDITIONAL_READ_RESPONSES`
        and :data:`CONDITIONAL_WRITE_RESPONSES`. With ``error``, the 400, 412
        and 428 carry its Links (not the endpoint's) and are served with its
        ``media_type`` as their Content-Type; without it they carry no Link
        and are ``application/json``. Headers dependencies set on the
        injected ``Response`` are kept: all of them on a 304 (except
        ``Content-*`` metadata), only ``Set-Cookie`` on the others (a Link a
        dependency set describes the success representation).

        The preconditions are evaluated before the endpoint runs, so two
        concurrent requests carrying the same If-Match can both pass. Strict
        lost-update protection needs an atomic re-check in the endpoint's
        storage layer (e.g. ``UPDATE ... WHERE version = :expected`` or
        ``SELECT ... FOR UPDATE`` inside a transaction).

        After the endpoint runs, 2xx responses to GET/HEAD get the validators
        computed for the precondition check, and 2xx responses to PUT/PATCH get
        freshly computed ones (the callbacks are called again, RFC 9110
        §9.3.4). Other methods and non-2xx responses get none. Headers the
        endpoint set itself are never overwritten.

    The Link header is added to the responses whose content the endpoint's
    links describe: the 2xx but 204 it produces, to any method but TRACE
    (whose content is the echoed request; see
    :func:`scrudful._adapter.has_default_entity`). Another response it
    returns (an error, a 204, a redirect) gets no Links from scrudful, and
    keeps any Link the endpoint set. Raise an error (``HTTPException``)
    rather than return it to give it Links, through :func:`add_error_links`.
    The Link header is appended to any Link value the endpoint (or a
    dependency) set; a Link it set with the relation of the schema link
    (``describedby`` by default) replaces that link, and one with either
    context relation (JSON-LD or ``rdf-context``, one slot) the context link
    (see :func:`scrudful._adapter.merge_links`). Link
    URLs are resolved against the ``base_url`` of the :func:`add_options`
    call covering the endpoint, as in its HTTP Schema Resource, else against
    the request's URL (``str(request.url)``), so a relative value such as
    ``"/schemas/item"`` is sent absolute.
    ``Access-Control-Expose-Headers`` is extended with whichever of
    ``ETag``, ``Link`` and ``Location`` the response carries. Starlette's
    ``CORSMiddleware`` replaces that header on cross-origin requests, so if
    you use it, list these names in its ``expose_headers`` instead.

    If the endpoint returns a Starlette ``Response`` the headers are added to
    it, otherwise to the response FastAPI builds. ``HTTPException`` raised by
    the endpoint propagates unchanged; :func:`add_error_links` gives the
    app's error responses Links (this endpoint's ``error`` ones, when it has
    one), and :func:`add_options` answers OPTIONS with the path's HTTP Schema
    Resource. FastAPI does not route HEAD to a ``@app.get`` endpoint (it
    answers 405); use ``@app.api_route(..., methods=["GET", "HEAD"])`` to
    serve HEAD, or stack ``@app.get(...)`` and ``@app.head(...)`` above
    ``@scrudful(...)``: FastAPI gives both methods of one ``api_route`` the
    same OpenAPI operation id, and warns about the duplicate.

    Parameters
    ----------
    etag_func : callable, optional
        Returns the entity-tag of the current representation, or ``None`` if
        there is none. ``str()`` of the result is used: a valid entity-tag
        (``'"abc"'`` or ``'W/"abc"'``) is sent unchanged, anything else is
        wrapped in double quotes (a strong entity-tag). Values that cannot be an
        entity-tag (containing a double quote, space, control character or a
        character above U+00FF) raise ``ValueError``. No library version is
        appended. Weak entity-tags are allowed, but If-Match uses the strong
        comparison, which they never pass: only If-Unmodified-Since (or
        ``If-Match: *``) can then make a write succeed.
    last_modified_func : callable, optional
        Returns the modification time of the current representation as a
        ``datetime`` (naive values are taken to be UTC) or an epoch timestamp,
        or ``None`` if there is none. Sub-second precision is discarded.
    schema_link_or_func : str or callable, optional
        URL of the schema of the response body. If no value is given, no
        schema link is sent.
    schema_rel_or_func : str or callable, optional
        ``rel`` of the schema link. Defaults to ``"describedby"``.
    schema_type_or_func : str or callable, optional
        ``type`` of the schema link. Defaults to ``"application/schema+json"``.
    context_link_or_func : str or callable, optional
        URL of the JSON-LD context of the response body. If no value is given,
        no context link is sent.
    context_rel_or_func : str or callable, optional
        ``rel`` of the context link. Defaults to
        ``"http://www.w3.org/ns/json-ld#context"``.
    context_type_or_func : str or callable, optional
        ``type`` of the context link. Defaults to ``"application/ld+json"``.
    entity : Entity, optional
        The schema and context of the success content, and its media type,
        in place of the link options above (giving both is a
        ``TypeError``). The context link's defaults then follow its media
        type (e.g. the ``rdf-context`` relation for ``text/csv``), and the
        HTTP Schema Resource describes the content under that media type.
        It is declarative: the endpoint must serve that media type (e.g.
        return ``Response(..., media_type="text/csv")``). Without it, the
        content is ``application/json`` with the link options' links.
    error : Entity, optional
        The schema and context of scrudful's own 400, 412 and 428 bodies.
        Its link callables get the endpoint's keyword arguments, like the
        other callbacks. Its ``media_type`` must be JSON content
        (``application/json`` or ``+json``, e.g.
        ``application/problem+json``), since those bodies are JSON objects.
        With :func:`add_error_links`, the errors the app's exception handlers
        answer for this endpoint get it too, and the HTTP Schema Resource
        describes it; its link callables are then called outside the
        endpoint (see "Callbacks").
    error_body_func : callable, optional
        ``error_body_func(request, result)`` returns the body (a ``dict``) of
        scrudful's own 400, 412 or 428 response in place of
        ``{"detail": ...}``; ``request`` is the Starlette ``Request`` and
        ``result`` the :class:`~scrudful.conditional.ConditionalResult`,
        whose default body is ``result.body``. It must be a plain (sync)
        function. The status, headers, Links and Content-Type are unchanged,
        and it is never called for a 304.
        :func:`scrudful.problem.problem_details` returns RFC 9457 problem
        details (pair it with ``error=Entity(media_type=PROBLEM_JSON)``).
        Without it the bodies are as before.

    Raises
    ------
    TypeError
        If a validator callback isn't callable, ``entity`` or ``error`` isn't
        an :class:`Entity`, or ``entity`` is given with a link option.
    ValueError
        If ``error``'s media type is not JSON content.
    """
    check_validators(etag_func, last_modified_func)
    links_given = dict(
        schema_link_or_func=schema_link_or_func,
        schema_rel_or_func=schema_rel_or_func,
        schema_type_or_func=schema_type_or_func,
        context_link_or_func=context_link_or_func,
        context_rel_or_func=context_rel_or_func,
        context_type_or_func=context_type_or_func,
    )
    _check_entity(entity, links_given)
    _check_error(error)
    check_error_body_func(error_body_func)
    has_validators = etag_func is not None or last_modified_func is not None
    # The endpoint's links: those of ``entity``, else the link options (an
    # ``application/json`` entity).
    link_fields = (
        links_given
        if entity is None
        else {name: getattr(entity, name) for name in links_given}
    )
    source = types.SimpleNamespace(**link_fields)
    media_type = _ENDPOINT_MEDIA_TYPE if entity is None else entity.media_type

    async def validators(kwargs: Mapping[str, Any]) -> tuple[str | None, int | None]:
        funcs = [f for f in (etag_func, last_modified_func) if f is not None]
        values = iter(await _call_all(funcs, kwargs))
        etag = last_modified = None
        if etag_func is not None:
            value = next(values)
            etag = None if value is None else quote_etag(value)
        if last_modified_func is not None:
            last_modified = http_timestamp(next(values))
        return etag, last_modified

    async def links(
        kwargs: Mapping[str, Any], base: str
    ) -> tuple[str | None, str | None]:
        return await _source_links(source, media_type, kwargs, base)

    async def early_response(
        request: Request,
        result: ConditionalResult,
        injected: Headers,
        kwargs: Mapping[str, Any],
        base: str,
    ) -> Response:
        result = with_error_body(error_body_func, request, result)
        if result.body is None or error is None:
            return _conditional_response(result, injected)
        link = join_links(*await _entity_links(error, kwargs, base))
        return _conditional_response(result, injected, link, error.media_type)

    def decorator(endpoint: Endpoint) -> Endpoint:
        if inspect.isgeneratorfunction(endpoint) or inspect.isasyncgenfunction(
            endpoint
        ):
            raise TypeError("@scrudful does not support generator endpoints")
        signature, request_name, pass_request, response_name, pass_response = (
            _wrapper_signature(endpoint)
        )
        config = _EndpointConfig(
            etag_func=etag_func,
            last_modified_func=last_modified_func,
            **link_fields,
            error=error,
            request_name=request_name if pass_request else None,
            param_names=_param_names(endpoint),
            entity=entity,
        )

        @functools.wraps(endpoint)
        async def wrapper(**kwargs: Any) -> Any:
            request: Request = (
                kwargs[request_name] if pass_request else kwargs.pop(request_name)
            )
            response: Response = (
                kwargs[response_name] if pass_response else kwargs.pop(response_name)
            )
            method = request.method.upper()
            base = _live_base(request, wrapper)

            if has_validators and evaluates_preconditions(method):
                etag, last_modified = await validators(kwargs)
                result = evaluate_preconditions(
                    method,
                    precondition_headers(request.headers.getlist),
                    etag=etag,
                    last_modified=last_modified,
                    require_precondition=True,
                )
                if result is not None:
                    return await early_response(
                        request, result, response.headers, kwargs, base
                    )

            content = await _call(endpoint, kwargs)

            if isinstance(content, Response):
                headers, status = content.headers, content.status_code
            else:
                headers, status = response.headers, _status_code(request, response)
            if has_validators:
                refresh, send = validator_policy(method, status)
                if refresh:
                    etag, last_modified = await validators(kwargs)
                if send:
                    set_validator_headers(headers, etag, last_modified)
            if has_default_entity(method, status):
                # Another response the endpoint returns (an error, a 204, a
                # redirect, or a TRACE) is not described by its links: it
                # gets none.
                link = merge_links(
                    ", ".join(headers.getlist("link")), *await links(kwargs, base)
                )
                if link is not None:
                    headers["Link"] = link
            _join_expose_lines(headers)
            expose_headers(headers)
            return content

        wrapper.__signature__ = signature  # type: ignore[attr-defined]
        wrapper._scrudful = config  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator


def add_error_links(app: FastAPI, error: Entity) -> None:
    """Give the error responses of ``app``'s exception handlers ``error``'s Links.

    Wraps the handlers ``app`` uses, when this is called, for
    ``starlette.exceptions.HTTPException`` and
    ``fastapi.exceptions.RequestValidationError`` (FastAPI's defaults are
    ``fastapi.exception_handlers.http_exception_handler`` and
    ``request_validation_exception_handler``), and those registered for
    ``fastapi.HTTPException`` or for a status code (other than 500, which is
    Starlette's server error handler): the errors the endpoints raise, the
    422 of request validation, and the 404 and 405 Starlette's routing
    raises as ``HTTPException`` for an unknown path or an unrouted method.
    A handler registered for another class is not wrapped.

    Each wrapped handler returns the response the original makes, with the
    same status, headers and body, and when it has JSON content (an
    ``application/json`` or ``+json`` Content-Type) adds ``error``'s Links to
    it, as :func:`scrudful` adds them (a Link the handler set with the
    relation of one of them suppresses it; see
    :func:`scrudful._adapter.merge_links`), and serves it as
    ``error.media_type``. Responses without content (e.g. a 304) or with
    other content are left unchanged. Link URLs are resolved against the
    ``base_url`` of :func:`add_options`, else the request's URL.

    The media type is relabelled whatever the handler chose: a custom
    handler's JSON response (``application/json``, or another ``+json``
    type) is served as ``error.media_type``, so the bodies of every wrapped
    handler must be content that ``error`` describes.

    The entity is ``error``, unless the request was routed to a
    :func:`scrudful` endpoint with an ``error`` of its own that serves the
    request's method: then the errors raised for it (by the endpoint, its
    dependencies or FastAPI's request validation) get that endpoint's
    ``error``, so that the Links of an error the endpoint raises (e.g.
    ``HTTPException(400)``, a dependency's 401, request validation's 422)
    agree with the ``default`` entry of the HTTP Schema Resource. A 405,
    which no endpoint serves, gets ``error``. The HTTP Schema Resources
    describe ``error`` as the content of those errors.

    So a custom handler registered before the call is kept, and calling this
    again replaces the entity of the earlier call: no Link is added twice. A
    handler registered after the call replaces the wrapped one; call this
    after registering your handlers and before the app serves requests (its
    middleware stack, holding the handlers, is built on the first request).

    scrudful's own 400, 412 and 428 are returned by the endpoint, not raised:
    they get the ``error`` of :func:`scrudful`, not this one.

    Parameters
    ----------
    app : FastAPI
        The application whose exception handlers are wrapped.
    error : Entity
        The schema and context of the error bodies. Its link callables are
        called with ``request=<Request>`` only; they may be ``def`` (run in
        the threadpool) or ``async def``. An endpoint's own ``error`` is
        called outside the endpoint: with the URL's path parameters it
        takes, plus the request under the name of the endpoint's ``Request``
        parameter if it declares one (see "Callbacks" in :func:`scrudful`).
        In the HTTP Schema Resource, both are called as ``func()`` (see
        :func:`add_options`). Both are checked for the handlers' call when
        this is called, against the endpoints the app has then: those of included
        routers and under any ``Mount`` or ``Host``, with or without
        middleware, since this app's handlers serve them, but not those of a
        mounted Starlette or FastAPI app, which has its own handlers: call
        this on that app too.

    Raises
    ------
    TypeError
        If ``app`` isn't a ``FastAPI`` application or ``error`` isn't an
        :class:`Entity`, or a link callable of ``error`` (or of an
        endpoint's ``error``) can't be called with the keyword arguments
        it gets here.
    ValueError
        If ``error``'s media type is not JSON content (``application/json``
        or ``+json``, other than JSON-LD).
    RuntimeError
        If ``app`` has already served a request: its exception handlers are
        then fixed, and wrapping them would have no effect.
    """
    if not isinstance(app, FastAPI):
        raise TypeError(f"app must be a FastAPI application, not {type(app).__name__}")
    if not isinstance(error, Entity):
        raise TypeError(f"error must be an Entity, not {type(error).__name__}")
    check_error_media_type(error.media_type)
    if getattr(app, "middleware_stack", None) is not None:
        raise RuntimeError(
            "add_error_links must be called before the app serves requests: its "
            "middleware stack, which holds the exception handlers, is already built"
        )
    _check_error_callables(app.router.routes, error)
    # For the default error entries of the HTTP Schema Resources.
    _APP_ERRORS[app] = error
    handlers = app.exception_handlers
    keys: list[Any] = [
        key
        for key in handlers
        if (isinstance(key, int) and key != 500) or key is FastAPIHTTPException
    ]
    defaults = {
        HTTPException: fastapi_handlers.http_exception_handler,
        RequestValidationError: fastapi_handlers.request_validation_exception_handler,
    }
    for exc_class, default in defaults.items():
        handlers.setdefault(exc_class, default)
        keys.append(exc_class)
    for key in keys:
        handler = handlers[key]
        # Calling this again replaces the earlier entity.
        handler = getattr(handler, "_scrudful_error_handler", handler)
        app.add_exception_handler(key, _error_handler(handler, error))


def _error_handler(
    handler: Callable[[Request, Exception], Any], error: Entity
) -> Callable[[Request, Exception], Any]:
    """``handler``, an exception handler of the app, adding ``error``'s Links."""

    @functools.wraps(handler)
    async def scrudful_handler(request: Request, exc: Exception) -> Any:
        response = await _call_handler(handler, request, exc)
        if (
            request.scope.get("type") != "http"
            or not isinstance(response, Response)
            or not _has_json_content(response)
        ):
            return response
        headers = response.headers
        route = request.scope.get("route")
        base = _live_base(request, getattr(route, "endpoint", None))
        entity, kwargs = _raised_entity(request, error)
        link = merge_links(
            ", ".join(headers.getlist("link")),
            *await _entity_links(entity, kwargs, base),
        )
        if link is not None:
            headers["Link"] = link
        headers["Content-Type"] = entity.media_type
        _join_expose_lines(headers)
        expose_headers(headers)
        return response

    scrudful_handler._scrudful_error_handler = handler  # type: ignore[attr-defined]
    return scrudful_handler


def _live_base(request: Request, endpoint: Any = None) -> str:
    """The URL the relative link URLs of a response resolve against.

    The ``base_url`` of the :func:`add_options` call covering the request: the
    app's, else that of the router whose ``endpoint`` serves it; else the
    request's URL.
    """
    options = getattr(request.scope.get("app"), _OPTIONS_ATTRIBUTE, None)
    if not isinstance(options, _HsrOptions) or not options.explicit:
        router_ref = None
        if endpoint is not None:
            with contextlib.suppress(TypeError):  # not weakly referenceable
                router_ref = _ROUTER_ENDPOINTS.get(endpoint)
        router = None if router_ref is None else router_ref()
        options = getattr(router, _OPTIONS_ATTRIBUTE, None)
    if isinstance(options, _HsrOptions) and options.explicit and options.base_url:
        return options.base_url
    return str(request.url)


def _raised_entity(request: Request, error: Entity) -> tuple[Entity, dict[str, Any]]:
    """The entity of an error the app's handlers answer, and its callables' kwargs.

    The ``error`` of the :func:`scrudful` endpoint the request was routed to,
    when it has one and serves the request's method (Starlette records a
    route matched by path alone, whose 405 it raises, too), called with the
    path parameters (see :func:`_description_kwargs`); else the app-wide
    ``error``, called with the request only.
    """
    route = request.scope.get("route")
    config = getattr(getattr(route, "endpoint", None), "_scrudful", None)
    methods = getattr(route, "methods", None)
    if (
        isinstance(config, _EndpointConfig)
        and config.error is not None
        and (methods is None or request.method in methods)
    ):
        return config.error, _description_kwargs(config, request)
    return error, {"request": request}


async def _call_handler(
    handler: Callable[[Request, Exception], Any], request: Request, exc: Exception
) -> Any:
    """Call an exception handler as Starlette does (sync ones in the threadpool)."""
    if inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)
    ):
        return await handler(request, exc)
    result = await run_in_threadpool(handler, request, exc)
    if inspect.isawaitable(result):
        result = await result
    return result


def _has_json_content(response: Response) -> bool:
    """Whether ``response`` has content of a JSON media type.

    A response without a ``body`` (e.g. a ``StreamingResponse``) is taken to
    have content.
    """
    content_type = response.headers.get("content-type")
    if content_type is None or not is_json(content_type):
        return False
    return getattr(response, "body", None) != b""


# --- OPTIONS and the HTTP Schema Resource ---------------------------------------

#: The media type of an endpoint's own content without ``entity=`` (DRF's).
_ENDPOINT_MEDIA_TYPE = "application/json"
#: The fields of an endpoint or entity whose callables the HTTP Schema Resource
#: calls (the rels are not needed there).
_LINK_FIELDS = (
    "schema_link_or_func",
    "schema_type_or_func",
    "context_link_or_func",
    "context_type_or_func",
)
#: What an endpoint's callables get outside the endpoint, in
#: :func:`add_error_links`'s handlers.
_OUTSIDE = (
    "the URL's path parameters the endpoint takes, and the request under the "
    "name of the endpoint's Request parameter if it declares one"
)
#: The fields of an entity whose callables :func:`add_error_links`'s handlers
#: call (via :func:`_entity_links`).
_ERROR_LINK_FIELDS = (*_LINK_FIELDS, "schema_rel_or_func", "context_rel_or_func")
#: Where the OPTIONS route keeps the path template it matched, in the scope.
_SERVED_KEY = "scrudful.options"
#: The description of each method's ``default`` response entry.
_DEFAULT_ERROR = "Error."
#: The descriptions of the status ranges a route's ``responses=`` may declare.
_RANGE_TEXT = {
    "1": "Informational",
    "2": "Success",
    "3": "Redirection",
    "4": "Client Error",
    "5": "Server Error",
}
_STATUS_KEY = re.compile(r"[1-5](?:[0-9]{2}|XX)")

#: FastAPI's flattening of included routers into their routes, as its OpenAPI
#: generation walks them (FastAPI versions that include routers lazily); older
#: versions copy an included router's routes, so ``routes`` is flat already.
_iter_route_contexts = getattr(fastapi_routing, "iter_route_contexts", None)


@dataclass
class _HsrOptions:
    """The :func:`add_options` options of an app or router, and its built
    HTTP Schema Resources."""

    base_url: str | None = None
    hsr_route: bool = True
    hsr_url: Callable[[str], str] | None = None
    hsr_cache_control: str = DEFAULT_HSR_CACHE_CONTROL
    #: ``allowed(request, method, obj)`` (Task 16).
    allowed: Callable[..., Any] | None = None
    #: ``load(request, **path_params)`` (Task 16).
    load: Callable[..., Any] | None = None
    #: Whether :func:`add_options` gave them (else they are the defaults, for
    #: :func:`http_schema_resource` on an app without it).
    explicit: bool = False
    #: Each built document, by path template, with what it was built from:
    #: the routes of the path (compared by identity) and the error entity of
    #: :func:`add_error_links`.
    built: dict[str, tuple[tuple[Any, ...], Entity | None, dict[str, Any]]] = field(
        default_factory=dict
    )


#: The attribute of an app or router holding its :class:`_HsrOptions` (an
#: ``APIRouter`` is not hashable on every FastAPI version).
_OPTIONS_ATTRIBUTE = "_scrudful_hsr_options"
#: The entity :func:`add_error_links` gave each app.
_APP_ERRORS: weakref.WeakKeyDictionary[FastAPI, Entity] = weakref.WeakKeyDictionary()
#: The router whose :func:`add_options` covers each scrudful endpoint of it,
#: for the base URL of the endpoint's live Links (see :func:`_live_base`).
_ROUTER_ENDPOINTS: weakref.WeakKeyDictionary[Any, weakref.ref[APIRouter]] = (
    weakref.WeakKeyDictionary()
)


def add_options(
    app: FastAPI | APIRouter,
    *,
    base_url: str | None = None,
    hsr_route: bool = True,
    hsr_url: Callable[[str], str] | None = None,
    hsr_cache_control: str | None = None,
    allowed: Callable[..., Any] | None = None,
    load: Callable[..., Any] | None = None,
) -> None:
    """Answer OPTIONS on ``app``'s scrudful paths with their HTTP Schema Resource.

    Each path template (the route's path string, e.g. ``"/items/{item_id}"``)
    that a :func:`scrudful` endpoint serves with some method has an HTTP
    Schema Resource (Semantic HTTP 0.1, an OpenAPI 3.1 Path Item), the same
    for every requester: :func:`http_schema_resource` returns it. An OPTIONS
    request to a URL of such a path, which no other route serves OPTIONS
    for, is answered with 200 and that document as ``application/json``, the
    Links of its published JSON Schema and JSON-LD context
    (:func:`scrudful._adapter.options_links`), an ``Allow`` listing the
    document's methods and ``Access-Control-Expose-Headers``. Other OPTIONS
    requests are routed as before (e.g. a 405, or your own OPTIONS route).
    The OPTIONS answer has no dependencies (neither the app's, a router's
    nor an ``include_router`` call's), so it needs no credentials, and is not
    in the OpenAPI document.

    Given a ``FastAPI`` app, the paths are the full path strings of its
    routes: those of the routers included in it (with their prefixes) and
    those under a ``Mount(routes=...)`` (with the Mount's path). A URL is
    answered with the document of the first route whose path matches it, as
    Starlette routes it. Routes with the same path string, on the app or on
    several routers, are one resource: its document describes, for each
    method, the first of them that serves it. Overlapping templates are
    separate resources: ``/items/new`` beside ``/items/{id}``, and
    ``/items/{id}`` beside ``/items/{item_id}`` (on another router), each
    have their own document and OPTIONS answer. Given an ``APIRouter``, it
    adds an OPTIONS route to each of the router's own paths that has a
    scrudful route, answered with the router's document of that path (keyed
    by the router's path strings); call it before including the router
    (older FastAPI versions copy a router's routes when it is included).
    Prefer calling it on the app.

    A mounted Starlette or FastAPI app, a ``Mount`` with ``middleware`` and
    a ``Router`` with ``middleware`` are left alone: answering for them from
    the outer app would skip their middleware (e.g. a ``CORSMiddleware``).
    Call this on a mounted app itself, whose OPTIONS answer then runs inside
    its middleware (its paths are those of the mounted app); the paths under
    a middleware-wrapped ``Mount`` or ``Router`` get no automatic OPTIONS.

    The document lists the methods in the order ``get``, ``put``, ``post``,
    ``delete``, ``options``, ``head``, ``patch``, ``trace``:

    - a scrudful GET: its success status (``status_code=``, else the
      response class's default, 200 for JSON) with the content of the
      endpoint's ``entity`` (by default ``application/json``, from the link
      options: a ``schema`` ``$ref`` and an ``x-rdf-context``), and, when it
      has validator callbacks, scrudful's 304, 400 and 412, the 400 and 412
      with the content of its ``error``;
    - a scrudful HEAD: the same responses, without content;
    - a scrudful TRACE: its success status, without content: a TRACE's
      content is the echoed request, which the endpoint's links don't
      describe, and it gets none of them;
    - a scrudful PUT, PATCH, DELETE or POST: the request body (the
      endpoint's content, when the endpoint takes a body; DELETE has none),
      the success status with that content (but a 204), and, when
      conditional, scrudful's 400, 412 and (but for POST) 428 with the
      ``error`` content and the precondition header parameters;
    - ``options``: ``{"responses": {"204": {"description": "No Content"}}}``;
    - a method served by a route that isn't scrudful: its success status,
      without content or a request body.

    Each method also lists the statuses its route declares in
    ``responses=`` (a status code, or a range such as ``"4XX"``), with the
    declared ``description`` (else the reason phrase): a success status
    (2xx but 204) with the endpoint's content, under its media type and
    every other media type the declared ``content`` lists (described by the
    same schema and context), an error status (4xx, 5xx) with the content
    of the error entity its raised errors get (below), any other status
    without content. A status scrudful describes itself (the success status,
    and scrudful's 304, 400, 412 and 428 when conditional) keeps scrudful's
    entry. A status set only at run time (``response.status_code = 201``)
    can't be known: declare it with ``status_code=`` or ``responses=``.

    Every method has a ``default`` entry ("Error.") with the content of the
    entity of the errors raised for it: those :func:`add_error_links`
    answers, with the endpoint's ``error`` when it has one, else the app's
    entity. FastAPI's own errors for a routed request (a dependency's 401
    or 403, a raised 404, request validation's 422) go through those
    handlers with that same entity, so ``default`` describes them. Without
    :func:`add_error_links`, the raised errors carry no Links, and
    ``default`` has no content.

    The document is built without a request, the first time it is needed
    (an OPTIONS request, or :func:`http_schema_resource`), and kept: it is
    built again only when the routes of its path or the app's
    :func:`add_error_links` entity change. Link callables are called in
    *describe* mode, ``func()``, with no arguments, even those that take the
    endpoint's keyword arguments inside it: give such parameters defaults
    (or accept ``**kwargs``). A callable must return the same target in both
    modes, or the document disagrees with the responses it describes. Async
    callables are awaited. Validator callbacks are not called. Relative URLs
    are resolved against ``base_url``; the live Links of the responses
    resolve against it too (else against the request's URL).

    Call it once the routes are added (the endpoints added later are
    described, but their callables are not checked); calling it again adds
    no route, and replaces the options. CORS preflight requests are meant to
    be answered by ``CORSMiddleware`` before routing; one that reaches this
    answer gets the description like any other OPTIONS.

    Parameters
    ----------
    app : FastAPI or APIRouter
        The application or router whose scrudful paths answer OPTIONS.
    base_url : str, optional
        The absolute URL relative link URLs resolve against, in the HTTP
        Schema Resource and on the Link headers of the responses of the
        paths covered. Without it, a relative URL in the document raises
        ``ValueError`` when the document is built.
    hsr_route : bool, optional
        Whether to serve each path's document by GET (default ``True``).
    hsr_url : callable, optional
        ``hsr_url(path)``: the URL the document of ``path`` is served at,
        when it is mounted elsewhere.
    hsr_cache_control : str, optional
        The ``Cache-Control`` of the document's GET response (default
        ``"public, max-age=300"``).
    allowed : callable, optional
        ``allowed(request, method, obj)``: whether the requester may use
        ``method``, for ``Allow``.
    load : callable, optional
        ``load(request, **path_params)``: the object at the URL, for
        ``allowed``.

    Raises
    ------
    TypeError
        If ``app`` is neither a ``FastAPI`` application nor an ``APIRouter``,
        or an option has the wrong type.
    ValueError
        If ``base_url`` is not an absolute URL, or a link callable of a
        scrudful endpoint (or of its ``error``) can't be called with no
        arguments.
    """
    _check_owner(app)
    options = _check_hsr_options(
        base_url, hsr_route, hsr_url, hsr_cache_control, allowed, load
    )
    routes = app.router.routes if isinstance(app, FastAPI) else app.routes
    _check_options_callables(routes)
    setattr(app, _OPTIONS_ATTRIBUTE, options)
    if isinstance(app, FastAPI):
        if not any(isinstance(route, _OptionsRoute) for route in routes):
            # First, so that it sees every OPTIONS request, including those
            # under a Mount; it matches only those it answers.
            routes.insert(0, _OptionsRoute(app))
    else:
        _add_path_options(app)
        for endpoint, _, _ in _endpoints(routes, for_options=True):
            with contextlib.suppress(TypeError):  # not weakly referenceable
                _ROUTER_ENDPOINTS[endpoint] = weakref.ref(app)


def _check_hsr_options(
    base_url: Any,
    hsr_route: Any,
    hsr_url: Any,
    hsr_cache_control: Any,
    allowed: Any,
    load: Any,
) -> _HsrOptions:
    """The :func:`add_options` options, checked."""
    for name, value, kind in (
        ("base_url", base_url, str),
        ("hsr_cache_control", hsr_cache_control, str),
    ):
        if value is not None and not isinstance(value, kind):
            raise TypeError(f"{name} must be a str, not {type(value).__name__}")
    if not isinstance(hsr_route, bool):
        raise TypeError(f"hsr_route must be a bool, not {type(hsr_route).__name__}")
    for name, value in (("hsr_url", hsr_url), ("allowed", allowed), ("load", load)):
        if value is not None and not callable(value):
            raise TypeError(f"{name} must be callable, not {type(value).__name__}")
    if base_url is not None:
        parts = urlsplit(base_url)
        if not (parts.scheme and parts.netloc):
            raise ValueError(
                f"base_url must be an absolute URL (with a scheme and a host), "
                f"not {base_url!r}"
            )
    return _HsrOptions(
        base_url=base_url,
        hsr_route=hsr_route,
        hsr_url=hsr_url,
        hsr_cache_control=(
            hsr_cache_control
            if hsr_cache_control is not None
            else DEFAULT_HSR_CACHE_CONTROL
        ),
        allowed=allowed,
        load=load,
        explicit=True,
    )


def _hsr_options(owner: FastAPI | APIRouter) -> _HsrOptions:
    """The :func:`add_options` options of ``owner``, else (kept) defaults."""
    options = getattr(owner, _OPTIONS_ATTRIBUTE, None)
    if not isinstance(options, _HsrOptions):
        options = _HsrOptions()
        setattr(owner, _OPTIONS_ATTRIBUTE, options)
    return options


def http_schema_resource(app: FastAPI | APIRouter, path: str) -> dict[str, Any]:
    """The HTTP Schema Resource (an OpenAPI 3.1 Path Item) of ``path``.

    ``path`` is the full path string of the routes, as :func:`add_options`
    keys them (e.g. ``"/v1/items/{item_id}"`` for a router included with
    ``prefix="/v1"``; for an ``APIRouter``, the router's own path). The
    document is the same for every requester: it is built without a
    request, once (see :func:`add_options`, which describes its content and
    whose ``base_url`` it uses), and a copy is returned.

    Link callables are called in *describe* mode, ``func()``: with no
    arguments. They must return the same target as on the responses they
    describe, or the description disagrees with the responses. An async
    one is awaited (in a worker thread when this is called inside a running
    event loop).

    Raises
    ------
    TypeError
        If ``app`` is neither a ``FastAPI`` application nor an ``APIRouter``.
    ValueError
        If no scrudful endpoint serves ``path``; a link URL is relative and
        there is no base URL; a link callable raises when called as
        ``func()``; or a router is included in apps that
        :func:`add_error_links` gave different entities.
    """
    _check_owner(app)
    return copy.deepcopy(_run_describing(_hsr_flow(app, path)))


def _check_owner(app: Any) -> None:
    if not isinstance(app, (FastAPI, APIRouter)):
        raise TypeError(
            "app must be a FastAPI application or an APIRouter, "
            f"not {type(app).__name__}"
        )


#: A describe-mode build: it yields each link callable to call (with the
#: entry it is for), and is sent back what it returns.
_Flow = Generator[tuple[Callable[[], Any], str], Any, T]


def _hsr_flow(owner: FastAPI | APIRouter, path: str) -> _Flow[dict[str, Any]]:
    """The HTTP Schema Resource of ``path`` on ``owner`` (not a copy): the
    one built, unless the routes of ``path`` or the error entity changed."""
    options = _hsr_options(owner)
    paths = _scrudful_paths(owner)
    routes = paths.get(path)
    if routes is None:
        raise ValueError(
            f"{path!r} is not the path of a scrudful endpoint of this "
            f"{type(owner).__name__} (its paths are {sorted(paths)}): give the "
            "route's full path string"
        )
    api_error = _api_error(owner)
    originals = tuple(getattr(route, "original_route", route) for route in routes)
    built = options.built.get(path)
    if (
        built is not None
        and len(built[0]) == len(originals)
        and all(old is new for old, new in zip(built[0], originals))
        and built[1] == api_error
    ):
        return built[2]
    document = yield from _describe_path(routes, options.base_url, api_error)
    options.built[path] = (originals, api_error, document)
    return document


def _run_describing(flow: _Flow[T]) -> T:
    """Run a describe-mode ``flow`` here: each callable is called, and an
    awaitable it returns awaited (see :func:`_await`)."""
    result = None
    while True:
        try:
            func, where = flow.send(result)
        except StopIteration as stop:
            return stop.value
        except HsrConfigError as exc:
            raise ValueError(str(exc)) from exc
        try:
            result = func()
            if inspect.isawaitable(result):
                result = _await(result)
        except Exception as exc:
            raise _describing_error(func, where, exc) from exc


async def _run_describing_async(flow: _Flow[T]) -> T:
    """:func:`_run_describing` in the event loop (sync callables in the
    threadpool)."""
    result = None
    while True:
        try:
            func, where = flow.send(result)
        except StopIteration as stop:
            return stop.value
        except HsrConfigError as exc:
            raise ValueError(str(exc)) from exc
        try:
            result = await _call(func, {})
        except Exception as exc:
            raise _describing_error(func, where, exc) from exc


def _await(awaitable: Any) -> Any:
    """The result of ``awaitable``, from sync code: in a new event loop, in
    a worker thread when this thread runs one."""

    async def wait() -> Any:
        return await awaitable

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(wait())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, wait()).result()


def _describing_error(func: Any, where: str, exc: Exception) -> ValueError:
    name = getattr(func, "__qualname__", None) or repr(func)
    return ValueError(
        f"{name}, a link callable of {where}, must work without a request when "
        f"the HTTP Schema Resource is built (called as func()): {exc!r}"
    )


def _scrudful_paths(owner: FastAPI | APIRouter) -> dict[str, list[Any]]:
    """The routes of each path template of ``owner`` that a scrudful endpoint
    serves, in routing order (see :func:`add_options`)."""
    paths: dict[str, list[Any]] = {}
    routes = owner.router.routes if isinstance(owner, FastAPI) else owner.routes
    _collect_paths(routes, "", paths)
    return {
        path: served
        for path, served in paths.items()
        if any(_endpoint_config(getattr(route, "endpoint", None)) for route in served)
    }


def _collect_paths(
    routes: Sequence[Any], prefix: str, paths: dict[str, list[Any]]
) -> None:
    """Add the HTTP routes under ``routes`` to ``paths``, by full path.

    Into the ``Mount``\\ s and ``Host``\\ s :func:`_contents` looks into; the
    automatic OPTIONS routes are skipped.
    """
    for route in _flat(routes):
        if _is_automatic(route):
            continue
        contents = _contents(route)
        if contents is _OPAQUE:
            continue
        if contents is not None:
            _collect_paths(contents, prefix + _mount_path(route), paths)
        elif isinstance(getattr(route, "original_route", route), Route):
            paths.setdefault(prefix + route.path, []).append(route)


def _mount_path(route: Any) -> str:
    """The path a ``Mount`` adds to its routes' (a ``Host`` adds none)."""
    original = getattr(route, "original_route", route)
    return str(original.path) if isinstance(original, Mount) else ""


def _api_error(owner: FastAPI | APIRouter) -> Entity | None:
    """The entity :func:`add_error_links` gave the app serving ``owner``'s
    routes: the app's own, or for a router, that of the apps including it.

    Raises
    ------
    ValueError
        If apps including the router were given different entities: its HTTP
        Schema Resources would differ between them.
    """
    if isinstance(owner, FastAPI):
        return _APP_ERRORS.get(owner)
    endpoints = [
        endpoint for endpoint, _, _ in _endpoints(owner.routes, for_options=True)
    ]
    errors: list[Entity] = []
    for app, error in list(_APP_ERRORS.items()):
        if error not in errors and any(
            any(endpoint is ours for ours in endpoints)
            for endpoint, _, _ in _endpoints(app.router.routes, for_options=False)
        ):
            errors.append(error)
    if len(errors) > 1:
        raise ValueError(
            "an APIRouter is included in apps that add_error_links gave "
            "different error entities, so its HTTP Schema Resources would "
            "differ between them: give them the same entity, or call "
            "add_options on each app"
        )
    return errors[0] if errors else None


def _describe_path(
    routes: Sequence[Any], base_url: str | None, api_error: Entity | None
) -> _Flow[dict[str, Any]]:
    """The HTTP Schema Resource of a path served by ``routes``: each method,
    described as the first route serving it, and ``options``."""
    memo: dict[tuple[int, str], Any] = {}
    body: dict[str, Any] = {}
    for method in METHOD_KEYS:
        if method == "options":
            body[method] = options_operation()
            continue
        route = next((route for route in routes if _serves(route, method)), None)
        if route is not None:
            body[method] = yield from _describe_route(
                method, route, base_url, api_error, memo
            )
    return body


def _serves(route: Any, method: str) -> bool:
    methods = getattr(route, "methods", None)
    return methods is None or method.upper() in methods


def _describe_route(
    method: str,
    route: Any,
    base_url: str | None,
    api_error: Entity | None,
    memo: dict[tuple[int, str], Any],
) -> _Flow[dict[str, Any]]:
    """Describe ``route`` as the operation of ``method`` (see :func:`add_options`).

    ``api_error`` is the entity of :func:`add_error_links` (``None``
    without it); ``memo`` keeps the values of the link callables called.
    """
    endpoint = getattr(route, "endpoint", None)
    config = _endpoint_config(endpoint)
    name = f"{_endpoint_name(endpoint)}()"
    status = _route_status(route)
    conditional = config is not None and _is_conditional(config, method)
    with_content = method != "head"
    # The entity of the errors raised for the method, which add_error_links'
    # handlers answer (see _raised_entity).
    raised = None
    if api_error is not None:
        raised = api_error
        if config is not None and config.error is not None:
            raised = config.error

    def media(source: Any, media_type: str, key: str) -> _Flow[Any]:
        return _media(source, media_type, base_url, f"{name}'s {key} response", memo)

    content = error_content = None
    if config is not None and with_content and method != "trace":
        content = yield from media(config, config.media_type, str(status))
    if conditional and with_content and config.error is not None:
        # scrudful's own 400, 412 and 428
        error_content = yield from media(config.error, config.error.media_type, "400")
    if method in ("get", "head", "trace"):
        description = describe_read(None, conditional, [status], error_content)
        if content and has_default_entity(method, status):
            description["responses"][str(status)]["content"] = copy.deepcopy(content)
    else:
        takes_body = getattr(route, "body_field", None) is not None
        description = describe_write(
            method,
            content if takes_body else None,
            conditional,
            config is not None and config.etag_func is not None,
            config is not None and config.last_modified_func is not None,
            [status],
            response_content=content if has_default_entity(method, status) else None,
            error_content=error_content,
        )
    responses = description["responses"]
    for key, declared in _declared_responses(route):
        entry = responses.get(key)
        fresh = entry is None
        if entry is None:
            entry = responses[key] = {"description": _declared_text(key, declared)}
        if not with_content:
            continue
        kind = _status_kind(method, key)
        if kind == "success" and content:
            described = entry.setdefault("content", copy.deepcopy(content))
            known = {essence(media_type) for media_type in described}
            for media_type in declared.get("content") or ():
                if essence(media_type_key(media_type)) not in known:
                    more = yield from media(config, media_type, key)
                    described.update(more or {})
                    known.add(essence(media_type))
        elif kind == "error" and fresh and raised is not None:
            more = yield from media(raised, raised.media_type, key)
            if more:
                entry["content"] = more
    default: dict[str, Any] = {"description": _DEFAULT_ERROR}
    if with_content and raised is not None:
        more = yield from media(raised, raised.media_type, "default")
        if more:
            default["content"] = more
    responses["default"] = default
    return description


def _route_status(route: Any) -> int:
    """The success status of ``route``: its ``status_code``, else its
    response class's default (FastAPI's choice), 200 for a Starlette route."""
    status = getattr(route, "status_code", None)
    if status:
        return int(status)
    response_class = getattr(route, "response_class", None)
    if isinstance(response_class, DefaultPlaceholder):
        response_class = response_class.value
    return _default_status(response_class)


def _declared_responses(route: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """The statuses ``route`` declares in ``responses=``, as response keys
    (``"404"``, ``"4XX"``), with their declared responses. ``"default"`` and
    keys that are no status are left out."""
    declared = []
    for key, response in (getattr(route, "responses", None) or {}).items():
        if isinstance(key, bool):
            continue
        text = str(key).strip().upper()
        if _STATUS_KEY.fullmatch(text):
            declared.append((text, response if isinstance(response, Mapping) else {}))
    return declared


def _declared_text(key: str, declared: Mapping[str, Any]) -> str:
    description = declared.get("description")
    if isinstance(description, str) and description:
        return description
    if key.endswith("XX"):
        return _RANGE_TEXT[key[0]]
    return reason_phrase(int(key))


def _status_kind(method: str, key: str) -> str | None:
    """``"success"`` for a status with the endpoint's content (see
    :func:`scrudful._adapter.has_default_entity`), ``"error"`` for a 4xx or
    5xx, else ``None``."""
    status = int(key[0]) * 100 if key.endswith("XX") else int(key)
    if has_default_entity(method, status):
        return "success"
    return "error" if status >= 400 else None


def _media(
    source: Any,
    media_type: str,
    base_url: str | None,
    where: str,
    memo: dict[tuple[int, str], Any],
) -> _Flow[dict[str, Any] | None]:
    """The ``content`` of ``source``'s links: its Media Type Object, or ``None``.

    Built by :func:`scrudful._adapter.describe_content` from the schema and
    context URLs and types of ``source`` (an endpoint's options or an
    :class:`Entity`), evaluated in describe mode (the context only when
    ``media_type`` takes one), with relative URLs resolved against
    ``base_url`` (:func:`scrudful._hsr.absolute_for_hsr`), as the Links of
    the responses it describes are (see :func:`_live_base`). ``None``
    without a schema URL. ``where`` names the entry, for errors.

    Raises
    ------
    HsrConfigError
        If a URL is relative and there is no ``base_url``, or a schema has
        no RDF context.
    """
    schema = yield from _field(source, "schema_link_or_func", where, memo)
    if not schema:
        return None
    schema_type = yield from _field(source, "schema_type_or_func", where, memo)
    context = context_type = None
    rel, default_type = context_defaults(media_type)
    if rel is not None:  # not in-band RDF
        context = yield from _field(source, "context_link_or_func", where, memo)
    if context:
        context_type = (
            yield from _field(source, "context_type_or_func", where, memo)
        ) or default_type
    schema_url = absolute_for_hsr(str(schema), base_url, f"{where} schema link")
    context_url = (
        absolute_for_hsr(str(context), base_url, f"{where} context link")
        if context
        else None
    )
    try:
        return describe_content(
            media_type,
            schema_url,
            str(schema_type or SCHEMA_TYPE),
            context_url,
            None if context_type is None else str(context_type),
        )
    except HsrConfigError as exc:
        raise HsrConfigError(f"{where}: {exc}") from exc


def _field(
    source: Any, name: str, where: str, memo: dict[tuple[int, str], Any]
) -> _Flow[Any]:
    """The value of ``source``'s field ``name``: a callable's result, called
    once per build (``memo``)."""
    value = getattr(source, name)
    if not callable(value):
        return value
    key = (id(source), name)
    if key not in memo:
        memo[key] = yield value, where
    return memo[key]


class _OptionsRoute(BaseRoute):
    """Answers OPTIONS where a scrudful endpoint of ``app`` serves the URL.

    It matches an OPTIONS request only when no other route serves OPTIONS for
    its URL and the first route whose path matches it is on a scrudful path
    (see :func:`_options_path`); every other request goes on to the other
    routes.
    """

    name = "scrudful_options"
    path = ""
    include_in_schema = False

    def __init__(self, app: FastAPI) -> None:
        self.owner = app

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope.get("type") != "http" or scope.get("method") != "OPTIONS":
            return Match.NONE, {}
        path = _options_path(self.owner, scope)
        if path is None:
            return Match.NONE, {}
        return Match.FULL, {_SERVED_KEY: path}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get(_SERVED_KEY) or _options_path(self.owner, scope)
        if path is None:
            raise HTTPException(status_code=404)
        body = await _run_describing_async(_hsr_flow(self.owner, path))
        await _options_response(body)(scope, receive, send)


def _options_path(app: FastAPI, scope: Scope) -> str | None:
    """The path template whose document answers the OPTIONS ``scope``.

    That of the first route whose path matches the URL (see
    :func:`_match_path`), when it is a scrudful path; ``None`` when another
    route (or a mounted app) serves OPTIONS for the URL, or that path has no
    scrudful endpoint.
    """
    routes = app.router.routes
    if _match(routes, {**scope, "method": "OPTIONS"}) is not None:
        return None
    path = _match_path(routes, scope)
    if path is None or path not in _scrudful_paths(app):
        return None
    return path


def _match_path(routes: Sequence[Any], scope: Scope, prefix: str = "") -> str | None:
    """The full path of the route Starlette routes ``scope`` to, by path.

    As Starlette routes: a ``Mount`` or ``Host`` that fully matches is gone
    into (``None`` for an app :func:`_contents` doesn't look into), else the
    first route whose path matches (a partial match: its methods aren't
    the request's). The automatic OPTIONS routes are skipped.
    """
    partial = None
    for route in _flat(routes):
        if _is_automatic(route):
            continue
        match, child_scope = route.matches(scope)
        if match == Match.NONE:
            continue
        contents = _contents(route)
        if contents is None:
            path = getattr(route, "path", None)
            if path is None:
                continue
            if match == Match.FULL:
                return prefix + path
            if partial is None:
                partial = prefix + path
        elif match == Match.FULL:
            if contents is _OPAQUE:
                return None
            return _match_path(
                contents, {**scope, **child_scope}, prefix + _mount_path(route)
            )
    return partial


def _flat(routes: Sequence[Any]) -> list[Any]:
    """``routes`` with the routes of the routers included in them, in order."""
    if _iter_route_contexts is None:
        return list(routes)
    return list(_iter_route_contexts(routes))


#: What :func:`_contents` returns for a ``Mount`` or ``Host`` it doesn't look
#: into (compared by identity).
_OPAQUE: Any = object()


def _contents(route: Any, for_options: bool = True) -> Sequence[Any] | Any:
    """The routes a ``Mount`` or ``Host`` routes to, else ``None``.

    For OPTIONS (``for_options``), only those of a bare ``Router``
    (``Mount(routes=...)``, a mounted ``APIRouter``) without middleware of its
    own or of the Mount's: anything else (a mounted Starlette or FastAPI app,
    a middleware-wrapped Mount or Router) gives :data:`_OPAQUE`, since
    answering for it from outside would skip its middleware (e.g. its
    ``CORSMiddleware``); call :func:`add_options` on a mounted app itself.

    For the checks of :func:`add_error_links` (not ``for_options``), the
    routes of anything but a mounted Starlette or FastAPI app, which has its
    own exception handlers: the outer app's handlers serve the others,
    middleware or not.
    """
    if isinstance(getattr(route, "original_route", route), Route):
        return None
    routes = getattr(route, "routes", None)
    if routes is None:
        return None
    app = getattr(route, "app", None)
    if not for_options:
        return _OPAQUE if isinstance(app, Starlette) else routes
    return routes if isinstance(app, Router) and _is_bare(app) else _OPAQUE


def _is_bare(router: Router) -> bool:
    """Whether ``router`` has no middleware of its own (``Router(middleware=)``,
    Starlette >= 0.35; older routers have none)."""
    stack = getattr(router, "middleware_stack", None)
    return stack is None or stack == router.app


def _path_param_names(route: Any) -> set[str]:
    """The names of the path parameters ``route`` adds to ``path_params``.

    A ``Mount`` matches its remainder as a ``path`` parameter, which it
    removes again.
    """
    names = set(getattr(route, "param_convertors", None) or ())
    if isinstance(getattr(route, "original_route", route), Mount):
        names.discard("path")
    return names


def _is_automatic(route: Any) -> bool:
    """Whether ``route`` is an OPTIONS route :func:`add_options` added."""
    return isinstance(getattr(route, "original_route", route), _OptionsRoute) or (
        getattr(getattr(route, "endpoint", None), "_scrudful_options", False)
    )


def _match(routes: Sequence[Any], scope: Scope) -> tuple[Any, Scope] | None:
    """The route that serves ``scope`` among ``routes``, and the scope it gets.

    As Starlette routes: the first route that fully matches, and within a
    ``Mount`` or ``Host`` of a router that does, the route of its own routes
    that does; the scope is ``scope`` updated with each child scope on the
    way. A ``Mount`` of an app is returned itself (see :func:`_contents`).
    The automatic OPTIONS routes are skipped.
    """
    for route in _flat(routes):
        if _is_automatic(route):
            continue
        match, child_scope = route.matches(scope)
        if match != Match.FULL:
            continue
        routed = {**scope, **child_scope}
        contents = _contents(route)
        if contents is not None and contents is not _OPAQUE:
            return _match(contents, routed)
        routed.setdefault("route", getattr(route, "original_route", route))
        return route, routed
    return None


def _add_path_options(router: APIRouter) -> None:
    """:func:`add_options` for an ``APIRouter``: an OPTIONS route per path."""
    paths: dict[str, list[Route]] = {}
    for route in router.routes:
        if isinstance(route, Route):
            paths.setdefault(route.path, []).append(route)
    for path, routes in paths.items():
        if any(_endpoint_config(route.endpoint) for route in routes) and not any(
            route.methods is None or "OPTIONS" in route.methods for route in routes
        ):
            router.add_route(
                path,
                _options_endpoint(router, path),
                methods=["OPTIONS"],
                name="scrudful_options",
                include_in_schema=False,
            )


def _options_endpoint(router: APIRouter, path: str) -> Callable[[Request], Any]:
    """The endpoint of the OPTIONS route of ``path`` on ``router``: the
    router's document of ``path``, whichever app or prefix the router is
    included through (routes added to the path later are described)."""

    async def scrudful_options(request: Request) -> Response:
        body = await _run_describing_async(_hsr_flow(router, path))
        return _options_response(body)

    scrudful_options._scrudful_options = True  # type: ignore[attr-defined]
    return scrudful_options


def _options_response(body: dict[str, Any]) -> Response:
    """The OPTIONS response with the HTTP Schema Resource ``body``."""
    response = JSONResponse(body)
    response.headers["Link"] = options_links()
    response.headers["Allow"] = allow_header(body)
    expose_headers(response.headers)
    return response


def _has_validators(config: _EndpointConfig) -> bool:
    return config.etag_func is not None or config.last_modified_func is not None


def _is_conditional(config: _EndpointConfig, method: str) -> bool:
    """Whether the endpoint evaluates the If-* headers for ``method`` (lower-case).

    With validators, for a method :func:`scrudful._adapter.evaluates_preconditions`
    accepts (not TRACE).
    """
    return _has_validators(config) and evaluates_preconditions(method.upper())


def _endpoint_config(endpoint: Any) -> _EndpointConfig | None:
    """The :func:`scrudful` options of ``endpoint``, if it is decorated."""
    config = getattr(endpoint, "_scrudful", None)
    return config if isinstance(config, _EndpointConfig) else None


def _description_kwargs(config: _EndpointConfig, request: Request) -> dict[str, Any]:
    """The kwargs of an endpoint's callables outside the endpoint, in
    :func:`add_error_links`'s handlers.

    The request's path parameters that the endpoint takes, under the names
    it takes them by, as FastAPI passes them inside it (see
    :func:`_path_names`), plus the request under the name of the endpoint's
    ``Request`` parameter, when it declares one.
    """
    names = _path_names(config, request.scope.get("route"))
    kwargs = {
        names[name]: value
        for name, value in request.path_params.items()
        if name in names
    }
    if config.request_name is not None:
        kwargs[config.request_name] = request
    return kwargs


# --- checking the callables called outside the endpoint ------------------------


def _path_names(config: _EndpointConfig, route: Any) -> dict[str, str]:
    """The endpoint's own path parameters: URL name to its keyword argument.

    From FastAPI's ``route.dependant``: an aliased parameter
    (``item: int = Path(alias="item_id")``) maps ``item_id`` to ``item``,
    and neither a parameter of an ``include_router`` prefix the endpoint
    doesn't declare nor one only a ``Depends(...)`` takes is listed: FastAPI
    passes those to the endpoint neither. Without a ``dependant``, the
    endpoint's parameter names, unaliased.
    """
    route = getattr(route, "original_route", route)
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return {name: name for name in config.param_names}
    return {
        getattr(field, "alias", None) or field.name: field.name
        for field in dependant.path_params
    }


def _endpoints(
    routes: Sequence[Any],
    for_options: bool,
    params: frozenset[str] = frozenset(),
) -> Iterator[tuple[Any, _EndpointConfig, set[str]]]:
    """The scrudful endpoints under ``routes``, with the names their callables
    get outside them (see :func:`_outside_names`).

    Including those under the ``Mount``\\ s and ``Host``\\ s that
    :func:`_contents` looks into for ``for_options``.
    """
    for route in _flat(routes):
        names = params | _path_param_names(route)
        contents = _contents(route, for_options)
        if contents is _OPAQUE:
            continue
        if contents is not None:
            yield from _endpoints(contents, for_options, names)
            continue
        endpoint = getattr(route, "endpoint", None)
        config = _endpoint_config(endpoint)
        if config is not None:
            yield endpoint, config, _outside_names(config, names, route)


def _check_options_callables(routes: Sequence[Any]) -> None:
    """Check the link callables the HTTP Schema Resource calls, as ``func()``
    (see :func:`add_options`): an endpoint's, and its ``error``'s."""
    for endpoint, config, _ in _endpoints(routes, for_options=True):
        where = f"endpoint {_endpoint_name(endpoint)}"
        _check_describe_call(config, "", where)
        if config.error is not None:
            _check_describe_call(config.error, "error.", where)


def _check_describe_call(
    source: _EndpointConfig | Entity, prefix: str, where: str
) -> None:
    """Raise ``ValueError`` unless each link callable of ``source`` can be
    called with no arguments."""
    for name in _LINK_FIELDS:
        func = getattr(source, name)
        if not callable(func):
            continue
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):  # e.g. some builtins: can't tell
            continue
        try:
            signature.bind()
        except TypeError as exc:
            raise ValueError(
                f"{prefix}{name} of {where} ({func!r}) is called as func(), with "
                "no arguments, when the HTTP Schema Resource is built, but "
                f"{exc}; give the parameters it takes inside the endpoint "
                "defaults, or accept **kwargs"
            ) from exc


def _check_error_callables(routes: Sequence[Any], error: Entity) -> None:
    """Check the callables :func:`add_error_links`'s handlers call."""
    _check_call(
        error,
        "",
        "add_error_links",
        {"request"},
        "in its handlers",
        "the request",
        _ERROR_LINK_FIELDS,
    )
    for endpoint, config, names in _endpoints(routes, for_options=False):
        if config.error is not None:
            _check_call(
                config.error,
                "error.",
                f"endpoint {_endpoint_name(endpoint)}",
                names,
                "in add_error_links's handlers",
                fields=_ERROR_LINK_FIELDS,
            )


def _outside_names(
    config: _EndpointConfig, names: frozenset[str], route: Any
) -> set[str]:
    """The kwargs names an endpoint's callables get outside the endpoint.

    Those the endpoint takes the path parameters ``names`` of ``route`` by
    (see :func:`_description_kwargs`), and its ``Request`` parameter's.
    """
    path_names = _path_names(config, route)
    request = {config.request_name} if config.request_name else set()
    return {path_names[name] for name in names if name in path_names} | request


def _param_names(endpoint: Callable[..., Any]) -> frozenset[str]:
    """The names of ``endpoint``'s parameters, but ``*args`` and ``**kwargs``.

    FastAPI passes nothing to those: an undeclared path parameter (e.g. of an
    ``include_router`` prefix) never reaches the endpoint or its callables.
    """
    variadic = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    return frozenset(
        name
        for name, param in evaluated_signature(endpoint).parameters.items()
        if param.kind not in variadic
    )


def _check_call(
    source: _EndpointConfig | Entity,
    prefix: str,
    where: str,
    names: set[str],
    purpose: str,
    meaning: str = _OUTSIDE,
    fields: Sequence[str] = _LINK_FIELDS,
) -> None:
    """Raise ``TypeError`` unless each callable of ``source``'s ``fields`` can
    be called with the keyword arguments ``names`` alone (``meaning``)."""
    for name in fields:
        func = getattr(source, name)
        if not callable(func):
            continue
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):  # e.g. some builtins: can't tell
            continue
        try:
            signature.bind(**dict.fromkeys(names))
        except TypeError as exc:
            raise TypeError(
                f"{prefix}{name} of {where} ({func!r}) is called {purpose} with "
                f"the keyword arguments {sorted(names)} only ({meaning}), but "
                f"{exc}; accept **kwargs or give its other parameters defaults"
            ) from None


def _endpoint_name(endpoint: Any) -> str:
    return getattr(endpoint, "__qualname__", None) or repr(endpoint)


async def _call(func: Callable[..., Any], kwargs: Mapping[str, Any]) -> Any:
    """Call ``func`` with ``kwargs``; sync callables run in the threadpool."""
    if inspect.iscoroutinefunction(func):
        return await func(**kwargs)
    result = await run_in_threadpool(functools.partial(func, **kwargs))
    if inspect.isawaitable(result):
        result = await result
    return result


async def _call_all(
    funcs: list[Callable[..., Any]], kwargs: Mapping[str, Any]
) -> list[Any]:
    """Call ``funcs`` one after the other, in order, and return the results.

    Consecutive sync callables share one threadpool hop, which ends after a
    callable returning an awaitable (awaited before the next one runs). They
    are never run concurrently: they typically share a database session.
    Because an awaitable is awaited before the next callback is called, a
    later callback that raises never leaves an un-awaited coroutine behind, so
    nothing needs closing.
    """
    results: list[Any] = []
    pending = list(funcs)

    def run_sync() -> list[Any]:
        out: list[Any] = []
        while pending and not inspect.iscoroutinefunction(pending[0]):
            out.append(pending.pop(0)(**kwargs))
            if inspect.isawaitable(out[-1]):
                break
        return out

    while pending:
        if inspect.iscoroutinefunction(pending[0]):
            results.append(await pending.pop(0)(**kwargs))
            continue
        out = await run_in_threadpool(run_sync)
        if inspect.isawaitable(out[-1]):
            out[-1] = await out[-1]
        results.extend(out)
    return results


async def _link(
    kwargs: Mapping[str, Any],
    values_or_funcs: tuple[Any, Any, Any],
    defaults: tuple[str | None, str | None],
    base: str,
    context_of: str | None = None,
) -> str | None:
    """Build one link-value from (url, rel, type) values or callables.

    Callables get ``kwargs``; the URL is resolved against ``base``, the
    request's absolute URI. The context link of ``context_of`` content takes
    its type from :func:`scrudful._adapter.context_link_type`, as the OPTIONS
    body does.
    """
    url_or_func, rel_or_func, type_or_func = values_or_funcs
    url = await _value(url_or_func, kwargs)
    if not url:
        return None
    rel = await _value(rel_or_func, kwargs) or defaults[0]
    content_type = await _value(type_or_func, kwargs)
    if context_of is not None:
        content_type = context_link_type(context_of, content_type)
    content_type = content_type or defaults[1]
    return link_content(resolve_uri(url, base), rel, content_type)


async def _value(value_or_func: Any, kwargs: Mapping[str, Any]) -> Any:
    """``value_or_func``, or what it returns when called with ``kwargs``."""
    if callable(value_or_func):
        return await _call(value_or_func, kwargs)
    return value_or_func


async def _entity_links(
    entity: Entity, kwargs: Mapping[str, Any], base: str
) -> tuple[str | None, str | None]:
    """The schema and the context link-values of ``entity`` (``None`` if unset).

    The context link's defaults come from ``entity.media_type``; in-band RDF
    has none.
    """
    return await _source_links(entity, entity.media_type, kwargs, base)


async def _source_links(
    entity: Any, media_type: str, kwargs: Mapping[str, Any], base: str
) -> tuple[str | None, str | None]:
    """:func:`_entity_links` of the link fields of ``entity`` (an
    :class:`Entity`, or an endpoint's link options) describing ``media_type``
    content."""
    schema = await _link(
        kwargs,
        (
            entity.schema_link_or_func,
            entity.schema_rel_or_func,
            entity.schema_type_or_func,
        ),
        (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
        base,
    )
    context = None
    defaults = context_defaults(media_type)
    if defaults != (None, None):
        context = await _link(
            kwargs,
            (
                entity.context_link_or_func,
                entity.context_rel_or_func,
                entity.context_type_or_func,
            ),
            defaults,
            base,
            context_of=media_type,
        )
    return schema, context


def _check_entity(entity: Any, links: Mapping[str, Any]) -> None:
    """Check an ``entity`` argument: ``None``, or an :class:`Entity` given
    without any of the link options ``links``, which it replaces."""
    if entity is None:
        return
    if not isinstance(entity, Entity):
        raise TypeError(f"entity must be an Entity, not {type(entity).__name__}")
    given = [name for name, value in links.items() if value is not None]
    if given:
        raise TypeError(
            f"entity replaces {', '.join(given)}: give the links in the Entity"
        )


def _check_error(error: Any) -> None:
    """Check an ``error`` argument: ``None`` or an :class:`Entity` of JSON."""
    if error is None:
        return
    if not isinstance(error, Entity):
        raise TypeError(f"error must be an Entity, not {type(error).__name__}")
    check_error_media_type(error.media_type)


def _conditional_response(
    result: ConditionalResult,
    injected: Headers,
    link: str | None = None,
    media_type: str | None = None,
) -> Response:
    """Build the early response, keeping headers set on the injected Response.

    FastAPI only merges the injected response's headers into responses it
    builds itself, so dependency headers would be lost. A 304 gets all of them
    (``Cache-Control``, ``Vary``, ... as a 200 would carry, RFC 9110 §15.4.5),
    except representation metadata and those it already has. Error responses
    get only the cookies: other headers (e.g. a ``public`` Cache-Control, or
    a Link describing the success representation) must not apply to them.

    ``link`` (the Links of the ``error`` entity) and ``media_type`` (its
    Content-Type, default ``application/json``) apply to a response with a
    body, the 400, 412 and 428; a 304 gets neither.
    """
    if result.body is None:
        response = Response(status_code=result.status, headers=result.headers)
    else:
        response = JSONResponse(
            result.body,
            status_code=result.status,
            headers=result.headers,
            media_type=media_type,
        )
        if link:
            response.headers["Link"] = link
    headers = response.headers
    own = {name for name, _ in headers.raw}
    for raw_name, raw_value in injected.raw:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name == "set-cookie":
            headers.append(name, value)
        elif name == "access-control-expose-headers":
            merged = merge_expose_headers(headers.get(name), value.split(","))
            if merged:
                headers[name] = merged
        elif (
            result.status == 304
            and name not in BODY_HEADERS
            and name.encode("latin-1") not in own
        ):
            headers.append(name, value)
    expose_headers(headers)
    return response


def _join_expose_lines(headers: MutableHeaders) -> None:
    """Join repeated Access-Control-Expose-Headers lines into one.

    ``expose_headers`` reads and replaces a single value, so names on later
    lines would be lost.
    """
    lines = headers.getlist("access-control-expose-headers")
    if len(lines) > 1:
        headers["Access-Control-Expose-Headers"] = ", ".join(lines)


def _status_code(request: Request, response: Response) -> int:
    """Status of the response FastAPI will build from a non-Response result."""
    if response.status_code:
        return response.status_code
    route = request.scope.get("route")
    status = getattr(route, "status_code", None)
    if status:
        return status
    # Neither is set: FastAPI lets the response class pick (RedirectResponse: 307).
    return _default_status(_response_class(request))


def _response_class(request: Request) -> Any:
    """The response class FastAPI uses for the matched route."""
    # Routes added with ``include_router(default_response_class=...)`` keep their
    # own placeholder on newer FastAPI; the effective class is in the scope.
    context = request.scope.get("fastapi", {}).get("effective_route_context")
    response_class = getattr(context, "response_class", None)
    if response_class is None:
        response_class = getattr(request.scope.get("route"), "response_class", None)
    if isinstance(response_class, DefaultPlaceholder):
        response_class = response_class.value
    return response_class


def _default_status(response_class: Any) -> int:
    """Default ``status_code`` of ``response_class``, or 200 if unknown."""
    try:
        hash(response_class)
    except TypeError:  # e.g. a callable instance defining __eq__ but no __hash__
        return _lookup_default_status(response_class)
    return _cached_default_status(response_class)


def _lookup_default_status(response_class: Any) -> int:
    try:
        default = inspect.signature(response_class).parameters["status_code"].default
    except (KeyError, TypeError, ValueError):
        return 200
    return default if isinstance(default, int) else 200


_cached_default_status = functools.lru_cache(maxsize=128)(_lookup_default_status)


def _wrapper_signature(
    endpoint: Callable[..., Any],
) -> tuple[inspect.Signature, str, bool, str, bool]:
    """Return the wrapper's signature and where the Request/Response go.

    The endpoint's parameters are kept, so FastAPI's dependency injection and
    OpenAPI generation see the endpoint unchanged. A ``Request`` and a
    ``Response`` parameter are appended (keyword-only, with private names)
    unless the endpoint already declares one; the boolean flags tell whether
    the endpoint itself receives them.
    """
    # FastAPI < 0.124 evaluates string annotations (``from __future__ import
    # annotations``) in the wrapper's globals (this module's).
    signature = evaluated_signature(endpoint)
    params = list(signature.parameters.values())
    globalns = getattr(inspect.unwrap(endpoint), "__globals__", {})
    names = {param.name for param in params}
    extra = []

    def find(cls: type, private_name: str) -> tuple[str, bool]:
        for param in params:
            if _annotation_is(param.annotation, cls, globalns):
                return param.name, True
        while private_name in names:
            private_name += "_"
        names.add(private_name)
        extra.append(
            inspect.Parameter(
                private_name, inspect.Parameter.KEYWORD_ONLY, annotation=cls
            )
        )
        return private_name, False

    request_name, pass_request = find(Request, "_scrudful_request")
    response_name, pass_response = find(Response, "_scrudful_response")
    if params and params[-1].kind is inspect.Parameter.VAR_KEYWORD:
        params[-1:-1] = extra
    else:
        params.extend(extra)
    return (
        signature.replace(parameters=params),
        request_name,
        pass_request,
        response_name,
        pass_response,
    )


def _annotation_is(annotation: Any, cls: type, globalns: dict[str, Any]) -> bool:
    """Whether ``annotation`` is ``cls`` or a subclass.

    String / forward-reference annotations are evaluated in the endpoint's
    globals and ``Annotated[cls, ...]`` is unwrapped, as FastAPI does.
    """
    annotation = resolve_annotation(annotation, globalns)
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    # ``list[int]`` is a ``type`` instance on Python 3.10 but not a class.
    if not inspect.isclass(annotation) or isinstance(annotation, types.GenericAlias):
        return False
    try:
        return issubclass(annotation, cls)
    except TypeError:
        return False
