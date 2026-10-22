"""SCRUDful conditional requests and Link headers for FastAPI endpoints.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to FastAPI's request/response handling.
"""

from __future__ import annotations

import functools
import inspect
import sys
from collections.abc import Callable, Mapping
from typing import Annotated, Any, TypeVar, get_args, get_origin

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders

from scrudful.conditional import (
    SAFE_METHODS,
    ConditionalResult,
    evaluate_preconditions,
    http_timestamp,
    quote_etag,
    validator_headers,
)
from scrudful.utils import (
    EXPOSABLE_HEADERS,
    join_links,
    link_content,
    merge_expose_headers,
)

__all__ = [
    "CONDITIONAL_READ_RESPONSES",
    "CONDITIONAL_WRITE_RESPONSES",
    "scrudful",
]

_MALFORMED = {"description": "Malformed If-Match or If-None-Match header."}
_PRECONDITION_FAILED = {"description": "Precondition Failed."}

#: OpenAPI descriptions of the responses ``@scrudful`` can produce for GET/HEAD
#: without calling the endpoint. Pass it to the route decorator, e.g.
#: ``@app.get("/items/{item_id}", responses=CONDITIONAL_READ_RESPONSES)``, or
#: merge it with your own: ``responses={**CONDITIONAL_READ_RESPONSES, 404: ...}``.
CONDITIONAL_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    304: {
        "description": "Not Modified: the representation matches the request's "
        "If-None-Match or If-Modified-Since validators.",
    },
    400: dict(_MALFORMED),
    412: dict(_PRECONDITION_FAILED),
}

#: OpenAPI descriptions of the responses ``@scrudful`` can produce for
#: PUT/PATCH/DELETE without calling the endpoint, e.g.
#: ``@app.put("/items/{item_id}", responses=CONDITIONAL_WRITE_RESPONSES)``.
CONDITIONAL_WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: dict(_MALFORMED),
    412: dict(_PRECONDITION_FAILED),
    428: {
        "description": "Precondition Required: this request must be conditional "
        "(send If-Match or If-Unmodified-Since).",
    },
}

DEFAULT_SCHEMA_REL = "describedBy"
DEFAULT_SCHEMA_TYPE = "application/json"
DEFAULT_CONTEXT_REL = "http://www.w3.org/ns/json-ld#context"
DEFAULT_CONTEXT_TYPE = "application/ld+json"

_PRECONDITION_FIELDS = (
    "if-match",
    "if-none-match",
    "if-modified-since",
    "if-unmodified-since",
)
_FRESH_VALIDATOR_METHODS = frozenset({"PUT", "PATCH"})

