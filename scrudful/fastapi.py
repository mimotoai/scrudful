"""SCRUDful conditional requests and Link headers for FastAPI endpoints.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to FastAPI's request/response handling.
"""

from __future__ import annotations

import copy
import functools
import inspect
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, get_args, get_origin

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
    DEFAULT_CONTEXT_REL,
    DEFAULT_CONTEXT_TYPE,
    DEFAULT_SCHEMA_REL,
    DEFAULT_SCHEMA_TYPE,
    METHOD_KEYS,
    SCHEMA_TYPE,
    allow_header,
    check_error_media_type,
    check_validators,
    context_defaults,
    context_link_type,
    describe_content,
    describe_options,
    describe_read,
    describe_write,
    evaluated_signature,
    evaluates_preconditions,
    expose_headers,
    has_default_entity,
    is_json,
    merge_links,
    options_links,
    precondition_headers,
    resolve_annotation,
    set_validator_headers,
    validator_policy,
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
    error: Entity | None = None,
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
        ``Request`` / ``Response`` parameters if it declares any (so a link
        callback can use ``request.url_for``). Accept ``**kwargs`` to ignore
        the ones you don't need. Callbacks may be ``def`` (run in the
        threadpool) or ``async def``; an awaitable result is awaited.

        Outside the endpoint, the link callables (schema and context URL and
        type) are also called by :func:`add_options` for the OPTIONS body,
        and those of ``error`` by :func:`add_options` and by
        :func:`add_error_links`'s exception handlers. There they get only the
        URL's path parameters that the endpoint takes (not those of an
        ``include_router`` prefix it doesn't declare, which FastAPI doesn't
        pass it either) as Starlette matched them (strings, unless the path
        uses a convertor such as ``{id:int}``), plus the request under
        the name of the endpoint's ``Request`` parameter when it declares
        one: no body, query or dependency values. Both helpers check, when
        they are called, that each such callable can be called that way, and
        raise ``TypeError`` otherwise. To use one callable in both places,
        accept ``**kwargs`` or give the parameters only the endpoint gets
        defaults, e.g. ``def schema(item_id, **kwargs)``.

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
    dependency) set; a Link it set with a ``describedby`` relation replaces
    the schema link, and one with a context relation the context link (see
    :func:`scrudful._adapter.merge_links`). Link URLs are resolved against the
    request's URL (``str(request.url)``), so a relative value such as
    ``"/schemas/item"`` is sent absolute. ``Access-Control-Expose-Headers``
    is extended with whichever of ``ETag``, ``Link`` and ``Location`` the
    response carries. Starlette's ``CORSMiddleware`` replaces that header on
    cross-origin requests, so if you use it, list these names in its
    ``expose_headers`` instead.

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
    error : Entity, optional
        The schema and context of scrudful's own 400, 412 and 428 bodies.
        Its link callables get the endpoint's keyword arguments, like the
        other callbacks. Its ``media_type`` must be JSON content
        (``application/json`` or ``+json``, e.g.
        ``application/problem+json``), since those bodies are JSON objects.
        With :func:`add_error_links`, the errors the app's exception handlers
        answer for this endpoint get it too, and :func:`add_options`
        describes it; its link callables are then called outside the
        endpoint (see "Callbacks").

    Raises
    ------
    TypeError
        If a validator callback isn't callable or ``error`` isn't an
        :class:`Entity`.
    ValueError
        If ``error``'s media type is not JSON content.
    """
    check_validators(etag_func, last_modified_func)
    _check_error(error)
    has_validators = etag_func is not None or last_modified_func is not None

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
        schema = await _link(
            kwargs,
            (schema_link_or_func, schema_rel_or_func, schema_type_or_func),
            (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
            base,
        )
        context = await _link(
            kwargs,
            (context_link_or_func, context_rel_or_func, context_type_or_func),
            (DEFAULT_CONTEXT_REL, DEFAULT_CONTEXT_TYPE),
            base,
        )
        return schema, context

    async def early_response(
        result: ConditionalResult,
        injected: Headers,
        kwargs: Mapping[str, Any],
        base: str,
    ) -> Response:
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
            schema_link_or_func=schema_link_or_func,
            schema_rel_or_func=schema_rel_or_func,
            schema_type_or_func=schema_type_or_func,
            context_link_or_func=context_link_or_func,
            context_rel_or_func=context_rel_or_func,
            context_type_or_func=context_type_or_func,
            error=error,
            request_name=request_name if pass_request else None,
            param_names=_param_names(endpoint),
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
            base = str(request.url)

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
                    return await early_response(result, response.headers, kwargs, base)

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
    it, as :func:`scrudful` adds them (a Link the handler set with a
    ``describedby`` or a context relation suppresses that kind; see
    :func:`scrudful._adapter.merge_links`), and serves it as
    ``error.media_type``. Responses without content (e.g. a 304) or with
    other content are left unchanged. Link URLs are resolved against the
    request's URL.

    The media type is relabelled whatever the handler chose: a custom
    handler's JSON response (``application/json``, or another ``+json``
    type) is served as ``error.media_type``, so the bodies of every wrapped
    handler must be content that ``error`` describes.

    The entity is ``error``, unless the request was routed to a
    :func:`scrudful` endpoint with an ``error`` of its own that serves the
    request's method: then the errors raised for it (by the endpoint, its
    dependencies or FastAPI's request validation) get that endpoint's
    ``error``, the entity :func:`add_options` describes, so that the Links
    of an error the endpoint raises with one of scrudful's statuses (e.g.
    ``HTTPException(400)``) agree with the OPTIONS body. A 405, which no
    endpoint serves, gets ``error``.

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
        called outside the endpoint, as in :func:`add_options`: with the
        URL's path parameters it takes, plus the request under the name of
        the endpoint's ``Request`` parameter if it declares one (see
        "Callbacks" in :func:`scrudful`). Both are checked when this is
        called, against the endpoints the app has then: those of included
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
        base = str(request.url)
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


def _raised_entity(request: Request, error: Entity) -> tuple[Entity, dict[str, Any]]:
    """The entity of an error the app's handlers answer, and its callables' kwargs.

    The ``error`` of the :func:`scrudful` endpoint the request was routed to,
    when it has one and serves the request's method (Starlette records a
    route matched by path alone, whose 405 it raises, too), called as in
    :func:`add_options`; else the app-wide ``error``, called with the
    request only.
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


