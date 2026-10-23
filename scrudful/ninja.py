"""SCRUDful conditional requests and Link headers for django-ninja operations.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to django-ninja's operations. Validators and
precondition results are computed only from the object the operation's
``load`` callable returns, i.e. from what authorization already approved.

Order of execution
    ``@scrudful`` wraps the operation's function, so everything it does runs
    inside the operation, after ninja's own steps:

    1. ninja's authentication and throttling (401, 429 as usual);
    2. ninja's parameter parsing and validation (422 as usual);
    3. ``load(request, **params)``, with the parameters ninja validated;
    4. the validator callbacks, called with the loaded object, and the
       preconditions (304, 400, 412, 428);
    5. the handler, which receives the loaded object;
    6. the response headers.

    Exceptions raised by ``load``, the callbacks or the handler (e.g.
    ``Http404``, ``ninja.errors.HttpError``) go through ninja's exception
    handlers, as for any operation.

Choosing what ``load`` returns
    ``load`` returns the object, ``None`` ("no current representation") or
    raises. Whatever it does decides what a client can observe: the adapter
    never tells a missing resource from a hidden one by itself. When ``load``
    raises, the client gets that error for every method and every If-* header.
    When it returns ``None``, RFC 9110 applies: a PUT with
    ``If-None-Match: *`` reaches the handler (which may create the resource), a
    PUT with If-Match is answered with 412, and other methods reach the handler
    (which answers, e.g. 404). Whether PUT may create, and how hidden rows are
    reported, is the application's choice.

Validators
    Without validator callbacks no preconditions are evaluated: the If-*
    headers are ignored and the operation behaves like an undecorated one
    (apart from the Link and expose headers). The validator callbacks are not
    called when ``load`` returned ``None``.

    When either validator callback is configured, PUT, PATCH and DELETE on an
    existing resource must be conditional: a request without If-Match (or a
    valid If-Unmodified-Since, when ``last_modified_func`` is set, or
    ``If-None-Match: *``, which then fails with 412) is answered with 428
    Precondition Required. A malformed If-Match or If-None-Match is answered
    with 400, a failed precondition with 412 and a matching GET/HEAD with 304
    carrying the validators. POST is evaluated but never required to be
    conditional. These early responses are returned without calling the
    handler, as a ``JsonResponse`` (304 without a body), so the operation's
    ``response`` schema does not apply to them. With ``error=Entity(...)``
    the 400, 412 and 428 bodies carry that entity's Links (its link callables
    get the loaded object); the 304 has no body and gets none, and without
    ``error`` they carry no Link. Describe them in OpenAPI with
    ``openapi_extra={"responses": CONDITIONAL_READ_RESPONSES}`` (or
    :data:`CONDITIONAL_WRITE_RESPONSES`). They are built fresh, without the
    headers and cookies of ninja's temporal response: with ninja's stock
    ``NinjaAPI.create_temporal_response`` that response is still empty at this
    point, because ninja creates it after authentication and only hands it to
    the operation's function. An API overriding ``create_temporal_response``
    to preset headers or cookies does not get them on these 304, 400, 412 and
    428 responses.

    The preconditions are evaluated before the handler runs, so two concurrent
    requests carrying the same If-Match can both pass. Strict lost-update
    protection needs an atomic re-check in the handler's storage layer (e.g.
    ``select_for_update()`` in ``load`` inside a transaction, or
    ``UPDATE ... WHERE version = <expected>``).

    After the handler runs, 2xx responses to GET/HEAD get the validators
    computed for the precondition check, and 2xx responses to PUT/PATCH get
    fresh ones: ``load`` is called again and the validators are computed from
    what it returns, so they describe the representation just written (RFC
    9110 §9.3.4). Other methods (e.g. a general POST such as
    ``POST /items/search``, or a POST action on an item) and non-2xx responses
    get none, unless the handler sets them on its ``ScrudfulResponse``. The
    status is the one of a returned ``HttpResponse``, else of a returned
    ``ninja.Status`` (or ``(status, body)`` tuple), else
    ``ScrudfulResponse.status``, else the operation's single declared response
    code, else 200.

Entities and Links
    An :class:`Entity` is the schema and JSON-LD context of a body. The six
    ``schema_*`` / ``context_*`` arguments of :func:`scrudful` are the default
    response entity, ``responses={status: Entity(...)}`` overrides it per
    status (also for non-2xx statuses the handler returns, e.g. a
    ``Status(404, ...)``), and ``request=Entity(...)`` describes the request
    body: OPTIONS uses it, and it puts no header on the response. The Links of
    a response the handler produces are those of, in this order: the
    ``ScrudfulResponse.entity`` the handler set; ``responses[status]``; the
    default entity, for 2xx responses only; else none. Link callables are
    called with the request and the loaded object (after a PUT/PATCH, the one
    loaded again), or ``None`` without ``load``.

The runtime response
    A handler parameter annotated :class:`ScrudfulResponse` receives a fresh
    instance on every request; like the ``load_as`` parameter, it is hidden
    from ninja's signature and OpenAPI. What the handler sets on it applies to
    this response:

    - ``status``: for a plain return value, the wrapper returns
      ``ninja.Status(status, result)``. ninja then validates the result
      against the response schema of that status, so the operation must
      declare it (or ``...``) in ``response=``; otherwise ninja raises
      ``ConfigError``. A returned ``Status``, tuple or ``HttpResponse`` keeps
      its own status;
    - ``location``: the ``Location`` header;
    - ``etag`` and ``last_modified``: sent in place of the computed
      validators, for any method and status (the preconditions were evaluated
      with the computed ones);
    - ``entity``: its Links, in place of the declared entity's.

Headers
    The headers go on the ``HttpResponse`` the handler returns, or else on
    ninja's temporal response (passed to a handler parameter annotated
    ``HttpResponse``, which the wrapper adds, hidden, when the handler has
    none). Headers the handler set itself are never overwritten, neither by
    computed values nor by those set on ``ScrudfulResponse``. The Link header
    is appended to any Link value the handler set.
    ``Access-Control-Expose-Headers`` is extended with whichever of ``ETag``,
    ``Link`` and ``Location`` the response carries (django-cors-headers
    replaces that header when ``CORS_EXPOSE_HEADERS`` is set; list the names
    there instead).

    ninja routes HEAD only to operations that list it, e.g.
    ``@router.api_operation(["GET", "HEAD"], "/items/{pk}")``.

Async operations
    The handler may be ``async def``: the decorated function is then a
    coroutine function too, so ninja builds an async operation, and the steps
    above run in the same order with the same results. In an async operation
    ``load``, the validator callbacks and the link callables may each be
    ``def`` or ``async def``: ``async def`` ones are awaited, sync ones run
    through ``asgiref.sync.sync_to_async(func, thread_sensitive=True)``, so
    they may use the Django ORM, and an awaitable a sync callable returns is
    awaited. They are called one after the other, never concurrently. With a
    sync handler, the operation is sync and every callable is called directly,
    so they must all be sync: an ``async def`` one raises ``TypeError`` at
    decoration time (also inside the declared entities), or at request time
    for a ``ScrudfulResponse.entity``.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, NamedTuple, TypeVar

from asgiref.sync import sync_to_async
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.utils.encoding import iri_to_uri
from ninja import Status

from scrudful._adapter import (
    DEFAULT_CONTEXT_REL,
    DEFAULT_CONTEXT_TYPE,
    DEFAULT_SCHEMA_REL,
    DEFAULT_SCHEMA_TYPE,
    READ_DESCRIPTIONS,
    WRITE_DESCRIPTIONS,
    check_validators,
    evaluated_signature,
    expose_headers,
    resolve_annotation,
    set_validator_headers,
    validator_policy,
)
from scrudful._django import aware, conditional_response, precondition_headers
from scrudful.conditional import evaluate_preconditions, http_timestamp, quote_etag
from scrudful.utils import join_links, link_content

__all__ = [
    "CONDITIONAL_READ_RESPONSES",
    "CONDITIONAL_WRITE_RESPONSES",
    "Entity",
    "ScrudfulResponse",
    "scrudful",
]

#: OpenAPI descriptions of the responses ``@scrudful`` can produce for GET/HEAD
#: without calling the handler. Pass it to the operation decorator, e.g.
#: ``openapi_extra={"responses": CONDITIONAL_READ_RESPONSES}``, or merge it with
#: your own. The keys are ints, as ninja keys an operation's responses, so a
#: status the operation also declares in ``response=`` (e.g. a 412 schema) is
#: merged into one entry; the JSON schema output has string keys.
CONDITIONAL_READ_RESPONSES: dict[int, dict[str, str]] = {
    status: {"description": text} for status, text in READ_DESCRIPTIONS.items()
}

#: OpenAPI descriptions of the responses ``@scrudful`` can produce for
#: PUT/PATCH/DELETE without calling the handler, e.g.
#: ``openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES}``.
CONDITIONAL_WRITE_RESPONSES: dict[int, dict[str, str]] = {
    status: {"description": text} for status, text in WRITE_DESCRIPTIONS.items()
}

Handler = TypeVar("Handler", bound=Callable[..., Any])
T = TypeVar("T")

#: The request flow, written once for sync and async operations: a generator
#: that yields each call of a user callable, as ``(func, args, kwargs)``, and
#: is sent its result. :func:`_run_sync` and :func:`_run_async` make the calls.
_Flow = Generator[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]], Any, T]


@dataclass(frozen=True)
class Entity:
    """The schema and JSON-LD context of a request or response body.

    Each field is a value or a callable ``func(request, obj)``, with the same
    meaning as the ``schema_*`` and ``context_*`` arguments of
    :func:`scrudful`: without a link, no Link is sent; rel and type default to
    ``"describedBy"`` / ``"application/json"`` for the schema and to
    ``"http://www.w3.org/ns/json-ld#context"`` / ``"application/ld+json"`` for
    the context. ``obj`` is the object the operation has at that point (the
    loaded object, or ``None``).
    """

    schema_link_or_func: Any = None
    schema_rel_or_func: Any = None
    schema_type_or_func: Any = None
    context_link_or_func: Any = None
    context_rel_or_func: Any = None
    context_type_or_func: Any = None


class _Unset:
    """The type of :data:`_UNSET`."""

    def __repr__(self) -> str:
        return "<unset>"


#: ``ScrudfulResponse.created`` when the handler did not set it (``None`` may
#: be what it created).
_UNSET: Any = _Unset()


@dataclass(eq=False, slots=True)
class ScrudfulResponse:
    """What the handler sets, for this response, on top of the declarations.

    A handler parameter annotated ``ScrudfulResponse`` receives a fresh
    instance on every request; the parameter is hidden from ninja's signature
    and OpenAPI. Attributes left at their default change nothing. Headers the
    handler sets directly on the response are never overwritten.

    Attributes
    ----------
    status : int or None
        The status of a plain return value: the wrapper returns
        ``ninja.Status(status, result)``. ninja then validates the result
        against the operation's response schema for that status, so the
        operation must declare it (or ``...``) in ``response=``, otherwise
        ninja raises ``ConfigError``. A returned ``Status``, ``(status, body)``
        tuple or ``HttpResponse`` keeps its own status. ``None``: ninja's usual
        rules.
    location : str or None
        The ``Location`` header, as an IRI (encoded as a URI).
    etag : object
        The entity-tag of this response, in place of the computed one (see
        ``etag_func`` of :func:`scrudful` for the conversion). Sent for any
        method and status.
    last_modified : datetime or float
        The modification time of this response, in place of the computed one
        (see ``last_modified_func``). Sent for any method and status.
    entity : Entity or None
        The entity whose Links this response gets, in place of the declared
        one.
    created : object
        The object an operation created, unset by default.
    """

    status: int | None = None
    location: str | None = None
    etag: Any = None
    last_modified: Any = None
    entity: Entity | None = None
    created: Any = _UNSET


@dataclass(frozen=True)
class _Config:
    """What a scrudful decorator declared, set as ``view_func._scrudful``.

    The six ``schema_*`` / ``context_*`` fields are the default (2xx) response
    entity, also available as :attr:`entity`.
    """

    kind: str
    load: Callable[..., Any] | None = None
    load_as: str = "obj"
    etag_func: Callable[[Any], Any] | None = None
    last_modified_func: Callable[[Any], Any] | None = None
    schema_link_or_func: Any = None
    schema_rel_or_func: Any = None
    schema_type_or_func: Any = None
    context_link_or_func: Any = None
    context_rel_or_func: Any = None
    context_type_or_func: Any = None
    #: The request body's entity (described by OPTIONS; no header).
    request: Entity | None = None
    #: The response entity of each status, overriding :attr:`entity`.
    responses: Mapping[int, Entity] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: The entity of scrudful's own 400, 412 and 428 bodies.
    error: Entity | None = None

    @property
    def entity(self) -> Entity:
        """The default response entity, for 2xx responses."""
        return Entity(
            **{
                entity_field.name: getattr(self, entity_field.name)
                for entity_field in fields(Entity)
            }
        )

    def response_entity(self, status: int) -> Entity | None:
        """The declared entity of a response with ``status``."""
        if status in self.responses:
            return self.responses[status]
        return self.entity if 200 <= status < 300 else None

    @property
    def has_etag(self) -> bool:
        return self.etag_func is not None

    @property
    def has_last_modified(self) -> bool:
        return self.last_modified_func is not None

    @property
    def has_validators(self) -> bool:
        return self.has_etag or self.has_last_modified


def scrudful(
    *,
    load: Callable[..., Any] | None = None,
    load_as: str = "obj",
    etag_func: Callable[[Any], Any] | None = None,
    last_modified_func: Callable[[Any], Any] | None = None,
    schema_link_or_func: Any = None,
    schema_rel_or_func: Any = None,
    schema_type_or_func: Any = None,
    context_link_or_func: Any = None,
    context_rel_or_func: Any = None,
    context_type_or_func: Any = None,
    request: Entity | None = None,
    responses: Mapping[int, Entity] | None = None,
    error: Entity | None = None,
) -> Callable[[Handler], Handler]:
    """Decorate a django-ninja operation on a single resource to make it SCRUDful.

    ``@scrudful(...)`` must sit *below* the operation decorator::

        @router.put(
            "/items/{pk}",
            response=ItemOut,
            openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES},
        )
        @scrudful(
            load=authorized_item,
            etag_func=lambda item: item.version,
            last_modified_func=lambda item: item.modified_at,
            schema_link_or_func="/schemas/item",
            context_link_or_func="/contexts/item",
        )
        def update_item(request, pk: int, payload: ItemIn, obj: Item): ...

    Placed above it, ninja registers the undecorated function and the
    decorator has no effect. The handler may be ``async def``, and then the
    callables may be too (see "Async operations" in the module docstring).
    All arguments are keyword-only; the behaviour is described in the module
    docstring.

    Parameters
    ----------
    load : callable, optional
        ``load(request, **params)`` returns the target object if the request
        may act on it, ``None`` if there is no current representation, or
        raises (e.g. ``HttpError(404)``). ``params`` are the parameters ninja
        validated for the operation (path, query, body ...), without the
        hidden ones; accept ``**kwargs`` to ignore those you don't need.
        Required when a validator callback is set.
    load_as : str, optional
        Name of the handler parameter receiving the loaded object (default
        ``"obj"``). The parameter is hidden from ninja's signature and
        OpenAPI; the handler must declare it when ``load`` is set.
    etag_func : callable, optional
        ``etag_func(obj)`` returns the entity-tag of the loaded object, or
        ``None`` if there is none. ``str()`` of the result is used: a valid
        entity-tag (``'"abc"'`` or ``'W/"abc"'``) is sent unchanged, anything
        else is wrapped in double quotes (a strong entity-tag). Values that
        cannot be an entity-tag (containing a double quote, space, control
        character or a character above U+00FF) raise ``ValueError``. Weak
        entity-tags are allowed, but If-Match uses the strong comparison, which
        they never pass: only If-Unmodified-Since (or ``If-Match: *``) can then
        make a write succeed.
    last_modified_func : callable, optional
        ``last_modified_func(obj)`` returns the modification time of the
        loaded object as a ``datetime`` or an epoch timestamp, or ``None`` if
        there is none. Naive datetimes are taken to be in the default time
        zone (``settings.TIME_ZONE``), as Django does. Sub-second precision is
        discarded.
    schema_link_or_func : str or callable, optional
        URL of the schema of the 2xx response body: this argument and the
        five after it are the default response entity (see ``responses``).
        If no value is given, no schema link is sent. Each ``*_or_func``
        argument is a value or a callable ``func(request, obj)``, where
        ``obj`` is the loaded object (after a PUT/PATCH, the one loaded
        again), or ``None`` without ``load``.
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
    request : Entity, optional
        The schema and context of the request body. Described by OPTIONS; it
        puts no header on the response.
    responses : mapping of int to Entity, optional
        The response entity of each status, in place of the default one (the
        six arguments above) for that status, e.g. ``{404: Entity(...)}`` for
        a ``Status(404, ...)`` the handler returns.
    error : Entity, optional
        The schema and context of scrudful's own 400, 412 and 428 bodies, sent
        as Links on them. Without it, they carry no Link.

    Raises
    ------
    TypeError
        If a validator callback is set without ``load``, if ``load`` or a
        validator callback isn't callable, if ``request``, ``error`` or a value
        of ``responses`` isn't an :class:`Entity` or a key of ``responses``
        isn't an HTTP status code, if ``load`` is set and the handler has no
        ``load_as`` parameter, if the handler is a generator, or if the
        handler is sync and ``load``, a validator callback or a link callable
        (also inside an entity) is ``async def``.
    """
    check_validators(etag_func, last_modified_func)
    if load is not None and not callable(load):
        raise TypeError(f"load must be callable, not {type(load).__name__}")
    _check_entity("request", request)
    _check_entity("error", error)
    config = _Config(
        kind="item",
        load=load,
        load_as=load_as,
        etag_func=etag_func,
        last_modified_func=last_modified_func,
        schema_link_or_func=schema_link_or_func,
        schema_rel_or_func=schema_rel_or_func,
        schema_type_or_func=schema_type_or_func,
        context_link_or_func=context_link_or_func,
        context_rel_or_func=context_rel_or_func,
        context_type_or_func=context_type_or_func,
        request=request,
        responses=_entities_by_status(responses),
        error=error,
    )
    if config.has_validators and load is None:
        raise TypeError(
            "etag_func and last_modified_func require load: the validators are "
            "computed from the object load returns"
        )

    def decorator(handler: Handler) -> Handler:
        if inspect.isgeneratorfunction(handler) or inspect.isasyncgenfunction(handler):
            raise TypeError("@scrudful does not support generator handlers")
        if not inspect.iscoroutinefunction(handler):
            _check_sync_callables(config, handler)
        hidden = _wrapper_signature(handler, load_as if load is not None else None)
        response_name = hidden.response_name
        # The single response code each method's operation declares, if any.
        declared: dict[str, int | None] = {}

        def flow(request: HttpRequest, values: dict[str, Any]) -> _Flow[Any]:
            """The request flow; each call of a user callable is a ``yield``."""
            response: HttpResponse = (
                values[response_name]
                if hidden.pass_response
                else values.pop(response_name)
            )
            params = {
                name: value for name, value in values.items() if name != response_name
            }
            meta = ScrudfulResponse()
            if hidden.meta_name is not None:
                values[hidden.meta_name] = meta
            method = request.method.upper()
            obj = None
            if load is not None:
                obj = yield load, (request,), params
                values[load_as] = obj

            if config.has_validators:
                etag, last_modified = yield from _validators(config, obj)
                result = evaluate_preconditions(
                    method,
                    precondition_headers(request),
                    etag=etag,
                    last_modified=last_modified,
                    require_precondition=True,
                )
                if result is not None:
                    early = conditional_response(result)
                    if result.body is not None and config.error is not None:
                        yield from _add_links(early, config.error, request, obj)
                        expose_headers(early)
                    return early

            content = yield handler, (request,), values

            if meta.status is not None and not _has_status(content):
                content = Status(meta.status, content)
            headers, status = _response_and_status(
                content, response, declared.get(method)
            )
            # Explicit values first: the computed ones never overwrite a header.
            set_validator_headers(
                headers,
                None if meta.etag is None else quote_etag(meta.etag),
                http_timestamp(aware(meta.last_modified)),
            )
            if meta.location is not None and "Location" not in headers:
                headers["Location"] = iri_to_uri(str(meta.location))
            if config.has_validators:
                refresh, send = validator_policy(method, status)
                if refresh:
                    obj = yield load, (request,), params
                    etag, last_modified = yield from _validators(config, obj)
                if send:
                    set_validator_headers(headers, etag, last_modified)
            if meta.entity is None:
                entity = config.response_entity(status)
            elif isinstance(meta.entity, Entity):
                entity = meta.entity
            else:
                raise TypeError(
                    "ScrudfulResponse.entity must be an Entity, not "
                    f"{type(meta.entity).__name__}"
                )
            if entity is not None:
                yield from _add_links(headers, entity, request, obj)
            expose_headers(headers)
            return content

        # ninja builds an AsyncOperation for a coroutine function: the wrapper
        # is one exactly when the handler is.
        if inspect.iscoroutinefunction(handler):

            @functools.wraps(handler)
            async def wrapper(request: HttpRequest, **values: Any) -> Any:
                return await _run_async(flow(request, values))

        else:

            @functools.wraps(handler)
            def wrapper(request: HttpRequest, **values: Any) -> Any:
                return _run_sync(flow(request, values))

        wrapper.__signature__ = hidden.signature  # type: ignore[attr-defined]
        wrapper._scrudful = config  # type: ignore[attr-defined]
        # ninja calls these at the end of ``Operation.__init__``. A fresh list:
        # ``functools.wraps`` copied the handler's (e.g. from ``@paginate``) by
        # reference, and appending to it would change the handler.
        wrapper._ninja_contribute_to_operation = [  # type: ignore[attr-defined]
            *getattr(handler, "_ninja_contribute_to_operation", []),
            functools.partial(_capture_declared_status, declared),
        ]
        return wrapper  # type: ignore[return-value]

    return decorator


def _run_sync(flow: _Flow[T]) -> T:
    """Run ``flow``, calling the callables it yields directly.

    The decorator already rejected the declared ``async def`` callables; this
    catches those set at request time (``ScrudfulResponse.entity``).
    """
    result = None
    while True:
        try:
            func, args, kwargs = flow.send(result)
        except StopIteration as stop:
            return stop.value
        if _is_async_callable(func):
            raise TypeError(
                f"{func!r} is async, but the operation is not: async callables "
                "need an async def handler"
            )
        result = func(*args, **kwargs)


async def _run_async(flow: _Flow[T]) -> T:
    """Run ``flow`` in an async operation, see :func:`_call_async`."""
    result = None
    while True:
        try:
            func, args, kwargs = flow.send(result)
        except StopIteration as stop:
            return stop.value
        result = await _call_async(func, args, kwargs)


async def _call_async(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """Call ``func`` from an async operation and return its result.

    ``async def`` callables are awaited. Sync ones run through
    ``sync_to_async(thread_sensitive=True)``, off the event loop, as the
    Django ORM requires; an awaitable they return is awaited.
    """
    if _is_async_callable(func):
        return await func(*args, **kwargs)
    result = await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _is_async_callable(func: Any) -> bool:
    """Whether calling ``func`` returns a coroutine: an ``async def`` function
    (also behind ``functools.partial``) or an object with an async
    ``__call__``."""
    return inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(
        getattr(func, "__call__", None)
    )


def _check_sync_callables(config: _Config, handler: Callable[..., Any]) -> None:
    """Reject ``async def`` callables for a sync handler (a sync operation)."""
    for name, func in _declared_callables(config):
        if _is_async_callable(func):
            raise TypeError(
                f"{name} is async, but {handler.__qualname__}() is not: async "
                "callables need an async def handler"
            )


def _declared_callables(config: _Config) -> Iterator[tuple[str, Any]]:
    """``(name, value)`` of every argument that may be a callable."""
    yield "load", config.load
    yield "etag_func", config.etag_func
    yield "last_modified_func", config.last_modified_func
    entities: list[tuple[str, Entity | None]] = [
        ("", config.entity),
        ("request.", config.request),
        ("error.", config.error),
    ]
    entities += [
        (f"responses[{status}].", entity) for status, entity in config.responses.items()
    ]
    for prefix, entity in entities:
        if entity is not None:
            for entity_field in fields(Entity):
                yield prefix + entity_field.name, getattr(entity, entity_field.name)


def _check_entity(name: str, entity: Any) -> None:
    """Raise ``TypeError`` unless ``entity`` is ``None`` or an :class:`Entity`."""
    if entity is not None and not isinstance(entity, Entity):
        raise TypeError(f"{name} must be an Entity, not {type(entity).__name__}")


def _entities_by_status(
    responses: Mapping[int, Entity] | None,
) -> Mapping[int, Entity]:
    """A read-only copy of the ``responses`` argument, checked."""
    if responses is None:
        responses = {}
    if not isinstance(responses, Mapping):
        raise TypeError(f"responses must be a mapping, not {type(responses).__name__}")
    for status, entity in responses.items():
        if not (
            isinstance(status, int)
            and not isinstance(status, bool)
            and 100 <= status <= 599
        ):
            raise TypeError(f"responses keys must be HTTP status codes, not {status!r}")
        _check_entity(f"responses[{status}]", entity)
    return MappingProxyType(dict(responses))


def _validators(config: _Config, obj: Any) -> _Flow[tuple[str | None, int | None]]:
    """The validators of ``obj``; none when there is no representation."""
    etag = last_modified = None
    if obj is None:
        return etag, last_modified
    if config.etag_func is not None:
        value = yield config.etag_func, (obj,), {}
        etag = None if value is None else quote_etag(value)
    if config.last_modified_func is not None:
        value = yield config.last_modified_func, (obj,), {}
        last_modified = http_timestamp(aware(value))
    return etag, last_modified


def _add_links(
    headers: HttpResponseBase, entity: Entity, request: HttpRequest, obj: Any
) -> _Flow[None]:
    """Append the Links of ``entity`` to the Link header of ``headers``."""
    links = yield from _links(entity, request, obj)
    link = join_links(headers.get("Link"), links)
    if link is not None:
        headers["Link"] = link


def _links(entity: Entity, request: HttpRequest, obj: Any) -> _Flow[str | None]:
    """The Link field value of the schema and context links of ``entity``."""
    schema = yield from _link(
        request,
        obj,
        (
            entity.schema_link_or_func,
            entity.schema_rel_or_func,
            entity.schema_type_or_func,
        ),
        (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
    )
    context = yield from _link(
        request,
        obj,
        (
            entity.context_link_or_func,
            entity.context_rel_or_func,
            entity.context_type_or_func,
        ),
        (DEFAULT_CONTEXT_REL, DEFAULT_CONTEXT_TYPE),
    )
    return join_links(schema, context)


def _link(
    request: HttpRequest,
    obj: Any,
    values_or_funcs: tuple[Any, Any, Any],
    defaults: tuple[str, str],
) -> _Flow[str | None]:
    """Build one link-value from (url, rel, type) values or callables."""

    def value(value_or_func: Any) -> _Flow[Any]:
        if callable(value_or_func):
            return (yield value_or_func, (request, obj), {})
        return value_or_func

    url_or_func, rel_or_func, type_or_func = values_or_funcs
    url = yield from value(url_or_func)
    if not url:
        return None
    rel = (yield from value(rel_or_func)) or defaults[0]
    content_type = (yield from value(type_or_func)) or defaults[1]
    return link_content(url, rel, content_type)


def _has_status(content: Any) -> bool:
    """Whether the handler's return value carries its own status."""
    return isinstance(content, (HttpResponseBase, Status)) or (
        isinstance(content, tuple) and len(content) == 2
    )