Endpoint = TypeVar("Endpoint", bound=Callable[..., Any])


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
        so ``response_model`` does not apply to them; document them with
        :data:`CONDITIONAL_READ_RESPONSES` and
        :data:`CONDITIONAL_WRITE_RESPONSES`.

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
    appended to any Link value the endpoint set. ``Access-Control-Expose-Headers``
    is extended with whichever of ``ETag``, ``Link`` and ``Location`` the
    response carries. Starlette's ``CORSMiddleware`` replaces that header on
    cross-origin requests, so if you use it, list these names in its
    ``expose_headers`` instead.

    If the endpoint returns a Starlette ``Response`` the headers are added to
    it, otherwise to the response FastAPI builds. ``HTTPException`` raised by
    the endpoint propagates unchanged. FastAPI does not route HEAD to a
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
        ``rel`` of the schema link. Defaults to ``"describedBy"``.
    schema_type_or_func : str or callable, optional
        ``type`` of the schema link. Defaults to ``"application/json"``.
    context_link_or_func : str or callable, optional
        URL of the JSON-LD context of the response body. If no value is given,
        no context link is sent.
    context_rel_or_func : str or callable, optional
        ``rel`` of the context link. Defaults to
        ``"http://www.w3.org/ns/json-ld#context"``.
    context_type_or_func : str or callable, optional
        ``type`` of the context link. Defaults to ``"application/ld+json"``.
    """
    for name, func in (
        ("etag_func", etag_func),
        ("last_modified_func", last_modified_func),
    ):
        if func is not None and not callable(func):
            raise TypeError(f"{name} must be callable, not {type(func).__name__}")
    has_validators = etag_func is not None or last_modified_func is not None

    async def validators(kwargs: Mapping[str, Any]) -> tuple[str | None, int | None]:
        etag = None
        if etag_func is not None:
            value = await _call(etag_func, kwargs)
            etag = None if value is None else quote_etag(value)
        last_modified = None
        if last_modified_func is not None:
            last_modified = http_timestamp(await _call(last_modified_func, kwargs))
        return etag, last_modified

    async def links(kwargs: Mapping[str, Any]) -> str | None:
        schema = await _link(
            kwargs,
            (schema_link_or_func, schema_rel_or_func, schema_type_or_func),
            (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
        )
        context = await _link(
            kwargs,
            (context_link_or_func, context_rel_or_func, context_type_or_func),
            (DEFAULT_CONTEXT_REL, DEFAULT_CONTEXT_TYPE),
        )
        return join_links(schema, context)

    def decorator(endpoint: Endpoint) -> Endpoint:
        if inspect.isgeneratorfunction(endpoint) or inspect.isasyncgenfunction(
            endpoint
        ):
            raise TypeError("@scrudful does not support generator endpoints")
        signature, request_name, pass_request, response_name, pass_response = (
            _wrapper_signature(endpoint)
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

            if has_validators:
                etag, last_modified = await validators(kwargs)
                result = evaluate_preconditions(
                    method,
                    _precondition_headers(request),
                    etag=etag,
                    last_modified=last_modified,
                    require_precondition=True,
                )
                if result is not None:
                    return _conditional_response(result)

            content = await _call(endpoint, kwargs)

            if isinstance(content, Response):
                headers, status = content.headers, content.status_code
            else:
                headers, status = response.headers, _status_code(request, response)
            if 200 <= status < 300 and has_validators:
                if method in _FRESH_VALIDATOR_METHODS:
                    etag, last_modified = await validators(kwargs)
                if method in SAFE_METHODS or method in _FRESH_VALIDATOR_METHODS:
                    for name, value in validator_headers(etag, last_modified).items():
                        if name not in headers:
                            headers[name] = value
            link = join_links(", ".join(headers.getlist("link")), await links(kwargs))
            if link is not None:
                headers["Link"] = link
            _expose_headers(headers)
            return content

        wrapper.__signature__ = signature  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator


async def _call(func: Callable[..., Any], kwargs: Mapping[str, Any]) -> Any:
    """Call ``func`` with ``kwargs``; sync callables run in the threadpool."""
    if inspect.iscoroutinefunction(func):
        return await func(**kwargs)
    result = await run_in_threadpool(functools.partial(func, **kwargs))
    if inspect.isawaitable(result):
        result = await result
    return result


async def _link(
    kwargs: Mapping[str, Any],
    values_or_funcs: tuple[Any, Any, Any],
    defaults: tuple[str, str],
) -> str | None:
    """Build one link-value from (url, rel, type) values or callables."""

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
    return link_content(url, rel, content_type)


def _precondition_headers(request: Request) -> dict[str, str]:
    # Repeated field lines are combined as one comma-separated list (RFC 9110
    # §5.3).
    headers = {}
    for name in _PRECONDITION_FIELDS:
        values = request.headers.getlist(name)
        if values:
            headers[name] = ", ".join(values)
    return headers


def _conditional_response(result: ConditionalResult) -> Response:
    if result.body is None:
        response = Response(status_code=result.status, headers=result.headers)
    else:
        response = JSONResponse(
            result.body, status_code=result.status, headers=result.headers
        )
    _expose_headers(response.headers)
    return response


def _expose_headers(headers: MutableHeaders) -> None:
    names = [name for name in EXPOSABLE_HEADERS if name in headers]
    if names:
        headers["Access-Control-Expose-Headers"] = merge_expose_headers(
            headers.get("access-control-expose-headers"), names
        )


def _status_code(request: Request, response: Response) -> int:
    """Status of the response FastAPI will build from a non-Response result."""
    if response.status_code:
        return response.status_code
    route = request.scope.get("route")
    return getattr(route, "status_code", None) or 200


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
    signature = _signature(endpoint)
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


def _signature(endpoint: Callable[..., Any]) -> inspect.Signature:
    # Evaluate string annotations (``from __future__ import annotations``) in
    # the endpoint's own module: FastAPI < 0.124 evaluates the strings it finds
    # on the wrapper in the wrapper's globals (this module's), where the
    # endpoint's names are undefined.
    try:
        return inspect.signature(endpoint, eval_str=True)
    except NameError:
        pass
    try:
        return inspect.signature(endpoint)
    except NameError:
        # Python 3.14+: annotations naming objects imported only under
        # TYPE_CHECKING can't be evaluated; FastAPI does the same.
        if sys.version_info < (3, 14):
            raise
        from annotationlib import Format

        return inspect.signature(endpoint, annotation_format=Format.FORWARDREF)


def _annotation_is(annotation: Any, cls: type, globalns: dict[str, Any]) -> bool:
    """Whether ``annotation`` is ``cls`` or a subclass.

    String / forward-reference annotations are evaluated in the endpoint's
    globals and ``Annotated[cls, ...]`` is unwrapped, as FastAPI does.
    """
    if not isinstance(annotation, (str, type)):
        annotation = getattr(annotation, "__forward_arg__", annotation)
    if isinstance(annotation, str):
        try:
            annotation = eval(annotation, globalns)  # same as typing/inspect
        except Exception:
            return False
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return isinstance(annotation, type) and issubclass(annotation, cls)