# --- OPTIONS -------------------------------------------------------------------

#: The media type the OPTIONS body gives an endpoint's own content (DRF's).
_ENDPOINT_MEDIA_TYPE = "application/json"
#: The fields of an endpoint or entity whose callables an OPTIONS body calls
#: (the rels are not needed there).
_LINK_FIELDS = (
    "schema_link_or_func",
    "schema_type_or_func",
    "context_link_or_func",
    "context_type_or_func",
)
#: What an endpoint's callables get outside the endpoint.
_OUTSIDE = (
    "the URL's path parameters the endpoint takes, and the request under the "
    "name of the endpoint's Request parameter if it declares one"
)
#: The fields of an entity whose callables :func:`add_error_links`'s handlers
#: call (via :func:`_entity_links`).
_ERROR_LINK_FIELDS = (*_LINK_FIELDS, "schema_rel_or_func", "context_rel_or_func")
#: Where the OPTIONS route keeps the routes it resolved, in the request scope.
_SERVED_KEY = "scrudful.options"

#: FastAPI's flattening of included routers into their routes, as its OpenAPI
#: generation walks them (FastAPI versions that include routers lazily); older
#: versions copy an included router's routes, so ``routes`` is flat already.
_iter_route_contexts = getattr(fastapi_routing, "iter_route_contexts", None)


