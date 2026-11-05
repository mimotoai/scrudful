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
import itertools
import logging
import re
import types
import warnings
import weakref
from collections.abc import (
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from typing import Annotated, Any, NamedTuple, TypeVar, get_args, get_origin
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi import params as fastapi_params
from fastapi import routing as fastapi_routing
from fastapi import exception_handlers as fastapi_handlers
from fastapi.datastructures import DefaultPlaceholder
from fastapi.encoders import jsonable_encoder
from fastapi.dependencies import models as fastapi_dependencies_models
from fastapi.dependencies import utils as fastapi_dependency_utils
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security.base import SecurityBase
from starlette.concurrency import run_in_threadpool
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders, URLPath
from starlette.exceptions import HTTPException
from starlette.routing import (
    BaseRoute,
    Match,
    Mount,
    NoMatchFound,
    Route,
    Router,
    compile_path,
)
from starlette.types import Receive, Scope, Send

from scrudful import _adapter
from scrudful._adapter import (
    BODY_HEADERS,
    DEFAULT_SCHEMA_REL,
    DEFAULT_SCHEMA_TYPE,
    JSON_LD_CONTEXT_REL,
    METHOD_KEYS,
    RDF_CONTEXT_REL,
    SCHEMA_REL,
    SCHEMA_TYPE,
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
    resolved_signature,
    expose_headers,
    has_default_entity,
    is_json,
    merge_links,
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
    HSR_ROUTE_SEGMENT,
    HsrConfigError,
    absolute_for_hsr,
    allow_value,
    check_base_url,
    check_hsr_cache_control,
    check_hsr_url_result,
    hsr_etag,
    hsr_link,
    hsr_response_links,
    hsr_route_name,
    is_preflight,
    options_operation,
    serialize_hsr,
)
from scrudful.conditional import (
    ConditionalResult,
    evaluate_preconditions,
    http_timestamp,
    quote_etag,
)
from scrudful.entity import Entity
from scrudful.problem import PROBLEM_JSON, problem_from_errors
from scrudful.utils import (
    _split_top_level,
    link_content,
    link_relations,
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
    "http_schema_resource_endpoint",
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EndpointConfig:
    """The :func:`scrudful` options of a decorated endpoint.

    Carried by the wrapper as ``wrapper._scrudful``, for :func:`add_options`
    and :func:`add_error_links`.
    """

    etag_func: Callable[..., Any] | None
    last_modified_func: Callable[..., Any] | None
    #: The entity of the endpoint's success content: the ``entity=`` given,
    #: else the link options' (an ``application/json`` entity).
    entity: Entity
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
        ``media_type`` as their Content-Type; without it, those of the app's
        :func:`add_error_links` entity (its callables called with
        ``request=`` only, as in the app's handlers), as the errors raised
        for the endpoint get; without either they carry no Link and are
        ``application/json``. Headers dependencies set on the
        injected ``Response`` are kept: all of them on a 304 (except
        ``Content-*`` metadata), only ``Set-Cookie`` and ``Link`` on the
        others. Of the Links a dependency set, those with the
        ``describedby`` relation or an RDF context one (JSON-LD's or
        ``rdf-context``) are dropped from the 400, 412 and 428: they
        describe the success representation, and the body is the error's,
        which only the error entity's Links describe. The others (an ``author``
        link, say) are kept, before the error's Links and the HSR link; one
        with the HSR relation replaces scrudful's.

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
    returns (an error, a 204, a redirect) gets none of them, and keeps any
    Link the endpoint set. Raise an error (``HTTPException``) rather than
    return it to give it Links, through :func:`add_error_links`.
    The Link header is appended to any Link value the endpoint (or a
    dependency) set; a Link it set with the relation of the schema link
    (``describedby`` by default) replaces that link, and one with either
    context relation (JSON-LD or ``rdf-context``, one slot) the context link
    (see :func:`scrudful._adapter.merge_links`). Link
    URLs are resolved against the ``base_url`` of the :func:`add_options`
    call covering the endpoint, as in its HTTP Schema Resource, else against
    the request's URL (``str(request.url)``), so a relative value such as
    ``"/schemas/item"`` is sent absolute.
    ``Access-Control-Expose-Headers`` is extended with ``Link`` and
    ``Allow``, and whichever of ``ETag`` and ``Location`` the response
    carries. Starlette's ``CORSMiddleware`` replaces that header on
    cross-origin requests, so if you use it, give it
    ``expose_headers=list(scrudful.utils.EXPOSE_HEADERS)`` instead.

    When an :func:`add_options` call describes the endpoint's path (the
    app's, or that of the router the endpoint is on), every response the
    endpoint returns (the 2xx, a HEAD, scrudful's 304, 400, 412 and 428, and
    any other) also gets the HTTP Schema Resource link of the path (the
    ``https://w3id.org/semantic-http/ns#http-schema-resource-document``
    relation, ``type="application/json"``), last; a Link with that relation
    the endpoint (or a dependency) set replaces it. A CORS preflight gets
    none. Without :func:`add_options`, no HTTP Schema Resource is served,
    and no response links one.

    If the endpoint returns a Starlette ``Response`` the headers are added to
    it, otherwise to the response FastAPI builds. ``HTTPException`` raised by
    the endpoint propagates unchanged; :func:`add_error_links` gives the
    app's error responses Links (this endpoint's ``error`` ones, when it has
    one), and :func:`add_options` answers OPTIONS with a 204 whose ``Allow``
    lists the requester's methods, links every response to the path's HTTP
    Schema Resource and serves that document by GET. FastAPI does not route
    HEAD to a ``@app.get`` endpoint (it answers 405); use
    ``@app.api_route(..., methods=["GET", "HEAD"])`` to serve HEAD, or stack
    ``@app.get(...)`` and ``@app.head(...)`` above ``@scrudful(...)``:
    FastAPI gives both methods of one ``api_route`` the
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
        The schema and context of scrudful's own 400, 412 and 428 bodies
        (without it, :func:`add_error_links`'s entity of the app describes
        them). Its link callables get the endpoint's keyword arguments, like the
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
    scrudful.entity.HsrConfigError
        A ``ValueError``: if ``schema_link_or_func`` is a value (not a
        callable) and there is no ``context_link_or_func``, as
        :class:`Entity` refuses it.
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
    # The endpoint's links: those of ``entity``, else the link options, an
    # application/json entity (Entity holds the rules: a schema link with no
    # context link is refused now, not when the HTTP Schema Resource is
    # first built).
    endpoint_entity = entity if entity is not None else Entity(**links_given)
    _check_error(error)
    check_error_body_func(error_body_func)
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
        return await _entity_links(endpoint_entity, kwargs, base)

    async def early_response(
        request: Request,
        result: ConditionalResult,
        injected: Headers,
        kwargs: Mapping[str, Any],
        base: str,
        hsr: str | None,
    ) -> Response:
        result = with_error_body(error_body_func, request, result)
        entity, entity_kwargs = error, kwargs
        if entity is None:
            # The entity the app's handlers give the errors raised for it.
            entity, entity_kwargs = _app_error(request), {"request": request}
        if result.body is None or entity is None:
            return _conditional_response(result, injected, hsr=hsr)
        links = await _entity_links(entity, entity_kwargs, base)
        return _conditional_response(result, injected, links, entity.media_type, hsr)

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
            entity=endpoint_entity,
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
            base = _live_base(request, wrapper)
            # Before the endpoint runs: a misconfiguration it raises (two
            # resources with one HSR route name) must not follow a write.
            hsr = _response_hsr_link(request)

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
                        request, result, response.headers, kwargs, base, hsr
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
            schema = context = None
            if has_default_entity(method, status):
                # Another response the endpoint returns (an error, a 204, a
                # redirect, or a TRACE) is not described by its links: it
                # gets none of them, only the HSR link.
                schema, context = await links(kwargs, base)
            link = merge_links(
                ", ".join(headers.getlist("link")) or None, schema, context, hsr
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
    ``fastapi.HTTPException`` or for a status code: the errors the endpoints
    raise, the 422 of request validation, and the 404 and 405 Starlette's
    routing raises as ``HTTPException`` for an unknown path or an unrouted
    method. A handler registered for another class is not wrapped.

    It also wraps the app's server error handler, the one for ``500`` or
    ``Exception`` (Starlette's ``ServerErrorMiddleware`` takes the last
    registered of the two), which answers an exception no other handler
    does; without one, it installs one for ``Exception`` answering a JSON
    500, ``{"detail": "Internal Server Error"}``, in place of Starlette's
    ``text/plain`` one. Starlette's ``ServerErrorMiddleware`` calls it, and
    sends its response, only with ``debug`` off: an app with ``debug=True``
    answers with Starlette's traceback page instead. Either way it raises
    the exception again once the response is sent (so that the server logs
    it; Starlette's ``TestClient`` raises it unless created with
    ``raise_server_exceptions=False``). That middleware is outside every
    user middleware, so the 500 gets no headers of a ``CORSMiddleware``.
    Its Links never replace the exception it answers: a link callable (or
    ``hsr_url``) that fails while they are built (as the endpoint did, say,
    with the database down) is logged on the ``scrudful.fastapi`` logger,
    and the 500 sent without that Link; the exception raised again is the
    endpoint's.

    Each wrapped handler returns the response the original makes, with the
    same status, headers and body, and when it has JSON content (an
    ``application/json`` or ``+json`` Content-Type) adds ``error``'s Links to
    it, as :func:`scrudful` adds them (a Link the handler set with the
    relation of one of them suppresses it; see
    :func:`scrudful._adapter.merge_links`), and serves it as
    ``error.media_type``. Responses without content (e.g. a 304) or with
    other content get no entity Links, and keep their Content-Type. Link
    URLs are resolved against the ``base_url`` of :func:`add_options`, else
    the request's URL.

    With an ``application/problem+json`` entity
    (:data:`scrudful.problem.PROBLEM_JSON`), the bodies of FastAPI's default
    validation handler and of the JSON 500 above are RFC 9457 problem
    details: the 422 is
    ``problem_from_errors(422, exc.errors(), "Request validation failed")``
    (:func:`scrudful.problem.problem_from_errors`, FastAPI's errors as the
    ``errors`` member, encoded with ``jsonable_encoder`` as FastAPI encodes
    them), the 500 ``problem_from_errors(500, None)``. A handler of the
    app's own keeps its body. The entity deciding is the one of the
    response's Links (below).

    When an :func:`add_options` call describes the resource the request was
    routed to (its URL matched a route of a path with a scrudful endpoint),
    every response of a wrapped handler, whatever its content, also gets the
    resource's HTTP Schema Resource link, last (a Link with that relation
    the handler set replaces it), and the expose headers: the 401, 403, 404,
    405 and 422 of a described path, and its 500. A request whose URL no
    route matches (an unknown path's 404), or that is to a path without a
    scrudful endpoint, gets none.

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

    So a custom handler registered before the call is kept (wrapped), and
    calling this again replaces the entity of the earlier call: no Link is
    added twice. A handler registered after the call replaces the wrapped
    one; call this after registering your handlers and before the app
    serves requests (its middleware stack, holding the handlers, is built on
    the first request).

    scrudful's own 400, 412 and 428 are returned by the endpoint, not raised:
    they get the ``error`` of :func:`scrudful` when it has one, else this one
    (its callables called with ``request=`` only, as here), on the wire and
    in the HTTP Schema Resource.

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
    # The server error handler: Starlette's ServerErrorMiddleware takes the
    # last of the app's handlers for 500 and Exception; without one, a JSON 500.
    server = [key for key in handlers if key is Exception or _is_500(key)]
    if not server:
        handlers[Exception] = _server_error
        server = [Exception]
    keys += server
    for key in keys:
        handler = handlers[key]
        # Calling this again replaces the earlier entity.
        handler = getattr(handler, "_scrudful_error_handler", handler)
        wrapped = _error_handler(handler, error, server=key in server)
        app.add_exception_handler(key, wrapped)


def _error_handler(
    handler: Callable[[Request, Exception], Any],
    error: Entity,
    server: bool = False,
) -> Callable[[Request, Exception], Any]:
    """``handler``, an exception handler of the app, adding ``error``'s Links.

    ``server``: it is the app's server error handler, answering an exception
    no other handler does. Building its Links calls user code (link
    callables, ``hsr_url``), which may fail as the endpoint did (the
    database is down): what fails is logged on this module's logger and the
    500 sent without it, so that the exception being answered is the one
    Starlette's ``ServerErrorMiddleware`` raises again (for the server's
    log), not the link's. Another handler's failing link is an error of its
    own, which the server error handler answers.
    """

    @functools.wraps(handler)
    async def scrudful_handler(request: Request, exc: Exception) -> Any:
        if request.scope.get("type") != "http":
            return await _call_handler(handler, request, exc)

        async def built(make: Callable[[], Any], fallback: Any) -> Any:
            try:
                result = make()
                return (await result) if inspect.isawaitable(result) else result
            except Exception:
                if not server:
                    raise
                logger.exception(
                    "scrudful: a Link of the 500 answering %r failed; the 500 "
                    "is sent without it",
                    exc,
                )
                return fallback

        entity, kwargs = await built(
            lambda: _raised_entity(request, error), (error, {"request": request})
        )
        response = _problem_response(handler, exc, entity)
        if response is None:
            response = await _call_handler(handler, request, exc)
        if not isinstance(response, Response):
            return response
        headers = response.headers
        hsr = await built(lambda: _response_hsr_link(request), None)
        schema = context = None
        if _has_json_content(response):
            route = request.scope.get("route")
            # The routed endpoint: an included router's Starlette route (as
            # scrudful's router-mode OPTIONS) leaves the router in "route".
            endpoint = getattr(route, "endpoint", None) or request.scope.get("endpoint")
            base = _live_base(request, endpoint)
            schema, context = await built(
                lambda: _entity_links(entity, kwargs, base), (None, None)
            )
            headers["Content-Type"] = entity.media_type
        elif hsr is None:
            # Other content (or none, e.g. a 304) gets no entity Links.
            return response
        link = merge_links(
            ", ".join(headers.getlist("link")) or None, schema, context, hsr
        )
        if link is not None:
            headers["Link"] = link
        _join_expose_lines(headers)
        expose_headers(headers)
        return response

    scrudful_handler._scrudful_error_handler = handler  # type: ignore[attr-defined]
    return scrudful_handler


def _is_500(key: Any) -> bool:
    """Whether an ``exception_handlers`` key is the status code 500 (not
    ``True`` or a class)."""
    return isinstance(key, int) and not isinstance(key, bool) and key == 500


async def _server_error(request: Request, exc: Exception) -> Response:
    """The app's server error handler :func:`add_error_links` installs when it
    has none: a JSON 500, ``{"detail": "Internal Server Error"}`` (problem
    details for a problem-details entity, see :func:`_problem_response`)."""
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


def _problem_response(
    handler: Callable[[Request, Exception], Any], exc: Exception, entity: Entity
) -> Response | None:
    """With an ``application/problem+json`` ``entity``, the RFC 9457 problem
    details answering ``exc`` in place of a default ``handler``'s body, else
    ``None`` (the handler answers).

    - FastAPI's request validation 422 (``{"detail": [errors]}``, whose
      ``detail`` problem details want a string):
      ``problem_from_errors(422, exc.errors(), "Request validation
      failed")``, JSON-safe as FastAPI's own (``jsonable_encoder``: an
      error's ``ctx`` may hold the exception a validator raised);
    - the JSON 500 of :func:`_server_error`: ``problem_from_errors(500, None)``.

    A handler of the app's own keeps its body.
    """
    if essence(entity.media_type) != PROBLEM_JSON:
        return None
    if handler is fastapi_handlers.request_validation_exception_handler and (
        isinstance(exc, RequestValidationError)
    ):
        problem = problem_from_errors(422, exc.errors(), "Request validation failed")
        return JSONResponse(jsonable_encoder(problem), status_code=422)
    if handler is _server_error:
        return JSONResponse(problem_from_errors(500, None), status_code=500)
    return None


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


def _app_error(request: Request) -> Entity | None:
    """The :func:`add_error_links` entity of the app serving ``request``
    (whose exception handlers answer the errors raised for it), if any."""
    app = request.scope.get("app")
    try:
        return _APP_ERRORS.get(app)
    except TypeError:  # not weakly referenceable
        return None


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
#: A path parameter of a route template, ``{name}`` or ``{name:convertor}``:
#: the name, and the convertor (see :func:`_convertor`).
_TEMPLATE_PARAMETER = re.compile(r"\{([^{}:]*)(?::([^{}]*))?\}")
#: The segments of a Mount's key that match any one segment (``str``), or
#: any rest of the path (``path``); a segment of another convertor (``int``,
#: ``uuid``, a custom one) matches only some, so it shadows only itself.
_ANY_SEGMENT = frozenset({"{}", "{:path}"})


@functools.lru_cache(maxsize=4096)
def _template_key(path: str) -> str:
    """The resource key of the route template ``path``: its parameters'
    names left out, their convertors kept (``str`` is no convertor), so two
    templates have one key exactly when they match the same URLs:
    ``/items/{id}``, ``/items/{item_id}`` and ``/items/{x:str}`` are
    ``/items/{}``, ``/items/{id:int}`` is ``/items/{:int}``."""

    def parameter(match: re.Match[str]) -> str:
        convertor = _convertor(match)
        return f"{{:{convertor}}}" if convertor else "{}"

    return _TEMPLATE_PARAMETER.sub(parameter, path)


def _convertor(match: re.Match[str]) -> str:
    """The convertor of a :data:`_TEMPLATE_PARAMETER` match, ``""`` for none
    (``str``, Starlette's default, is none)."""
    convertor = (match.group(2) or "").strip()
    return "" if convertor == "str" else convertor


@dataclass
class _Resource:
    """The routes of one resource (a :func:`_template_key`), in routing order,
    and the template of the first, which names it."""

    template: str
    routes: list[Any] = field(default_factory=list)


#: FastAPI's flattening of included routers into their routes, as its OpenAPI
#: generation walks them (FastAPI versions that include routers lazily); older
#: versions copy an included router's routes, so ``routes`` is flat already.
_iter_route_contexts = getattr(fastapi_routing, "iter_route_contexts", None)


class _Built(NamedTuple):
    """A built HTTP Schema Resource, with what it was built from: valid while
    its path's routes are the same objects in the same order, and its entity
    the same object (it is kept by resource key and ``id`` of the entity)."""

    #: The path's routes (their ``original_route``), compared by identity.
    routes: tuple[Any, ...]
    #: The :func:`add_error_links` entity described (``None``: none), compared
    #: by identity; held here, so its ``id`` in the key isn't reused.
    error: Entity | None
    document: dict[str, Any]


class _Serialized(NamedTuple):
    """A served document's bytes and ``ETag``: valid while ``document`` is
    the built one (compared by identity)."""

    document: dict[str, Any]
    body: bytes
    etag: str


@dataclass
class _HsrOptions:
    """The :func:`add_options` options of an app or router, and its built
    HTTP Schema Resources."""

    base_url: str | None = None
    hsr_route: bool = True
    hsr_url: Callable[[str], str] | None = None
    hsr_cache_control: str = DEFAULT_HSR_CACHE_CONTROL
    #: ``allowed(request, method, obj)``, for the OPTIONS ``Allow``.
    allowed: Callable[..., Any] | None = None
    #: ``load(request, **path_params)``, the object of the OPTIONS checks.
    load: Callable[..., Any] | None = None
    #: Whether :func:`add_options` gave them (else they are the defaults, for
    #: :func:`http_schema_resource` on an app without it).
    explicit: bool = False
    #: Each built document, by resource key and ``id`` of the
    #: :func:`add_error_links` entity it describes (a router included in
    #: several apps has one per app's entity, ``None``'s for an app without
    #: one). By identity, not equality: an entity may be unhashable (a link
    #: callable object defining ``__eq__``), and the cache is of the entity
    #: given, which the :class:`_Built` holds.
    built: dict[tuple[str, int], _Built] = field(default_factory=dict)
    #: Each served document's bytes and ETag, by the same keys.
    serialized: dict[tuple[str, int], _Serialized] = field(default_factory=dict)
    #: The owner's resources, indexed (see :func:`_scrudful_paths`).
    index: _PathsIndex | None = field(default=None, compare=False, repr=False)
    #: The resource keys of a router announced as having no OPTIONS or HSR
    #: route (see :func:`_announce_unserved`), each once.
    announced: set[str] = field(default_factory=set, compare=False, repr=False)
    #: A router's routes as :func:`add_options` and the completions since
    #: left them (see :func:`_complete`).
    completion: _Completion | None = field(default=None, compare=False, repr=False)


@dataclass(eq=False)
class _PathsIndex:
    """An app's or router's :func:`_scrudful_paths`, and what each request
    looks up in them, kept until the routes change (see
    :func:`_routes_fingerprint`): a response's HSR link needs no walk of
    every route of the app."""

    #: :func:`_routes_fingerprint` of the routes indexed.
    fingerprint: tuple[Any, ...]
    #: The resources, by :func:`_template_key`.
    paths: dict[str, _Resource]
    #: The keys of the resources each route is on, by ``id`` of the route
    #: (its ``original_route``), in the order of ``paths``.
    by_route: dict[int, list[str]]
    #: The same, by ``id`` of the route's endpoint.
    by_endpoint: dict[int, list[str]]
    #: :func:`_hsr_names` of ``paths``, once computed (it may raise).
    names: dict[str, str] | None = None
    #: The HSR route name of each resource key, from ``names``.
    name_of: dict[str, str] | None = None


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
    hsr_route: bool | None = None,
    hsr_url: Callable[[str], str] | None = None,
    hsr_cache_control: str | None = None,
    allowed: Callable[..., Any] | None = None,
    load: Callable[..., Any] | None = None,
) -> None:
    """Answer OPTIONS on ``app``'s scrudful paths, linking to their HTTP
    Schema Resource.

    Each path template (the route's path string, e.g. ``"/items/{item_id}"``)
    that a :func:`scrudful` endpoint serves with some method has an HTTP
    Schema Resource (Semantic HTTP 0.1, an OpenAPI 3.1 Path Item), the same
    for every requester: :func:`http_schema_resource` returns it, and the HSR
    route (below) serves it. An OPTIONS request to a URL of such a path,
    which no other route serves OPTIONS for, is answered with a 204 without
    content (and without ``Content-Type``), an ``Allow`` listing exactly the
    methods this requester may use (below), one Link to the document (the
    ``https://w3id.org/semantic-http/ns#http-schema-resource-document``
    relation, ``type="application/json"``) and
    ``Access-Control-Expose-Headers``. Other OPTIONS requests are routed as
    before (e.g. a 405, or your own OPTIONS route). The OPTIONS answer has
    no dependencies (neither the app's, a router's nor an ``include_router``
    call's): OPTIONS itself needs no credentials (an anonymous requester on
    a locked resource gets ``Allow: OPTIONS``, not a 401), and it is not in
    the OpenAPI document.

    The answer runs in this order:

    1. A CORS preflight (an OPTIONS with ``Origin`` and
       ``Access-Control-Request-Method``) is a 204 with no content, no Links
       and no ``Allow``, before any dependency, ``load`` or ``allowed`` runs.
       A ``CORSMiddleware`` in front usually answers it first, and adds its
       headers to this one when it doesn't. Configure it for Semantic HTTP's
       browser clients: ``allow_methods=["*"]`` (or a list with ``OPTIONS``
       and the resources' methods: its default, ``GET`` only, answers the
       preflight of a PUT or of an OPTIONS with a 400), and
       ``expose_headers=list(scrudful.utils.EXPOSE_HEADERS)`` (it replaces
       the ``Access-Control-Expose-Headers`` scrudful sets on a
       cross-origin response).
    2. Each method the resource's routes serve (all of them: a plain FastAPI
       or Starlette route on the path too) is checked for this requester,
       without the object. With ``allowed``, every method passes this step:
       the hook needs the object. Otherwise the route's security
       dependencies decide, and only they run: those declared with
       ``Security(...)``, and any ``Depends`` relying on a security scheme
       (a ``fastapi.security`` class such as ``HTTPBearer``, or a
       ``Security(...)``, at any depth, as a ``get_current_user`` reading a
       token does), wherever they are declared (a parameter of the
       endpoint, or ``dependencies=`` of the route, its routers,
       ``include_router`` or the app). A dependency with no security scheme
       below it is not run, even in ``dependencies=``: authorization written
       as a plain ``Depends`` (hand-rolled header checks) doesn't show in
       ``Allow``; declare it with ``Security(...)``, or give ``allowed``.
       They are resolved by FastAPI (its
       ``solve_dependencies``, so with ``dependency_overrides``) on a copy
       of the request whose method is the method checked, without its body
       and without validating the endpoint's own parameters; a dependency
       with ``yield`` is cleaned up before the answer. An ``HTTPException``
       with 401, 403 or 404, or a parameter of theirs failing validation
       (FastAPI's 422), means not allowed. No other dependency of the
       route, and never the endpoint, runs: a throttling dependency
       declared beside the security ones (in ``dependencies=`` or as a
       parameter) isn't run, and consumes no quota (FastAPI has no
       throttles of its own). A security dependency runs whole, though,
       with its own sub-dependencies, plain ones included: a database
       session or a throttle that a ``get_current_user`` depends on runs
       for each method checked, and what it consumes or writes, it does
       on OPTIONS too. Keep such work out of security dependencies, or
       give ``allowed``. A security dependency that needs the
       request's body (a body, form or file parameter, at any depth) can't
       be resolved on OPTIONS, which has none: its method is not allowed,
       for every requester (it fails closed), and a ``RuntimeWarning``
       names the route, once, when the routes are indexed (by this call,
       or by a request after routes were added); give ``allowed`` to
       decide such a method. The same holds when OPTIONS can't tell
       whether a parameter declares a security dependency: its annotation
       can't be evaluated (a name imported only under ``TYPE_CHECKING``,
       with ``from __future__ import annotations``) and names
       ``Security(`` or ``Depends(``, or a signature (of the endpoint or of
       a dependency) can't be read. Each annotation is evaluated on its
       own, so one that can't be doesn't hide the ``Security(...)`` of
       another. ``HEAD``
       is listed only when a route serves it (FastAPI's GET route doesn't;
       list ``"HEAD"`` in its methods, as ``@app.head`` does), with that
       route's checks.
    3. When at least one method passed and the URL has path parameters,
       the object is loaded: ``load(request, **path_params)``, else there is
       none (``None``). So a
       requester whom the security dependencies refuse everything gets a 204
       with ``Allow: OPTIONS`` for a missing and an existing object alike,
       and can't tell which exist. With ``allowed``, every requester passes
       step 2, so the object is loaded for each: ``load`` should then hide
       (``None``, or a 404) the objects a requester may not see, or OPTIONS
       tells which exist.
    4. With ``allowed``, ``allowed(request, method, obj)`` alone decides
       each method.
    5. The 204 answers, ``Allow`` listing the methods allowed and OPTIONS.

    An ``HTTPException`` raised by ``load`` (a 404 for a missing object, or
    any other status), by ``allowed`` with a status other than 401 and 403
    (which refuse the method), or by a check with a status other than 401,
    403 and 404 (a throttle's 429), is answered by the app's exception
    handlers (those of :func:`add_error_links` add the error
    entity's Links), with the HSR link; anything else raised propagates (a
    500). Without ``allowed``, object-level rules (who may change *this*
    object) can't show in ``Allow``: give ``allowed`` (and ``load``) for an
    exact one.

    Given a ``FastAPI`` app, the resources are keyed by the full path
    templates of its routes (those of the routers included in it, with their
    prefixes, and those under a ``Mount(routes=...)``, with the Mount's
    path), with the parameters' names left out and their convertors kept
    (``str`` being none): templates that match the same URLs, such as
    ``/items/{id}`` and ``/items/{item_id}``, on the app or on several
    routers, are one resource. Its document describes, for each method, the
    first of them that serves it, as Starlette routes it: a route after a
    ``Mount`` whose
    path covers all its URLs is never reached, and is left out. The OPTIONS
    of a URL is answered for the resource of the first route whose path
    matches it: its ``Allow`` covers the methods of that resource's routes,
    and it links to that resource's document.
    Templates that overlap otherwise are separate resources: ``/items/new``
    beside ``/items/{id}``, or ``/items/{id:int}`` beside ``/items/{name}``
    (or ``/files/{p:path}`` beside ``/files/{name}``), each have their own
    document and OPTIONS answer, although a URL both match (``/items/new``,
    ``/items/5``) reaches whichever route comes first; avoid such overlaps,
    or give them the same methods. Given an ``APIRouter``,
    it adds an OPTIONS route to each resource of the router's routes that
    has a scrudful route (at the path of its first route), those of the
    routers included in it, at any depth and with their prefixes, included
    (``/c/items/{id}`` for a router included with ``prefix="/c"``), unless a
    route already serves OPTIONS for it, answered as
    above with the router's options, linking to the router's document of it
    (keyed by the router's own templates), its checks running the
    dependencies the app and ``include_router`` give the routes;
    call it before including the router (older FastAPI versions copy a
    router's routes when it is included). Prefer calling it on the app.

    With ``hsr_route`` (the default), it also serves each resource's
    document by GET and HEAD at ``/_hsr/<name>``, where ``name`` is the
    resource's template (that of its first route) with its segments joined
    by ``.``, and each parameter's convertor but ``str`` joined to its name
    by ``-``: ``/_hsr/users.id`` for ``/users/{id}``,
    ``/_hsr/users.id-int`` for ``/users/{id:int}``, ``/_hsr/_root`` for
    ``/``. Given an app, one route, ``/_hsr/{name}``, resolves the name on
    each request, so the paths added later are served too; it matches only
    the names of the app's resources, so any other ``/_hsr/<x>`` goes on to
    your routes (or a sub-app mounted at ``""``). It is a Starlette
    ``Route``, not an ``APIRoute``, so no app, router or ``include_router``
    dependency applies to it (it needs no credentials), and it is not in
    the OpenAPI document; it is put before every other route, ahead of a
    path such as ``"/{id}"``. It answers ``application/json``, the compact
    JSON of the document (:func:`~scrudful._hsr.serialize_hsr`), the Links
    of the HTTP Schema Resource's own schema and context, a strong
    ``ETag``, ``Cache-Control`` (``hsr_cache_control``) and
    ``Access-Control-Expose-Headers``; a 304 to a matching
    ``If-None-Match`` (weak comparison); and a 405 with ``Allow: GET,
    HEAD``, and no content, to any other method. The bytes are the same
    for every requester. Its CORS headers are your middleware's: have it
    cover ``/_hsr/`` with the resources' origins. Given an ``APIRouter``,
    as for its OPTIONS routes, each of its resources gets its own route,
    ``/_hsr/<name>``, on the router (call this before including the
    router), so the prefix it is included under applies:
    ``/v1/_hsr/items.id`` for its ``/items/{id}`` included with
    ``prefix="/v1"``. Paths under ``/_hsr/`` are reserved,
    and two resources can't have one name (``/a.b`` beside ``/a/b``): this
    raises ``ValueError`` for the paths there are, and an app's HSR route
    raises it on a request for paths added since.

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
      with the content of its ``error``, else of :func:`add_error_links`'s
      entity (none without either);
    - a scrudful HEAD: the same responses, without content;
    - a scrudful TRACE: its success status, without content: a TRACE's
      content is the echoed request, which the endpoint's links don't
      describe, and it gets none of them;
    - a scrudful PUT, PATCH, DELETE or POST: the request body (the
      endpoint's content, when the endpoint takes a body; DELETE has none),
      the success status with that content (but a 204), and, when
      conditional, scrudful's 400, 412 and (but for POST) 428 with the
      content of the ``error`` (else of :func:`add_error_links`'s entity)
      and the precondition header parameters;
    - ``options``: ``{"responses": {"204": {"description": "No Content"}}}``;
    - a method served by a route that isn't scrudful: its success status,
      without content or a request body.

    Each method also lists the statuses its route declares in
    ``responses=`` (a status code, or a range such as ``"4XX"``), with the
    declared ``description`` (else the reason phrase): a success status
    (2xx but 204) with the endpoint's content, under its entity's media
    type (another media type the declared ``content`` lists is not
    described, since scrudful's Links describe the entity's: give the
    endpoint ``entity=`` to describe another), an error status (4xx, 5xx)
    with the content of the error entity its raised errors get (below), any
    other status
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
    :func:`add_error_links` entity change. A router included in several
    apps has a document per app's entity (``None`` for an app without
    :func:`add_error_links`): each app's HSR route serves the one that
    agrees with that app's responses. Link callables are called in
    *describe* mode, ``func()``, with no arguments, even those that take the
    endpoint's keyword arguments inside it: give such parameters defaults
    (or accept ``**kwargs``). A callable must return the same target in both
    modes, or the document disagrees with the responses it describes. Async
    callables are awaited. Validator callbacks are not called. Relative URLs
    are resolved against ``base_url``; the live Links of the responses
    resolve against it too (else against the request's URL).

    Every response of a resource it describes links to the resource's
    document as its OPTIONS does: those the :func:`scrudful` endpoints
    return, and the errors :func:`add_error_links`'s handlers answer for it
    (see there).

    Call it once the routes are added. Routes added later are described
    too. On an app, as they are found (their callables are not checked). On
    a router, as they are added with its route decorators (``get`` ...),
    ``add_api_route`` or ``include_router``, its own or those of a router
    included in it: they get their OPTIONS and HSR routes then, and their
    callables are checked then (this ``ValueError`` is raised by the call
    adding them); each is completed alone, without a walk of the router's
    other routes. Those methods are wrapped on the router instances (and
    those included in them): a ``copy.copy`` of the router shares the
    wrappers, which add to, and complete, the original; ``copy.deepcopy``
    of a router given this fails (``RecursionError``). A route added to a
    router otherwise (appended to its ``routes``, or by
    ``APIRouter.add_api_route(router, ...)``) gets neither until the next
    route is added with the router's methods (which completes it too), so
    until then it isn't described and its responses get no HSR link (it
    would lead to a 404): a ``RuntimeWarning`` names its resource, once,
    when the router's resources are next indexed (a request to one of them,
    or the route's own first response); call this again. An HSR route
    replaced or removed from the routes (``routes[i] = ...``) is forgotten
    when they are next indexed: its resource's OPTIONS answers without the
    HSR link, and its responses carry none, until the next route added or
    call gives it back. Calling it again replaces the options, and
    adds the routes still missing: the HSR route on an app (when
    ``hsr_route`` is now on), the OPTIONS and HSR routes of a router's
    resources, served at once, also by a router already included and
    serving requests (on FastAPI versions that include routers lazily; the
    others copy a router's routes when it is included).

    Parameters
    ----------
    app : FastAPI or APIRouter
        The application or router whose scrudful paths answer OPTIONS.
    base_url : str, optional
        The absolute URL relative link URLs resolve against, in the HTTP
        Schema Resource and on the Link headers of the responses of the
        paths covered. Without it, a relative URL in the document raises
        ``ValueError`` when the document is built.
    hsr_route : bool or None, optional
        Whether to serve each path's document by GET at ``/_hsr/<name>``
        (``None``, the default, is ``True``, as there is no setting to read).
        Turning it off needs ``hsr_url``.
    hsr_url : callable, optional
        ``hsr_url(path)``: the URL the document of ``path`` (the resource's
        template, its first route's) is served at, when it is mounted
        elsewhere (see :func:`http_schema_resource_endpoint`). The
        resources link to it, as it returns it, instead of the HSR route.
    hsr_cache_control : str, optional
        The ``Cache-Control`` of the document's GET response (default
        ``"public, max-age=300"``).
    allowed : callable, optional
        ``allowed(request, method, obj) -> bool``: whether the requester may
        use ``method`` (``"GET"``, ``"PUT"`` ...) on ``obj`` (what ``load``
        returned, else ``None``), for ``Allow``; when given, it alone
        decides, for every method of every path covered (no security
        dependency runs). ``request`` is a copy of the OPTIONS request whose
        method is ``method``, with the URL's path parameters. It is called
        for every requester, after ``load`` (see step 3 above for what that
        reveals). A ``def`` runs in the threadpool, an ``async def`` (or an
        awaitable result) is awaited. Anything but a ``bool`` returned is a
        ``TypeError``. An ``HTTPException`` 401 or 403 it raises refuses the
        method; another (a 404) is the OPTIONS's answer.
    load : callable, optional
        ``load(request, **path_params)``: the object at the URL, for
        ``allowed``, or ``None`` for none (a collection, a missing object);
        an ``HTTPException`` it raises (a 404) is the answer. ``request`` is
        the OPTIONS request, and ``path_params`` the URL's path parameters,
        as Starlette converts them (a ``str`` without a convertor), named as
        in the template of the resource's first route, all of them (those
        of a ``Mount`` or of the prefix a router is included under too).
        Called only when the matched URL has path parameters, and only when
        a method passed step 2: a URL without any (a top-level collection
        such as ``/items``) has no object, and ``load`` isn't called. A
        nested collection (``/orgs/{org}/items``) is called with its
        parent's parameters (``org``): return ``None`` for it, or raise its
        parent's 404. A ``def`` runs in the threadpool, an ``async def`` is
        awaited.

    Raises
    ------
    TypeError
        If ``app`` is neither a ``FastAPI`` application nor an ``APIRouter``,
        or an option has the wrong type.
    ValueError
        If ``base_url`` is not an absolute URL; ``hsr_route`` is off without
        ``hsr_url``; a scrudful path is under ``/_hsr/``; two resources have
        one HSR route name (with ``hsr_route``); or a link callable of a
        scrudful endpoint (or of its ``error``) can't be called with no
        arguments.
    RuntimeError
        If the installed FastAPI's
        ``fastapi.dependencies.utils.solve_dependencies``, which the
        security checks rely on, or the way it builds a route's dependants
        from its ``dependencies=``, differs from those of the FastAPI
        releases scrudful supports (or a route's ``dependencies`` were
        changed after it was created), when a route has security
        dependencies to check (no ``allowed``). An OPTIONS request raises
        it for the routes added since, and if the ``Dependant`` or exit
        stack ``solve_dependencies`` takes differ.
    """
    _check_owner(app)
    options = _check_hsr_options(
        base_url, hsr_route, hsr_url, hsr_cache_control, allowed, load
    )
    routes = app.router.routes if isinstance(app, FastAPI) else app.routes
    _check_options_callables(routes)
    # Found afresh, not indexed: the index belongs to the options.
    paths = _find_scrudful_paths(routes)
    _check_reserved(paths)
    names = _hsr_names(paths) if options.hsr_route else {}
    _check_internals(options, paths)
    setattr(app, _OPTIONS_ATTRIBUTE, options)
    if isinstance(app, FastAPI):
        if not any(isinstance(route, _OptionsRoute) for route in routes):
            # First, so that it sees every OPTIONS request, including those
            # under a Mount; it matches only those it answers.
            routes.insert(0, _OptionsRoute(app))
            _routes_changed(app.router)
        if options.hsr_route:
            _add_hsr_routes(app, routes, paths, names)
    else:
        _add_router_routes(app, paths, names)
        options.completion = _completion_of(app, names)
        _watch(app)
        _remember_watched(app)
    # Indexed now, at startup: what indexing reports is reported here.
    _paths_index(app)


def _check_hsr_options(
    base_url: Any,
    hsr_route: Any,
    hsr_url: Any,
    hsr_cache_control: Any,
    allowed: Any,
    load: Any,
) -> _HsrOptions:
    """The :func:`add_options` options, checked."""
    if hsr_cache_control is not None and not isinstance(hsr_cache_control, str):
        raise TypeError(
            f"hsr_cache_control must be a str, not {type(hsr_cache_control).__name__}"
        )
    if hsr_route is None:
        # As ninja's and DRF's None, which read a setting FastAPI hasn't.
        hsr_route = True
    if not isinstance(hsr_route, bool):
        raise TypeError(
            f"hsr_route must be a bool or None, not {type(hsr_route).__name__}"
        )
    for name, value in (("hsr_url", hsr_url), ("allowed", allowed), ("load", load)):
        if value is not None and not callable(value):
            raise TypeError(f"{name} must be callable, not {type(value).__name__}")
    if base_url is not None:
        check_base_url(base_url, "base_url")
    if hsr_cache_control is not None:
        check_hsr_cache_control(hsr_cache_control, "hsr_cache_control")
    if not hsr_route and hsr_url is None:
        raise ValueError(
            "hsr_route=False needs hsr_url=, hsr_url(path): the URL each "
            "path's HTTP Schema Resource is served at (see "
            "http_schema_resource_endpoint), which every resource links to"
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


def http_schema_resource(
    app: FastAPI | APIRouter, path: str, *, for_app: FastAPI | None = None
) -> dict[str, Any]:
    """The HTTP Schema Resource (an OpenAPI 3.1 Path Item) of ``path``.

    ``path`` is the full path template of the routes, as :func:`add_options`
    keys them (e.g. ``"/v1/items/{item_id}"`` for a router included with
    ``prefix="/v1"``; for an ``APIRouter``, the router's own template), with
    any parameter names: ``"/v1/items/{id}"`` gives the same document. The
    document is the same for every requester: it is built without a
    request, once (see :func:`add_options`, which describes its content and
    whose ``base_url`` it uses), and a copy is returned.

    For an ``APIRouter`` included in several apps, each app's document
    describes that app's :func:`add_error_links` entity (none for an app
    without one), as its HSR route serves it: ``for_app`` names the app.
    Without it, or when ``for_app`` includes none of the router's endpoints
    (a docs app serving it with :func:`http_schema_resource_endpoint`), the
    entity of the apps including the router, which must then agree.

    Link callables are called in *describe* mode, ``func()``: with no
    arguments. They must return the same target as on the responses they
    describe, or the description disagrees with the responses. An async
    one is awaited (in a worker thread when this is called inside a running
    event loop).

    Raises
    ------
    TypeError
        If ``app`` is neither a ``FastAPI`` application nor an ``APIRouter``,
        or ``for_app`` (if given) isn't a ``FastAPI`` application.
    ValueError
        If no scrudful endpoint serves ``path``; a link URL is relative and
        there is no base URL; a link callable raises when called as
        ``func()``; or, without ``for_app``, a router is included in apps
        that :func:`add_error_links` gave different entities.
    """
    _check_owner(app)
    if for_app is not None and not isinstance(for_app, FastAPI):
        raise TypeError(
            f"for_app must be a FastAPI application, not {type(for_app).__name__}"
        )
    built = _run_describing(_hsr_flow(app, path, for_app=for_app))
    return copy.deepcopy(built.document)


def http_schema_resource_endpoint(
    app: FastAPI | APIRouter, path: str
) -> Callable[[Request], Awaitable[Response]]:
    """A Starlette endpoint serving the HTTP Schema Resource of ``path``.

    It answers GET and HEAD as :func:`add_options`'s HSR route does (the
    document, its Links, a strong ``ETag``, ``Cache-Control`` and a 304 for
    a matching ``If-None-Match``), and any other method it is routed with
    405 (``Allow: GET, HEAD``, no content). It is for serving the document
    elsewhere, e.g. with ``hsr_route=False`` and an ``hsr_url`` naming where
    it is mounted::

        add_options(app, hsr_route=False, hsr_url=lambda path: ...)
        docs.add_route(
            "/schemas/items", http_schema_resource_endpoint(app, "/items/{id}")
        )

    Add it with ``add_route`` (a Starlette route, as the HSR route is), so it
    has no dependencies. ``path`` is any template of the resource, as
    :func:`http_schema_resource` takes it; it is checked on the first
    request (a ``ValueError`` if no scrudful endpoint serves it).

    Raises
    ------
    TypeError
        If ``app`` is neither a ``FastAPI`` application nor an ``APIRouter``,
        or ``path`` isn't a str.
    """
    _check_owner(app)
    if not isinstance(path, str):
        raise TypeError(f"path must be a str, not {type(path).__name__}")

    async def scrudful_hsr_endpoint(request: Request) -> Response:
        if request.method not in ("GET", "HEAD"):
            return _hsr_not_allowed()
        return await _hsr_get(request, app, path)

    return scrudful_hsr_endpoint


def _check_owner(app: Any) -> None:
    if not isinstance(app, (FastAPI, APIRouter)):
        raise TypeError(
            "app must be a FastAPI application or an APIRouter, "
            f"not {type(app).__name__}"
        )


#: A describe-mode build: it yields each link callable to call (with the
#: entry it is for), and is sent back what it returns.
_Flow = Generator[tuple[Callable[[], Any], str], Any, T]


def _hsr_flow(
    owner: FastAPI | APIRouter,
    path: str,
    paths: Mapping[str, _Resource] | None = None,
    *,
    for_app: Any = None,
) -> _Flow[_Built]:
    """The HTTP Schema Resource of ``path`` on ``owner`` (its document not a
    copy) for the app ``for_app`` (see :func:`_owner_error`): the one built
    for that app's error entity, unless the routes of ``path`` changed.
    ``paths`` is ``owner``'s :func:`_scrudful_paths`, when the caller has
    them."""
    options = _hsr_options(owner)
    if paths is None:
        paths = _scrudful_paths(owner)
    resource = _resource(owner, paths, path)
    routes = resource.routes
    api_error = _owner_error(owner, for_app)
    cache_key = (_template_key(path), id(api_error))
    originals = tuple(getattr(route, "original_route", route) for route in routes)
    built = options.built.get(cache_key)
    if (
        built is not None
        and built.error is api_error
        and len(built.routes) == len(originals)
        and all(old is new for old, new in zip(built.routes, originals))
    ):
        return built
    document = yield from _describe_path(routes, options.base_url, api_error)
    built = _Built(originals, api_error, document)
    options.built[cache_key] = built
    return built


def _resource(
    owner: FastAPI | APIRouter, paths: Mapping[str, _Resource], path: str
) -> _Resource:
    """The resource of ``path`` (a resource key, or any template of it)
    among ``owner``'s ``paths``.

    Raises
    ------
    ValueError
        If no scrudful endpoint serves ``path``.
    """
    resource = paths.get(_template_key(path))
    if resource is None:
        templates = sorted(resource.template for resource in paths.values())
        raise ValueError(
            f"{path!r} is not the path of a scrudful endpoint of this "
            f"{type(owner).__name__} (its paths are {templates}): give the "
            "route's full path string"
        )
    return resource


def _run_describing(flow: _Flow[T]) -> T:
    """Run a describe-mode ``flow`` here: each callable is called, and an
    awaitable it returns awaited (see :func:`_await`)."""
    result = None
    while True:
        try:
            func, where = flow.send(result)
        except StopIteration as stop:
            return stop.value
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


def _scrudful_paths(owner: FastAPI | APIRouter) -> dict[str, _Resource]:
    """The resources of ``owner`` that a scrudful endpoint serves, by
    :func:`_template_key` (see :func:`add_options`); kept, with their index,
    until ``owner``'s routes change (see :func:`_paths_index`). The caller
    must not change them."""
    return _paths_index(owner).paths


def _paths_index(owner: FastAPI | APIRouter) -> _PathsIndex:
    """``owner``'s :class:`_PathsIndex`, rebuilt when its routes changed
    since (their :func:`_routes_fingerprint`), or always when it can't
    tell."""
    options = _hsr_options(owner)
    routes = owner.router.routes if isinstance(owner, FastAPI) else owner.routes
    fingerprint = _routes_fingerprint(routes)
    index = options.index
    if index is not None and fingerprint is not None:
        if index.fingerprint == fingerprint:
            return index
    paths = _find_scrudful_paths(routes)
    by_route: dict[int, list[str]] = {}
    by_endpoint: dict[int, list[str]] = {}
    for key, resource in paths.items():
        for route in resource.routes:
            original = getattr(route, "original_route", route)
            endpoint = getattr(original, "endpoint", None)
            for found, of in ((original, by_route), (endpoint, by_endpoint)):
                if found is None:
                    continue
                keys = of.setdefault(id(found), [])
                if key not in keys:
                    keys.append(key)
    index = _PathsIndex(fingerprint or (), paths, by_route, by_endpoint)
    if fingerprint is not None:
        options.index = index
    if options.explicit:
        _forget_removed(owner, options)
        _announce_unchecked_routes(options, paths)
        if not isinstance(owner, FastAPI):
            _announce_unserved(owner, options, paths)
    return index


def _forget_removed(owner: FastAPI | APIRouter, options: _HsrOptions) -> None:
    """Forget the HSR routes of ``owner`` that are no longer among its routes
    (one replaced, ``router.routes[i] = ...``, or removed): reversing them
    would fail, so the resources they served are taken as having none (no
    HSR link, see :func:`_response_hsr_link`; OPTIONS answers without it).
    For a router, :func:`_announce_unserved` warns of them, and its next
    completion walks it whole (as one whose routes seen changed otherwise),
    which gives them their routes again, as calling :func:`add_options`
    again does. For an app, whose one HSR route serves all its resources,
    this warns (once for each route forgotten): only :func:`add_options`
    adds it again."""
    routes = owner.router.routes if isinstance(owner, FastAPI) else owner.routes
    present = {id(route) for route in routes}
    hsr = getattr(owner, _HSR_ROUTE_ATTRIBUTE, None)
    gone = False
    if isinstance(hsr, Route) and id(hsr) not in present:
        delattr(owner, _HSR_ROUTE_ATTRIBUTE)
        gone = True
        if options.hsr_route and options.hsr_url is None:
            warnings.warn(
                f"the HTTP Schema Resource route of this FastAPI app "
                f"('/{HSR_ROUTE_SEGMENT}/{{name}}') was removed from its routes, "
                "or replaced, after add_options(app), so none of its resources "
                "is described: their responses get no HSR link, and OPTIONS "
                "answers without one; call add_options on the app again",
                RuntimeWarning,
                stacklevel=2,
            )
    elif isinstance(hsr, dict):
        for key in [key for key, route in hsr.items() if id(route) not in present]:
            del hsr[key]
            gone = True
    completion = options.completion
    if completion is None or not completion.simple:
        return
    if not gone:
        current = {_entry_key(route) for route in _flat(routes)}
        gone = any(key not in current for key in completion.seen)
    if gone:
        completion.simple = False


def _find_scrudful_paths(routes: Sequence[Any]) -> dict[str, _Resource]:
    """:func:`_scrudful_paths` of an owner whose routes are ``routes``,
    found by walking them."""
    paths: dict[str, _Resource] = {}
    _collect_paths(routes, "", paths, [])
    return {
        key: resource
        for key, resource in paths.items()
        if any(
            _endpoint_config(getattr(route, "endpoint", None))
            for route in resource.routes
        )
    }


def _routes_fingerprint(routes: list[Any]) -> tuple[Any, ...] | None:
    """What changes when a route is added under ``routes``, removed, replaced
    or moved: the identity of each list of routes :func:`_collect_paths`
    walks, ``routes``, those of the ``Mount``\\ s and ``Host``\\ s it looks
    into and those of the routers FastAPI includes lazily
    (``original_router``, the included ``APIRouter``, of the entries
    :func:`_flat` expands), with each of their entries, in order (a tuple
    of the entries themselves, not of their ids: copied and compared, by
    identity, in C rather than in a Python loop, and kept alive, so a new
    route can't reuse a removed one's id); ``None`` when one of those
    entries can't be seen into (a FastAPI
    whose included routers have no ``original_router``), so the caller
    walks the routes each time, as before the index: reading
    ``original_router`` needs no version guard, since without it nothing is
    kept, so nothing goes stale.

    It walks the lists, not the routes' templates and endpoints, so it is
    cheap next to :func:`_find_scrudful_paths`.
    """
    parts: list[Any] = []
    pending = [routes]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append((id(current), tuple(current)))
        for route in current:
            if isinstance(route, Route):
                continue
            contents = _contents(route)
            if contents is not None:
                if contents is not _OPAQUE:
                    pending.append(contents)
                continue
            if _iter_route_contexts is None:
                continue
            inner = getattr(getattr(route, "original_router", None), "routes", None)
            if isinstance(inner, list):
                pending.append(inner)
            elif not _is_leaf(route):
                # An entry _flat expands that this can't see into: a FastAPI
                # whose included routers differ. Not kept, so never stale.
                return None
    return tuple(parts)


def _is_leaf(route: Any) -> bool:
    """Whether :func:`_flat` gives ``route`` as itself (a ``WebSocketRoute``,
    a route of a kind of its own), rather than the routes of an included
    router."""
    flat = _flat([route])
    return len(flat) == 1 and getattr(flat[0], "original_route", flat[0]) is route


def _collect_paths(
    routes: Sequence[Any],
    prefix: str,
    paths: dict[str, _Resource],
    mounts: list[list[str]],
) -> None:
    """Add the HTTP routes under ``routes`` to ``paths``, by
    :func:`_template_key` of their full path, in routing order.

    Into the ``Mount``\\ s and ``Host``\\ s :func:`_contents` looks into; the
    automatic OPTIONS routes are skipped. ``mounts`` collects the segments of
    the ``Mount``\\ s passed: Starlette routes every URL under a Mount into it,
    so a later route (or Mount) whose URLs are all under one is never
    reached, and is left out (see :func:`_shadowed`). A ``Host`` routes
    only its host's requests, so the Mounts under it shadow only within it.
    """
    for route in _flat(routes):
        if _is_automatic(route):
            continue
        contents = _contents(route)
        mount = _mount_path(route)
        is_mount = isinstance(getattr(route, "original_route", route), Mount)
        if contents is not None:
            if is_mount and _shadowed(prefix + mount + "/", mounts):
                continue
            if contents is not _OPAQUE:
                # Under a Host, the Mounts shadow only within it.
                within = mounts if is_mount else list(mounts)
                _collect_paths(contents, prefix + mount, paths, within)
            if is_mount:
                mounts.append(_segments(_template_key(prefix + mount)))
        elif isinstance(getattr(route, "original_route", route), Route):
            template = prefix + route.path
            if _shadowed(template, mounts):
                continue
            resource = paths.setdefault(
                _template_key(template), _Resource(template=template)
            )
            resource.routes.append(route)


def _segments(key: str) -> list[str]:
    """The segments of a Mount's path key (none for the root Mount)."""
    stripped = key.strip("/")
    return stripped.split("/") if stripped else []


def _shadowed(template: str, mounts: list[list[str]]) -> bool:
    """Whether every URL of ``template`` is under one of the ``mounts``
    (segment lists of :func:`_template_key`\\ s). A Mount segment matches the
    template's segment when they are equal, or when it is a ``str`` or
    ``path`` parameter (see :data:`_ANY_SEGMENT`); a parameter of another
    convertor (``{x:int}``) matches only its like. A template only partly
    under one (a parameter where the Mount has a literal, or ``int``) is not.
    """
    segments = _template_key(template).split("/")[1:]
    return any(
        len(segments) > len(mount)
        and all(
            own in _ANY_SEGMENT or own == other for own, other in zip(mount, segments)
        )
        for mount in mounts
    )


def _mount_path(route: Any) -> str:
    """The path a ``Mount`` adds to its routes' (a ``Host`` adds none)."""
    original = getattr(route, "original_route", route)
    return str(original.path) if isinstance(original, Mount) else ""


def _owner_error(owner: FastAPI | APIRouter, for_app: Any) -> Entity | None:
    """The :func:`add_error_links` entity the HTTP Schema Resources of
    ``owner``'s paths describe: an app's own; for a router, that of the app
    ``for_app`` (the app serving the request, or the one named), whose
    handlers answer its errors there, when it includes the router's
    endpoints; else :func:`_api_error`'s (an app serving the HTTP Schema
    Resource with :func:`http_schema_resource_endpoint` alone, such as a
    separate docs app, answers none of the router's errors)."""
    if (
        isinstance(owner, APIRouter)
        and isinstance(for_app, FastAPI)
        and _includes(for_app, _router_endpoints(owner))
    ):
        return _APP_ERRORS.get(for_app)
    return _api_error(owner)


def _router_endpoints(router: APIRouter) -> list[Any]:
    """The scrudful endpoints of ``router``'s resources."""
    return [endpoint for endpoint, _, _ in _endpoints(router.routes, for_options=True)]


def _includes(app: FastAPI, endpoints: Sequence[Any]) -> bool:
    """Whether ``app``'s handlers answer the errors of one of ``endpoints``:
    it routes to it (but under a mounted app, which has its own)."""
    return any(
        any(endpoint is ours for ours in endpoints)
        for endpoint, _, _ in _endpoints(app.router.routes, for_options=False)
    )


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
    endpoints = _router_endpoints(owner)
    errors: list[Entity] = []
    for app, error in list(_APP_ERRORS.items()):
        if error not in errors and _includes(app, endpoints):
            errors.append(error)
    if len(errors) > 1:
        raise ValueError(
            "an APIRouter is included in apps that add_error_links gave "
            "different error entities, so its HTTP Schema Resources differ "
            "between them: name the app, http_schema_resource(router, path, "
            "for_app=app)"
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
        content = yield from media(config.entity, config.entity.media_type, str(status))
    # The entity of scrudful's own 400, 412 and 428: the endpoint's error,
    # else the app's (see early_response in scrudful).
    own_error = api_error if config is None or config.error is None else config.error
    if conditional and with_content and own_error is not None:
        error_content = yield from media(own_error, own_error.media_type, "400")
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
            # The endpoint's content, under its entity's media type. Other
            # media types the declared ``content`` lists are not described:
            # scrudful's Links describe the entity's (use entity= for another).
            entry.setdefault("content", copy.deepcopy(content))
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
    except ValueError as exc:  # e.g. a non-JSON context without a type
        raise ValueError(f"{where}: {exc}") from exc


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
        served = _options_path(self.owner, scope)
        if served is None:
            return Match.NONE, {}
        return Match.FULL, {_SERVED_KEY: served}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        served = scope.get(_SERVED_KEY) or _options_path(self.owner, scope)
        if served is None:
            raise HTTPException(status_code=404)
        path, paths = served
        request = Request(scope, receive, send)
        routes = self.owner.router.routes

        def routed(method: str) -> tuple[Any, Scope] | None:
            return _match(routes, {**scope, "method": method})

        response = await _options_answer(request, self.owner, path, routed, None, paths)
        await response(scope, receive, send)


def _options_path(
    app: FastAPI, scope: Scope
) -> tuple[str, Mapping[str, _Resource]] | None:
    """The resource key (:func:`_template_key`) whose document answers the
    OPTIONS ``scope``, and ``app``'s :func:`_scrudful_paths` (looked up once
    for the request).

    That of the first route whose path matches the URL (see
    :func:`_match_path`), when it is a scrudful path; ``None`` when another
    route (or a mounted app) serves OPTIONS for the URL, or that path has no
    scrudful endpoint.
    """
    routes = app.router.routes
    if _match(routes, {**scope, "method": "OPTIONS"}) is not None:
        return None
    path = _match_path(routes, scope)
    if path is None:
        return None
    paths = _scrudful_paths(app)
    key = _template_key(path)
    if key not in paths:
        return None
    return key, paths


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
        if "route" not in child_scope:
            # Not the route that routed ``scope`` (the OPTIONS route).
            routed["route"] = getattr(route, "original_route", route)
        return route, routed
    return None


def _add_path_options(router: APIRouter) -> None:
    """:func:`add_options` for an ``APIRouter``: an OPTIONS route per
    resource (:func:`_template_key`), at its first route's path, of the
    router's routes and of those of the routers included in it, at any
    depth, with their prefixes (FastAPI includes them lazily, or copies
    their routes: :func:`_flat` gives them either way). A resource a route
    already serves OPTIONS for (this router's OPTIONS route from an earlier
    call, a nested router's, or one of yours) gets none."""
    paths: dict[str, list[Any]] = {}
    for route in _flat(router.routes):
        if isinstance(getattr(route, "original_route", route), Route):
            paths.setdefault(_template_key(route.path), []).append(route)
    for key, routes in paths.items():
        if any(_endpoint_config(route.endpoint) for route in routes) and not any(
            route.methods is None or "OPTIONS" in route.methods for route in routes
        ):
            router.add_route(
                routes[0].path,
                _options_endpoint(router, key),
                methods=["OPTIONS"],
                name="scrudful_options",
                include_in_schema=False,
            )


def _add_router_routes(
    router: APIRouter, paths: Mapping[str, _Resource], names: Mapping[str, str]
) -> None:
    """The routes :func:`add_options` adds to ``router``, whose resources are
    ``paths`` (their HSR route ``names``): the HSR routes (with
    ``hsr_route``) and OPTIONS routes of the resources that have none yet,
    and the router of each scrudful endpoint (see :func:`_live_base`)."""
    if _hsr_options(router).hsr_route:
        _add_hsr_routes(router, router.routes, paths, names)
    _add_path_options(router)
    for endpoint, _, _ in _endpoints(router.routes, for_options=True):
        with contextlib.suppress(TypeError):  # not weakly referenceable
            _ROUTER_ENDPOINTS[endpoint] = weakref.ref(router)


@dataclass(eq=False)
class _Completion:
    """What a router's :func:`add_options` (and the completions since) gave
    its routes, so that a route added later is completed alone
    (:func:`_complete`), without walking every route again."""

    #: The original route of each entry of ``_flat(router.routes)`` seen, by
    #: :func:`_entry_key` (kept, so that the ids stay theirs).
    seen: dict[tuple[int, str | None], Any]
    #: The template of the first route of each resource key seen (scrudful or
    #: not): the resource's template, which names it.
    templates: dict[str, str]
    #: The resource key of each HSR route name given, and its inverse.
    key_of: dict[str, str]
    name_of: dict[str, str]
    #: The keys some route serves OPTIONS for (one of scrudful's included),
    #: in the order recorded (a dict, so that :meth:`rollback` can drop the
    #: last ones).
    with_options: dict[str, None]
    #: Whether every entry is a route: no ``Mount``, ``Host`` or other entry
    #: whose contents the full walk (:func:`_collect_paths`) looks into, so a
    #: route added later is under nothing that shadows it; and no route seen
    #: was removed or replaced since (see :func:`_forget_removed`).
    simple: bool
    #: The length of ``router.routes`` when last completed: a call that
    #: finds another length, a route was added to it past its methods.
    count: int = 0

    def mark(self) -> tuple[int, int, int, bool]:
        """Where the records are now, for :meth:`rollback`: :func:`_record`
        only adds to them, in order."""
        return len(self.seen), len(self.templates), len(self.with_options), self.simple

    def rollback(self, mark: tuple[int, int, int, bool]) -> None:
        """Drop what :func:`_record` recorded since ``mark``: a completion
        that failed leaves its routes unseen, so the next one checks and
        completes them again, rather than taking them as completed."""
        seen, templates, with_options, self.simple = mark
        for records, length in (
            (self.seen, seen),
            (self.templates, templates),
            (self.with_options, with_options),
        ):
            for key in list(records)[length:]:
                del records[key]  # type: ignore[attr-defined]


def _completion_of(router: APIRouter, names: Mapping[str, str]) -> _Completion:
    """The :class:`_Completion` of ``router`` once :func:`add_options` gave
    its resources (HSR route ``names``) their routes."""
    completion = _Completion({}, {}, dict(names), {}, {}, True)
    completion.name_of = {key: name for name, key in names.items()}
    _record(completion, _flat(router.routes))
    completion.count = len(router.routes)
    return completion


def _entry_key(route: Any) -> tuple[int, str | None]:
    """What tells the entries of ``_flat(router.routes)`` apart: the original
    route, and the path the entry has (a router included twice, under two
    prefixes, gives two entries of one original route)."""
    return id(getattr(route, "original_route", route)), getattr(route, "path", None)


def _record(completion: _Completion, entries: Iterable[Any]) -> list[Any]:
    """Record ``entries`` of a router's ``_flat`` routes in ``completion``,
    and return the routes among them it hadn't seen. ``simple`` turns off at
    an entry that isn't a route."""
    new = []
    for route in entries:
        original = getattr(route, "original_route", route)
        key = _entry_key(route)
        if key in completion.seen:
            continue
        completion.seen[key] = original
        if not isinstance(original, Route):
            completion.simple = False
            continue
        key = _template_key(route.path)
        completion.templates.setdefault(key, route.path)
        if route.methods is None or "OPTIONS" in route.methods:
            completion.with_options[key] = None
        new.append(route)
    return new


def _complete(
    router: APIRouter,
    changed: APIRouter | None = None,
    added: Sequence[Any] | None = None,
    before: int | None = None,
) -> None:
    """Give the resources of the routes added to ``router`` since its
    :func:`add_options` their routes, checked as :func:`add_options` checks
    them: only the routes added, and the resource keys they are on, are
    looked at (see :class:`_Completion`). ``added`` are the entries a call
    on ``changed`` appended to its ``routes``, which had ``before`` entries.
    When a router included in ``router`` changed, or ``router``'s routes
    changed past its methods since it was last completed (a route appended
    to them), the router's routes are compared with those seen, so that
    such a route is completed too. A router with an entry that isn't a
    route (a ``Mount``), or whose routes seen were removed or replaced, is
    walked whole, as :func:`add_options` walks it.

    Raises
    ------
    ValueError
        As :func:`add_options`.
    """
    _WATCH_GENERATION[0] += 1
    options = _hsr_options(router)
    completion = options.completion
    if completion is not None and completion.simple:
        entries: Iterable[Any] | None = None
        if changed is router and added is not None and before == completion.count:
            entries = _flat(added)
            # Appended, so none seen yet; else they weren't appended.
            if any(_entry_key(route) in completion.seen for route in entries):
                entries = None
        mark = completion.mark()
        new = _record(completion, _flat(router.routes) if entries is None else entries)
        if completion.simple:
            try:
                _complete_routes(router, options, completion, new)
            except BaseException:
                completion.rollback(mark)
                raise
            completion.count = len(router.routes)
            return
    routes = router.routes
    _check_options_callables(routes)
    paths = _find_scrudful_paths(routes)
    _check_reserved(paths)
    names = _hsr_names(paths) if options.hsr_route else {}
    _add_router_routes(router, paths, names)
    options.completion = _completion_of(router, names)


def _complete_routes(
    router: APIRouter,
    options: _HsrOptions,
    completion: _Completion,
    new: Sequence[Any],
) -> None:
    """:func:`_complete` of the ``new`` routes of ``router``: check their
    scrudful endpoints' callables, name the resources they add, and give
    those their HSR and OPTIONS routes; register the endpoints' router.
    Everything is checked before anything is committed: a ``ValueError``
    leaves the names unchanged (and :func:`_complete` the routes unseen)."""
    keys: dict[str, None] = {}
    for route in new:
        endpoint = getattr(route, "endpoint", None)
        config = _endpoint_config(endpoint)
        if config is None:
            continue
        _check_endpoint_callables(endpoint, config)
        keys[_template_key(route.path)] = None
    names: dict[str, str] = {}
    for key in keys:
        template = completion.templates[key]
        if template.startswith(f"/{HSR_ROUTE_SEGMENT}/"):
            raise ValueError(
                f"{template!r} is under '/{HSR_ROUTE_SEGMENT}/', which is "
                "reserved for scrudful's HTTP Schema Resource routes"
            )
        if not options.hsr_route or key in completion.name_of:
            continue
        name = _hsr_route_name(template)
        other = completion.key_of.get(name, names.get(name))
        if other is not None:
            raise ValueError(
                f"{completion.templates[other]!r} and {template!r} have the "
                f"same HTTP Schema Resource route name {name!r} "
                f"('/{HSR_ROUTE_SEGMENT}/{name}'): rename a path segment, or "
                "turn the route off (hsr_route=False) and give hsr_url="
            )
        names[name] = key
    # All checked: the names and the routes are committed together.
    for name, key in names.items():
        completion.key_of[name] = key
        completion.name_of[key] = name
    if names:
        _add_hsr_routes(router, router.routes, {}, names)
        by_key = getattr(router, _HSR_ROUTE_ATTRIBUTE)
        _record(completion, [by_key[key] for key in names.values()])
    for key in keys:
        if key in completion.with_options:
            continue
        router.add_route(
            completion.templates[key],
            _options_endpoint(router, key),
            methods=["OPTIONS"],
            name="scrudful_options",
            include_in_schema=False,
        )
        _record(completion, router.routes[-1:])
    for route in new:
        endpoint = getattr(route, "endpoint", None)
        if _endpoint_config(endpoint) is not None:
            with contextlib.suppress(TypeError):  # not weakly referenceable
                _ROUTER_ENDPOINTS[endpoint] = weakref.ref(router)


def _routes_changed(router: Any) -> None:
    """Tell FastAPI that ``router``'s routes changed when scrudful inserted
    some (``routes.insert``), as its route-adding methods do: FastAPI
    versions that include routers lazily keep each included router's routes
    until then (``APIRouter._mark_routes_changed``, absent elsewhere)."""
    mark = getattr(router, "_mark_routes_changed", None)
    if callable(mark):
        mark()


#: The routers given :func:`add_options` (weakly), where a scrudful endpoint
#: no :func:`add_options` covers is looked for (see :func:`_uncovered_router`).
_WATCHED: list[weakref.ref[APIRouter]] = []
#: Bumped whenever a watched router's routes are completed: an endpoint found
#: on none of them is looked for again only after that.
_WATCH_GENERATION = [0]
#: The scrudful endpoints found on no watched router, with the generation.
_UNCOVERED: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()


def _remember_watched(router: APIRouter) -> None:
    _WATCH_GENERATION[0] += 1
    _WATCHED[:] = [ref for ref in _WATCHED if ref() is not None]
    if not any(ref() is router for ref in _WATCHED):
        _WATCHED.append(weakref.ref(router))


def _uncovered_router(endpoint: Any) -> APIRouter | None:
    """The router given :func:`add_options` that the scrudful ``endpoint``
    is on although nothing registered it: its route was added past the
    router's route-adding methods (appended to ``routes``, or by
    ``APIRouter.add_api_route(router, ...)``, see :func:`_watch`).

    The endpoint is registered now, so its responses get the HSR link when
    its resource has an HSR route; and the router's index, rebuilt since
    its routes changed, announces a resource without its routes
    (:func:`_announce_unserved`). An endpoint found on none is looked for
    again only after a watched router's routes change.
    """
    if _endpoint_config(endpoint) is None:
        return None
    try:
        if _UNCOVERED.get(endpoint) == _WATCH_GENERATION[0]:
            return None
    except TypeError:  # not weakly referenceable
        return None
    _WATCHED[:] = [ref for ref in _WATCHED if ref() is not None]
    for ref in list(_WATCHED):
        router = ref()
        if router is None or not _hsr_options(router).explicit:
            continue
        if any(
            found is endpoint
            for found, _, _ in _endpoints(router.routes, for_options=True)
        ):
            _ROUTER_ENDPOINTS[endpoint] = weakref.ref(router)
            _scrudful_paths(router)
            return router
    _UNCOVERED[endpoint] = _WATCH_GENERATION[0]
    return None


#: The attribute of a router holding weak references to the routers whose
#: :func:`add_options` covers its routes: the router itself, and those it is
#: included in (see :func:`_watch`).
_WATCHERS_ATTRIBUTE = "_scrudful_watchers"
#: The attribute of a router counting the calls of its route-adding methods
#: in progress (an ``include_router`` calls ``add_api_route`` on older
#: FastAPI versions).
_ADDING_ATTRIBUTE = "_scrudful_adding"
#: The methods of an ``APIRouter`` that add routes to it: its route
#: decorators (``get``, ``api_route`` ...) call ``add_api_route``.
_ADDING_METHODS = ("add_api_route", "include_router")


def _watch(owner: APIRouter) -> None:
    """Have the routes added later to ``owner``, or to a router included in
    it, at any depth, get their OPTIONS and HSR routes as they are added
    (:func:`_complete`), as an app's routes added later are described.

    Each of those routers' route-adding methods (:data:`_ADDING_METHODS`)
    is wrapped, once, on the router itself: after a call adds routes, the
    routers watching it are completed. A route added otherwise (appended to
    ``routes``, or by the class's method) is not seen: its endpoint is found
    when it responds (:func:`_uncovered_router`), and
    :func:`_announce_unserved` warns of its resource.
    """
    pending: list[Any] = [owner]
    seen: set[int] = set()
    while pending:
        router = pending.pop()
        if id(router) in seen:
            continue
        seen.add(id(router))
        watchers = vars(router).get(_WATCHERS_ATTRIBUTE)
        if watchers is None:
            watchers = []
            setattr(router, _WATCHERS_ATTRIBUTE, watchers)
            for name in _ADDING_METHODS:
                method = getattr(router, name)
                setattr(router, name, _completing(router, method, name))
        if not any(ref() is owner for ref in watchers):
            watchers.append(weakref.ref(owner))
        for route in router.routes:
            # A router FastAPI includes lazily (older versions copy its
            # routes, so the routes added to it later aren't served).
            inner = getattr(route, "original_router", None)
            if isinstance(inner, APIRouter):
                pending.append(inner)


def _completing(
    router: APIRouter, method: Callable[..., T], name: str
) -> Callable[..., T]:
    """``method``, the route-adding method ``name`` of ``router``,
    completing the routers watching it (see :func:`_watch`) once it has
    added the routes, with those it appended to ``router.routes``
    (:func:`_complete`); after an ``include_router``, the routers newly
    included are watched too."""

    @functools.wraps(method)
    def adding(*args: Any, **kwargs: Any) -> T:
        depth = vars(router).get(_ADDING_ATTRIBUTE, 0)
        setattr(router, _ADDING_ATTRIBUTE, depth + 1)
        routes = router.routes
        before = len(routes)
        try:
            result = method(*args, **kwargs)
        finally:
            setattr(router, _ADDING_ATTRIBUTE, depth)
        if depth == 0:
            added = routes[before:] if router.routes is routes else None
            for ref in list(vars(router).get(_WATCHERS_ATTRIBUTE) or ()):
                owner = ref()
                if owner is not None and _hsr_options(owner).explicit:
                    _complete(owner, router, added, before)
                    if name == "include_router":
                        _watch(owner)
        return result

    return adding


def _announce_unserved(
    router: APIRouter, options: _HsrOptions, paths: Mapping[str, _Resource]
) -> None:
    """Warn, once per resource, of each resource of ``router`` (its
    ``paths``) that has no OPTIONS route, or no HSR route while the router
    serves them: one whose route was added otherwise than with the router's
    methods (see :func:`_watch`), after :func:`add_options`. It is not
    described: its responses get no HSR link."""
    with_options = {
        _template_key(route.path)
        for route in _flat(router.routes)
        if isinstance(getattr(route, "original_route", route), Route)
        and (route.methods is None or "OPTIONS" in route.methods)
    }
    served = options.hsr_route and options.hsr_url is None
    for key, resource in paths.items():
        if key in options.announced:
            continue
        if key in with_options and not (served and _hsr_route_of(router, key) is None):
            continue
        options.announced.add(key)
        warnings.warn(
            f"{resource.template!r} of an APIRouter has no OPTIONS or HTTP "
            "Schema Resource route, so it isn't described: its route was "
            "added to the router after add_options(router) otherwise than "
            "with the router's route decorators, add_api_route or "
            "include_router; call add_options on the router again",
            RuntimeWarning,
            stacklevel=2,
        )


def _options_endpoint(router: APIRouter, path: str) -> Callable[[Request], Any]:
    """The endpoint of the OPTIONS route of the resource ``path`` (a
    :func:`_template_key`) on ``router``: the router's document of it,
    whichever app or prefix the router is included through (routes added to
    it later are described)."""

    async def scrudful_options(request: Request) -> Response:
        paths = _scrudful_paths(router)
        resource = _resource(router, paths, path)

        def routed(method: str) -> tuple[Any, Scope] | None:
            return _router_match(request, resource, method)

        params = dict(request.path_params)
        return await _options_answer(request, router, path, routed, params, paths)

    scrudful_options._scrudful_options = True  # type: ignore[attr-defined]
    with contextlib.suppress(TypeError):  # not weakly referenceable
        # For the base URL of the Links of its errors (see _live_base).
        _ROUTER_ENDPOINTS[scrudful_options] = weakref.ref(router)
    return scrudful_options


def _router_match(
    request: Request, resource: _Resource, method: str
) -> tuple[Any, Scope] | None:
    """The route serving ``method`` at the URL of ``request`` (to a router's
    OPTIONS route), and the scope it gets, for the router's ``resource``.

    The first of the resource's routes serving ``method``, as the app the
    request reached serves it: the route of the app (under the Mounts
    :func:`_contents` looks into) with that route's endpoint that matches the
    request, so with the dependencies ``include_router`` and the app give
    it (FastAPI copies an included router's routes, or wraps them in route
    contexts); else the router's own route.
    """
    route = next((r for r in resource.routes if _serves(r, method)), None)
    if route is None:
        return None
    scope = {**request.scope, "method": method}
    endpoint = getattr(route, "endpoint", None)
    app = request.scope.get("app")
    routes = getattr(getattr(app, "router", None), "routes", None) or []
    for candidate in _leaf_routes(routes):
        if (
            endpoint is None
            or getattr(candidate, "endpoint", None) is not endpoint
            or not _serves(candidate, method)
        ):
            continue
        match, child_scope = candidate.matches(scope)
        if match == Match.FULL:
            routed = {**scope, **child_scope}
            if "route" not in child_scope:
                routed["route"] = getattr(candidate, "original_route", candidate)
            return candidate, routed
    _, child_scope = route.matches(scope)
    return route, {**scope, **child_scope, "route": route}


def _leaf_routes(routes: Sequence[Any]) -> Iterator[Any]:
    """The routes under ``routes`` that aren't a ``Mount`` or ``Host``, in
    routing order, with those under the Mounts and Hosts :func:`_contents`
    looks into; but the automatic OPTIONS routes."""
    for route in _flat(routes):
        if _is_automatic(route):
            continue
        contents = _contents(route)
        if contents is None:
            yield route
        elif contents is not _OPAQUE:
            yield from _leaf_routes(contents)


# --- OPTIONS: a 204 with a per-requester Allow -------------------------------------

#: The statuses of an ``HTTPException`` a per-method check of OPTIONS raises
#: that mean "not allowed".
_DENIAL_STATUSES = frozenset({401, 403, 404})
#: Those an ``allowed`` hook raises that do (a 404 is the OPTIONS's answer,
#: as ``load``'s is).
_HOOK_DENIAL_STATUSES = frozenset({401, 403})


async def _options_answer(
    request: Request,
    owner: FastAPI | APIRouter,
    path: str,
    routed: Callable[[str], tuple[Any, Scope] | None],
    params: dict[str, Any] | None,
    paths: Mapping[str, _Resource],
) -> Response:
    """The answer to the OPTIONS ``request`` of the resource ``path`` (a
    :func:`_template_key`) of ``owner``; ``routed(method)`` is the route
    serving ``method`` at the request's URL and the scope it gets
    (:func:`_match`), ``params`` the URL's path parameters (``None``: those
    of the route of the resource's first method), ``paths`` ``owner``'s
    :func:`_scrudful_paths`, looked up once for the request. In order:

    1. a CORS preflight (:func:`scrudful._hsr.is_preflight`) is a 204 with no
       content and no Links, before anything else runs;
    2. each method the resource serves is checked for this requester, without
       the object: with an ``allowed`` hook, it passes (the hook needs the
       object); else by its security dependencies (:func:`_may_use`);
    3. only when one passed, and the URL has path parameters, is the object
       loaded: ``load(request, **params)``, else there is none (``None``),
       so a requester refused everything can't tell which objects exist;
    4. with an ``allowed`` hook, ``allowed(clone, method, obj)`` decides each
       method (:func:`_hooked`);
    5. the answer is a 204 with no content and no ``Content-Type``, the
       ``Allow`` of the methods allowed (:func:`scrudful._hsr.allow_value`),
       the HSR link (:func:`_hsr_target`) and the exposed headers.

    An ``HTTPException`` raised by ``load``, the hook or a check (but a
    check's 401, 403 or 404, and the hook's 401 or 403, which refuse the
    method) is answered by the app's exception handlers, with the HSR link
    (:func:`_options_error`). Anything else raised propagates.
    """
    if is_preflight(request.method, request.headers.get):
        return Response(status_code=204)
    options = _hsr_options(owner)
    resource = _resource(owner, paths, path)
    target: str | None = None
    if options.hsr_url is not None or not options.hsr_route:
        target = _hsr_target(request, path, owner=owner, paths=paths)
    elif _hsr_route_of(owner, _template_key(resource.template)) is not None:
        target = _hsr_target(request, path, owner=owner, paths=paths)
    found = {method: routed(method) for method in _served_methods(resource)}
    served = {method: f for method, f in found.items() if f is not None}
    if params is None:
        first = next(
            (f for m, f in served.items() if _serves(resource.routes[0], m)), None
        )
        params = dict(first[1].get("path_params") or {}) if first else {}
        subject = Request({**request.scope, "path_params": params}, request.receive)
    else:
        subject = request
    try:
        allowed = {
            method: options.allowed is not None or await _may_use(f)
            for method, f in served.items()
        }
        obj = None
        # A URL without path parameters (a top-level collection) has no
        # object: nothing to load.
        if options.load is not None and params and any(allowed.values()):
            obj = await _call(functools.partial(options.load, subject), params)
        if options.allowed is not None:
            for method in allowed:
                allowed[method] = await _hooked(options.allowed, subject, method, obj)
    except HTTPException as exc:
        return await _options_error(subject, exc, target)
    response = Response(status_code=204)
    response.headers["Allow"] = allow_value(m for m, ok in allowed.items() if ok)
    if target is not None:
        response.headers["Link"] = hsr_link(target)
    expose_headers(response.headers)
    return response


def _served_methods(resource: _Resource) -> list[str]:
    """The methods ``resource``'s routes serve, but OPTIONS, in the order of
    :data:`~scrudful._adapter.METHOD_KEYS`, then any others sorted. HEAD only
    when a route lists it: FastAPI's ``APIRoute`` serves no HEAD for GET (a
    Starlette ``Route`` with GET does, and lists HEAD)."""
    methods: set[str] = set()
    for route in resource.routes:
        route_methods = getattr(route, "methods", None)
        if route_methods is None:
            methods.update(key.upper() for key in METHOD_KEYS)
        else:
            methods.update(method.upper() for method in route_methods)
    methods.discard("OPTIONS")
    known = [key.upper() for key in METHOD_KEYS]
    return [m for m in known if m in methods] + sorted(methods - set(known))


async def _hooked(
    allowed: Callable[..., Any], request: Request, method: str, obj: Any
) -> bool:
    """``allowed(clone, method, obj)``, with ``clone`` a copy of ``request``
    whose method is ``method`` (sync hooks run in the threadpool, an
    awaitable result is awaited). An ``HTTPException`` 401 or 403 it raises
    means not allowed (as with DRF's and ninja's hooks); anything else it
    raises propagates (an ``HTTPException``, a 404 included, is then the
    OPTIONS's answer).

    Raises
    ------
    TypeError
        If it returns something other than a ``bool``.
    """
    clone = Request({**request.scope, "method": method}, request.receive)
    try:
        result = await _call(functools.partial(allowed, clone, method, obj), {})
    except HTTPException as exc:
        if exc.status_code in _HOOK_DENIAL_STATUSES:
            return False
        raise
    if not isinstance(result, bool):
        raise TypeError(
            f"allowed returned {result!r} for {method}: it must return a bool"
        )
    return result


async def _options_error(
    request: Request, exc: HTTPException, target: str | None
) -> Response:
    """The app's exception handlers' answer to ``exc``, raised for the OPTIONS
    ``request``, with the HSR link to ``target`` merged into its Link (a Link
    with the HSR relation the handler set wins) and the exposed headers.

    The handler is the one Starlette's ``ExceptionMiddleware`` would pick
    from the app's ``exception_handlers``: the one for the exception's status
    code, else for the nearest of its classes (500 and ``Exception`` are
    Starlette's server error handler's, not looked up). Without one, ``exc``
    is raised again.
    """
    handler = _exception_handler(request, exc)
    if handler is None:
        raise exc
    response = await _call_handler(handler, request, exc)
    if not isinstance(response, Response):
        return response  # type: ignore[no-any-return]
    headers = response.headers
    hsr = None if target is None else hsr_link(target)
    link = merge_links(", ".join(headers.getlist("link")), None, None, hsr)
    if link is not None:
        headers["Link"] = link
    _join_expose_lines(headers)
    expose_headers(headers)
    return response


def _exception_handler(
    request: Request, exc: Exception
) -> Callable[[Request, Exception], Any] | None:
    """The handler of ``request``'s app for ``exc`` (see :func:`_options_error`)."""
    handlers = getattr(request.scope.get("app"), "exception_handlers", None)
    if not isinstance(handlers, Mapping):
        return None
    status = getattr(exc, "status_code", None)
    if isinstance(exc, HTTPException) and status != 500 and status in handlers:
        return handlers[status]  # type: ignore[no-any-return]
    for cls in type(exc).__mro__:
        if cls is not Exception and cls in handlers:
            return handlers[cls]  # type: ignore[no-any-return]
    return None


async def _may_use(found: tuple[Any, Scope]) -> bool:
    """Whether the requester may use the method of ``found``, a route serving
    it and the scope it gets (whose method is the method checked, see
    :func:`_match`), by the route's security dependencies
    (:func:`_security_dependencies`), resolved on a copy of that scope
    (:func:`_resolves`). A route without dependencies (a Starlette
    ``Route``) has none: allowed. One with a security dependency that needs
    the request's body, which OPTIONS doesn't have, can't be checked: not
    allowed, for every requester, and so is one whose security dependencies
    can't be told (:class:`_Unchecked`); either is announced (see
    :func:`_announce_unchecked`)."""
    route, scope = found
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return True
    checks, unchecked = _security_dependencies(route, dependant)
    if unchecked is not None:
        _announce_unchecked(route, unchecked)
        return False
    if not checks:
        return True
    return await _resolves(route, checks, scope)


class _Unchecked(Exception):
    """Why the per-method checks of OPTIONS can't tell a route's security
    dependencies (its message, the end of a sentence naming the route): the
    route's method is then refused for every requester (it fails closed)."""


#: Why a route's method is refused: one of its security dependencies needs
#: the request's body.
_NEEDS_BODY = (
    "has a security dependency that needs the request's body, which OPTIONS "
    "doesn't have"
)


def _security_dependencies(route: Any, dependant: Any) -> tuple[list[Any], str | None]:
    """The sub-dependants of ``route``'s ``dependant`` that the per-method
    checks of OPTIONS resolve, and why they can't be (``None``: they can):
    one of them needs the request's body (:data:`_NEEDS_BODY`), or a
    signature doesn't tell which are security dependencies
    (:class:`_Unchecked`).

    The security dependencies, wherever they are declared (a parameter of
    the endpoint, or ``dependencies=`` of the route, its routers,
    ``include_router`` or the app). A security dependency is one declared
    with ``Security(...)`` (see :func:`_parameterless_security` for those
    of ``dependencies=``), or one whose subtree has a security scheme (a
    ``fastapi.security`` class such as ``HTTPBearer``) or a
    ``Security(...)``: the ``Depends(get_current_user)`` of a user resolved
    from a token (:func:`_uses_security`). One that needs the request's body
    (a body, form or file parameter anywhere below it) can't be resolved
    without it: the caller refuses the method (it fails closed). So does a
    parameter whose annotation can't be evaluated (a name imported only under
    ``TYPE_CHECKING``) and names ``Security(`` or ``Depends(``, or a
    signature that can't be read, of the endpoint or of a dependency whose
    subtree has no security scheme: whether it declares a security
    dependency can't be told. Each annotation is evaluated on its own
    (:func:`~scrudful._adapter.resolved_signature`), so one that can't be
    hides nothing else. No other
    dependency of the route (a plain ``Depends`` without a scheme below it,
    even in ``dependencies=``: a throttle, a database write) and never the
    endpoint is resolved. A security dependency is resolved whole, though,
    its plain sub-dependencies included: what those consume or write (a
    throttle or a session a ``get_current_user`` depends on), the checks do
    too.

    Raises
    ------
    RuntimeError
        As :func:`_parameterless_security`.
    """
    parameterless = _parameterless_security(route, dependant)
    subs = list(getattr(dependant, "dependencies", None) or ())
    try:
        declared, unread = _declared_security(getattr(dependant, "call", None))
    except _Unchecked as exc:
        return [], str(exc)
    checks = []
    unchecked: str | None = None
    for sub in subs:
        name = getattr(sub, "name", None)
        secured = (
            any(sub is marked for marked in parameterless)
            if name is None
            else name in declared
        )
        try:
            secured = secured or _uses_security(sub)
        except _Unchecked as exc:
            unchecked = unchecked or str(exc)
            continue
        if secured:
            if _needs_body(sub):
                unchecked = unchecked or _NEEDS_BODY
            else:
                checks.append(sub)
        elif name in unread:
            unchecked = unchecked or unread[name]
    named = {getattr(sub, "name", None) for sub in subs}
    for name, why in unread.items():
        if name not in named:
            unchecked = unchecked or why
    return checks, unchecked


#: The attribute of a route marking that :func:`_announce_unchecked`
#: announced it.
_UNCHECKED_ANNOUNCED_ATTRIBUTE = "_scrudful_unchecked_announced"


def _announce_unchecked(route: Any, why: str) -> None:
    """Warn, once per route, that OPTIONS leaves ``route``'s methods out of
    ``Allow`` for every requester, because its security dependencies can't
    be checked: ``why`` (see :func:`_security_dependencies`)."""
    original = getattr(route, "original_route", route)
    if getattr(original, _UNCHECKED_ANNOUNCED_ATTRIBUTE, False):
        return
    with contextlib.suppress(AttributeError, TypeError):
        setattr(original, _UNCHECKED_ANNOUNCED_ATTRIBUTE, True)
    methods = ", ".join(sorted(getattr(route, "methods", None) or ()))
    endpoint = _endpoint_name(getattr(route, "endpoint", None))
    remedy = (
        "declare the dependency without the body"
        if why == _NEEDS_BODY
        else "make the annotation evaluable at runtime (import its names "
        "outside TYPE_CHECKING)"
    )
    warnings.warn(
        f"{methods} {getattr(route, 'path', route)!r} ({endpoint}) {why}: "
        "Allow leaves the method out for every requester. Give add_options "
        f"allowed= to decide it, or {remedy}",
        RuntimeWarning,
        stacklevel=2,
    )


def _announce_unchecked_routes(
    options: _HsrOptions, paths: Mapping[str, _Resource]
) -> None:
    """:func:`_announce_unchecked` of the routes of ``paths`` whose security
    dependencies can't be checked, when they decide ``Allow`` (no
    ``allowed`` hook). A route whose ``dependencies=`` can't be read is left
    to OPTIONS, which raises its ``RuntimeError``."""
    if options.allowed is not None:
        return
    for resource in paths.values():
        for route in resource.routes:
            dependant = getattr(route, "dependant", None)
            if dependant is None:
                continue
            try:
                _, unchecked = _security_dependencies(route, dependant)
            except RuntimeError:
                continue
            if unchecked is not None:
                _announce_unchecked(route, unchecked)


def _parameterless_security(route: Any, dependant: Any) -> list[Any]:
    """The sub-dependants of ``route``'s ``dependant`` from its
    ``dependencies=`` (those of its routers, ``include_router`` and the app
    included) that are declared with ``Security(...)``.

    A ``Security(fn)`` without scopes leaves no trace on its sub-dependant,
    so they are read off ``route.dependencies``, the ``params.Depends`` that
    FastAPI turns, in order, into the leading unnamed sub-dependants of
    ``route.dependant``.

    Raises
    ------
    RuntimeError
        If ``route.dependencies`` doesn't match those sub-dependants (their
        calls, in order), as in the FastAPI releases scrudful supports: it
        was changed after FastAPI built the route (FastAPI builds
        ``route.dependant`` once, when the route is created), or the
        installed FastAPI builds them otherwise.
    """
    declared = getattr(route, "dependencies", None)
    if not declared:
        return []
    subs = list(getattr(dependant, "dependencies", None) or ())[: len(declared)]
    if len(subs) != len(declared) or any(
        getattr(sub, "name", None) is not None
        or getattr(sub, "call", None) is not getattr(depends, "dependency", None)
        for sub, depends in zip(subs, declared)
    ):
        raise RuntimeError(
            "scrudful's per-requester Allow of OPTIONS reads which of a route's "
            "dependencies= are Security(...) off APIRoute.dependencies, which "
            "doesn't match the leading sub-dependants of APIRoute.dependant "
            f"of the route {getattr(route, 'path', route)!r}: either its "
            "dependencies were changed after the route was created (FastAPI "
            "builds the dependant once, so a dependency added then doesn't run "
            "either: give it in dependencies= of the route, its router or the "
            "app), or the installed FastAPI builds them otherwise than FastAPI "
            "0.100 to 0.159 do (upgrade scrudful, or use a FastAPI release it "
            "supports)"
        )
    return [
        sub
        for sub, depends in zip(subs, declared)
        if isinstance(depends, fastapi_params.Security)
    ]


def _uses_security(dependant: Any) -> bool:
    """Whether ``dependant`` is, or depends on (at any depth), a security
    scheme or a ``Security(...)`` dependency.

    Raises
    ------
    _Unchecked
        If it can't be told, and nothing below ``dependant`` is one: a
        signature of the subtree can't be read, or an annotation that can't
        be evaluated may declare one (:func:`_declared_security`).
    """
    call = getattr(dependant, "call", None)
    scheme = call
    while isinstance(scheme, functools.partial):
        scheme = scheme.func
    if isinstance(scheme, SecurityBase) or isinstance(
        inspect.unwrap(scheme) if scheme is not None else None, SecurityBase
    ):
        return True
    if getattr(dependant, "own_oauth_scopes", None) or getattr(
        dependant, "security_requirements", None
    ):
        return True
    subs = list(getattr(dependant, "dependencies", None) or ())
    doubt: _Unchecked | None = None
    try:
        declared, unread = _declared_security(call)
    except _Unchecked as exc:
        # No name can be told: only a scheme below can still decide.
        declared, unread, doubt = frozenset(), {}, exc
    for sub in subs:
        name = getattr(sub, "name", None)
        if name in declared:
            return True
        try:
            if _uses_security(sub):
                return True
        except _Unchecked as exc:
            doubt = doubt or exc
            continue
        if name in unread:
            doubt = doubt or _Unchecked(unread[name])
    named = {getattr(sub, "name", None) for sub in subs}
    for name, why in unread.items():
        if name not in named:
            doubt = doubt or _Unchecked(why)
    if doubt is not None:
        raise doubt
    return False


#: What an annotation that can't be evaluated names when it may declare a
#: dependency.
_DECLARES_DEPENDENCY = re.compile(r"\b(?:Security|Depends)\s*\(")


def _declared_security(call: Any) -> tuple[frozenset[str], dict[str, str]]:
    """The names of ``call``'s parameters declared with ``Security(...)`` (as
    their default, or in their ``Annotated`` metadata), and those whose
    annotation can't be evaluated but names ``Security(`` or ``Depends(``,
    each with why it can't be told (:class:`_Unchecked`'s message). Each
    annotation is evaluated on its own
    (:func:`~scrudful._adapter.resolved_signature`).

    Raises
    ------
    _Unchecked
        If ``call``'s signature can't be read.
    """
    if call is None:
        return frozenset(), {}
    try:
        signature = resolved_signature(call)
    except (TypeError, ValueError, NameError) as exc:
        raise _Unchecked(
            f"has a dependency, {_endpoint_name(call)}, whose signature can't "
            f"be read ({exc}), so OPTIONS can't tell whether it declares a "
            "security dependency"
        ) from exc
    names = set()
    unread = {}
    for parameter in signature.parameters.values():
        marks = [parameter.default]
        annotation = parameter.annotation
        if get_origin(annotation) is Annotated:
            marks += get_args(annotation)[1:]
        if any(isinstance(mark, fastapi_params.Security) for mark in marks):
            names.add(parameter.name)
            continue
        text = getattr(annotation, "__forward_arg__", annotation)
        if isinstance(text, str) and _DECLARES_DEPENDENCY.search(text):
            unread[parameter.name] = (
                f"has a dependency, {_endpoint_name(call)}, whose annotation "
                f"{text!r} of {parameter.name!r} can't be evaluated, so "
                "OPTIONS can't tell whether it declares a security dependency"
            )
    return frozenset(names), unread


def _needs_body(dependant: Any) -> bool:
    """Whether ``dependant``, or a dependency of it, takes the request's body."""
    return bool(getattr(dependant, "body_params", None)) or any(
        _needs_body(sub) for sub in getattr(dependant, "dependencies", None) or ()
    )


async def _empty_body() -> dict[str, Any]:
    """The ``receive`` of a per-method check's request: no body."""
    return {"type": "http.request", "body": b"", "more_body": False}


#: The internal of FastAPI the per-method checks of OPTIONS resolve
#: dependencies with.
_SOLVER = "fastapi.dependencies.utils.solve_dependencies"
#: The keyword parameters scrudful passes it: always, and when it has them.
_SOLVER_PARAMETERS = frozenset(
    {"request", "dependant", "dependency_overrides_provider"}
)
_SOLVER_OPTIONAL = frozenset({"async_exit_stack", "embed_body_fields"})
#: The scope keys of the exit stack its dependencies with ``yield`` are
#: entered on: ``fastapi_astack`` (FastAPI < 0.106), then the request's and
#: the function's stacks (FastAPI >= 0.121).
_EXIT_STACK_KEYS = (
    "fastapi_astack",
    "fastapi_inner_astack",
    "fastapi_function_astack",
)


def _solver_error(detail: str) -> RuntimeError:
    return RuntimeError(
        f"scrudful's per-requester Allow of OPTIONS resolves the routes' "
        f"security dependencies with FastAPI's {_SOLVER}, which {detail}: "
        "upgrade scrudful, or use a FastAPI release it supports (0.100 to "
        "0.159)"
    )


def _check_internals(options: _HsrOptions, paths: Mapping[str, _Resource]) -> None:
    """Check, when :func:`add_options` is called, the FastAPI internals the
    per-method checks of OPTIONS rely on, rather than on the first OPTIONS
    request: how the routes' ``dependencies=`` map onto their dependants
    (:func:`_parameterless_security`) and ``solve_dependencies``
    (:func:`_solver`). Only when they run: without an ``allowed`` hook, for
    the routes of ``paths``, and the solver when one has a security
    dependency to resolve. OPTIONS still checks a route added since.

    Raises
    ------
    RuntimeError
        As those do.
    """
    if options.allowed is not None:
        return
    resolves = False
    for resource in paths.values():
        for route in resource.routes:
            dependant = getattr(route, "dependant", None)
            if dependant is not None:
                checks, _ = _security_dependencies(route, dependant)
                resolves = resolves or bool(checks)
    if resolves:
        _solver()


def _solver() -> tuple[Callable[..., Any], frozenset[str]]:
    """FastAPI's ``solve_dependencies``, and the parameters it takes of
    :data:`_SOLVER_OPTIONAL` (:func:`_checked_solver`, inspected once per
    function).

    Raises
    ------
    RuntimeError
        As :func:`_checked_solver`.
    """
    return _checked_solver(
        getattr(fastapi_dependency_utils, "solve_dependencies", None)
    )


@functools.lru_cache(maxsize=8)
def _checked_solver(solve: Any) -> tuple[Callable[..., Any], frozenset[str]]:
    """``solve``, FastAPI's ``solve_dependencies``, and the parameters it
    takes of :data:`_SOLVER_OPTIONAL`.

    Raises
    ------
    RuntimeError
        If the installed FastAPI's can't be called as scrudful calls it: an
        ``async`` function taking :data:`_SOLVER_PARAMETERS` by keyword,
        whose other parameters have defaults, but those of
        :data:`_SOLVER_OPTIONAL`.
    """
    if not inspect.iscoroutinefunction(solve):
        raise _solver_error("the installed FastAPI doesn't have as an async function")
    keyword = (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    parameters = {
        name: parameter
        for name, parameter in inspect.signature(solve).parameters.items()
        if parameter.kind in keyword
    }
    required = {
        name
        for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    if not _SOLVER_PARAMETERS <= set(parameters) or (
        required - _SOLVER_PARAMETERS - _SOLVER_OPTIONAL
    ):
        raise _solver_error(
            "the installed FastAPI's takes other parameters than "
            f"{sorted(_SOLVER_PARAMETERS | _SOLVER_OPTIONAL)}"
        )
    return solve, frozenset(parameters) & _SOLVER_OPTIONAL


async def _resolves(route: Any, checks: list[Any], scope: Scope) -> bool:
    """Whether ``checks`` (sub-dependants of ``route``'s) resolve for the
    request of ``scope`` (which has the method checked), without the body
    (:func:`_empty_body`): FastAPI's ``solve_dependencies`` (:func:`_solver`)
    runs them, with the route's ``dependency_overrides_provider`` (the app's
    ``dependency_overrides`` apply), and their dependencies with ``yield``
    are cleaned up before this returns, on an exit stack of their own.

    Not when one raises an ``HTTPException`` with 401, 403 or 404 (another
    propagates, as anything else raised does), or when their parameters fail
    validation (where FastAPI would answer 422).

    Raises
    ------
    RuntimeError
        If the installed FastAPI's ``solve_dependencies``, its ``Dependant``
        or the scope key of its exit stack differ from those of the FastAPI
        releases scrudful supports.
    """
    solve, optional = _solver()
    try:
        dependant = fastapi_dependencies_models.Dependant(dependencies=list(checks))
    except TypeError as exc:
        raise _solver_error(
            "takes a fastapi.dependencies.models.Dependant the installed "
            "FastAPI doesn't build as scrudful does (Dependant(dependencies=...))"
        ) from exc
    scope = dict(scope)
    if isinstance(scope.get("state"), dict):
        # What a check sets on ``request.state`` stays with it.
        scope["state"] = dict(scope["state"])
    kwargs: dict[str, Any] = {
        "dependant": dependant,
        "dependency_overrides_provider": getattr(
            route, "dependency_overrides_provider", None
        ),
    }
    if "embed_body_fields" in optional:
        kwargs["embed_body_fields"] = False
    resolved = False
    try:
        async with contextlib.AsyncExitStack() as stack:
            for key in _EXIT_STACK_KEYS:
                scope[key] = stack
            if "async_exit_stack" in optional:
                kwargs["async_exit_stack"] = stack
            try:
                result = await solve(request=Request(scope, _empty_body), **kwargs)
            except AssertionError as exc:
                if "astack" in str(exc):
                    raise _solver_error(
                        f"needs an exit stack scrudful doesn't give it ({exc})"
                    ) from exc
                raise
            errors = _solved_errors(result)
            resolved = True
    except HTTPException as exc:
        if exc.status_code in _DENIAL_STATUSES:
            return False
        raise
    # A dependency with ``yield`` that swallowed an exception leaves it unresolved.
    return resolved and not errors


def _solved_errors(result: Any) -> list[Any]:
    """The validation errors of a ``solve_dependencies`` result: a
    ``SolvedDependency`` (FastAPI >= 0.113), or a 5-tuple (older)."""
    errors = getattr(result, "errors", None)
    if errors is None and isinstance(result, tuple) and len(result) == 5:
        errors = result[1]
    if not isinstance(errors, list):
        raise _solver_error(
            "returned a result scrudful doesn't know (no list of errors)"
        )
    return errors


# --- The HSR route -----------------------------------------------------------------

#: The path parameter of the HSR route: the route name of the resource
#: (:func:`_hsr_route_name`). Named so that it can't clash with
#: a parameter of the prefix of a router it is on (``"/users/{name}"``).
_HSR_PARAMETER = "scrudful_hsr_name"
#: The path of an app's HSR route.
_HSR_PATH = f"/{HSR_ROUTE_SEGMENT}/{{{_HSR_PARAMETER}}}"
#: The attribute of an app holding its HSR route, or of a router holding its
#: HSR routes by resource key.
_HSR_ROUTE_ATTRIBUTE = "_scrudful_hsr_route"
#: Where an app's HSR route keeps the resource key it matched, and the
#: :func:`_scrudful_paths` it found it in, in the scope.
_HSR_SERVED_KEY = "scrudful.hsr"
#: Numbers the HSR routes' names: each is unique, so that :func:`_hsr_target`
#: reverses its own app's or router's route.
_HSR_SERIAL = itertools.count(1)


class _HsrApp:
    """The ASGI app of an HSR route of ``owner`` (an app or router): of the
    resource ``key`` (a router's), or of the one the route matched (an
    app's, see :class:`_HsrRoute`).

    An ASGI app, not a function, so that Starlette routes every method to it
    (``methods=None``), also in the copy ``include_router`` makes of a
    router's route (``methods=[]``, which matches every method too): it
    answers GET and HEAD, and others with its own 405 (see
    :func:`_hsr_not_allowed`), rather than Starlette's JSON one.
    """

    def __init__(self, owner: FastAPI | APIRouter, key: str | None = None) -> None:
        self.owner = owner
        self.key = key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        response = await _hsr_route_response(request, self.owner, self.key)
        await response(scope, receive, send)


class _HsrRoute(Route):
    """An app's HSR route, ``/_hsr/{name}``: it resolves the name on each
    request, so the paths added after :func:`add_options` are served too.

    It matches only a name of one of the app's resources (with the route
    on): any other ``/_hsr/<x>`` goes on to the app's other routes (a route
    of yours, a sub-app mounted at ``""`` with its own HSR route), and
    without one gets the app's 404.
    """

    def __init__(self, owner: FastAPI, name: str) -> None:
        super().__init__(_HSR_PATH, _HsrApp(owner), name=name, include_in_schema=False)
        self.owner = owner

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        if match == Match.NONE or not _hsr_options(self.owner).hsr_route:
            return Match.NONE, {}
        paths = _scrudful_paths(self.owner)
        name = child_scope["path_params"].get(_HSR_PARAMETER)
        key = _owner_hsr_names(self.owner, paths)[0].get(name)
        if key is None:
            return Match.NONE, {}
        return match, {**child_scope, _HSR_SERVED_KEY: (key, paths)}


def _add_hsr_routes(
    owner: FastAPI | APIRouter,
    routes: list[Any],
    paths: Mapping[str, _Resource],
    names: Mapping[str, str],
) -> None:
    """Add ``owner``'s HSR routes before its ``routes``: Starlette ``Route``\\ s,
    so they have no dependencies, and not in the OpenAPI document.

    An app gets one :class:`_HsrRoute` (unless it has it). A router gets a
    literal ``/_hsr/<name>`` route for each of its resources (of ``paths``,
    whose ``names`` they are) that has none yet, as its OPTIONS routes: an
    included router's routes are copied as plain ``Route``\\ s, which would
    lose :class:`_HsrRoute`'s matching.
    """
    if isinstance(owner, FastAPI):
        if getattr(owner, _HSR_ROUTE_ATTRIBUTE, None) is None:
            route = _HsrRoute(owner, f"scrudful_hsr_{next(_HSR_SERIAL)}")
            # First, ahead of a path such as "/{kind}/{id}" that matches it.
            routes.insert(0, route)
            setattr(owner, _HSR_ROUTE_ATTRIBUTE, route)
            _routes_changed(owner.router)
        return
    by_key = getattr(owner, _HSR_ROUTE_ATTRIBUTE, None)
    if not isinstance(by_key, dict):
        by_key = {}
        setattr(owner, _HSR_ROUTE_ATTRIBUTE, by_key)
    for name, key in names.items():
        if key in by_key:
            continue
        by_key[key] = Route(
            f"/{HSR_ROUTE_SEGMENT}/{name}",
            _HsrApp(owner, key),
            name=f"scrudful_hsr_{next(_HSR_SERIAL)}",
            include_in_schema=False,
        )
        routes.insert(0, by_key[key])
    _routes_changed(owner)


def _hsr_route_of(owner: FastAPI | APIRouter, key: str) -> Route | None:
    """``owner``'s HSR route serving the resource ``key``, if it has one."""
    route = getattr(owner, _HSR_ROUTE_ATTRIBUTE, None)
    if isinstance(route, dict):
        return route.get(key)
    return route if isinstance(route, Route) else None


@functools.lru_cache(maxsize=4096)
def _hsr_route_name(template: str) -> str:
    """The HSR route name of the Starlette route template ``template``.

    :func:`~scrudful._hsr.hsr_route_name` of it, with each parameter's
    convertor but ``str`` joined to its name by ``-`` (that function reads
    ninja's ``{convertor:name}`` order, so it would drop Starlette's
    ``{name:convertor}`` one): ``/items/{id}`` and
    ``/items/{id:str}`` are ``items.id``, ``/items/{id:int}`` is
    ``items.id-int``, ``/files/{p:path}`` is ``files.p-path``. So two
    resource keys (:func:`_template_key`) have one name only when the scheme
    loses a difference: ``/a.b`` and ``/a/b`` are both ``a.b``.
    """

    def parameter(match: re.Match[str]) -> str:
        convertor = _convertor(match)
        name = match.group(1).strip()
        return f"{{{name}-{convertor}}}" if convertor else f"{{{name}}}"

    return hsr_route_name(_TEMPLATE_PARAMETER.sub(parameter, template))


def _check_reserved(paths: Mapping[str, _Resource]) -> None:
    """Raise ``ValueError`` if a scrudful path is under ``/_hsr/``."""
    for resource in paths.values():
        if resource.template.startswith(f"/{HSR_ROUTE_SEGMENT}/"):
            raise ValueError(
                f"{resource.template!r} is under '/{HSR_ROUTE_SEGMENT}/', which "
                "is reserved for scrudful's HTTP Schema Resource routes"
            )


def _owner_hsr_names(
    owner: FastAPI | APIRouter, paths: Mapping[str, _Resource]
) -> tuple[dict[str, str], dict[str, str]]:
    """:func:`_hsr_names` of ``owner``'s ``paths`` and its inverse (the name
    of each resource key), kept on ``owner``'s index when ``paths`` are its
    (see :func:`_paths_index`).

    Raises
    ------
    ValueError
        As :func:`_hsr_names`.
    """
    index = _hsr_options(owner).index
    if index is None or index.paths is not paths:
        names = _hsr_names(paths)
        return names, {key: name for name, key in names.items()}
    if index.names is None or index.name_of is None:
        names = _hsr_names(paths)
        index.names, index.name_of = names, {key: n for n, key in names.items()}
    return index.names, index.name_of


def _hsr_names(paths: Mapping[str, _Resource]) -> dict[str, str]:
    """The resource key of each HSR route name (the :func:`_hsr_route_name`
    of the resource's template) of ``paths``.

    Raises
    ------
    ValueError
        If a path is under ``/_hsr/``, or two resources have one name (such
        as ``/a.b`` and ``/a/b``, see :func:`_hsr_route_name`).
    """
    _check_reserved(paths)
    names: dict[str, str] = {}
    for key, resource in paths.items():
        name = _hsr_route_name(resource.template)
        other = names.get(name)
        if other is not None:
            raise ValueError(
                f"{paths[other].template!r} and {resource.template!r} have the "
                f"same HTTP Schema Resource route name {name!r} "
                f"('/{HSR_ROUTE_SEGMENT}/{name}'): rename a path segment, or "
                "turn the route off (hsr_route=False) and give hsr_url="
            )
        names[name] = key
    return names


async def _hsr_route_response(
    request: Request, owner: FastAPI | APIRouter, key: str | None
) -> Response:
    """An HSR route's answer: the document of the resource ``key`` (else of
    the one :class:`_HsrRoute` matched); a JSON 404 when the route was
    turned off, or the resource is gone, since; and a 405 to a method but
    GET and HEAD."""
    if request.method not in ("GET", "HEAD"):
        return _hsr_not_allowed()
    if not _hsr_options(owner).hsr_route:
        return _hsr_not_found()
    served = request.scope.get(_HSR_SERVED_KEY)
    if key is None and served is not None:
        key, paths = served
    else:
        paths = _scrudful_paths(owner)
        if key is None:
            names = _owner_hsr_names(owner, paths)[0]
            key = names.get(request.path_params.get(_HSR_PARAMETER))
    if key is None or key not in paths:
        return _hsr_not_found()
    return await _hsr_get(request, owner, key, paths)


async def _hsr_get(
    request: Request,
    owner: FastAPI | APIRouter,
    path: str,
    paths: Mapping[str, _Resource] | None = None,
) -> Response:
    """The answer to a GET or HEAD of ``path``'s HTTP Schema Resource on
    ``owner``, with ``owner``'s ``Cache-Control`` (see :func:`_hsr_response`).
    A router's describes the error entity of the app serving ``request``."""
    app = request.scope.get("app")
    built = await _run_describing_async(_hsr_flow(owner, path, paths, for_app=app))
    document = built.document
    options = _hsr_options(owner)
    key = (_template_key(path), id(built.error))
    serialized = options.serialized.get(key)
    if serialized is None or serialized.document is not document:
        body = serialize_hsr(document)
        serialized = _Serialized(document, body, hsr_etag(body))
        options.serialized[key] = serialized
    return _hsr_response(
        request, serialized.body, serialized.etag, options.hsr_cache_control
    )


def _hsr_response(
    request: Request, body: bytes, etag: str, cache_control: str
) -> Response:
    """The answer to a GET or HEAD of an HTTP Schema Resource whose
    serialization is ``body`` (:func:`~scrudful._hsr.serialize_hsr`) and
    ``ETag`` ``etag`` (:func:`~scrudful._hsr.hsr_etag`), as the Django
    adapters' (``scrudful._django.hsr_response``).

    A 200, ``application/json``, with the HSR's own Links and
    ``cache_control`` as ``Cache-Control``; or, for an ``If-None-Match``
    matching the ``ETag`` (weak comparison), a 304 with the ``ETag`` and
    ``Cache-Control``. The same for every requester.
    """
    result = evaluate_preconditions(
        request.method,
        precondition_headers(request.headers.getlist),
        etag=etag,
        last_modified=None,
        require_precondition=False,
    )
    if result is not None:
        early = _conditional_response(result, Headers())
        if result.status == 304:
            early.headers["Cache-Control"] = cache_control
        return early
    response = Response(body, media_type="application/json")
    response.headers["Link"] = hsr_response_links()
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control
    expose_headers(response.headers)
    return response


def _hsr_not_allowed() -> Response:
    """The 405 of an HSR route or endpoint to a method but GET and HEAD:
    ``Allow: GET, HEAD``, the expose headers, and neither content nor
    ``Content-Type`` (as the Django adapters' ``hsr_not_allowed``)."""
    response = Response(status_code=405, headers={"Allow": "GET, HEAD"})
    expose_headers(response.headers)
    return response


def _hsr_not_found() -> Response:
    """The HSR route's 404, with FastAPI's own 404 body."""
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _response_hsr_link(request: Request) -> str | None:
    """The HTTP Schema Resource link of a response to ``request``, or ``None``
    when it gets none.

    The rule: a response gets the link exactly when the request was routed
    to a route of a resource that an :func:`add_options` call describes, the
    resource the link is about. The decorator gives it to every response a
    :func:`scrudful` endpoint returns, and :func:`add_error_links`'s
    handlers to every error they answer (the 500 included); the OPTIONS
    answer has its own.

    - A request no route serves (an unknown URL's 404) gets none: there is
      no resource to describe.
    - So does one that no :func:`add_options` covers (:func:`_covering_owners`:
      the app's, else that of the router whose endpoint serves it), or whose
      route is on no resource it describes (a path without a scrudful
      endpoint, or under a mounted app or a Mount with middleware); and a
      CORS preflight (it carries no Links).
    - So does one of a router's resource that has no HSR route (its route
      appended to the router's ``routes`` after its :func:`add_options`, and
      before calling it again; :func:`_announce_unserved` warns of it): the
      router serves neither its document nor its OPTIONS, so it is not
      described, and a link would lead to a 404. As does every resource of
      an app whose HSR route was removed or replaced since
      (:func:`_forget_removed` warns of it). Failing each of its
      responses instead would turn a call made too early into an outage of
      the resource.
    - Otherwise the link targets :func:`_hsr_target`, whose ``ValueError``
      (two resources with one HSR route name) propagates.
    """
    scope = request.scope
    if scope.get("type") != "http" or (
        "route" not in scope and "endpoint" not in scope
    ):
        return None
    if is_preflight(request.method, request.headers.get):
        return None
    for owner in _covering_owners(request):
        paths = _scrudful_paths(owner)
        key = _routed_resource(request, owner, paths)
        if key is None:
            continue
        options = _hsr_options(owner)
        if (
            options.hsr_url is None
            and options.hsr_route
            and _hsr_route_of(owner, key) is None
        ):
            return None
        return hsr_link(_hsr_target(request, key, owner=owner, paths=paths))
    return None


def _covering_owners(request: Request) -> list[FastAPI | APIRouter]:
    """The apps and routers whose :func:`add_options` may cover ``request``:
    the request's app, when it has it, then the router of the endpoint
    serving the request, when it has it (see :func:`_live_base`)."""
    owners: list[FastAPI | APIRouter] = []
    app = request.scope.get("app")
    options = getattr(app, _OPTIONS_ATTRIBUTE, None)
    if isinstance(app, FastAPI) and isinstance(options, _HsrOptions):
        if options.explicit:
            owners.append(app)
    endpoint = request.scope.get("endpoint")
    router_ref = None
    with contextlib.suppress(TypeError):  # not weakly referenceable
        router_ref = _ROUTER_ENDPOINTS.get(endpoint)
    router = None if router_ref is None else router_ref()
    if router is None and not owners:
        router = _uncovered_router(endpoint)
    if router is not None:
        owners.append(router)
    return owners


def _routed_resource(
    request: Request, owner: FastAPI | APIRouter, paths: Mapping[str, _Resource]
) -> str | None:
    """The key of the resource of ``owner``'s ``paths`` whose route served
    ``request``, or ``None``.

    The route is the scope's ``route`` (the one Starlette routed to, also
    the one matched by path alone whose 405 it raises), found among the
    resources' routes; else its ``endpoint`` (a router's route, of which an
    older FastAPI routes a copy, or a Starlette ``Route`` that records no
    ``route``). Of the resources it is on (a route included twice, or under
    two Mounts), the first one whose template matches the request's URL
    (see :func:`_root_of`).
    """
    route = request.scope.get("route")
    endpoint = request.scope.get("endpoint")
    index = _hsr_options(owner).index
    indexed = index is not None and index.paths is paths

    def keys_of(found: Callable[[Any], bool]) -> list[str]:
        return [
            key
            for key, resource in paths.items()
            if any(found(getattr(r, "original_route", r)) for r in resource.routes)
        ]

    keys: list[str] = []
    if route is not None:
        keys = (
            index.by_route.get(id(route), [])  # type: ignore[union-attr]
            if indexed
            else keys_of(lambda r: r is route)
        )
    if not keys and endpoint is not None:
        keys = (
            index.by_endpoint.get(id(endpoint), [])  # type: ignore[union-attr]
            if indexed
            else keys_of(lambda r: getattr(r, "endpoint", None) is endpoint)
        )
    for key in keys:
        if _root_of(request, paths[key], owner) is not None:
            return key
    return None


def _hsr_target(
    request: Request,
    path: str,
    owner: FastAPI | APIRouter | None = None,
    paths: Mapping[str, _Resource] | None = None,
) -> str:
    """The URL of the HTTP Schema Resource of ``path``: the target of the HSR
    link of the responses (and OPTIONS) of ``path``.

    ``request`` is a request to a URL of ``path``; ``owner`` is the app or
    router whose :func:`add_options` covers it (by default as
    :func:`_live_base` finds it: the app's, else that of the router whose
    scrudful endpoint serves it), and ``path`` a resource key
    (:func:`_template_key`) or any template of a resource of ``owner``'s
    (a router's own templates, for a router); ``paths`` are ``owner``'s
    :func:`_scrudful_paths`, when the caller has them. Resolved in this
    order:

    1. ``hsr_url(template)``, with ``template`` the resource's (its first
       route's), as it returns it;
    2. else (with ``hsr_route=False``, which :func:`add_options` refuses
       without ``hsr_url``) a ``ValueError``;
    3. else the path of ``owner``'s HSR route for the resource, reversed on
       ``owner``'s own routes, under the request's URL path of ``owner``'s
       root (see :func:`_root_of`: where the request's app is mounted,
       parameters filled, and for a router, the prefix it came through);
       made absolute against ``owner``'s ``base_url`` (an absolute path, so
       the base URL's own path is replaced) when it has one, else against
       the request.

    Raises
    ------
    ValueError
        If no :func:`add_options` covers the request; no scrudful endpoint
        of ``owner`` serves ``path``; the route is off and there is no
        ``hsr_url``; the resource has no HSR route (a router's resource
        whose route was appended after its :func:`add_options`, whose
        responses therefore get no HSR link, see
        :func:`_response_hsr_link`); two resources have one
        route name, or a path is under ``/_hsr/``; or the request isn't to
        a URL of ``path``.
    """
    if owner is None:
        owner = _hsr_owner(request)
    options = _hsr_options(owner)
    if paths is None:
        paths = _scrudful_paths(owner)
    resource = _resource(owner, paths, path)
    if options.hsr_url is not None:
        return check_hsr_url_result(
            options.hsr_url(resource.template), f"hsr_url({resource.template!r})"
        )
    if not options.hsr_route:
        raise ValueError("hsr_route=False needs hsr_url=")
    _, name_of = _owner_hsr_names(owner, paths)
    key = _template_key(resource.template)
    route = _hsr_route_of(owner, key)
    if route is None:
        raise ValueError(
            f"{resource.template!r} has no HTTP Schema Resource route on this "
            f"{type(owner).__name__}: call add_options on it after adding its "
            "routes"
        )
    params: dict[str, str] = {}
    if isinstance(owner, FastAPI):
        params[_HSR_PARAMETER] = name_of[key]
    route_path = owner.url_path_for(route.name, **params)
    root = _root_of(request, resource, owner)
    if root is None:
        raise ValueError(
            f"the HTTP Schema Resource link of {resource.template!r} needs a "
            f"request to a URL of it, not to {request.scope['path']!r}"
        )
    url_path = root + str(route_path)
    if options.base_url:
        return resolve_uri(url_path, options.base_url)
    return str(request.url.replace(path=url_path, query="", fragment=""))


#: The characters a URL path keeps as they are (RFC 3986 ``pchar`` and ``/``;
#: ``%`` is quoted, as the request's path is decoded).
_PATH_SAFE = "/!$&'()*+,;=:@-._~"


#: Starlette's ``compile_path``, once per template (each request's
#: :func:`_root_of` needs its template's).
_compiled_path = functools.lru_cache(maxsize=4096)(compile_path)


def _root_of(
    request: Request, resource: _Resource, owner: FastAPI | APIRouter
) -> str | None:
    """The URL path (quoted) of ``owner``'s root, for a request to a URL of
    ``resource``: the part of the request's path before the part
    ``resource``'s template (in ``owner``'s frame) matches; ``None`` when
    the request's path isn't a URL of ``resource``.

    For an app, the request's ``root_path`` (where the app is mounted,
    parameters and all, whether the Mount is named or not), or a shorter
    prefix of it when ``resource`` is under a ``Mount`` of the app's own
    (whose path the template has); for a router, the ``root_path`` and the
    prefix the router is included under, as the request filled it.

    A template containing a ``path`` parameter anywhere (``"/{p:path}"``,
    ``"/files/{p:path}"``, ``"/{p:path}/raw"``) can match the rest of the
    path after several of these prefixes: ``"/files/{p:path}"`` matches
    after both ``/v1`` and ``/v1/files/a`` in ``/v1/files/a/files/b``. Of
    those, the longest whose parameters are the request's ``path_params``
    (as the route serving it converted them: here ``p`` is ``a/files/b``,
    so ``/v1``) is taken. When the request lacks the template's parameters
    (an OPTIONS the app answers, a route of the resource naming them
    otherwise than the template), they can't tell, and the longest is taken,
    which may be another prefix than the one the request came through.
    ``scope["route"].path_format`` can't tell either: FastAPI's lazily
    included routers route the router's own route, without the prefix.
    """
    scope = request.scope
    root = scope.get("root_path", "")
    path = scope["path"]
    if not path.startswith(root):  # Starlette < 0.33: the rest of the path
        path = root + path
    regex, _, convertors = _compiled_path(resource.template)
    if isinstance(owner, FastAPI):
        # root_path, then its prefixes: without the Mounts within the app.
        candidates = [root[:i] for i in range(len(root), -1, -1)]
        candidates = [c for c in candidates if c == root or root[len(c)] == "/"]
    else:
        # root_path and a prefix: the longest first.
        candidates = [
            path[:i] for i in range(len(path) - 1, len(root) - 1, -1) if path[i] == "/"
        ]
    known = scope.get("path_params") or {}
    matching = []
    for base in candidates:
        match = regex.match(path[len(base) :])
        if match is None:
            continue
        if _same_parameters(match, convertors, known):
            return quote(base, safe=_PATH_SAFE)
        matching.append(base)
    return quote(matching[0], safe=_PATH_SAFE) if matching else None


def _same_parameters(
    match: re.Match[str], convertors: Mapping[str, Any], known: Mapping[str, Any]
) -> bool:
    """Whether the parameters of a template's ``match`` that the request's
    path parameters ``known`` have are those, once ``convertors`` convert
    them. A convertor refusing a value (``ValueError``, as Starlette's do,
    or ``TypeError``) means they aren't; anything else it raises is a bug,
    which propagates rather than make another prefix the HSR link's."""
    for name, value in match.groupdict().items():
        if name not in known:
            continue
        try:
            converted = convertors[name].convert(value)
        except (ValueError, TypeError):  # the convertor refuses the value
            return False
        if converted != known[name]:
            return False
    return True


def _hsr_owner(request: Request) -> FastAPI | APIRouter:
    """The app or router whose :func:`add_options` covers ``request``: the
    app's, else that of the router whose scrudful endpoint serves it (the
    first of :func:`_covering_owners`).

    Raises
    ------
    ValueError
        If there is none.
    """
    owners = _covering_owners(request)
    if not owners:
        raise ValueError(
            "no add_options covers this request: call add_options on the app, "
            "or on the router of the endpoint serving it"
        )
    return owners[0]


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
        _check_endpoint_callables(endpoint, config)


def _check_endpoint_callables(endpoint: Any, config: _EndpointConfig) -> None:
    """:func:`_check_options_callables` of one endpoint."""
    where = f"endpoint {_endpoint_name(endpoint)}"
    _check_describe_call(config.entity, "", where)
    if config.error is not None:
        _check_describe_call(config.error, "error.", where)


def _check_describe_call(source: Entity, prefix: str, where: str) -> None:
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
    source: Entity,
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
    media_type = entity.media_type
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
    links: tuple[str | None, str | None] = (None, None),
    media_type: str | None = None,
    hsr: str | None = None,
) -> Response:
    """Build the early response, keeping headers set on the injected Response.

    FastAPI only merges the injected response's headers into responses it
    builds itself, so dependency headers would be lost. A 304 gets all of them
    (``Cache-Control``, ``Vary``, ... as a 200 would carry, RFC 9110 §15.4.5),
    except representation metadata and those it already has. Error responses
    get only the cookies and the Links: other headers (e.g. a ``public``
    Cache-Control) must not apply to them.

    ``links`` (the schema and context link-values of the ``error`` entity)
    and ``media_type`` (its Content-Type, default ``application/json``) apply
    to a response with a body, the 400, 412 and 428; a 304 gets neither.

    The injected ``Link`` lines are kept, with ``hsr`` (the HSR link) after
    them unless one of them has its relation (it wins,
    :func:`scrudful._adapter.merge_links`). On the 400, 412 and 428 an
    injected link with the ``describedby`` relation or an RDF context one
    (JSON-LD's or ``rdf-context``; relations compared case-insensitively)
    is dropped: a dependency's schema and context links describe the
    success representation, not the error body, which only the error
    entity's describe (the endpoint's ``error``, else the app's; without one
    nothing does). A 304 keeps them: it stands for the success
    representation.
    """
    if result.body is None:
        response = Response(status_code=result.status, headers=result.headers)
        links = (None, None)
    else:
        response = JSONResponse(
            result.body,
            status_code=result.status,
            headers=result.headers,
            media_type=media_type,
        )
    headers = response.headers
    own = {name for name, _ in headers.raw}
    injected_links = []
    for raw_name, raw_value in injected.raw:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name == "link":
            injected_links.append(value)
        elif name == "set-cookie":
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
    existing = ", ".join(injected_links) or None
    if result.status != 304:
        existing = _without_content_links(existing)
    link = merge_links(existing, *links, hsr)
    if link is not None:
        headers["Link"] = link
    expose_headers(headers)
    return response


#: The relations of the links describing a response's content: its schema
#: link and its RDF context links (lower-cased).
_CONTENT_LINK_RELS = frozenset(
    rel.lower() for rel in (SCHEMA_REL, JSON_LD_CONTEXT_REL, RDF_CONTEXT_REL)
)


def _without_content_links(value: str | None) -> str | None:
    """The Link field ``value`` without its link-values having a relation of
    :data:`_CONTENT_LINK_RELS` (any of a ``rel`` holding several), or
    ``None`` when none is left."""
    kept = [
        link.strip(" \t")
        for link in _split_top_level(value or "", ",")
        if link.strip(" \t") and not link_relations(link) & _CONTENT_LINK_RELS
    ]
    return ", ".join(kept) or None


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
    # annotations``) in the wrapper's globals (this module's). Each is
    # evaluated on its own: one naming something imported only under
    # TYPE_CHECKING stays a string, and leaves the others (a Security(...) in
    # an Annotated) evaluated.
    signature = resolved_signature(endpoint)
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