def _response_and_status(
    content: Any, temporal: HttpResponse, declared: int | None
) -> tuple[HttpResponseBase, int]:
    """The response the headers go on, and the status ninja will send."""
    if isinstance(content, HttpResponseBase):
        return content, content.status_code
    if isinstance(content, Status):
        return temporal, content.status_code
    if isinstance(content, tuple) and len(content) == 2:
        # ``(status, body)``: deprecated by ninja, still honoured.
        return temporal, content[0]
    return temporal, 200 if declared is None else declared


def _capture_declared_status(declared: dict[str, int | None], operation: Any) -> None:
    """Record the operation's single declared response code, per method.

    ninja uses it as the status of a plain return value. Called by ninja at the
    end of ``Operation.__init__``.
    """
    codes = list(operation.response_models)
    code = codes[0] if len(codes) == 1 and isinstance(codes[0], int) else None
    for method in operation.methods:
        declared[method.upper()] = code


class _HiddenParameters(NamedTuple):
    """The wrapper's signature, and the handler parameters hidden from ninja."""

    signature: inspect.Signature
    #: The parameter ninja passes its temporal response to.
    response_name: str
    #: Whether the handler itself declares ``response_name``.
    pass_response: bool
    #: The handler parameter receiving the ``ScrudfulResponse``, if any.
    meta_name: str | None