def add_options(app: FastAPI | APIRouter) -> None:
    """Answer OPTIONS on ``app``'s scrudful paths with an HTTP Schema Resource.

    An OPTIONS request to a URL that a :func:`scrudful` endpoint serves with
    some method, and that no other route serves OPTIONS for, is answered with
    200 and the URL's HTTP Schema Resource (Semantic HTTP 0.1) as
    ``application/json``, the Links of that resource's published JSON Schema
    and JSON-LD context (:func:`scrudful._adapter.options_links`), an
    ``Allow`` listing the body's methods and
    ``Access-Control-Expose-Headers``. Other OPTIONS requests are routed as
    before (e.g. a 405, or your own OPTIONS route). The OPTIONS answer has no
    dependencies (neither the app's, a router's nor an ``include_router``
    call's), so it needs no credentials, and is not in the OpenAPI document.

    Given a ``FastAPI`` app, the body describes, for each method, the
    endpoint the app routes that method of the URL to, found the way
    Starlette routes it: by matching the request against the app's routes,
    those of the routers included in it, and the routes of a
    ``Mount(routes=...)`` the URL is under. So a path served by several
    routers, by the app and a router, or under differently named path
    parameters (``/items/{id}`` and ``/items/{item_id}``), is described as a
    whole, and ``Allow`` lists every method. Given an ``APIRouter``, it adds
    an OPTIONS route to each of the router's own paths that has a scrudful
    route, describing the routes on that path; call it before including the
    router (older FastAPI versions copy a router's routes when it is
    included). Prefer calling it on the app.

    A mounted Starlette or FastAPI app, a ``Mount`` with ``middleware`` and
    a ``Router`` with ``middleware`` are left alone: answering for them from
    the outer app would skip their middleware (e.g. a ``CORSMiddleware``).
    Call this on a mounted app itself, whose OPTIONS answer then runs inside
    its middleware; the paths under a middleware-wrapped ``Mount`` or
    ``Router`` get no automatic OPTIONS.

    The body lists the methods in the order ``get``, ``put``, ``post``,
    ``delete``, ``options``, ``head``, ``patch``, ``trace``:

    - a scrudful GET: its success status (``route.status_code``, else 200)
      with the content of the endpoint's links (``application/json``, a
      ``schema`` ``$ref`` and an ``x-rdf-context``), and, when it has
      validator callbacks, scrudful's 304, 400 and 412, the 400 and 412 with
      the content of its ``error``;
    - a scrudful HEAD: the same responses, without content;
    - a scrudful TRACE: its success status, without content: a TRACE's
      content is the echoed request, which the endpoint's links don't
      describe, and it gets none of them;
    - a scrudful PUT, PATCH, DELETE or POST: the request body (the content
      of the endpoint's links, when the endpoint takes a body; DELETE has
      none), the success status with that content (but a 204), and, when
      conditional, scrudful's 400, 412 and (but for POST) 428 with the
      ``error`` content and the precondition header parameters;
    - ``options``: this response;
    - a method served by a route that isn't scrudful: ``{"responses":
      {"200": {"description": "OK"}}}``, without content or a request body.

    Only the error statuses scrudful itself answers are listed: an
    ``HTTPException`` the endpoint raises is not, but when it has one of
    those statuses, :func:`add_error_links` gives it the same entity.

    Callables are called outside the endpoint here: the endpoints' link
    callables (schema and context URL and type), and their ``error``'s when
    they are conditional, get only the path parameters of the URL that the
    endpoint takes (as inside it: not an ``include_router`` prefix's that it
    doesn't declare), as Starlette matched them (strings, unless the path
    uses a convertor such as ``{id:int}``), plus the request under the name
    of the endpoint's ``Request`` parameter when it declares one: not the
    values FastAPI resolves for the endpoint (body, query, dependencies).
    Each is checked when this is called, and one that can't be called that
    way raises ``TypeError``; accept ``**kwargs`` or give the other
    parameters defaults to use one both inside the endpoint and here.
    Validator callbacks are not called. Every URL is resolved against the
    request's URL, as the Links are.

    Call it once the routes are added (the endpoints added later are
    described, but their callables are not checked); calling it again adds
    nothing. CORS preflight requests are meant to be answered by
    ``CORSMiddleware`` before routing; one that reaches this answer gets the
    description like any other OPTIONS.

    Parameters
    ----------
    app : FastAPI or APIRouter
        The application or router whose scrudful paths answer OPTIONS.

    Raises
    ------
    TypeError
        If ``app`` is neither a ``FastAPI`` application nor an ``APIRouter``,
        or a callable the OPTIONS body would call can't be called with the
        path parameters and the request alone.
    """
    if isinstance(app, FastAPI):
        _check_options_callables(app.router.routes)
        routes = app.router.routes
        if not any(isinstance(route, _OptionsRoute) for route in routes):
            # First, so that it sees every OPTIONS request, including those
            # under a Mount; it matches only those it answers.
            routes.insert(0, _OptionsRoute(app.router))
    elif isinstance(app, APIRouter):
        _check_options_callables(app.routes)
        _add_path_options(app)
    else:
        raise TypeError(
            "app must be a FastAPI application or an APIRouter, "
            f"not {type(app).__name__}"
        )


