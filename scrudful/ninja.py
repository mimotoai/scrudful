"""SCRUDful conditional requests and Link headers for django-ninja operations.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to django-ninja's operations. Validators and
precondition results are computed only from the object the operation's
``load`` callable returns, i.e. from what authorization already approved (for
a create, from the object the handler created).

Order of execution
    ``@scrudful`` wraps the operation's function, so everything it does runs
    inside the operation, after ninja's own steps:

    1. ninja's authentication and throttling (401, 429 as usual), with the
       optional ``before`` and ``after_throttles`` hooks, which scrudful runs
       in its copy of ninja's checks: authentication, ``before``, throttling,
       ``after_throttles`` (DRF's order; see :func:`scrudful_list`);
    2. ninja's parameter parsing and validation (422 as usual);
    3. ``load(request, **params)``, with the parameters ninja validated;
    4. the validator callbacks, called with the loaded object, and the
       preconditions (304, 400, 412, 428);
    5. the handler, which receives the loaded object;
    6. the response headers.

    Exceptions raised by ``load``, the callbacks or the handler (e.g.
    ``Http404``, ``ninja.errors.HttpError``) go through ninja's exception
    handlers, as for any operation; the response of an ``HttpError`` or
    ``Http404`` then gets the operation's Links (see "Error responses").

Placement
    The decorator sits directly below the operation decorator. When ninja
    builds the operation, it replaces ``operation.view_func`` by a wrapper
    bound to that operation's ``response=``. Code placed below the decorator
    (a decorator on the handler) runs after ``load`` and the preconditions,
    when a 304, 412 or 428, or an ETag, may already be decided: never put an
    authorization or caching decorator there. Instead:

    - per-operation authorization goes in ``load`` (fetch-if-allowed) or
      ninja's ``auth=``;
    - a decorator for every operation of a router or the API is added with
      ``add_decorator(decorator, mode="operation")``: it wraps ``view_func``,
      so it runs inside ``Operation.run``, after ninja's authentication and
      throttling and before ``load``, and its exceptions go through ninja's
      exception handlers;
    - a wrapper for one operation, in that same place, is a
      ``ninja.utils.contribute_operation_callback`` callback wrapping
      ``operation.view_func``, contributed by a decorator applied above this
      one (it then runs after the binding);
    - ``ninja.decorators.decorate_view`` (and ``add_decorator`` with
      ``mode="view"``) wraps ``Operation.run``: it runs before ninja's
      authentication and outside its exception handlers, so it suits
      response-level concerns such as ``cache_page``, not a check that
      raises.

    Something that replaces ``view_func`` before the binding (such a callback
    contributed below the decorator, or a decorator between it and the
    operation decorator) would be discarded by it: ninja raises
    ``ninja.errors.ConfigError`` when the operation is built. Stacking two
    scrudful decorators on one handler is not supported (it raises
    ``ConfigError``, or ``TypeError`` when the inner one has ``load``).

Choosing what ``load`` returns
    ``load`` returns the object, ``None`` ("no current representation") or
    raises. Whatever it does decides what a client can observe: the adapter
    never tells a missing resource from a hidden one by itself. When ``load``
    raises, the client gets that error for every method and every If-* header.
    When it returns ``None``, RFC 9110 applies: a PUT with
    ``If-None-Match: *`` reaches the handler (which may create the resource), a
    PUT with If-Match is answered with 412, and other methods reach the handler
    (which answers, e.g. 404). Whether PUT may create, and how hidden rows are
    reported, is the application's choice. When it returns an object, the
    resource exists, even if the validator callbacks return ``None`` for it
    (RFC 9110 §13.1.1-2): a write then needs ``If-Match: *`` (428 without
    it), an If-Match listing entity-tags fails with 412, and
    ``If-None-Match: *`` fails with 412 (304 for GET/HEAD).

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
    handler, their JSON bodies written by the API's renderer (304 without a
    body; see "The API's renderer" in the documentation), so the operation's
    ``response`` schema does not apply to them. With ``error=Entity(...)``
    the 400, 412 and 428 bodies carry that entity's Links (its link callables
    get the loaded object) and are served with its media type, which must be
    JSON content (e.g. ``application/problem+json``); the 304 has no body and
    gets none, and without ``error`` they carry no Link and are
    ``application/json``. Describe them in OpenAPI with
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
    9110 §9.3.4). That second call happens after the handler's write: if it
    raises (e.g. the write moved the object out of the caller's scope), the
    client gets that error although the write is committed. Other methods
    (e.g. a general POST such as
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
    default entity, for 2xx responses other than 204 (which has no body);
    else none. Link callables are called with the request and the loaded
    object, or ``None`` without ``load``. After a 2xx PUT/PATCH they get the
    object loaded again: ``load`` is called a second time (as for the
    validators, with the same consequence if it raises) when a validator
    callback is set or a value of the response's entity is callable, and not
    otherwise.

Error responses
    An ``HttpError`` (or a subclass) or ``Http404`` raised by ``load``, a
    callback, the handler or a list's paging (the 404 of a page past the
    last one) propagates out of the operation's function as for any
    operation, and ninja answers it with the API's exception handler: its
    status and body are the API's. When that response has content, it gets
    the Links of the entity the operation's OPTIONS describes for its
    status: ``responses[status]`` (``error`` first for scrudful's own 400,
    412 and 428 when the operation evaluates the If-* headers), else
    ``error``, else none. Link callables get ``None``. A response with JSON
    content (ninja's renderer's ``application/json``) is then served as that
    entity's media type when it is JSON content too, e.g.
    ``application/problem+json``, as OPTIONS lists it. The errors ninja
    answers before the operation runs (401, 422, 429, ...) and those of
    operations without an entity for them get Links from
    :func:`add_error_links`, the API-wide error entity.

Create
    :func:`scrudful_create` decorates a POST on a collection that creates an
    item. It is the flow above with a preset, not a separate one: there is no
    ``load`` and no precondition evaluation (the If-* headers are ignored and
    a create is never answered with 428), so authorization is the handler's,
    decided from the parsed body. A plain return value gets status 201: the
    wrapper returns ``ninja.Status(201, result)``, so the operation must
    declare 201 (e.g. ``response={201: ItemOut}``) or ``...`` in
    ``response=``; with ``response=ItemOut`` (a 200) or no ``response=``,
    ninja raises ``ConfigError``. ``ScrudfulResponse.status`` and a returned
    ``Status`` or ``HttpResponse`` set another status.

    The created object is ``ScrudfulResponse.created`` if the handler set it,
    else the body the handler returned (the value of a returned ``Status``).
    A returned ``HttpResponse`` is not an object, and neither is ``None``
    (e.g. with ``response={201: None}``): such a handler sets
    ``ScrudfulResponse.created`` to get the Location, validators and item
    Links, or ``ScrudfulResponse.location`` for the Location alone. A 201
    response with a created object gets:

    - ``Location``: ``location_func(request, obj)``, not called when the
      response already has one (from the handler or ``ScrudfulResponse``);
    - ``ETag`` and ``Last-Modified``: from ``etag_func`` and
      ``last_modified_func`` applied to the created object. Pass the
      callbacks of the item operations, so the 201 carries the validators a
      GET of the new item sends and a client can edit it without a GET;
    - the item entity's Links (``responses[201]``, if declared, replaces it);
      link callables get the created object.

    A 201 without a created object gets no Location and no validators
    (``location_func`` and the validator callbacks are not called). If it
    has a body (an ``HttpResponse`` with non-empty or streaming content, or a
    body other than ``None`` with ``ScrudfulResponse.created = None``), it
    gets the item entity's Links when none of the entity's six values is
    callable; a callable would be called with ``None``, not the created
    item, so with one the 201 gets no item Links and no item callable is
    called. Without a body (``None``, or an empty ``HttpResponse``) it gets
    no item Links. Any other status (e.g. a ``Status(400, ...)``) gets none
    of these. ``responses[status]`` replaces the item entity on any of them
    (its link callables get ``ScrudfulResponse.created``, or ``None`` if
    unset), and the values set on ``ScrudfulResponse`` and headers the
    handler set win, as below.

Collections
    :func:`scrudful_list` decorates a GET on a collection. It works like
    ninja's ``@paginate`` and is the flow above with a preset: there is no
    ``load``, and the handler returns the authorized queryset (or a sequence,
    possibly in a ``ninja.Status``). The operation must declare a collection
    response, a list, set or tuple of the item schema, e.g.
    ``response=List[ItemOut]``; without one, and for one that is
    ``Optional`` or another union (the envelope is always sent) or names no
    item type (a bare ``list``), ninja raises ``ninja.errors.ConfigError``
    when the operation is built. It is replaced by a generated
    schema, ``Scrudful<Item>Page``, so ninja's validation and OpenAPI describe
    the envelope (:mod:`scrudful.collection` builds its JSON Schema and
    JSON-LD context)::

        {"count": 240, "page_count": 3,
         "first": ".../items?page=1", "previous": ".../items?page=1",
         "next": ".../items?page=3", "last": ".../items?page=3",
         "content": [{"href": ..., "etag": ..., "last_modified": ...,
                      "link": ..., "content": <the item, as ItemOut>}]}

    - The query parameters ``page`` (from 1) and ``page_size`` (``page_size``
      by default, at most ``max_page_size``) are added to the operation and
      not passed to the handler. Invalid values are answered with 422 by
      ninja, a page past the last one with 404 (page 1 always exists). A
      ``before`` callable runs before ninja validates any parameter (see
      :func:`scrudful_list`), e.g. to answer 401 or 403 first, and an
      ``after_throttles`` one too, but after ninja's throttles, e.g. for an
      expensive check DRF makes in ``list()``.
    - ``count`` is ``queryset.count()`` for a ``QuerySet``, else ``len()``;
      ``page_count`` is ``ceil(count / page_size)``. An unordered
      ``QuerySet`` (no ``order_by()`` and no ``Meta.ordering``) gets Django's
      ``UnorderedObjectListWarning``, as with Django's and DRF's paginators:
      its pages are not stable. Order it.
    - ``first``, ``previous``, ``next`` and ``last`` are the request's
      absolute URI with ``page`` replaced and the other query parameters kept
      (sorted by name, as DRF does). ``previous`` and ``next`` are ``null``
      at the ends; ``first`` and ``last`` are page 1 for an empty collection.
    - Each entry's ``href`` is ``href_func(request, item)``; its ``etag``,
      ``last_modified`` (an HTTP-date) and ``link`` come from the item
      callbacks and links, so they equal the ``ETag``, ``Last-Modified`` and
      ``Link`` of a detail GET given the same arguments. Each is ``null``
      without its callbacks.
    - The response's Links are the collection entity's (the ``list_*`` link
      arguments; their callables get the queryset). With ``list_etag_func``
      or ``list_last_modified_func``, which are called with the queryset, the
      preconditions are evaluated after the handler returns it, since it is
      the authorized object: a matching GET is answered with 304, a failed
      If-Match with 412, a malformed one with 400 (with ``error``'s Links),
      and 2xx responses carry the collection's validators. The collection
      exists even if they return ``None`` (``If-None-Match: *`` gives 304,
      ``If-Match: *`` passes). These early
      responses keep the cookies the handler set; the 304 also keeps its
      other headers (e.g. Cache-Control, Vary), except Content-Type,
      Content-Length, Content-Encoding, Content-Range and Content-Language
      (RFC 9110 §15.4.5), and the 400 and 412 keep no other header (so a
      ``public`` Cache-Control does not make them cacheable). Without the
      list validators, the If-* headers are ignored.

    Only a response ninja validates as the collection response is
    paginated: a ``Status`` with another code (e.g. ``Status(403, ...)``) or
    an ``HttpResponse`` is sent as the handler returned it. Each operation
    keeps its own: a function registered on several paths with different
    ``response=`` is paginated as each declares. In an async operation the
    queryset is counted and sliced through ``sync_to_async``, never on the
    event loop, so the handler may return it unevaluated; when ``href_func``,
    the item validator callbacks and the item entity's callables are all
    sync, the count, the slice and every entry's values take a single
    ``sync_to_async`` call per page.

OPTIONS
    A :class:`ScrudfulRouter` adds an OPTIONS operation to every path that has
    a scrudful operation; an OPTIONS operation the implementer declares on the
    path wins. It runs no ``load``, handler or validator callback, scrudful
    runs no permission check for it, and it ignores the API's and the
    router's ``auth``: it describes the path template, so its answer is the
    same for every client, and for a missing item as for a hidden one. It is
    not in the OpenAPI document. ninja still applies the decorators added with
    the API's or a router's ``add_decorator`` and the API's and routers'
    throttles to it, as to any operation: a permission decorator added that
    way gates OPTIONS too. Every automatic OPTIONS operation has the URL name
    ``scrudful_options``, so don't ``reverse()`` it.

    The body is the semantic-http 0.1 "HTTP Schema Resource" (an OpenAPI 3.1
    Path Item): for each method the path serves, in the order ``get``,
    ``put``, ``post``, ``delete``, ``options``, ``head``, ``patch``,
    ``trace``, an operation in the shape DRF's ``ScrudfulMetadata`` produces
    (``scrudful.rest_framework``), which it extends:

    - ``requestBody`` (POST, PUT, PATCH) with the ``content`` of the
      ``request`` entity, else of the item entity, as in DRF; left out when
      that entity has no schema link;
    - ``responses``: the statuses the operation answers, with their reason
      phrase: those it declares in ``response=`` (ninja's default, 200,
      without one), and 201 for a :func:`scrudful_create` operation, whose
      plain return value is a 201. So a search POST declaring
      ``response=List[...]`` lists 200 (DRF lists 201 for every POST), and
      a DELETE lists 204 only if it declares it. When the operation
      evaluates the If-* headers, the early responses are added: 304, 400
      and 412 for GET/HEAD; 400, 412 and 428 for writes, only 400 and 412
      for POST. Each status with a body gets the ``content`` of its entity:
      scrudful's own 400, 412 and 428 get ``error``'s, if declared, the
      others ``responses[status]``, else the default entity (2xx, 201 for a
      create, the collection for a list; never for TRACE, whose content is
      the echoed request, not the item);
    - ``parameters``: the precondition headers of a conditional write.

    ``head`` (when an operation routes it) is described like ``get``, but
    without ``content``: a HEAD response has none. ``options`` is this
    operation: a 200 whose content is the HTTP Schema Resource.

    A ``content`` maps the entity's ``media_type`` to its Media Type Object:
    ``{"schema": {"$ref": <schema URL>}}`` for a JSON Schema (the schema
    type ``application/schema+json``, the default), else ``{"x-schema":
    {"href": <schema URL>, "type": <schema type>}}``, and, unless the media
    type is in-band RDF, ``"x-rdf-context": {"href": <context URL>, "type":
    <context type>}`` when the entity has a context link. An entity without
    a schema link (such as ``Entity()``) has no ``content``. The URLs are
    resolved against the request's absolute URI, as those of the Links are,
    so the body agrees with the Links of the responses it describes. Link
    callables are called with the request and ``obj=None`` (there is no
    loaded object), so they must accept ``None``; the OPTIONS view is sync,
    so the callables of an async operation are run with
    ``asgiref.sync.async_to_sync``, as the operation runs them (ninja runs
    the view in a thread on a path with async operations). Methods whose
    operation is not scrudful get no content, no ``requestBody`` and no
    preconditions, as in DRF.

    The response is ``application/json``, with the Links of the HTTP Schema
    Resource's JSON Schema
    (``https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json``,
    ``rel="describedby"``) and JSON-LD context
    (``https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld``),
    and an ``Allow`` listing the body's methods in its order.

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
      with the computed ones). With ``etag_func`` or ``last_modified_func``
      set, keep them consistent with what those return: the preconditions of
      the next request use the computed values, so a client echoing a
      different value gets 412;
    - ``entity``: its Links, in place of the declared entity's.

Headers
    The headers go on the ``HttpResponse`` the handler returns, or else on
    ninja's temporal response (passed to a handler parameter annotated
    ``HttpResponse``, which the wrapper adds, hidden, when the handler has
    none). Headers the handler set itself are never overwritten, neither by
    computed values nor by those set on ``ScrudfulResponse``. The Links are
    appended to any Link value the handler set, except that a handler's link
    with the relation of the schema link (``describedby`` by default)
    replaces that link, and one with either context relation (the JSON-LD
    or the rdf-context one, a single slot) replaces the context link, when
    it has one of those; an overridden relation is matched exactly.
    Relations are compared case-insensitively; a link with another relation
    (e.g. ``next``) replaces nothing.
    The Link URLs are resolved against the request's absolute URI
    (``request.build_absolute_uri()``), so a relative one such as
    ``"/schemas/item"`` is sent absolute.
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
    awaited. They are called one after the other, never concurrently (a list
    page's sync per-item callables run together in one ``sync_to_async``
    call, see "Collections"). With a
    sync handler, the operation is sync and every callable is called directly,
    so they must all be sync: an ``async def`` one raises ``TypeError`` at
    decoration time (also inside the declared entities), or at request time
    for a ``ScrudfulResponse.entity``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import inspect
import io
import json
import re
import textwrap
import tokenize
import types
from collections.abc import Callable, Generator, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import (
    Any,
    NamedTuple,
    NoReturn,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from asgiref.sync import async_to_sync, sync_to_async
from django.core.serializers.json import DjangoJSONEncoder
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
)
from django.utils.encoding import iri_to_uri
from ninja import NinjaAPI, Query, Router, Schema, Status
from ninja import errors as ninja_errors
from ninja.constants import NOT_SET
from ninja.errors import ConfigError, HttpError
from ninja.operation import AsyncOperation, Operation
from ninja.renderers import JSONRenderer
from ninja.responses import NinjaJSONEncoder
from ninja.signature.details import is_collection_type
from ninja.signature.utils import get_path_param_names
from pydantic import Field, create_model

from scrudful import _adapter
from scrudful._adapter import (
    BODY_HEADERS,
    METHOD_KEYS,
    SCHEMA_REL,
    SCHEMA_TYPE,
    allow_header,
    check_error_body_func,
    check_error_media_type,
    check_validators,
    context_defaults,
    context_link_type,
    describe_content,
    describe_options,
    describe_read,
    describe_write,
    evaluates_preconditions,
    expose_headers,
    has_default_entity,
    is_json,
    is_json_syntax,
    merge_links,
    options_links,
    resolve_annotation,
    resolved_signature,
    set_validator_headers,
    validator_policy,
    with_charset,
    with_error_body,
)
from scrudful._django import (
    Render,
    aware,
    conditional_response,
    json_response,
    page_url,
    precondition_headers,
    slice_page,
)
from scrudful.collection import (
    ENVELOPE_FIELDS,
    PAGE_FIELDS,
    InvalidPage,
    envelope_entry,
    page_envelope,
)
from scrudful.conditional import (
    ConditionalResult,
    evaluate_preconditions,
    format_http_date,
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
    "ScrudfulResponse",
    "ScrudfulRouter",
    "add_error_links",
    "scrudful",
    "scrudful_create",
    "scrudful_list",
]

#: OpenAPI descriptions of the responses ``@scrudful`` can produce for GET/HEAD
#: without calling the handler. Pass it to the operation decorator, e.g.
#: ``openapi_extra={"responses": CONDITIONAL_READ_RESPONSES}``, or merge it with
#: your own. The keys are ints, as ninja keys an operation's responses, so a
#: status the operation also declares in ``response=`` (e.g. a 412 schema) is
#: merged into one entry; the JSON schema output has string keys.
CONDITIONAL_READ_RESPONSES: dict[int, dict[str, str]] = (
    _adapter.CONDITIONAL_READ_RESPONSES
)

#: OpenAPI descriptions of the responses ``@scrudful`` can produce for
#: PUT/PATCH/DELETE without calling the handler, e.g.
#: ``openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES}``.
CONDITIONAL_WRITE_RESPONSES: dict[int, dict[str, str]] = (
    _adapter.CONDITIONAL_WRITE_RESPONSES
)

Handler = TypeVar("Handler", bound=Callable[..., Any])
T = TypeVar("T")

#: The request flow, written once for sync and async operations: a generator
#: that yields each call of a user callable, as ``(func, args, kwargs)``, and
#: is sent its result. :func:`_run_sync` and :func:`_run_async` make the calls.
_Flow = Generator[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]], Any, T]

#: The argument a list operation's query parameters ``page`` and ``page_size``
#: are passed in (pydantic rejects names with a leading underscore).
_PAGINATION = "scrudful_pagination"


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
        method and status. When the operation also has ``etag_func``, keep
        the two consistent: preconditions are always evaluated with the
        computed value, so a client sending this one back in If-Match would
        get 412.
    last_modified : datetime or float
        The modification time of this response, in place of the computed one
        (see ``last_modified_func``). Sent for any method and status; keep it
        consistent with ``last_modified_func``, as for ``etag``.
    entity : Entity or None
        The entity whose Links this response gets, in place of the declared
        one.
    created : object
        The object a :func:`scrudful_create` operation created, whose
        Location, validators and item Links a 201 gets. Unset by default: the
        handler's return value is used (the value of a returned ``Status``;
        a returned ``HttpResponse`` is none). ``None`` (also a ``None``
        return value) is no created object: the 201 gets no Location and no
        validators, and the item Links only if it has a body and none of the
        item entity's values is callable. Ignored by :func:`scrudful`.
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

    The six ``schema_*`` / ``context_*`` fields and ``media_type`` are the
    item entity, also available as :attr:`entity`: the default entity of 2xx
    responses but 204 for an item operation, of 201 responses (the created
    item) for a create operation, and the ``link`` of each entry of a list
    operation's envelope, whose responses get :attr:`collection`.
    """

    #: ``"item"`` (:func:`scrudful`), ``"create"`` (:func:`scrudful_create`)
    #: or ``"list"`` (:func:`scrudful_list`).
    kind: str
    load: Callable[..., Any] | None = None
    #: ``location_func(request, obj)`` of a create operation.
    location_func: Callable[[Any, Any], Any] | None = None
    #: ``href_func(request, item)`` of a list operation.
    href_func: Callable[[Any, Any], Any] | None = None
    load_as: str = "obj"
    etag_func: Callable[[Any], Any] | None = None
    last_modified_func: Callable[[Any], Any] | None = None
    schema_link_or_func: Any = None
    schema_rel_or_func: Any = None
    schema_type_or_func: Any = None
    context_link_or_func: Any = None
    context_rel_or_func: Any = None
    context_type_or_func: Any = None
    media_type: str = "application/json"
    #: The request body's entity (described by OPTIONS; no header).
    request: Entity | None = None
    #: The response entity of each status, overriding :attr:`entity`.
    responses: Mapping[int, Entity] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: The entity of scrudful's own 400, 412 and 428 bodies.
    error: Entity | None = None
    #: ``error_body_func(request, result)``: the body of those responses.
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None = None
    #: The validators of a list operation's collection (called with the
    #: queryset the handler returned).
    list_etag_func: Callable[[Any], Any] | None = None
    list_last_modified_func: Callable[[Any], Any] | None = None
    #: The collection entity of a list operation: its 2xx responses' Links
    #: (but a 204's).
    collection: Entity | None = None
    #: A list operation's default and maximum ``page_size``.
    page_size: int | None = None
    max_page_size: int | None = None
    #: ``before(request, **path_params)``: run after authentication, before
    #: throttling and before any parameter is validated (see
    #: :func:`_install_hooks`).
    before: Callable[..., Any] | None = None
    #: ``after_throttles(request, **path_params)``: run after
    #: authentication, ``before`` and throttling, before any parameter is
    #: validated (see :func:`_install_hooks`).
    after_throttles: Callable[..., Any] | None = None

    @property
    def decorator_name(self) -> str:
        """The public decorator declaring an operation of this kind."""
        return {"item": "@scrudful", "create": "@scrudful_create"}.get(
            self.kind, "@scrudful_list"
        )

    @property
    def hooks(self) -> str | None:
        """The hooks run in scrudful's copy of ninja's checks, as errors name
        them (``"before="``, ``"after_throttles="`` or both); ``None``
        without either."""
        names = [
            f"{name}="
            for name in ("before", "after_throttles")
            if getattr(self, name) is not None
        ]
        return " and ".join(names) or None

    @property
    def entity(self) -> Entity:
        """The item entity (see the class docstring)."""
        return Entity(
            **{
                entity_field.name: getattr(self, entity_field.name)
                for entity_field in fields(Entity)
            }
        )

    @property
    def default_status(self) -> int | None:
        """The status of a plain return value, unless the handler sets one."""
        return 201 if self.kind == "create" else None

    @property
    def evaluates_preconditions(self) -> bool:
        """Whether the If-* headers are evaluated before the handler runs."""
        return self.kind == "item" and self.has_validators

    def has_default_entity(self, method: str, status: int) -> bool:
        """Whether a ``method`` response with ``status`` gets the default entity.

        Not a 204, which has no body for the entity to describe, nor a
        TRACE, whose content is the echoed request (see
        :func:`scrudful._adapter.has_default_entity`).
        """
        if self.kind == "create":
            return status == 201
        return has_default_entity(method, status)

    @property
    def default_entity(self) -> Entity | None:
        """The entity of the responses :meth:`has_default_entity` selects."""
        return self.collection if self.kind == "list" else self.entity

    def response_entity(self, method: str, status: int) -> Entity | None:
        """The declared entity of a ``method`` response with ``status``."""
        if status in self.responses:
            return self.responses[status]
        if self.has_default_entity(method, status):
            return self.default_entity
        return None

    @property
    def has_etag(self) -> bool:
        return self.etag_func is not None

    @property
    def has_last_modified(self) -> bool:
        return self.last_modified_func is not None

    @property
    def has_validators(self) -> bool:
        return self.has_etag or self.has_last_modified

    @property
    def has_list_etag(self) -> bool:
        return self.list_etag_func is not None

    @property
    def has_list_last_modified(self) -> bool:
        return self.list_last_modified_func is not None

    @property
    def has_list_validators(self) -> bool:
        return self.has_list_etag or self.has_list_last_modified


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
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None = None,
    before: Callable[..., Any] | None = None,
    after_throttles: Callable[..., Any] | None = None,
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

    Placed above it, ninja would have registered the undecorated function,
    so the decorator raises ``TypeError``. The handler may be ``async def``,
    and then the callables may be too (see "Async operations" in the module
    docstring).
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
        Required when a validator callback is set. After a 2xx PUT/PATCH it
        is called again, for the validators and Links of what was just
        written, when a validator callback is set or a value of the
        response's entity is callable: if it raises then, the client gets
        that error although the handler's write is committed.
    load_as : str, optional
        Name of the handler parameter receiving the loaded object (default
        ``"obj"``). The parameter is hidden from ninja's signature and
        OpenAPI; the handler must declare it when ``load`` is set.
    etag_func : callable, optional
        ``etag_func(obj)`` returns the entity-tag of the loaded object, or
        ``None`` if there is none: the object ``load`` returned still exists
        (see "Choosing what ``load`` returns" in the module docstring).
        ``str()`` of the result is used: a valid entity-tag (``'"abc"'`` or
        ``'W/"abc"'``) is sent unchanged, anything else is wrapped in double
        quotes (a strong entity-tag). Values that cannot be an entity-tag
        (containing a double quote, space, control character or a character
        above U+00FF) raise ``ValueError``. Weak entity-tags are allowed, but
        If-Match uses the strong comparison, which they never pass: only
        If-Unmodified-Since (or ``If-Match: *``) can then make a write
        succeed.
    last_modified_func : callable, optional
        ``last_modified_func(obj)`` returns the modification time of the
        loaded object as a ``datetime`` or an epoch timestamp, or ``None`` if
        there is none: the object still exists, as for ``etag_func``. Naive
        datetimes are taken to be in the default time zone
        (``settings.TIME_ZONE``), as Django does. Sub-second precision is
        discarded.
    schema_link_or_func : str or callable, optional
        URL of the schema of the 2xx response body: this argument and the
        five after it are the default response entity (see ``responses``),
        not sent on a 204, which has no body.
        If no value is given, no schema link is sent. Each ``*_or_func``
        argument is a value or a callable ``func(request, obj)``, where
        ``obj`` is the loaded object, or ``None`` without ``load``. After a
        2xx PUT/PATCH, ``obj`` is the object ``load`` returns when called
        again, which it is when a validator callback is set or a value of the
        response's entity is callable.
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
    request : Entity, optional
        The schema and context of the request body. Described by OPTIONS; it
        puts no header on the response.
    responses : mapping of int to Entity, optional
        The response entity of each status, in place of the default one (the
        six arguments above) for that status, e.g. ``{404: Entity(...)}`` for
        a ``Status(404, ...)`` the handler returns or an ``HttpError(404)``
        it raises.
    error : Entity, optional
        The schema and context of scrudful's own 400, 412 and 428 bodies, sent
        as Links on them, and of the response to an ``HttpError`` or
        ``Http404`` the operation raises, unless ``responses`` declares its
        status (see "Error responses" in the module docstring; link
        callables then get ``None``). Without it, they carry no Link. Its
        ``media_type`` must be JSON content (``application/json`` or
        ``+json``, e.g. ``application/problem+json``), since scrudful's error
        bodies are JSON objects; they, and the JSON responses to the errors
        the operation raises, are served with that Content-Type.
    error_body_func : callable, optional
        ``error_body_func(request, result)`` returns the body (a ``dict``) of
        scrudful's own 400, 412 or 428 response in place of
        ``{"detail": ...}``; ``result`` is the
        :class:`~scrudful.conditional.ConditionalResult`, whose default body
        is ``result.body``. It must be a plain (sync) function. The status,
        headers, Links and Content-Type are unchanged, and it is never called
        for a 304. :func:`scrudful.problem.problem_details` returns RFC 9457
        problem details (pair it with
        ``error=Entity(media_type=PROBLEM_JSON)``). Without it the bodies are
        as before.
    before : callable, optional
        ``before(request, **path_params)``, called once per request where DRF
        checks permissions (``has_permission``): after ninja's
        authentication, before its throttles and before it validates any
        parameter or the body, so before ``load`` and the preconditions: a
        client it refuses never gets a 304, 412 or 428, or a validator. As
        for :func:`scrudful_list`: the path parameters are the URL's raw
        strings, it returns ``None`` to let the request go on (anything else,
        or an awaitable in a sync operation, raises ``TypeError``) and raises
        to answer first (the API's exception handler answers, with the
        operation's error Links, and the request uses none of a throttle's
        quota); neither its error nor a later 422 passes through the
        ``add_decorator(mode="operation")`` decorators. It may read
        ``request.body``.
    after_throttles : callable, optional
        ``after_throttles(request, **path_params)``, the same, called after
        ``before`` and ninja's throttles: a throttled request (429) never
        calls it, and one it refuses has used its quota, as in DRF. With
        either hook, scrudful runs its own copy of ninja's per-operation
        checks, with the guards described for :func:`scrudful_list`
        (``ConfigError`` when the operation is built, ``RuntimeError`` for a
        request reaching the function without the hooks having allowed it).

    Raises
    ------
    TypeError
        If a validator callback is set without ``load``, if ``load``, a hook
        or a validator callback isn't callable, if ``request``, ``error`` or a value
        of ``responses`` isn't an :class:`Entity` or a key of ``responses``
        isn't an HTTP status code, if ``load`` is set and the handler has no
        ``load_as`` parameter, if the handler is a generator or already a
        ninja operation (the decorator sits above the operation decorator),
        or if the handler is sync and ``load``, a hook, a validator callback
        or a link callable (also inside an entity) is ``async def``.
    ValueError
        If ``error``'s media type is not JSON content.
    ninja.errors.ConfigError
        When the operation is built, if something replaced its ``view_func``
        before the decorator bound it: a ``_ninja_contribute_to_operation``
        callback contributed below the decorator, or a decorator between it
        and the operation decorator (see "Placement" in the module
        docstring). Also with a hook, as for :func:`scrudful_list`.
    """
    check_validators(etag_func, last_modified_func)
    if load is not None and not callable(load):
        raise TypeError(f"load must be callable, not {type(load).__name__}")
    _check_entity("request", request)
    _check_error(error)
    check_error_body_func(error_body_func)
    _check_hooks(before, after_throttles)
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
        error_body_func=error_body_func,
        before=before,
        after_throttles=after_throttles,
    )
    if config.has_validators and load is None:
        raise TypeError(
            "etag_func and last_modified_func require load: the validators are "
            "computed from the object load returns"
        )

    return _decorator(config, "@scrudful")


def scrudful_create(
    *,
    location_func: Callable[[Any, Any], Any],
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
    before: Callable[..., Any] | None = None,
    after_throttles: Callable[..., Any] | None = None,
) -> Callable[[Handler], Handler]:
    """Decorate a django-ninja operation creating an item (POST on a collection).

    ``@scrudful_create(...)`` must sit *below* the operation decorator, which
    must declare a 201 response::

        @router.post("/items", response={201: ItemOut})
        @scrudful_create(
            location_func=lambda request, item: f"/items/{item.pk}",
            etag_func=lambda item: item.version,
            last_modified_func=lambda item: item.modified_at,
            schema_link_or_func="/schemas/item",
            context_link_or_func="/contexts/item",
        )
        def create_item(request, payload: ItemIn):
            return Item.objects.create(**payload.dict())

    It is :func:`scrudful` with a preset: no ``load`` and no precondition
    evaluation, a default status of 201, and on a 201 the ``Location``, the
    Links and the validators of the created item (see "Create" in the module
    docstring). Pass the same validator callbacks and link arguments as the
    item operations, so the 201 carries the validators a detail GET sends.
    The handler may be ``async def``, and then the callables may be too.

    Parameters
    ----------
    location_func : callable
        ``location_func(request, obj)`` returns the URL of the created object
        ``obj``, as an IRI (encoded as a URI), or ``None`` for no Location.
        Not called without a created object (a handler returning ``None`` or
        an ``HttpResponse`` sets ``ScrudfulResponse.created`` or
        ``ScrudfulResponse.location``). Such a 201 gets the item entity's
        Links only if it has a body and none of the six link values is
        callable (a callable would get ``None``).
    etag_func : callable, optional
        ``etag_func(obj)``: the entity-tag of the created object, as for
        :func:`scrudful` (no ``load`` is needed: the validators are computed
        from the created object).
    last_modified_func : callable, optional
        ``last_modified_func(obj)``: the modification time of the created
        object, as for :func:`scrudful`.
    schema_link_or_func : str or callable, optional
        URL of the item's schema, sent on 201 responses: this argument and the
        five after it are the item entity, with the meaning they have for
        :func:`scrudful`. Link callables get the created object; without
        one they are not called (see ``location_func``).
    schema_rel_or_func : str or callable, optional
        ``rel`` of the schema link. Defaults to ``"describedby"``.
    schema_type_or_func : str or callable, optional
        ``type`` of the schema link. Defaults to ``"application/schema+json"``.
    context_link_or_func : str or callable, optional
        URL of the item's JSON-LD context, sent on 201 responses.
    context_rel_or_func : str or callable, optional
        ``rel`` of the context link. Defaults to
        ``"http://www.w3.org/ns/json-ld#context"``.
    context_type_or_func : str or callable, optional
        ``type`` of the context link. Defaults to ``"application/ld+json"``.
    request : Entity, optional
        The schema and context of the request body, described by OPTIONS.
        Defaults to the item entity (the body is an item, as for DRF's
        create).
    responses : mapping of int to Entity, optional
        The response entity of each status, e.g. ``{400: Entity(...)}`` for a
        ``Status(400, ...)`` the handler returns, or an ``HttpError(400)`` it
        raises (see "Error responses" in the module docstring).
        ``responses[201]`` replaces the item entity's Links on 201 responses.
        There is no ``error`` argument: a create produces no 400, 412 or 428
        of its own.
    before : callable, optional
        ``before(request, **path_params)``, called once per request where DRF
        checks permissions (``has_permission``): after ninja's
        authentication, before its throttles and before it validates any
        parameter or the body. As for :func:`scrudful_list`: the path
        parameters are the URL's raw strings, it returns ``None`` to let the
        request go on (anything else, or an awaitable in a sync operation,
        raises ``TypeError``) and raises to answer first (the API's exception
        handler answers, with the operation's error Links, and the request
        uses none of a throttle's quota); neither its error nor a later 422
        passes through the ``add_decorator(mode="operation")`` decorators. It
        may read ``request.body``.
    after_throttles : callable, optional
        ``after_throttles(request, **path_params)``, the same, called after
        ``before`` and ninja's throttles: a throttled request (429) never
        calls it, and one it refuses has used its quota, as in DRF. With
        either hook, scrudful runs its own copy of ninja's per-operation
        checks, with the guards described for :func:`scrudful_list`
        (``ConfigError`` when the operation is built, ``RuntimeError`` for a
        request reaching the function without the hooks having allowed it).

    Raises
    ------
    TypeError
        If ``location_func``, a hook or a validator callback isn't callable, if
        ``request`` or a value of ``responses`` isn't an :class:`Entity` or a
        key of ``responses`` isn't an HTTP status code, if the handler is a
        generator or already a ninja operation (the decorator sits above the
        operation decorator), or if the handler is sync and
        ``location_func``, a hook, a validator callback or a link callable
        (also inside an entity) is ``async def``.
    ninja.errors.ConfigError
        When the operation is built, if something replaced its ``view_func``
        before the decorator bound it: a ``_ninja_contribute_to_operation``
        callback contributed below the decorator, or a decorator between it
        and the operation decorator (see "Placement" in the module
        docstring). Also with a hook, as for :func:`scrudful_list`.
    """
    if not callable(location_func):
        raise TypeError(
            f"location_func must be callable, not {type(location_func).__name__}"
        )
    check_validators(etag_func, last_modified_func)
    _check_entity("request", request)
    _check_hooks(before, after_throttles)
    item = Entity(
        schema_link_or_func=schema_link_or_func,
        schema_rel_or_func=schema_rel_or_func,
        schema_type_or_func=schema_type_or_func,
        context_link_or_func=context_link_or_func,
        context_rel_or_func=context_rel_or_func,
        context_type_or_func=context_type_or_func,
    )
    config = _Config(
        kind="create",
        location_func=location_func,
        etag_func=etag_func,
        last_modified_func=last_modified_func,
        **{
            entity_field.name: getattr(item, entity_field.name)
            for entity_field in fields(Entity)
        },
        request=item if request is None else request,
        responses=_entities_by_status(responses),
        before=before,
        after_throttles=after_throttles,
    )
    return _decorator(config, "@scrudful_create")


def _check_hooks(before: Any, after_throttles: Any) -> None:
    """Raise ``TypeError`` unless each hook given is callable."""
    for name, func in (("before", before), ("after_throttles", after_throttles)):
        if func is not None and not callable(func):
            raise TypeError(f"{name} must be callable, not {type(func).__name__}")


def scrudful_list(
    *,
    href_func: Callable[[Any, Any], Any],
    etag_func: Callable[[Any], Any] | None = None,
    last_modified_func: Callable[[Any], Any] | None = None,
    schema_link_or_func: Any = None,
    schema_rel_or_func: Any = None,
    schema_type_or_func: Any = None,
    context_link_or_func: Any = None,
    context_rel_or_func: Any = None,
    context_type_or_func: Any = None,
    list_etag_func: Callable[[Any], Any] | None = None,
    list_last_modified_func: Callable[[Any], Any] | None = None,
    list_schema_link_or_func: Any = None,
    list_schema_rel_or_func: Any = None,
    list_schema_type_or_func: Any = None,
    list_context_link_or_func: Any = None,
    list_context_rel_or_func: Any = None,
    list_context_type_or_func: Any = None,
    page_size: int = 100,
    max_page_size: int = 1000,
    error: Entity | None = None,
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None = None,
    before: Callable[..., Any] | None = None,
    after_throttles: Callable[..., Any] | None = None,
) -> Callable[[Handler], Handler]:
    """Decorate a django-ninja operation listing a collection (GET on a list).

    ``@scrudful_list(...)`` must sit *below* the operation decorator, which
    must declare a collection response (as for ninja's ``@paginate``)::

        # auth=: request.auth exists only when an auth callback succeeded.
        @router.get("/items", response=List[ItemOut], auth=...)
        @scrudful_list(
            href_func=lambda request, item: request.build_absolute_uri(
                f"/items/{item.pk}"
            ),
            etag_func=lambda item: item.version,
            last_modified_func=lambda item: item.modified_at,
            schema_link_or_func="/schemas/item",
            context_link_or_func="/contexts/item",
            list_schema_link_or_func="/schemas/items",
            list_context_link_or_func="/contexts/items",
        )
        def list_items(request):
            return Item.objects.filter(owner=request.auth)

    The handler returns the authorized queryset (or a sequence); the wrapper
    paginates it and returns the scrud envelope, whose response schema
    replaces ``List[ItemOut]`` (see "Collections" in the module docstring).
    Pass the validator callbacks and link arguments of the item operations,
    so each entry carries the validators and Link a detail GET sends. The
    handler may be ``async def``, and then the callables may be too.

    A 2-tuple the handler returns is ninja's deprecated ``(status, body)``,
    not a sequence of two items: return a list (or wrap it in ``Status``).
    Tuples of any other length are sequences.

    Parameters
    ----------
    href_func : callable
        ``href_func(request, item)`` returns the URL of ``item``, an entry's
        ``href``, as an IRI (encoded as a URI). An absolute URL is
        recommended (the collection schema says ``"format": "uri"``).
        Returning ``None`` raises ``TypeError``.
    etag_func : callable, optional
        ``etag_func(item)``: the entity-tag of an item, as for
        :func:`scrudful`: an entry's ``etag``, else ``null``.
    last_modified_func : callable, optional
        ``last_modified_func(item)``: the modification time of an item, as for
        :func:`scrudful`: an entry's ``last_modified`` (an HTTP-date), else
        ``null``.
    schema_link_or_func : str or callable, optional
        URL of the item's schema: this argument and the five after it are the
        item entity, with the meaning they have for :func:`scrudful`. Their
        Link value is an entry's ``link`` (``null`` without links); link
        callables get the item.
    schema_rel_or_func : str or callable, optional
        ``rel`` of the item schema link. Defaults to ``"describedby"``.
    schema_type_or_func : str or callable, optional
        ``type`` of the item schema link. Defaults to ``"application/schema+json"``.
    context_link_or_func : str or callable, optional
        URL of the item's JSON-LD context.
    context_rel_or_func : str or callable, optional
        ``rel`` of the item context link. Defaults to
        ``"http://www.w3.org/ns/json-ld#context"``.
    context_type_or_func : str or callable, optional
        ``type`` of the item context link. Defaults to
        ``"application/ld+json"``.
    list_etag_func : callable, optional
        ``list_etag_func(queryset)``: the entity-tag of the collection, called
        with what the handler returned (the whole queryset, not the page). With
        either list validator, the preconditions are evaluated after the
        handler returns (304, 400, 412) and 2xx responses carry the
        validators.
    list_last_modified_func : callable, optional
        ``list_last_modified_func(queryset)``: the modification time of the
        collection.
    list_schema_link_or_func : str or callable, optional
        URL of the collection's schema (e.g. one built with
        :func:`scrudful.collection.collection_schema`): this argument and the
        five after it are the collection entity, whose Links 2xx responses
        (but 204) get. Link callables get the queryset.
    list_schema_rel_or_func : str or callable, optional
        ``rel`` of the collection schema link.
    list_schema_type_or_func : str or callable, optional
        ``type`` of the collection schema link.
    list_context_link_or_func : str or callable, optional
        URL of the collection's JSON-LD context (e.g. one built with
        :func:`scrudful.collection.collection_context`).
    list_context_rel_or_func : str or callable, optional
        ``rel`` of the collection context link.
    list_context_type_or_func : str or callable, optional
        ``type`` of the collection context link.
    page_size : int, optional
        The default of the ``page_size`` query parameter (100).
    max_page_size : int, optional
        The largest ``page_size`` a client may ask for (1000); a larger one
        is answered with 422.
    error : Entity, optional
        The schema and context of scrudful's own 400 and 412 bodies (with a
        list validator set), sent as Links on them; link callables get the
        queryset. Also those of the response to an ``HttpError`` or
        ``Http404`` the operation raises, e.g. the 404 of a page past the
        last one; link callables then get ``None``. Its ``media_type`` must
        be JSON content, as for :func:`scrudful`; scrudful's 400 and 412,
        and the JSON responses to the errors the operation raises, are
        served with it.
    error_body_func : callable, optional
        ``error_body_func(request, result)`` returns the body (a ``dict``) of
        scrudful's own 400 or 412 response (with a list validator set; a list
        never answers 428) in place of ``{"detail": ...}``; ``result`` is the
        :class:`~scrudful.conditional.ConditionalResult`, whose default body
        is ``result.body``. It must be a plain (sync) function. The status,
        headers, Links and Content-Type are unchanged, and it is never called
        for a 304. :func:`scrudful.problem.problem_details` returns RFC 9457
        problem details (pair it with
        ``error=Entity(media_type=PROBLEM_JSON)``). Without it the bodies are
        as before.
    before : callable, optional
        ``before(request, **path_params)``, called once per request where DRF
        checks permissions: after ninja's authentication, before its
        throttles and before it validates any parameter of the operation
        (path, query or body) or calls its function. ``path_params`` are the
        URL's values, not validated yet (strings, unless the path uses a
        converter such as ``{int:pk}``): ``"07"``, ``"+7"`` and ``" 7"`` are
        all 7 to an ``int`` parameter. Never deny by testing a raw string for
        a value: compare the value the function gets (declare the parameter
        ``str``), or parse it as its type does and refuse what doesn't
        parse. ``request.auth`` is set only when an
        auth callback succeeded: on an operation that may run without
        ``auth``, fail closed (``user = getattr(request, "auth", None)``, and
        ``raise HttpError(401, ...)`` when it is ``None``) before using it.
        It returns ``None`` to let the request go on; anything else it
        returns (``False``, a response) raises ``TypeError``, as a refusal is
        raised, never returned. Raise ninja's ``HttpError`` (or another
        exception the API handles) to answer first, e.g. with 401 or 403 whatever the
        parameters are; the API's exception handler answers it, as ninja
        answers its own errors, an ``HttpError`` gets the operation's error
        Links as when the operation raises it, and the request uses none of
        a throttle's quota. When it returns, ninja throttles and validates
        every parameter as without ``before``: one 422 lists every bad one,
        ``page`` and ``page_size`` included, and the OpenAPI description is
        the same. Neither its error nor that 422 passes through the
        decorators added with ``add_decorator(mode="operation")``. It may be
        ``async def`` in an ``async def`` operation; a sync one in an async
        operation runs in a thread. In a sync operation, returning an
        awaitable raises ``TypeError``, and the request does not go on (a
        coroutine is closed and a future cancelled: never awaited). scrudful
        runs ``before`` in its own copy of ninja's per-operation checks,
        verified against ninja 1.7.0 and 1.7.1, though the ``ninja`` extra
        allows any ninja from 1.7 below 2: on a ninja whose checks, or
        whose ``Operation.run`` calling them, differ (compared by a digest
        of their source,
        comments, docstrings and blank lines aside, or, without installed
        source, only by the names, locals and arguments they use),
        ``before=`` raises ``ConfigError`` when the operation is built (at
        startup); upgrade scrudful then, or keep ninja at a verified release.
    after_throttles : callable, optional
        ``after_throttles(request, **path_params)``, called once per request
        where DRF's ``list()`` starts (e.g. its filters): after ninja's
        authentication, after ``before`` (if given) and after ninja's
        throttles, and before ninja validates any parameter of the operation
        or calls its function. Its contract is ``before``'s (above): the
        same raw path parameters, ``None`` to let the request go on (anything
        else, or an awaitable in a sync operation, raises ``TypeError``), an
        error raised to answer first (the API's exception handler answers
        it, with the operation's error Links), neither that error nor the 422
        that follows passes through the ``add_decorator(mode="operation")``
        decorators, ``async def`` in an ``async def`` operation, and the same
        ``ConfigError`` and ``RuntimeError`` guards. A throttled request (429)
        never calls it, and a request it refuses has used its throttle quota,
        as in DRF. So an expensive check (e.g. a filter that may answer 400)
        can't be forced by a throttled client, and its error beats the 422 of
        a bad ``page``. Use it alone or with ``before``.

    Raises
    ------
    TypeError
        If ``href_func``, ``before``, ``after_throttles`` or a validator
        callback isn't callable, if ``page_size`` or ``max_page_size`` isn't
        an ``int``, if ``error`` isn't an :class:`Entity`, if the handler is a
        generator or already a ninja operation (the decorator sits above the
        operation decorator), or if the handler is sync and ``href_func``, a
        validator callback or a link callable (also inside ``error``),
        ``before`` or ``after_throttles`` is ``async def``.
    ValueError
        If ``page_size`` is less than 1 or ``max_page_size`` less than
        ``page_size``, or if ``error``'s media type is not JSON content.
    ninja.errors.ConfigError
        When the operation is built, if it declares no collection response
        (e.g. ``response=List[ItemOut]``), or one that is a union (e.g.
        ``Optional[List[ItemOut]]``: the envelope is never ``None``) or names
        no item type (a bare ``list``), or if something replaced its
        ``view_func`` before the decorator bound it (see "Placement" in the
        module docstring). Also with ``before`` or ``after_throttles``, if a
        ``decorate_view`` decorator below the decorator wraps
        ``Operation.run`` without ``functools.wraps`` (scrudful then can't
        install them), if the
        installed ninja's per-operation checks, or the ``Operation.run``
        calling them, differ from ninja 1.7.0's and 1.7.1's, or if an
        ``Operation`` subclass overrides either.
    """
    if not callable(href_func):
        raise TypeError(f"href_func must be callable, not {type(href_func).__name__}")
    check_validators(etag_func, last_modified_func)
    for name, func in (
        ("list_etag_func", list_etag_func),
        ("list_last_modified_func", list_last_modified_func),
    ):
        if func is not None and not callable(func):
            raise TypeError(f"{name} must be callable, not {type(func).__name__}")
    for name, value in (("page_size", page_size), ("max_page_size", max_page_size)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    if page_size < 1:
        raise ValueError(f"page_size must be at least 1, not {page_size}")
    if max_page_size < page_size:
        raise ValueError(
            f"max_page_size ({max_page_size}) must be at least page_size ({page_size})"
        )
    _check_error(error)
    check_error_body_func(error_body_func)
    _check_hooks(before, after_throttles)
    config = _Config(
        kind="list",
        href_func=href_func,
        etag_func=etag_func,
        last_modified_func=last_modified_func,
        schema_link_or_func=schema_link_or_func,
        schema_rel_or_func=schema_rel_or_func,
        schema_type_or_func=schema_type_or_func,
        context_link_or_func=context_link_or_func,
        context_rel_or_func=context_rel_or_func,
        context_type_or_func=context_type_or_func,
        error=error,
        error_body_func=error_body_func,
        list_etag_func=list_etag_func,
        list_last_modified_func=list_last_modified_func,
        collection=Entity(
            schema_link_or_func=list_schema_link_or_func,
            schema_rel_or_func=list_schema_rel_or_func,
            schema_type_or_func=list_schema_type_or_func,
            context_link_or_func=list_context_link_or_func,
            context_rel_or_func=list_context_rel_or_func,
            context_type_or_func=list_context_type_or_func,
        ),
        page_size=page_size,
        max_page_size=max_page_size,
        before=before,
        after_throttles=after_throttles,
    )
    return _decorator(config, "@scrudful_list")


class ScrudfulRouter(Router):
    """A ninja ``Router`` answering OPTIONS on every path with a scrudful operation.

    The first scrudful operation added to a path (one decorated with
    :func:`scrudful`, :func:`scrudful_create` or :func:`scrudful_list`) also
    adds an OPTIONS operation to it, unless the path already has one. It is
    registered with ``auth=None`` and ``include_in_schema=False``, so it needs
    no credentials whatever the API's or router's ``auth`` and is not in the
    OpenAPI document, and it answers with the path's HTTP Schema Resource
    (see "OPTIONS" in the module docstring). An operation the implementer
    adds later whose methods include ``"OPTIONS"`` replaces it. The API's
    and routers' ``add_decorator`` decorators and throttles apply to it as to
    any operation. Its URL name is ``scrudful_options`` on every path. The
    link callables of the operations it describes are called with
    ``obj=None`` (there is no loaded object), so they must accept it.

    Use it for the routers holding scrudful operations, nested or not, and
    for the API's own operations (``default_router``); scrudful operations
    on a plain ``ninja.Router`` get no OPTIONS (ninja answers 405)::

        api = NinjaAPI(auth=..., default_router=ScrudfulRouter())
        items = ScrudfulRouter()
        api.add_router("/items", items)

    The OPTIONS response is the semantic-http 0.1 HTTP Schema Resource, with
    the Links of its published JSON Schema and JSON-LD context and an
    ``Allow`` listing the methods of the body.

    CORS preflight requests (an OPTIONS with ``Access-Control-Request-Method``)
    are sent by browsers without credentials and are meant to be answered by
    the CORS middleware (e.g. django-cors-headers) before the view runs.
    scrudful's OPTIONS is the other, non-preflight, case: a request that does
    reach it is answered with the description like any other OPTIONS.

    It takes ``ninja.Router``'s arguments.
    """

    def add_api_operation(
        self,
        path: str,
        methods: list[str],
        view_func: Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        super().add_api_operation(path, methods, view_func, **kwargs)
        path_view = _path_view(self, view_func)
        # ninja routes a request to the operations listing its method as is.
        if "OPTIONS" in methods:
            # The implementer's OPTIONS replaces the automatic one.
            path_view.operations = [
                operation
                for operation in path_view.operations
                if not _is_automatic_options(operation)
            ]
        elif isinstance(getattr(view_func, "_scrudful", None), _Config) and not any(
            "OPTIONS" in operation.methods for operation in path_view.operations
        ):
            super().add_api_operation(
                path,
                ["OPTIONS"],
                _options_view(path, path_view),
                auth=None,
                response=NOT_SET,
                include_in_schema=False,
            )


def add_error_links(api: NinjaAPI, error: Entity) -> None:
    """Give the error responses of ``api``'s exception handlers ``error``'s Links.

    Wraps the handlers registered, when this is called, for
    ``ninja.errors.ValidationError`` (422), ``AuthenticationError`` (401),
    ``AuthorizationError`` (403, when the installed ninja defines it),
    ``Throttled`` (429), ``HttpError`` and ``django.http.Http404``: the
    responses ninja answers before an operation runs, and the errors any
    operation raises. Only a class with a handler of its own is wrapped;
    one without (with ninja's defaults, the three ``HttpError`` subclasses)
    keeps falling back to its base's handler, which is. Each wrapped handler
    returns the response the original makes, with the same status and body,
    and adds ``error``'s Links to it when it has content, as :func:`scrudful`
    adds them (a Link the handler set with the relation of one of them
    suppresses it; see :func:`scrudful._adapter.merge_links`).
    So a custom handler registered before the call is kept, and calling this
    again replaces the entity of the earlier call: no Link is added twice,
    and only the latest ``error``'s link callables run. A handler registered
    after the call replaces the wrapped one, for its class and the
    subclasses without a handler of their own (e.g. a later ``HttpError``
    handler answers the 401, 403 and 429 too, without Links): call this
    after registering your handlers.

    A response with JSON content (an ``application/json`` or ``+json``
    Content-Type, as ninja's renderer gives) is served as
    ``error.media_type``, whatever the handler chose, so the JSON bodies of
    every wrapped handler must be content that ``error`` describes. Other
    content keeps its Content-Type.

    An error raised inside a scrudful operation gets that operation's
    entity for the status, if it has one (``responses[status]``, else its
    ``error``), and its media type: it describes the response, as the
    OPTIONS of the operation does, and ``error`` here applies only otherwise.

    Not covered: ninja's 405 for a method no operation of the path routes,
    which no exception handler makes, Django's own 404 for an unknown URL
    and Django's 500 page.

    Parameters
    ----------
    api : NinjaAPI
        The API whose exception handlers are wrapped.
    error : Entity
        The schema, context and media type of the error bodies. Its link
        callables are called with the request and ``obj=None``. ninja's
        exception handlers are sync, also in an async operation, where they
        run on the event loop: the callables must be sync, and must not use
        the Django ORM there.

    Raises
    ------
    TypeError
        If ``api`` isn't a ``NinjaAPI``, ``error`` isn't an :class:`Entity`
        or one of its values is an ``async def`` callable.
    ValueError
        If ``error``'s media type is not JSON content (``application/json``
        or ``+json``, other than JSON-LD).
    """
    if not isinstance(api, NinjaAPI):
        raise TypeError(f"api must be a NinjaAPI, not {type(api).__name__}")
    if not isinstance(error, Entity):
        raise TypeError(f"error must be an Entity, not {type(error).__name__}")
    check_error_media_type(error.media_type)
    for entity_field in fields(Entity):
        if _is_async_callable(getattr(error, entity_field.name)):
            raise TypeError(
                f"error.{entity_field.name} is async, but ninja's exception "
                "handlers are sync"
            )
    # Only the classes with a handler of their own: the others (with ninja's
    # defaults, AuthenticationError, AuthorizationError and Throttled) fall
    # back to their base's through the MRO, as ninja's
    # ``_lookup_exception_handler`` does, and so to the wrapped one, or to
    # one the app registers for the base later.
    handlers = api._exception_handlers
    for exc_class in _ERROR_CLASSES:
        if exc_class in handlers:
            handler = handlers[exc_class]
            # Calling this again replaces the earlier entity.
            handler = getattr(handler, "_scrudful_error_handler", handler)
            api.add_exception_handler(exc_class, _error_handler(handler, error))


def _decorator(config: _Config, name: str) -> Callable[[Handler], Handler]:
    """The decorator of ``config``: one request flow for every kind.

    ``name`` is the public decorator's, for error messages.
    """
    load, load_as = config.load, config.load_as

    def decorator(handler: Handler) -> Handler:
        if hasattr(handler, "_ninja_operation"):
            # ninja already registered the undecorated function: wrapping it
            # now would silently change nothing.
            raise TypeError(
                f"{name} must sit below the operation decorator: "
                f"{handler.__qualname__}() is already a ninja operation"
            )
        if inspect.isgeneratorfunction(handler) or inspect.isasyncgenfunction(handler):
            raise TypeError(f"{name} does not support generator handlers")
        if not inspect.iscoroutinefunction(handler):
            _check_sync_callables(config, handler)
        hidden = _wrapper_signature(handler, load_as if load is not None else None)
        response_name = hidden.response_name
        asynchronous = inspect.iscoroutinefunction(handler)
        label = f"{name}: {handler.__qualname__}()"
        # In an async operation with sync per-item callables, a page is built
        # in one call (one thread hop), not one call per callable.
        page_in_one_call = asynchronous and not _has_async_item_callables(config)

        def flow(
            request: HttpRequest, values: dict[str, Any], answers: _Answers
        ) -> _Flow[Any]:
            """The request flow; each call of a user callable is a ``yield``.

            ``answers`` is what the running operation declares.
            """
            response: HttpResponse = (
                values[response_name]
                if hidden.pass_response
                else values.pop(response_name)
            )
            pagination = (
                values.pop(_PAGINATION, None) if config.kind == "list" else None
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

            if config.evaluates_preconditions and evaluates_preconditions(method):
                etag, last_modified = yield from _item_validators(config, obj)
                result = evaluate_preconditions(
                    method,
                    precondition_headers(request),
                    etag=etag,
                    last_modified=last_modified,
                    require_precondition=True,
                    # load decides existence, not the validators (spec §1).
                    exists=obj is not None,
                )
                if result is not None:
                    return (yield from _early_response(config, result, request, obj))

            content = yield handler, (request,), values

            default = config.default_status if meta.status is None else meta.status
            if default is not None and not _has_status(content):
                content = Status(default, content)
            headers, status = _response_and_status(content, response, answers.declared)
            list_etag = list_last_modified = None
            if (
                config.kind == "list"
                and not isinstance(content, HttpResponseBase)
                and answers.collection is not None
                and answers.collection.includes(status)
            ):
                # The queryset is the authorized object: the collection's
                # preconditions are evaluated now, then the page is built.
                obj = _body(content)
                if config.has_list_validators and evaluates_preconditions(method):
                    list_etag, list_last_modified = yield from _validators(
                        config.list_etag_func, config.list_last_modified_func, obj
                    )
                    result = evaluate_preconditions(
                        method,
                        precondition_headers(request),
                        etag=list_etag,
                        last_modified=list_last_modified,
                        require_precondition=False,
                        # The collection exists, even without validators.
                        exists=True,
                    )
                    if result is not None:
                        early = yield from _early_response(config, result, request, obj)
                        _keep_handler_headers(early, response)
                        return early
                page = _page(config, request, obj, pagination)
                if page_in_one_call:
                    # The count, the slice and every entry's values.
                    page = yield _run_in_thread, (page,), {}
                else:
                    page = yield from page
                content = _with_body(content, page)
            # Explicit values first: the computed ones never overwrite a header.
            set_validator_headers(
                headers,
                None if meta.etag is None else quote_etag(meta.etag),
                http_timestamp(aware(meta.last_modified)),
            )
            if meta.location is not None and "Location" not in headers:
                headers["Location"] = iri_to_uri(str(meta.location))
            if config.kind == "create":
                created = meta.created
                if created is _UNSET and status == 201:
                    created = _body(content)
                # None and _UNSET: no created object, nothing to describe.
                obj = None if created is _UNSET else created
                if status == 201 and obj is not None:
                    yield from _created_headers(config, headers, request, obj)
            if meta.entity is not None:
                if not isinstance(meta.entity, Entity):
                    raise TypeError(
                        "ScrudfulResponse.entity must be an Entity, not "
                        f"{type(meta.entity).__name__}"
                    )
                entity = meta.entity
            elif config.kind == "create" and obj is None:
                # The item entity describes the created object: without one,
                # an explicit ``responses[status]``, else the item entity's
                # Links for a 201 body when they are all static (a callable
                # would get None, not the created item).
                entity = config.responses.get(status)
                if (
                    entity is None
                    and status == 201
                    and _carries_body(content)
                    and not _has_callables(config.entity)
                ):
                    entity = config.entity
            else:
                entity = config.response_entity(method, status)
            if config.kind == "list":
                # None unless the collection's validators were computed.
                if validator_policy(method, status)[1]:
                    set_validator_headers(headers, list_etag, list_last_modified)
            elif config.kind == "item" and load is not None:
                refresh, send = validator_policy(method, status)
                if refresh and (config.has_validators or _has_callables(entity)):
                    # What was just written: for the validators and the Links.
                    obj = yield load, (request,), params
                    if config.has_validators:
                        etag, last_modified = yield from _item_validators(config, obj)
                if send and config.has_validators:
                    set_validator_headers(headers, etag, last_modified)
            if entity is not None:
                yield from _add_links(headers, entity, request, obj)
            expose_headers(headers)
            return content

        def bind(answers: _Answers) -> Callable[..., Any]:
            """The wrapper running :func:`flow` with ``answers``."""
            # ninja builds an AsyncOperation for a coroutine function: the
            # wrapper is one exactly when the handler is.
            if asynchronous:

                @functools.wraps(handler)
                async def wrapper(request: HttpRequest, **values: Any) -> Any:
                    _check_hooks_allowed(config, label)
                    try:
                        return await _run_async(flow(request, values, answers))
                    except Exception as exc:
                        _note_raised(config, exc)
                        raise

            else:

                @functools.wraps(handler)
                def wrapper(request: HttpRequest, **values: Any) -> Any:
                    _check_hooks_allowed(config, label)
                    try:
                        return _run_sync(flow(request, values, answers))
                    except Exception as exc:
                        _note_raised(config, exc)
                        raise

            wrapper.__signature__ = hidden.signature  # type: ignore[attr-defined]
            wrapper._scrudful = config  # type: ignore[attr-defined]
            return wrapper

        def bind_operation(operation: Any) -> None:
            """Give ``operation`` a wrapper bound to its own declarations.

            Called by ninja at the end of ``Operation.__init__``. The function
            may be registered on several paths, with different ``response=``:
            each operation runs its own wrapper, which ninja's clones of it
            (one per mount of the router) share, with its response models.

            ``operation.view_func`` must still be the registered wrapper:
            replacing what something else put there (a callback contributed
            below the decorator, which runs first, or a decorator between it
            and the operation decorator) would silently discard it.
            ``add_decorator`` applies after the binding, and ``decorate_view``
            wraps ``operation.run``, not ``view_func``.
            """
            if operation.view_func is not wrapper:
                raise ConfigError(
                    f"{name}: operation {'/'.join(operation.methods)} "
                    f"{operation.path!r} ({handler.__qualname__}()): something "
                    "replaced operation.view_func before "
                    f"{name} could bind it (a decorator between the operation "
                    f"decorator and {name}, or a hook contributed below it), "
                    f"and binding would discard it. Keep {name} directly below "
                    "the operation decorator, and never move an authorization "
                    f"or caching decorator below {name}: code there runs after "
                    "load and the preconditions. Put per-operation "
                    "authorization in load or auth=, and wrap a router's or "
                    "the API's operations with add_decorator(mode='operation'). "
                    "Stacking two scrudful decorators is not supported. See "
                    '"Placement of other decorators" in the scrudful.ninja '
                    "documentation."
                )
            collection = (
                _envelope_response(operation) if config.kind == "list" else None
            )
            operation.view_func = bind(
                _Answers(_declared_status(operation), collection)
            )
            # The Links of an error the wrapper raises go on the response of
            # the API's exception handler, which ``Operation.run`` returns. It
            # is wrapped as ``decorate_view`` wraps it, so that ninja's clones
            # of the operation (one per mount) wrap theirs too.
            run_decorator = _raised_links_run(asynchronous, config, label)
            _add_run_decorator(operation, run_decorator)

        # Registered with ninja, which replaces it in each operation by the
        # wrapper :func:`bind_operation` binds (called directly, it declares
        # nothing: a plain return value is a 200).
        wrapper = bind(_Answers())
        # ninja calls these at the end of ``Operation.__init__``. A fresh list:
        # ``functools.wraps`` copied the handler's (e.g. from ``@paginate``) by
        # reference, and appending to it would change the handler.
        wrapper._ninja_contribute_to_operation = [  # type: ignore[attr-defined]
            *getattr(handler, "_ninja_contribute_to_operation", []),
            bind_operation,
        ]
        if config.kind == "list":
            # The query parameters, passed to the wrapper as ``_PAGINATION``
            # (as ninja's ``contribute_operation_args`` does, on a fresh list).
            wrapper._ninja_contribute_args = [  # type: ignore[attr-defined]
                *getattr(handler, "_ninja_contribute_args", []),
                (_PAGINATION, _pagination_model(config), Query(...)),
            ]
        return wrapper  # type: ignore[return-value]

    return decorator


def _early_response(
    config: _Config, result: ConditionalResult, request: HttpRequest, obj: Any
) -> _Flow[HttpResponseBase]:
    """scrudful's own response to a failed or short-circuited precondition.

    The 400, 412 and 428 bodies get the Links of ``config.error``, whose link
    callables get ``obj``, and its media type as their Content-Type. The
    serving API's renderer writes them (see :func:`_render`).
    """
    error = config.error
    result = with_error_body(config.error_body_func, request, result)
    early = conditional_response(
        result,
        content_type=None if error is None else error.media_type,
        render=_render(request),
    )
    if result.body is not None and error is not None:
        yield from _add_links(early, error, request, obj)
        expose_headers(early)
    return early


def _keep_handler_headers(early: HttpResponseBase, temporal: HttpResponse) -> None:
    """Copy onto ``early`` what the handler set on ninja's temporal response.

    As ``scrudful.fastapi`` does: a 304 gets the headers a 200 would carry
    (``Cache-Control``, ``Vary``, ..., RFC 9110 §15.4.5), except
    representation metadata and those it already has; every early response
    gets the cookies. A 400 or 412 gets no other header (e.g. a ``public``
    Cache-Control must not let a shared cache store it).
    """
    if early.status_code == 304:
        for name, value in temporal.items():
            lower = name.lower()
            if lower == "access-control-expose-headers":
                merged = merge_expose_headers(early.get(name), value.split(","))
                if merged:
                    early[name] = merged
            elif lower not in BODY_HEADERS and name not in early:
                early[name] = value
    early.cookies.update(temporal.cookies)
    expose_headers(early)


# --- Raised errors -------------------------------------------------------------

#: The exceptions whose handlers :func:`add_error_links` wraps.
_ERROR_CLASSES: tuple[type[Exception], ...] = tuple(
    exc_class
    for exc_class in (
        ninja_errors.ValidationError,
        ninja_errors.AuthenticationError,
        getattr(ninja_errors, "AuthorizationError", None),
        ninja_errors.Throttled,
        HttpError,
        Http404,
    )
    if exc_class is not None
)


@dataclass(eq=False, slots=True)
class _Raised:
    """What the scrudful operation running ``Operation.run`` raised, and the
    path parameters ``run`` got (for the hooks, see :func:`_install_hooks`)."""

    #: The operation's, once its wrapper or one of its hooks raised an
    #: ``HttpError`` or ``Http404``.
    config: _Config | None = None
    #: The path parameters, as the URL gives them (strings, unless the path
    #: uses a converter).
    path_params: dict[str, Any] = field(default_factory=dict)
    #: Whether the operation's hooks (``before``, ``after_throttles``) ran
    #: and allowed the request (see :func:`_check_hooks_allowed`).
    hooks_allowed: bool = False


#: Set by :func:`_raised_links_run` while ``Operation.run`` runs. A mutable
#: holder, so the wrapper's note reaches it from a copied context too.
_RAISED: ContextVar[_Raised | None] = ContextVar("scrudful_raised", default=None)


#: The API serving the scrudful operation (or automatic OPTIONS) running, set
#: by its ``Operation.run`` decorator (:func:`_raised_links_run`,
#: :func:`_serving_api_run`); ``None`` outside one.
_SERVING_API: ContextVar[NinjaAPI | None] = ContextVar(
    "scrudful_serving_api", default=None
)


def _operation_of(run: Callable[..., Any]) -> Any:
    """The ninja operation whose ``run`` method ``run`` is, or wraps.

    ninja decorates the bound ``Operation.run`` of each operation and of each
    of its clones (one per mount); decorators applied before scrudful's are
    seen through when they use ``functools.wraps``. ``None`` when it can't be
    found (scrudful's bodies are then written as ``JsonResponse`` writes them).
    """
    return getattr(inspect.unwrap(run), "__self__", None)


@contextlib.contextmanager
def _serving(operation: Any) -> Iterator[None]:
    """:data:`_SERVING_API` is ``operation``'s API within."""
    token = _SERVING_API.set(getattr(operation, "api", None))
    try:
        yield
    finally:
        _SERVING_API.reset(token)


def _add_run_decorator(
    operation: Any, decorator: Callable[[Callable[..., Any]], Callable[..., Any]]
) -> None:
    """Decorate ``operation.run`` with ``decorator``, as ``decorate_view``
    does, so the clones ninja makes of ``operation`` (one per mount) are
    decorated too: ninja re-applies ``_run_decorators`` to each."""
    if not hasattr(operation, "_run_decorators"):
        operation._run_decorators = []
    operation.run = decorator(operation.run)
    operation._run_decorators.append(decorator)


def _serving_api_run(run: Callable[..., Any]) -> Callable[..., Any]:
    """The ``Operation.run`` decorator of the (sync) automatic OPTIONS operation,
    setting :data:`_SERVING_API` while it runs."""
    operation = _operation_of(run)

    @functools.wraps(run)
    def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
        with _serving(operation):
            return run(request, *args, **kw)

    return scrudful_run


def _record_serving_api(operation: Any) -> None:
    """Decorate ``operation.run`` with :func:`_serving_api_run`."""
    _add_run_decorator(operation, _serving_api_run)


def _render(request: HttpRequest) -> Render | None:
    """How the API serving ``request`` writes a JSON body, for scrudful's own
    responses (OPTIONS and the early 400, 412 and 428).

    Its renderer, when that writes JSON content (an ``application/json`` or
    ``+json`` ``media_type``), so these bodies have the bytes and charset of
    every other response of the API; their Content-Type is the body's media
    type with that charset. Not the API's ``create_response``, which a
    subclass may override to add headers (a public ``Cache-Control``, say)
    a 400 or 412 must not carry. Unless the
    renderer is ninja's ``JSONRenderer`` with its encoder, which writes them
    so itself, the body is first reduced to JSON values as ninja's
    ``NinjaJSONEncoder`` (a ``DjangoJSONEncoder``) writes them, so a renderer
    of plain JSON (``orjson``, ``json.dumps``) writes a datetime,
    ``Decimal``, ``UUID`` or lazy string as ``JsonResponse`` did. ``None``
    (Django's ``JsonResponse``)
    outside a scrudful operation, or for a renderer of other content: the
    bodies are JSON whatever the renderer.
    """
    api = _SERVING_API.get()
    media_type = getattr(getattr(api, "renderer", None), "media_type", None)
    if not media_type or not is_json_syntax(media_type):
        return None

    renderer = api.renderer
    as_is = _writes_django_values(renderer)

    def render(data: Any, status: int, content_type: str) -> HttpResponse:
        plain = data if as_is else _json_values(data)
        content = renderer.render(request, plain, response_status=status)
        # A str is encoded in the charset the Content-Type names.
        return HttpResponse(
            content,
            status=status,
            content_type=with_charset(content_type, renderer.charset),
        )

    return render


def _writes_django_values(renderer: Any) -> bool:
    """Whether ``renderer`` writes Django's values itself: ninja's
    ``JSONRenderer`` (its ``render``) with a ``DjangoJSONEncoder`` (ninja's
    ``NinjaJSONEncoder`` is one), which writes them its own way. Its bodies
    get no reduction (:func:`_json_values`) first."""
    encoder = getattr(renderer, "encoder_class", None)
    return (
        type(renderer).render is JSONRenderer.render
        and isinstance(encoder, type)
        and issubclass(encoder, DjangoJSONEncoder)
    )


def _json_values(data: Any) -> Any:
    """``data`` as JSON values, written as ``NinjaJSONEncoder`` writes them."""
    return json.loads(json.dumps(data, cls=NinjaJSONEncoder))


def _note_raised(config: _Config, exc: Exception) -> None:
    """Note ``exc`` if it is an ``HttpError`` or ``Http404`` the operation of
    ``config`` raised (its wrapper, or a hook): its response gets the
    Links of the operation's entity for the status (see
    :func:`_raised_links_run`)."""
    raised = _RAISED.get()
    if raised is not None and isinstance(exc, (HttpError, Http404)):
        raised.config = config


def _raised_links_run(
    asynchronous: bool, config: _Config, label: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The ``Operation.run`` decorator adding the Links of a raised error.

    The exception propagates out of the wrapper as before (through the
    decorators added with ``add_decorator(mode="operation")``, e.g. one
    rolling a transaction back), and ninja answers it with the API's
    exception handler. The response ``run`` returns then gets the Links of
    the operation's entity for its status, see :func:`_raised_entity`.

    While ``run`` runs, :data:`_SERVING_API` is the operation's API, whose
    renderer writes scrudful's own bodies (see :func:`_render`). A list
    operation's hooks (``before``, ``after_throttles``) are installed on the
    operation, and on each clone ninja decorates again (see
    :func:`_install_hooks`; ``label`` names the
    operation in its errors).
    """

    def decorator(run: Callable[..., Any]) -> Callable[..., Any]:
        operation = _operation_of(run)
        if config.hooks is not None:
            _install_hooks(operation, config, asynchronous, label)
        if asynchronous:

            @functools.wraps(run)
            async def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
                raised = _Raised(path_params=kw)
                token = _RAISED.set(raised)
                try:
                    with _serving(operation):
                        response = await run(request, *args, **kw)
                finally:
                    _RAISED.reset(token)
                if raised.config is not None:
                    await _run_async(_raised_links(raised.config, request, response))
                return response

        else:

            @functools.wraps(run)
            def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
                raised = _Raised(path_params=kw)
                token = _RAISED.set(raised)
                try:
                    with _serving(operation):
                        response = run(request, *args, **kw)
                finally:
                    _RAISED.reset(token)
                if raised.config is not None:
                    _run_sync(_raised_links(raised.config, request, response))
                return response

        return scrudful_run

    return decorator


#: The digests (:func:`_source_digest`) of the methods of ninja's
#: ``Operation`` and ``AsyncOperation`` that scrudful's copy of the checks
#: (:func:`_install_hooks`) was written from: ``_run_checks``, which it
#: copies, and ``run``, which answers the response it returns before reading
#: any parameter. ninja 1.7.0 and 1.7.1 on Python 3.10 to 3.14 (``run``'s
#: f-string is tokenized differently from Python 3.12 on).
_NINJA_SOURCES = {
    "Operation._run_checks": frozenset(
        {"1f879fdc88dd517f7ff2f3961c324160638e3de021cf294b6e4e9f9ecccfc2d4"}
    ),
    "Operation.run": frozenset(
        {
            "0ff387914c346a2460656c70de211ba5041530c8244a7c25db9b0f49fa4e9ac0",
            "66dd21c42a3edd6b7a60a54586afd7e6cf7ae0026afef83629e444aafa46b628",
        }
    ),
    "AsyncOperation._run_checks": frozenset(
        {"a15261992147f87c4ec2ad74fd67ebe1984020a2f4b2476679771d2c4e920cea"}
    ),
    "AsyncOperation.run": frozenset(
        {"5189477ffad3b05335304fa19f3900e208d8d234ca99626b9117cf7067921fc7"}
    ),
}
#: The code fingerprint of ``_run_checks``, sync and async (see :data:`_NINJA_CODE`).
_CHECKS_CODE = (
    (
        "csrf_exempt",
        "_ninja_csrf_exempt",
        "auth_callbacks",
        "_run_authentication",
        "throttle_objects",
        "_check_throttles",
    ),
    ("self", "request", "error"),
    2,
)
#: Where the source isn't available: the names, local variables and argument
#: count of each (the same in ninja 1.7.0 and 1.7.1 on Python 3.10 to 3.14).
#: Weaker: a body using the same names in another order passes.
_NINJA_CODE = {
    "Operation._run_checks": _CHECKS_CODE,
    "Operation.run": (
        (
            "_run_checks",
            "api",
            "create_temporal_response",
            "_get_values",
            "view_func",
            "stream_format",
            "_stream_response",
            "_result_to_response",
            "Exception",
            "isinstance",
            "TypeError",
            "str",
            "args",
            "on_exception",
        ),
        (
            "self",
            "request",
            "kw",
            "error",
            "temporal_response",
            "values",
            "result",
            "e",
            "msg",
        ),
        2,
    ),
    "AsyncOperation._run_checks": _CHECKS_CODE,
    "AsyncOperation.run": (
        (
            "_run_checks",
            "api",
            "create_temporal_response",
            "_get_values",
            "stream_format",
            "view_func",
            "_async_stream_response",
            "_result_to_response",
            "Exception",
            "on_exception",
        ),
        (
            "self",
            "request",
            "kw",
            "error",
            "temporal_response",
            "values",
            "result",
            "e",
        ),
        2,
    ),
}
#: Tokens that don't change the code: comments and blank lines.
_IGNORED_TOKENS = frozenset({tokenize.COMMENT, tokenize.NL})
#: Tokens kept by type only (their text is whitespace).
_STRUCTURE_TOKENS = frozenset(
    {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.ENDMARKER}
)


def _source_digest(func: Any) -> str | None:
    """The SHA-256 of ``func``'s source tokens, insensitive to comments, blank
    lines and its docstring; ``None`` when the source isn't available or
    can't be read.

    Each other token counts, as its type and text (only the type for
    indentation and line ends).
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tokens = [
            token
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in _IGNORED_TOKENS
        ]
    except (OSError, TypeError, tokenize.TokenError, SyntaxError):
        return None
    digest = hashlib.sha256()
    for token in _without_docstring(tokens):
        text = "" if token.type in _STRUCTURE_TOKENS else token.string
        digest.update(f"{tokenize.tok_name[token.type]}\0{text}\1".encode())
    return digest.hexdigest()


def _without_docstring(tokens: list[tokenize.TokenInfo]) -> list[tokenize.TokenInfo]:
    """The tokens of a function but its docstring: the strings alone on the
    first line of its body."""
    body = next(
        (
            index + 1
            for index, token in enumerate(tokens)
            if token.type == tokenize.INDENT
        ),
        len(tokens),
    )
    end = body
    while end < len(tokens) and tokens[end].type == tokenize.STRING:
        end += 1
    if end == body or end == len(tokens) or tokens[end].type != tokenize.NEWLINE:
        return tokens
    return tokens[:body] + tokens[end + 1 :]


@functools.cache
def _is_ninja_code(func: Any, name: str) -> bool:
    """Whether ``func`` is the code of ninja's method ``name`` (e.g.
    ``"Operation.run"``) scrudful's copy of the checks was written from: by
    the digest of its source, else (no source) by its names, locals and
    arguments. Memoized: each function is read once, whatever the number of
    operations and mounts."""
    digest = _source_digest(func)
    if digest is not None:
        return digest in _NINJA_SOURCES[name]
    code = getattr(func, "__code__", None)
    return (
        code is not None
        and (code.co_names, code.co_varnames, code.co_argcount) == _NINJA_CODE[name]
    )


def _check_ninja_checks(
    operation: Any, config: _Config, asynchronous: bool, label: str
) -> None:
    """Raise ``ConfigError`` unless ``operation`` can run ``config``'s hooks
    (``before``, ``after_throttles``) in scrudful's copy of ninja's checks
    (:func:`_install_hooks`).

    So an opaque decorator hiding the operation, an ``Operation`` subclass
    overriding ``_run_checks`` or ``run``, or a ninja whose checks (or the
    ``run`` calling them) changed fails when the operation is built instead
    of losing a hook or a check.
    """
    hooks = config.hooks
    if operation is None:
        raise ConfigError(
            f"{label}: {hooks} can't be installed: scrudful can't find the "
            "ninja operation through Operation.run, because a decorate_view "
            f"decorator below {config.decorator_name} wraps it without "
            "functools.wraps. "
            "Use functools.wraps in that decorator, or apply decorate_view "
            "above the operation decorator."
        )
    ninja_class = AsyncOperation if asynchronous else Operation
    cls = type(operation)
    if "_run_checks" in vars(operation):
        raise _checks_error(label, config, "the operation has its own _run_checks")
    for method in ("_run_checks", "run"):
        func = getattr(cls, method, None)
        if func is not getattr(ninja_class, method):
            raise _checks_error(
                label,
                config,
                f"{cls.__qualname__} overrides {method} of ninja's "
                f"{ninja_class.__name__}",
            )
        if not _is_ninja_code(func, f"{ninja_class.__name__}.{method}"):
            raise _checks_error(
                label,
                config,
                f"the installed ninja's {ninja_class.__name__}.{method} differs "
                f"from ninja 1.7.0's and 1.7.1's, which scrudful's {hooks} "
                f"{_verb(config, 'is', 'are')} verified against: upgrade "
                "scrudful, or keep ninja at one of those releases",
            )


def _verb(config: _Config, one: str, both: str) -> str:
    """``one`` for a single hook, ``both`` for ``before`` and ``after_throttles``."""
    return (
        both
        if config.before is not None and config.after_throttles is not None
        else one
    )


def _checks_error(label: str, config: _Config, reason: str) -> ConfigError:
    """The ``ConfigError`` of an operation that can't run ``config``'s hooks."""
    where = []
    if config.before is not None:
        where.append("before between authentication and throttling")
    if config.after_throttles is not None:
        where.append("after_throttles after throttling")
    return ConfigError(
        f"{label}: {config.hooks} {_verb(config, 'needs', 'need')} ninja's own "
        "Operation._run_checks and the Operation.run calling it (as in ninja "
        f"1.7.0 and 1.7.1), as scrudful runs {' and '.join(where)}; {reason}."
    )


def _install_hooks(
    operation: Any, config: _Config, asynchronous: bool, label: str
) -> None:
    """Give ``operation`` checks running ``config.before`` where DRF checks
    permissions (after authentication, before throttling) and
    ``config.after_throttles`` where DRF's ``list()`` starts (after
    throttling).

    ``Operation.run`` answers a response its ``_run_checks`` returns before it
    reads any parameter. These checks are ninja 1.7.0's and 1.7.1's (CSRF
    exemption flag, authentication, throttles) with ``before`` called between
    authentication and throttling, so a request it refuses uses none of a
    throttle's quota, and ``after_throttles`` after throttling, so a
    throttled request never calls it. Each gets its own copy of the path
    parameters, as the URL gives them. An exception either raises is
    answered by the API's exception handler, as ninja answers its own;
    neither reaches the decorators added with
    ``add_decorator(mode="operation")``, which wrap the operation's function.
    A hook allows the request by returning ``None`` and refuses it by
    raising: anything else it returns raises ``TypeError`` (a returned
    ``HttpResponse`` or ``False`` must not let the request go on as if it
    were allowed). So does an awaitable in a sync operation, which can't
    await it; a coroutine is closed and a future (or task) cancelled, so
    neither runs on unawaited. Without the path parameters (the checks run
    outside the operation's ``Operation.run``) it raises ``RuntimeError``
    instead of calling a hook without them. The request is marked allowed
    (:func:`_check_hooks_allowed`) once every hook allowed it.

    Raises
    ------
    ninja.errors.ConfigError
        See :func:`_check_ninja_checks`.
    """
    _check_ninja_checks(operation, config, asynchronous, label)
    # In DRF's order; ``None`` for ninja's throttles.
    steps: list[tuple[str, Callable[..., Any] | None]] = [("throttles", None)]
    if config.before is not None:
        steps.insert(0, ("before", config.before))
    if config.after_throttles is not None:
        steps.append(("after_throttles", config.after_throttles))

    def current() -> _Raised:
        raised = _RAISED.get()
        if raised is None:
            raise RuntimeError(
                f"{label}: {config.hooks} can't get the path parameters: the "
                "operation's checks ran outside its Operation.run"
            )
        return raised

    def answer(request: HttpRequest, exc: Exception) -> HttpResponseBase:
        """The API's answer to what a hook raised."""
        _note_raised(config, exc)
        return operation.api.on_exception(request, exc)  # type: ignore[no-any-return]

    def mark_csrf_exempt(request: HttpRequest) -> None:
        if operation.csrf_exempt:
            request._ninja_csrf_exempt = True  # type: ignore[attr-defined]

    def throttle(request: HttpRequest) -> HttpResponseBase | None:
        if operation.throttle_objects:
            error = operation._check_throttles(request)
            if error:
                return error  # type: ignore[no-any-return]
        return None

    if asynchronous:

        async def run_checks(request: HttpRequest) -> HttpResponseBase | None:
            mark_csrf_exempt(request)
            if operation.auth_callbacks:
                error = await operation._run_authentication(request)
                if error:
                    return error  # type: ignore[no-any-return]
            raised = current()
            for name, hook in steps:
                if hook is None:
                    error = throttle(request)
                    if error is not None:
                        return error
                    continue
                try:
                    result = await _call_async(
                        hook, (request,), dict(raised.path_params)
                    )
                except Exception as exc:
                    return answer(request, exc)
                if result is not None:
                    _refuse_hook_result(name, result, asynchronous)
            raised.hooks_allowed = True
            return None

    else:

        def run_checks(  # type: ignore[misc]
            request: HttpRequest,
        ) -> HttpResponseBase | None:
            mark_csrf_exempt(request)
            if operation.auth_callbacks:
                error = operation._run_authentication(request)
                if error:
                    return error  # type: ignore[no-any-return]
            raised = current()
            for name, hook in steps:
                if hook is None:
                    error = throttle(request)
                    if error is not None:
                        return error
                    continue
                try:
                    result = hook(request, **raised.path_params)
                except Exception as exc:
                    return answer(request, exc)
                if result is not None:
                    _refuse_hook_result(name, result, asynchronous)
            raised.hooks_allowed = True
            return None

    operation._run_checks = run_checks


def _check_hooks_allowed(config: _Config, label: str) -> None:
    """Raise ``RuntimeError`` unless the operation's hooks (``before``,
    ``after_throttles``; if any) ran and allowed this request.

    So a request never reaches the function without them: if ninja served an
    operation without scrudful's checks (e.g. a copy it made without
    re-applying ``Operation.run``'s decorators), the request fails instead
    of going on unchecked.
    """
    hooks = config.hooks
    if hooks is None:
        return
    raised = _RAISED.get()
    if raised is None or not raised.hooks_allowed:
        raise RuntimeError(
            f"{label}: {hooks} didn't run for this request: the operation "
            "ninja served lacks scrudful's checks. Refusing the request."
        )


def _refuse_hook_result(name: str, result: Any, asynchronous: bool) -> NoReturn:
    """Raise ``TypeError`` for what hook ``name`` (``before`` or
    ``after_throttles``) returned instead of ``None``; an awaitable is closed
    (a coroutine) or cancelled (a future) first."""
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        elif asyncio.isfuture(result):
            result.cancel()
        if not asynchronous:
            raise TypeError(
                f"{name} returned an awaitable ({result!r}) in a sync "
                "operation, which can't await it: make the operation async "
                f"def, or {name} a plain function"
            )
    raise TypeError(
        f"{name} returned {result!r}: it allows the request by returning "
        "None and refuses it by raising (e.g. HttpError(403, ...)), never by "
        "returning a response or a value"
    )


def _raised_links(
    config: _Config, request: HttpRequest, response: HttpResponseBase
) -> _Flow[None]:
    """Add the Links of an error the operation raised to ``response``.

    ``response`` is the API's answer to it; when it has content, it gets
    the entity :func:`_raised_entity` picks for its status, whose link
    callables get ``None``, and its media type (see :func:`_error_links`).
    """
    if not _carries_body(response):
        return
    entity = _raised_entity(config, request.method.lower(), response.status_code)
    if entity is not None:
        yield from _error_links(response, entity, request)


def _raised_entity(config: _Config, method: str, status: int) -> Entity | None:
    """The entity of the response to an error the operation raised.

    The one the operation's OPTIONS describes for ``status`` (see
    :func:`_described_entity`: ``responses[status]``, but ``error`` first
    for scrudful's own early statuses), else ``error``. None below 400: an
    operation decorator turned the error into another response, which is
    its own to describe (a link callable of the default entity would get
    ``None``, not the object).
    """
    if status < 400:
        return None
    entity = _described_entity(config, method, status, _is_conditional(config, method))
    return config.error if entity is None else entity


def _error_handler(
    handler: Callable[[HttpRequest, Exception], HttpResponseBase], error: Entity
) -> Callable[[HttpRequest, Exception], HttpResponseBase]:
    """``handler``, an exception handler of the API, adding ``error``'s Links."""

    @functools.wraps(handler)
    def scrudful_handler(request: HttpRequest, exc: Exception) -> HttpResponseBase:
        response = handler(request, exc)
        raised = _RAISED.get()
        if (
            raised is not None
            and raised.config is not None
            and _raised_entity(
                raised.config, request.method.lower(), response.status_code
            )
            is not None
        ):
            # A scrudful operation raised it: its entity describes the
            # response, and :func:`_raised_links_run` adds it.
            return response
        if _carries_body(response):
            _run_sync(_error_links(response, error, request))
        return response

    scrudful_handler._scrudful_error_handler = handler  # type: ignore[attr-defined]
    return scrudful_handler


def _error_links(
    response: HttpResponseBase, entity: Entity, request: HttpRequest
) -> _Flow[None]:
    """Give the error ``response`` the Links and media type of ``entity``.

    Its link callables get ``None``. A response with JSON content, as
    ninja's renderer makes, is served as ``entity.media_type`` when that is
    JSON content too (an ``error`` entity's always is), with the charset its
    bytes are in, as scrudful's own error bodies are; other content keeps
    its Content-Type.
    """
    yield from _add_links(response, entity, request, None)
    content_type = response.get("Content-Type")
    if content_type and is_json_syntax(content_type) and is_json(entity.media_type):
        response["Content-Type"] = with_charset(entity.media_type, response.charset)
    expose_headers(response)


class _CollectionResponse(NamedTuple):
    """The response of a list operation that ninja validates as the envelope."""

    #: The code whose response model is the envelope (or ``...``).
    code: Any
    #: Every code the operation declares.
    codes: frozenset[Any]

    def includes(self, status: int) -> bool:
        """Whether ninja validates a response with ``status`` as the envelope."""
        return status == self.code if status in self.codes else self.code is ...


def _envelope_response(operation: Any) -> _CollectionResponse:
    """Replace the operation's collection response by the envelope.

    The first response declared as a collection, e.g. ``List[ItemOut]``,
    becomes ``Scrudful<Item>Page``, as ninja's ``@paginate`` does. Called when
    ninja builds the operation.

    Raises
    ------
    ninja.errors.ConfigError
        If the operation declares no collection response, or one that is not
        a list, set or tuple of one item type (e.g. ``Optional[List[ItemOut]]``
        or a bare ``list``).
    """
    for code, model in operation.response_models.items():
        if model is None or model is NOT_SET:
            continue
        annotation = model.model_fields["response"].annotation
        if is_collection_type(annotation):
            page = _page_model(_collection_item(annotation, operation))
            operation.response_models[code] = operation._create_response_model(page)
            return _CollectionResponse(code, frozenset(operation.response_models))
    raise ConfigError(
        f"{operation.view_func.__qualname__}() has no collection response "
        "(e.g. response=List[ItemOut]), which @scrudful_list requires"
    )


def _collection_item(annotation: Any, operation: Any) -> Any:
    """The item type of the collection response ``annotation``.

    Raises
    ------
    ninja.errors.ConfigError
        If ``annotation`` is a union (the envelope is always sent, so it is
        never ``None``) or names no item type.
    """
    name = operation.view_func.__qualname__
    if get_origin(annotation) in (Union, types.UnionType):
        raise ConfigError(
            f"{name}(): the collection response {_type_name(annotation)} must "
            "be a list, set or tuple of the item schema, e.g. "
            "response=List[ItemOut]: @scrudful_list always sends the envelope, "
            "never None or another type"
        )
    args = get_args(annotation)
    if not args or args[0] is Ellipsis:
        raise ConfigError(
            f"{name}(): the collection response {_type_name(annotation)} names "
            "no item type, which @scrudful_list needs for the envelope's "
            "entries: declare e.g. response=List[ItemOut]"
        )
    return args[0]


def _type_name(annotation: Any) -> str:
    """``annotation`` as written, without module prefixes."""
    if isinstance(annotation, type) and not get_args(annotation):
        return annotation.__name__
    return re.sub(r"\b(?:[A-Za-z_]\w*\.)+(?=[A-Za-z_])", "", repr(annotation))


@functools.cache
def _page_model(item: Any) -> type[Schema]:
    """The schema of a page of ``item`` (cached, so one per item schema).

    Named ``Scrudful<Item>Page``, with entries ``Scrudful<Item>Envelope``.
    Every field is required, as in :func:`scrudful.collection.collection_schema`.
    """
    name = getattr(item, "__name__", None) or re.sub(r"\W+", "_", str(item))
    envelope = create_model(  # type: ignore[call-overload]
        f"Scrudful{name}Envelope",
        __base__=Schema,
        **dict(
            zip(
                ENVELOPE_FIELDS,
                [(str, ...), (str | None, ...), (str | None, ...)]
                + [(str | None, ...), (item, ...)],
            )
        ),
    )
    return create_model(  # type: ignore[call-overload,no-any-return]
        f"Scrudful{name}Page",
        __base__=Schema,
        **dict(
            zip(
                PAGE_FIELDS,
                [(int, ...), (int, ...), (str, ...), (str | None, ...)]
                + [(str | None, ...), (str, ...), (list[envelope], ...)],
            )
        ),
    )


def _pagination_model(config: _Config) -> type[Schema]:
    """The schema of a list operation's query parameters."""
    return create_model(  # type: ignore[call-overload,no-any-return]
        "ScrudfulPagination",
        __base__=Schema,
        page=(int, Field(1, ge=1)),
        page_size=(int, Field(config.page_size, ge=1, le=config.max_page_size)),
    )


def _page(
    config: _Config, request: HttpRequest, queryset: Any, pagination: Any
) -> _Flow[dict[str, Any]]:
    """The envelope of the requested page of ``queryset``.

    Raises
    ------
    ninja.errors.HttpError
        404 for a page past the last one (page 1 always exists).
    """
    number = 1 if pagination is None else pagination.page
    size = config.page_size if pagination is None else pagination.page_size
    # Counted and sliced in one call: in an async operation, in a thread.
    count, items = yield _slice, (queryset, number, size), {}
    entries = []
    for item in items:
        href = yield config.href_func, (request, item), {}
        if href is None:
            raise TypeError(
                f"href_func returned None for {item!r}: it must return the item's URL"
            )
        etag, last_modified = yield from _item_validators(config, item)
        link = yield from _links(config.entity, request, item)
        entries.append(
            envelope_entry(
                href=iri_to_uri(str(href)),
                etag=etag,
                last_modified=(
                    None if last_modified is None else format_http_date(last_modified)
                ),
                link=link,
                content=item,
            )
        )
    return page_envelope(
        count=count,
        number=number,
        size=size,
        page_url=functools.partial(page_url, request.build_absolute_uri(), request.GET),
        entries=entries,
    )


def _has_async_item_callables(config: _Config) -> bool:
    """Whether a callable :func:`_page` calls for each item is async."""
    item_callables = [config.href_func, config.etag_func, config.last_modified_func]
    item_callables += [
        getattr(config.entity, entity_field.name) for entity_field in fields(Entity)
    ]
    return any(_is_async_callable(func) for func in item_callables)


def _slice(queryset: Any, number: int, size: int) -> tuple[int, list[Any]]:
    """:func:`~scrudful.collection.slice_page`, with ninja's 404 for a bad page."""
    try:
        return slice_page(queryset, number, size)
    except InvalidPage:
        raise HttpError(404, "Invalid page.") from None


def _with_body(content: Any, body: Any) -> Any:
    """The handler's return value ``content`` with ``body`` in place of its own."""
    if isinstance(content, Status):
        return Status(content.status_code, body)
    if isinstance(content, tuple) and len(content) == 2:
        return content[0], body
    return body


def _body(content: Any) -> Any:
    """The body the handler returned, or ``_UNSET`` for an ``HttpResponse``."""
    if isinstance(content, HttpResponseBase):
        return _UNSET
    if isinstance(content, Status):
        return content.value
    if isinstance(content, tuple) and len(content) == 2:
        return content[1]
    return content


def _carries_body(content: Any) -> bool:
    """Whether the response of the handler's return value ``content`` has a body.

    An ``HttpResponse`` with non-empty content or a streaming one, else a
    body other than ``None``.
    """
    if isinstance(content, HttpResponseBase):
        return getattr(content, "streaming", False) or bool(
            getattr(content, "content", b"")
        )
    return _body(content) is not None


def _created_headers(
    config: _Config, headers: HttpResponseBase, request: HttpRequest, obj: Any
) -> _Flow[None]:
    """Set the Location and validators of the created object ``obj``."""
    if "Location" not in headers:
        location = yield config.location_func, (request, obj), {}
        if location is not None:
            headers["Location"] = iri_to_uri(str(location))
    etag, last_modified = yield from _item_validators(config, obj)
    set_validator_headers(headers, etag, last_modified)


def _run_sync(flow: _Flow[T], *, in_thread: bool = False) -> T:
    """Run ``flow``, calling the callables it yields directly.

    The decorator already rejected the declared ``async def`` callables; this
    catches those set at request time (``ScrudfulResponse.entity``).
    ``in_thread``: see :func:`_run_in_thread`.
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
        if in_thread and inspect.isawaitable(result):
            result = async_to_sync(_await)(result)


def _run_in_thread(flow: _Flow[T]) -> T:
    """Run ``flow``, of sync callables only, in one call of an async operation.

    :func:`_run_async` runs this call in one ``sync_to_async`` thread, instead
    of one thread per callable. As in :func:`_call_async`, an awaitable a sync
    callable returns is awaited, on the event loop (``async_to_sync``).
    """
    return _run_sync(flow, in_thread=True)


async def _await(awaitable: Any) -> Any:
    return await awaitable


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
    yield "before", config.before
    yield "after_throttles", config.after_throttles
    yield "location_func", config.location_func
    yield "href_func", config.href_func
    yield "etag_func", config.etag_func
    yield "last_modified_func", config.last_modified_func
    yield "list_etag_func", config.list_etag_func
    yield "list_last_modified_func", config.list_last_modified_func
    entities: list[tuple[str, Entity | None]] = [
        ("", config.entity),
        ("list_", config.collection),
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


def _check_error(error: Any) -> None:
    """:func:`_check_entity` for ``error``, whose media type must be JSON."""
    _check_entity("error", error)
    if error is not None:
        check_error_media_type(error.media_type)


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


def _item_validators(config: _Config, obj: Any) -> _Flow[tuple[str | None, int | None]]:
    """The validators of the item ``obj``, see :func:`_validators`."""
    return (yield from _validators(config.etag_func, config.last_modified_func, obj))


def _validators(
    etag_func: Callable[[Any], Any] | None,
    last_modified_func: Callable[[Any], Any] | None,
    obj: Any,
) -> _Flow[tuple[str | None, int | None]]:
    """The validators of ``obj``; none when there is no representation."""
    etag = last_modified = None
    if obj is None:
        return etag, last_modified
    if etag_func is not None:
        value = yield etag_func, (obj,), {}
        etag = None if value is None else quote_etag(value)
    if last_modified_func is not None:
        value = yield last_modified_func, (obj,), {}
        last_modified = http_timestamp(aware(value))
    return etag, last_modified


def _has_callables(entity: Entity | None) -> bool:
    """Whether a value of ``entity`` is a callable (called with the object)."""
    return entity is not None and any(
        callable(getattr(entity, entity_field.name)) for entity_field in fields(Entity)
    )


def _add_links(
    headers: HttpResponseBase, entity: Entity, request: HttpRequest, obj: Any
) -> _Flow[None]:
    """Append the Links of ``entity`` to the Link header of ``headers``.

    A Link the handler set with the relation of the schema link replaces
    that link, and one with either context relation (JSON-LD or
    ``rdf-context``, one slot) the context link; one with another relation
    (e.g. ``next``) replaces nothing (see
    :func:`scrudful._adapter.merge_links`).
    """
    schema, context = yield from _entity_links(entity, request, obj)
    link = merge_links(headers.get("Link"), schema, context)
    if link is not None:
        headers["Link"] = link


def _links(entity: Entity, request: HttpRequest, obj: Any) -> _Flow[str | None]:
    """The Link field value of the schema and context links of ``entity``."""
    return join_links(*(yield from _entity_links(entity, request, obj)))


def _entity_links(
    entity: Entity, request: HttpRequest, obj: Any
) -> _Flow[tuple[str | None, str | None]]:
    """The schema and the context link-values of ``entity`` (``None`` if unset)."""
    schema = yield from _link(
        request,
        obj,
        (
            entity.schema_link_or_func,
            entity.schema_rel_or_func,
            entity.schema_type_or_func,
        ),
        (SCHEMA_REL, SCHEMA_TYPE),
    )
    context = None
    defaults = context_defaults(entity.media_type)
    if defaults != (None, None):
        context = yield from _link(
            request,
            obj,
            (
                entity.context_link_or_func,
                entity.context_rel_or_func,
                entity.context_type_or_func,
            ),
            defaults,
            context_of=entity.media_type,
        )
    return schema, context


def _value(value_or_func: Any, request: HttpRequest, obj: Any) -> _Flow[Any]:
    """``value_or_func``, or what it returns when called with ``(request, obj)``."""
    if callable(value_or_func):
        return (yield value_or_func, (request, obj), {})
    return value_or_func


def _link(
    request: HttpRequest,
    obj: Any,
    values_or_funcs: tuple[Any, Any, Any],
    defaults: tuple[str | None, str | None],
    context_of: str | None = None,
) -> _Flow[str | None]:
    """Build one link-value from (url, rel, type) values or callables.

    The URL is resolved against the request's absolute URI, as in the OPTIONS
    body (see :func:`_media`). The context link of ``context_of`` content
    takes its type from :func:`scrudful._adapter.context_link_type`, as the
    OPTIONS body does.
    """
    url_or_func, rel_or_func, type_or_func = values_or_funcs
    url = yield from _value(url_or_func, request, obj)
    if not url:
        return None
    rel = (yield from _value(rel_or_func, request, obj)) or defaults[0]
    content_type = yield from _value(type_or_func, request, obj)
    if context_of is not None:
        content_type = context_link_type(context_of, content_type)
    content_type = content_type or defaults[1]
    return link_content(
        resolve_uri(url, request.build_absolute_uri()), rel, content_type
    )


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


def _declared_status(operation: Any) -> int | None:
    """The operation's single declared response code, if it has one.

    ninja uses it as the status of a plain return value (200 otherwise).
    """
    codes = list(operation.response_models)
    return codes[0] if len(codes) == 1 and isinstance(codes[0], int) else None


class _Answers(NamedTuple):
    """What an operation declares in ``response=``, for its request flow."""

    #: The status of a plain return value, see :func:`_declared_status`.
    declared: int | None = None
    #: A list operation's collection response.
    collection: _CollectionResponse | None = None


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
    # Each annotation evaluated on its own: one that can't be (e.g. the
    # hidden parameter's, imported only under TYPE_CHECKING) stays a string
    # and the others reach ninja evaluated.
    signature = resolved_signature(handler)
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


# --- OPTIONS -------------------------------------------------------------------

#: The statuses of scrudful's own responses with a body (``error``'s entity).
_EARLY_STATUSES = frozenset({400, 412, 428})


def _is_automatic_options(operation: Any) -> bool:
    return getattr(operation.view_func, "_scrudful_options", False)


def _path_view(router: Router, view_func: Callable[..., Any]) -> Any:
    """The ``PathView`` of ``router`` holding the operation just added.

    Found by the operation ninja sets as ``view_func._ninja_operation``, so it
    does not depend on how ninja normalizes the path key.
    """
    operation = getattr(view_func, "_ninja_operation", None)
    for path_view in router.path_operations.values():
        if any(candidate is operation for candidate in path_view.operations):
            return path_view
    raise LookupError(f"{view_func!r} is not an operation of {router!r}")


def _options_view(path: str, path_view: Any) -> Callable[..., HttpResponseBase]:
    """The view of the automatic OPTIONS operation of ``path``.

    It reads ``path_view``, the router's own (template) ``PathView``, when a
    request comes, so it describes the operations of the path as declared,
    whichever API, prefix or mount the request came through (ninja serves
    clones of them). It declares the path parameters, as ``Any`` (a
    converter such as ``{int:pk}`` passes an ``int``), so that ninja passes
    them and validates nothing. The response carries the HTTP Schema
    Resource's own Links and an ``Allow`` listing the body's methods. The
    body is written by the serving API's renderer (see :func:`_render`), and
    is ``application/json``.
    """

    def scrudful_options(request: HttpRequest, **params: Any) -> HttpResponseBase:
        body = _describe_path(request, path_view)
        response = json_response(body, 200, "application/json", _render(request))
        response["Link"] = options_links()
        response["Allow"] = allow_header(body)
        expose_headers(response)
        return response

    parameters = [
        inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=Any)
        for name in sorted(get_path_param_names(path))
    ]
    scrudful_options.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            *parameters,
        ]
    )
    scrudful_options._scrudful_options = True  # type: ignore[attr-defined]
    # The API's renderer writes the body (see _render).
    scrudful_options._ninja_contribute_to_operation = [  # type: ignore[attr-defined]
        _record_serving_api
    ]
    return scrudful_options


def _describe_path(request: HttpRequest, path_view: Any) -> dict[str, Any]:
    """The HTTP Schema Resource of the path: each method it serves.

    In the order of :data:`~scrudful._adapter.METHOD_KEYS`, ``options`` (this
    operation) among them, so ``Allow`` follows it too.
    """
    operations = [
        operation
        for operation in path_view.operations
        if not _is_automatic_options(operation)
    ]
    body = {}
    for method in METHOD_KEYS:
        if method == "options":
            body[method] = describe_options()
            continue
        # The operation ninja routes the method to: the first that lists it.
        operation = next(
            (op for op in operations if method.upper() in op.methods), None
        )
        if operation is None:
            continue
        flow = _describe_operation(method, operation, request)
        if getattr(operation, "is_async", False):
            # Its link callables may be async def; this view is sync, and
            # ninja runs it in a thread (sync_to_async) on an async path.
            body[method] = async_to_sync(_run_async)(flow)
        else:
            body[method] = _run_sync(flow)
    return body


def _describe_operation(
    method: str, operation: Any, request: HttpRequest
) -> _Flow[dict[str, Any]]:
    """Describe ``operation`` as ``method``; each link callable is a ``yield``.

    The description of :func:`scrudful._adapter.describe_read` /
    :func:`~scrudful._adapter.describe_write` (DRF's), with the statuses the
    operation answers (see :func:`_answered_statuses`) and the response
    content of each status's entity, but for HEAD, whose responses have no
    content. Link callables are called with ``(request, None)``.
    """
    config = getattr(operation.view_func, "_scrudful", None)
    if not isinstance(config, _Config):
        config = None
    read = method in ("get", "head")
    conditional = config is not None and _is_conditional(config, method)
    statuses = _answered_statuses(operation, config)
    if read:
        description = describe_read(None, conditional, statuses)
    elif method == "trace":
        # Neither a read nor one of the writes: its statuses only, without
        # scrudful's early responses or a request body.
        description = describe_read(None, False, statuses)
    else:
        content = None
        if config is not None and method != "delete":
            # As DRF's: the item's unless declared (a create's is the item's).
            entity = config.entity if config.request is None else config.request
            content = yield from _media(entity, request)
        description = describe_write(
            method,
            content,
            conditional,
            config is not None and config.has_etag,
            config is not None and config.has_last_modified,
            statuses,
        )

    responses = description["responses"]
    if config is not None and method != "head":
        for key, response in responses.items():
            if key in ("204", "304"):
                continue  # no content
            entity = _described_entity(config, method, int(key), conditional)
            content = yield from _media(entity, request)
            if content:
                response["content"] = content
    return description


def _is_conditional(config: _Config, method: str) -> bool:
    """Whether the operation evaluates the If-* headers for ``method``.

    A list's (after the handler, never required) only matter for GET/HEAD; a
    create never evaluates them. ``method`` is lowercase.
    """
    if config.kind == "list":
        return method in ("get", "head") and config.has_list_validators
    return config.evaluates_preconditions and evaluates_preconditions(method.upper())


def _answered_statuses(operation: Any, config: _Config | None) -> list[int]:
    """The statuses ``operation`` answers with, for OPTIONS.

    Those it declares in ``response=`` (with a 201 first for a create, whose
    plain return value is a 201), else ninja's default, 200.
    """
    declared = [
        code
        for code, model in operation.response_models.items()
        if isinstance(code, int) and not isinstance(code, bool) and model is not NOT_SET
    ]
    if config is not None and config.kind == "create":
        return [201, *(code for code in declared if code != 201)]
    return declared or [200]


def _described_entity(
    config: _Config, method: str, status: int, conditional: bool
) -> Any:
    """The entity of the ``method`` responses with ``status``, for OPTIONS.

    scrudful's own early responses (listed when ``conditional``) get the
    ``error`` entity, if declared; the others the declared entity.
    """
    if conditional and status in _EARLY_STATUSES:
        return (
            config.error if config.error is not None else config.responses.get(status)
        )
    return config.response_entity(method, status)


def _media(entity: Entity | None, request: HttpRequest) -> _Flow[Any]:
    """The ``content`` of ``entity``: its Media Type Object, or ``None``.

    Built by :func:`scrudful._adapter.describe_content` from the entity's
    schema and context URLs and types, which are evaluated as for its Links
    (the context only when its media type takes one), and with the URLs
    resolved the same way, so that the entry agrees with the Links of the
    responses it describes. ``None`` without a schema URL (e.g. ``Entity()``).
    """
    if entity is None:
        return None
    schema = yield from _value(entity.schema_link_or_func, request, None)
    if not schema:
        return None
    schema_type = yield from _value(entity.schema_type_or_func, request, None)
    context = context_type = None
    rel, default_type = context_defaults(entity.media_type)
    if rel is not None:  # not in-band RDF
        context = yield from _value(entity.context_link_or_func, request, None)
    if context:
        context_type = (
            yield from _value(entity.context_type_or_func, request, None)
        ) or default_type
    base = request.build_absolute_uri()
    return describe_content(
        entity.media_type,
        resolve_uri(schema, base),
        str(schema_type or SCHEMA_TYPE),
        resolve_uri(context, base) if context else None,
        None if context_type is None else str(context_type),
    )