def _wrapper_signature(
    handler: Callable[..., Any], load_as: str | None
) -> _HiddenParameters:
    """Return the wrapper's signature and where the hidden parameters go.

    The handler's parameters are kept, so ninja's parameter parsing and OpenAPI
    see the handler unchanged, except for the ``load_as`` parameter and the
    (first) parameter annotated ``ScrudfulResponse``, which are removed. ninja
    passes its temporal response to the parameter annotated ``HttpResponse``
    (by identity); one is appended (keyword-only, with a private name) unless
    the handler declares it.
    """
    signature = evaluated_signature(handler)
    if load_as is not None and load_as not in signature.parameters:
        raise TypeError(
            f"{handler.__qualname__}() has no parameter {load_as!r} to receive "
            "the object load returns (see load_as)"
        )
    globalns = getattr(inspect.unwrap(handler), "__globals__", {})
    params = []
    response_name = meta_name = None
    for param in signature.parameters.values():
        if param.name == load_as:
            continue
        annotation = resolve_annotation(param.annotation, globalns)
        if meta_name is None and annotation is ScrudfulResponse:
            meta_name = param.name
            continue
        if response_name is None and annotation is HttpResponse:
            response_name = param.name
            # ninja would evaluate a string in this module's globals.
            param = param.replace(annotation=HttpResponse)
        params.append(param)
    if response_name is not None:
        return _HiddenParameters(
            signature.replace(parameters=params), response_name, True, meta_name
        )

    # Not a possible ninja parameter name (pydantic rejects leading
    # underscores), so it can't collide with the handler's.
    response_name = "_scrudful_response"
    extra = inspect.Parameter(
        response_name, inspect.Parameter.KEYWORD_ONLY, annotation=HttpResponse
    )
    if params and params[-1].kind is inspect.Parameter.VAR_KEYWORD:
        params.insert(-1, extra)
    else:
        params.append(extra)
    return _HiddenParameters(
        signature.replace(parameters=params), response_name, False, meta_name
    )