class _OptionsRoute(BaseRoute):
    """Answers OPTIONS where a scrudful endpoint of ``router`` serves the URL.

    It matches an OPTIONS request only when no other route serves OPTIONS for
    its URL and a scrudful endpoint serves another method (see
    :func:`_resolve`); every other request goes on to the other routes.
    """

    name = "scrudful_options"
    path = ""
    include_in_schema = False

    def __init__(self, router: Router) -> None:
        self.router = router

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope.get("type") != "http" or scope.get("method") != "OPTIONS":
            return Match.NONE, {}
        served = _resolve(self.router.routes, scope)
        if served is None:
            return Match.NONE, {}
        return Match.FULL, {_SERVED_KEY: served}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        served = scope.get(_SERVED_KEY)
        if served is None:
            served = _resolve(self.router.routes, scope) or {}
        request = Request(scope, receive)
        response = _options_response(await _describe(request, served))
        await response(scope, receive, send)


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


def _resolve(
    routes: Sequence[Any], scope: Scope
) -> dict[str, tuple[Any, Any, Scope]] | None:
    """The ``(route, endpoint, scope)`` serving each method of the URL.

    The scope is the one the route would get for that method (its
    ``path_params``, ``route`` and ``endpoint``). ``None`` when another route
    (or a mounted app) serves OPTIONS for the URL, or no scrudful endpoint
    serves any of its methods.
    """
    served = {}
    for method in METHOD_KEYS:
        found = _match(routes, {**scope, "method": method.upper()})
        if method == "options":
            if found is not None:
                return None
            continue
        if found is not None:
            route, routed = found
            endpoint = routed.get("endpoint", getattr(route, "endpoint", None))
            served[method] = (route, endpoint, routed)
    if not any(_endpoint_config(endpoint) for _, endpoint, _ in served.values()):
        return None
    return served


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
    """The endpoint of the OPTIONS route of ``path`` on ``router``.

    It reads ``router``'s routes when a request comes, so that routes added
    later are described, and through whichever app or prefix the router is
    included.
    """

    async def scrudful_options(request: Request) -> Response:
        routes = [
            route
            for route in router.routes
            if isinstance(route, Route) and route.path == path
        ]
        served = {}
        for method in METHOD_KEYS:
            # The first route serving a method describes it, as Starlette
            # routes it.
            route = next(
                (
                    route
                    for route in routes
                    if route.methods is None or method.upper() in route.methods
                ),
                None,
            )
            if route is not None and method != "options":
                routed = {**request.scope, "route": route, "endpoint": route.endpoint}
                served[method] = (route, route.endpoint, routed)
        return _options_response(await _describe(request, served))

    scrudful_options._scrudful_options = True  # type: ignore[attr-defined]
    return scrudful_options


def _options_response(body: dict[str, Any]) -> Response:
    """The OPTIONS response with the HTTP Schema Resource ``body``."""
    response = JSONResponse(body)
    response.headers["Link"] = options_links()
    response.headers["Allow"] = allow_header(body)
    expose_headers(response.headers)
    return response


async def _describe(
    request: Request, served: Mapping[str, tuple[Any, Any, Scope]]
) -> dict[str, Any]:
    """The HTTP Schema Resource of the methods ``served``, and ``options``."""
    body: dict[str, Any] = {}
    for method in METHOD_KEYS:
        if method == "options":
            body[method] = describe_options()
        elif method in served:
            route, endpoint, routed = served[method]
            body[method] = await _describe_route(
                method, route, endpoint, routed, request
            )
    return body


