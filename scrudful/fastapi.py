"""SCRUDful conditional requests and Link headers for FastAPI endpoints.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to FastAPI's request/response handling.
"""

from __future__ import annotations

import functools
import inspect
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, get_args, get_origin

from fastapi import FastAPI, Request, Response
from fastapi import exception_handlers as fastapi_handlers
from fastapi.datastructures import DefaultPlaceholder
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException

from scrudful import _adapter
from scrudful._adapter import (
    BODY_HEADERS,
    DEFAULT_CONTEXT_REL,
    DEFAULT_CONTEXT_TYPE,
    DEFAULT_SCHEMA_REL,
    DEFAULT_SCHEMA_TYPE,
    check_error_media_type,
    check_validators,
    context_defaults,
    evaluated_signature,
    expose_headers,
    is_json,
    merge_links,
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

    Carried by the wrapper as ``wrapper._scrudful``, for the OPTIONS helper.
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
        Every ``*_func`` / ``*_or_func`` callable is called with the keyword
        arguments FastAPI resolved for the endpoint: path, query, body and
        dependency values, plus the endpoint's own ``Request`` / ``Response``
        parameters if it declares any (so a link callback can use
        ``request.url_for``). Accept ``**kwargs`` to ignore the ones you don't
        need. Callbacks may be ``def`` (run in the threadpool) or ``async def``;
        an awaitable result is awaited.

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

    The Link header is added to every response the endpoint produces and is
    appended to any Link value the endpoint (or a dependency) set; a Link it
    set with a ``describedby`` relation replaces the schema link, and one with
    a context relation the context link (see
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
    app's error responses Links. FastAPI does not route HEAD to a
    ``@app.get`` endpoint (it answers 405); use
    ``@app.api_route(..., methods=["GET", "HEAD"])`` to serve HEAD.

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

            if has_validators:
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
        the threadpool) or ``async def``.

    Raises
    ------
    TypeError
        If ``app`` isn't a ``FastAPI`` application or ``error`` isn't an
        :class:`Entity`.
    ValueError
        If ``error``'s media type is not JSON content (``application/json``
        or ``+json``, other than JSON-LD).
    """
    if not isinstance(app, FastAPI):
        raise TypeError(f"app must be a FastAPI application, not {type(app).__name__}")
    if not isinstance(error, Entity):
        raise TypeError(f"error must be an Entity, not {type(error).__name__}")
    check_error_media_type(error.media_type)
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
        link = merge_links(
            ", ".join(headers.getlist("link")),
            *await _entity_links(error, {"request": request}, base),
        )
        if link is not None:
            headers["Link"] = link
        headers["Content-Type"] = error.media_type
        _join_expose_lines(headers)
        expose_headers(headers)
        return response

    scrudful_handler._scrudful_error_handler = handler  # type: ignore[attr-defined]
    return scrudful_handler


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
) -> str | None:
    """Build one link-value from (url, rel, type) values or callables.

    Callables get ``kwargs``; the URL is resolved against ``base``, the
    request's absolute URI.
    """

    async def value(value_or_func: Any) -> Any:
        if callable(value_or_func):
            return await _call(value_or_func, kwargs)
        return value_or_func

    url_or_func, rel_or_func, type_or_func = values_or_funcs
    url = await value(url_or_func)
    if not url:
        return None
    rel = await value(rel_or_func) or defaults[0]
    content_type = await value(type_or_func) or defaults[1]
    return link_content(resolve_uri(url, base), rel, content_type)


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