async def _describe_route(
    method: str,
    route: Any,
    endpoint: Any,
    routed: Scope,
    request: Request,
) -> Any:
    """Describe ``route`` as the operation of ``method`` (see :func:`add_options`).

    The callables get a ``Request`` of the OPTIONS request with the scope
    ``routed``, the route's (its ``path_params``, ``route`` and ``endpoint``).
    """
    config = _endpoint_config(endpoint)
    if config is None:
        return {"responses": {"200": {"description": "OK"}}}
    routed_request = Request({**routed, "method": request.method}, request.receive)
    kwargs = _description_kwargs(config, routed_request)
    base = str(request.url)
    status = int(getattr(route, "status_code", None) or 200)
    conditional = _is_conditional(config, method)
    if method == "head":
        return describe_read(None, conditional, [status])
    if method == "trace":
        # Neither a read nor a write: its status, without content (and, never
        # conditional, without early responses), as django-ninja's.
        return describe_read(None, conditional, [status])
    content = await _media(config, _ENDPOINT_MEDIA_TYPE, kwargs, base)
    error_content = None
    if conditional and config.error is not None:  # scrudful's 400, 412, 428
        error_content = await _media(
            config.error, config.error.media_type, kwargs, base
        )
    if method == "get":
        description = describe_read(content, conditional, [status], error_content)
        if content and has_default_entity(method, status):
            # describe_read puts the content on a 200 only.
            success = description["responses"][str(status)]
            success.setdefault("content", copy.deepcopy(content))
        return description
    takes_body = getattr(route, "body_field", None) is not None
    return describe_write(
        method,
        content if takes_body else None,
        conditional,
        config.etag_func is not None,
        config.last_modified_func is not None,
        [status],
        response_content=content if has_default_entity(method, status) else None,
        error_content=error_content,
    )


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
    """The kwargs of an endpoint's callables outside the endpoint.

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
    """Check the callables an OPTIONS body calls (see :func:`add_options`)."""
    for endpoint, config, names in _endpoints(routes, for_options=True):
        where = f"endpoint {_endpoint_name(endpoint)}"
        _check_call(config, "", where, names, "in OPTIONS")
        # Called for the conditional methods: checked whichever it serves.
        if config.error is not None and _has_validators(config):
            _check_call(config.error, "error.", where, names, "in OPTIONS")


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
    for field in fields:
        func = getattr(source, field)
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
                f"{prefix}{field} of {where} ({func!r}) is called {purpose} with "
                f"the keyword arguments {sorted(names)} only ({meaning}), but "
                f"{exc}; accept **kwargs or give its other parameters defaults"
            ) from None


def _endpoint_name(endpoint: Any) -> str:
    return getattr(endpoint, "__qualname__", None) or repr(endpoint)


async def _media(
    source: _EndpointConfig | Entity,
    media_type: str,
    kwargs: Mapping[str, Any],
    base: str,
) -> dict[str, Any] | None:
    """The ``content`` of ``source``'s links: its Media Type Object, or ``None``.

    Built by :func:`scrudful._adapter.describe_content` from the schema and
    context URLs and types of ``source`` (an endpoint's options or an
    :class:`Entity`), which are evaluated as for its Links (the context only
    when ``media_type`` takes one), and with the URLs resolved against
    ``base`` the same way, so that the entry agrees with the Links of the
    responses it describes. ``None`` without a schema URL.
    """
    schema = await _value(source.schema_link_or_func, kwargs)
    if not schema:
        return None
    schema_type = await _value(source.schema_type_or_func, kwargs)
    context = context_type = None
    rel, default_type = context_defaults(media_type)
    if rel is not None:  # not in-band RDF
        context = await _value(source.context_link_or_func, kwargs)
    if context:
        context_type = await _value(source.context_type_or_func, kwargs) or default_type
    return describe_content(
        media_type,
        resolve_uri(schema, base),
        str(schema_type or SCHEMA_TYPE),
        resolve_uri(context, base) if context else None,
        None if context_type is None else str(context_type),
    )


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
    defaults = context_defaults(entity.media_type)
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
            context_of=entity.media_type,
        )
    return schema, context


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
