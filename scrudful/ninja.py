"""SCRUDful conditional requests and Link headers for django-ninja operations.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to django-ninja's operations. Validators and
precondition results are computed only from the object the operation's
``load`` callable returns, i.e. from what authorization already approved (for
a create, from the object the handler created).

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
    default entity, for 2xx responses other than 204 (which has no body);
    else none. Link callables are
    called with the request and the loaded object (after a PUT/PATCH, the one
    loaded again), or ``None`` without ``load``.

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
    A returned ``HttpResponse`` is not an object: set ``created`` to get the
    Location and validators. A 201 response with a created object gets:

    - ``Location``: ``location_func(request, obj)``, not called when the
      response already has one (from the handler or ``ScrudfulResponse``);
    - ``ETag`` and ``Last-Modified``: from ``etag_func`` and
      ``last_modified_func`` applied to the created object. Pass the
      callbacks of the item operations, so the 201 carries the validators a
      GET of the new item sends and a client can edit it without a GET;
    - the item entity's Links (``responses[201]``, if declared, replaces it);
      link callables get the created object.

    Any other status (e.g. a ``Status(400, ...)``) gets none of these, only
    the Links of ``responses[status]``; their link callables get
    ``ScrudfulResponse.created``, or ``None`` if unset. The values set on
    ``ScrudfulResponse`` and headers the handler set win, as below.

Collections
    :func:`scrudful_list` decorates a GET on a collection. It works like
    ninja's ``@paginate`` and is the flow above with a preset: there is no
    ``load``, and the handler returns the authorized queryset (or a sequence,
    possibly in a ``ninja.Status``). The operation must declare a collection
    response, e.g. ``response=List[ItemOut]`` (``ninja.errors.ConfigError``
    otherwise, when the operation is built). It is replaced by a generated
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
      ninja, a page past the last one with 404 (page 1 always exists).
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
    an ``HttpResponse`` is sent as the handler returned it. In an async
    operation the queryset is counted and sliced through ``sync_to_async``,
    never on the event loop, so the handler may return it unevaluated.

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

    The body is the semantic-http spec's "HTTP Schema Resource": for each of
    ``get``, ``head`` (when an operation routes it, described like ``get``),
    ``post``, ``put``, ``patch`` and ``delete`` that the path serves, an
    OpenAPI-like operation, in the shape DRF's ``ScrudfulMetadata`` produces
    (``scrudful.rest_framework``), which it extends:

    - ``requestBody`` (POST, PUT, PATCH) with the ``content`` of the
      ``request`` entity, else of the item entity, as in DRF;
    - ``responses``: DRF's success status (200, 201 for POST, 204 for
      DELETE) and, when the operation evaluates the If-* headers, the early
      responses (304, 400 and 412 for GET/HEAD; 400, 412 and 428 for writes,
      only 400 and 412 for POST), plus every status the operation declares
      in ``response=``, with its reason phrase. Each status with a body gets
      the ``content`` of its entity: scrudful's own 400, 412 and 428 get
      ``error``'s, if declared, the others ``responses[status]``, else the
      default entity (2xx, 201 for a create, the collection for a list);
    - ``parameters``: the precondition headers of a conditional write.

    A ``content`` is ``{"application/json": {"schema": <url>, "context":
    <url>}}``, with the entity's links; an entity without links (such as
    ``Entity()``) has none. Link callables are called with the request and
    ``obj=None`` (there is no loaded object), so they must accept ``None``;
    the OPTIONS view is sync, so the callables of an async
    operation are run with ``asgiref.sync.async_to_sync``, as the operation
    runs them (ninja runs the view in a thread on a path with async
    operations). Methods whose operation is not scrudful get no content and
    no preconditions, as in DRF.

    The response is JSON, with the Links of the ``options_schema_link`` and
    ``options_context_link`` of :class:`ScrudfulRouter` (the HTTP Schema
    Resource's own schema and context). Without them it has no Link, which
    the spec does not allow. The body follows the spec's 2019 draft, which
    publishes no resolvable URLs for these two documents: point them at
    copies the application serves. Alignment with the spec's 0.1 revision is
    planned.

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
import http
import inspect
import math
import re
import warnings
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, NamedTuple, TypeVar, get_args
from urllib.parse import urlencode, urlsplit, urlunsplit

from asgiref.sync import async_to_sync, sync_to_async
from django.core.paginator import UnorderedObjectListWarning
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.utils.encoding import iri_to_uri
from ninja import Query, Router, Schema, Status
from ninja.constants import NOT_SET
from ninja.errors import ConfigError, HttpError
from ninja.signature.details import is_collection_type
from ninja.signature.utils import get_path_param_names
from pydantic import Field, create_model

from scrudful import _adapter
from scrudful._adapter import (
    BODY_HEADERS,
    DEFAULT_CONTEXT_REL,
    DEFAULT_CONTEXT_TYPE,
    DEFAULT_SCHEMA_REL,
    DEFAULT_SCHEMA_TYPE,
    check_validators,
    describe_read,
    describe_write,
    expose_headers,
    resolve_annotation,
    resolved_signature,
    set_validator_headers,
    validator_policy,
)
from scrudful._django import aware, conditional_response, precondition_headers
from scrudful.collection import ENVELOPE_FIELDS, PAGE_FIELDS
from scrudful.conditional import (
    ConditionalResult,
    evaluate_preconditions,
    format_http_date,
    http_timestamp,
    quote_etag,
)
from scrudful.utils import join_links, link_content, merge_expose_headers

__all__ = [
    "CONDITIONAL_READ_RESPONSES",
    "CONDITIONAL_WRITE_RESPONSES",
    "Entity",
    "ScrudfulResponse",
    "ScrudfulRouter",
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
        Location and validators a 201 gets. Unset by default: the handler's
        return value is used (the value of a returned ``Status``). Ignored by
        :func:`scrudful`.
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

    The six ``schema_*`` / ``context_*`` fields are the item entity, also
    available as :attr:`entity`: the default entity of 2xx responses but 204
    for an item operation, of 201 responses (the created item) for a create
    operation, and the ``link`` of each entry of a list operation's envelope,
    whose responses get :attr:`collection`.
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
    #: The request body's entity (described by OPTIONS; no header).
    request: Entity | None = None
    #: The response entity of each status, overriding :attr:`entity`.
    responses: Mapping[int, Entity] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: The entity of scrudful's own 400, 412 and 428 bodies.
    error: Entity | None = None
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

    def has_default_entity(self, status: int) -> bool:
        """Whether a response with ``status`` gets the default entity.

        Not a 204: it has no body for the entity to describe.
        """
        if self.kind == "create":
            return status == 201
        return 200 <= status < 300 and status != 204

    @property
    def default_entity(self) -> Entity | None:
        """The entity of the responses :meth:`has_default_entity` selects."""
        return self.collection if self.kind == "list" else self.entity

    def response_entity(self, status: int) -> Entity | None:
        """The declared entity of a response with ``status``."""
        if status in self.responses:
            return self.responses[status]
        return self.default_entity if self.has_default_entity(status) else None

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
        Required when a validator callback is set.
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
        ``load_as`` parameter, if the handler is a generator or already a
        ninja operation (the decorator sits above the operation decorator),
        or if the handler is sync and ``load``, a validator callback or a
        link callable (also inside an entity) is ``async def``.
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
        :func:`scrudful`. Link callables get the created object.
    schema_rel_or_func : str or callable, optional
        ``rel`` of the schema link. Defaults to ``"describedBy"``.
    schema_type_or_func : str or callable, optional
        ``type`` of the schema link. Defaults to ``"application/json"``.
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
        ``Status(400, ...)`` the handler returns. ``responses[201]`` replaces
        the item entity's Links on 201 responses. There is no ``error``
        argument: a create produces no 400, 412 or 428 of its own.

    Raises
    ------
    TypeError
        If ``location_func`` or a validator callback isn't callable, if
        ``request`` or a value of ``responses`` isn't an :class:`Entity` or a
        key of ``responses`` isn't an HTTP status code, if the handler is a
        generator or already a ninja operation (the decorator sits above the
        operation decorator), or if the handler is sync and
        ``location_func``, a validator callback or a link callable (also
        inside an entity) is ``async def``.
    """
    if not callable(location_func):
        raise TypeError(
            f"location_func must be callable, not {type(location_func).__name__}"
        )
    check_validators(etag_func, last_modified_func)
    _check_entity("request", request)
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
    )
    return _decorator(config, "@scrudful_create")


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
) -> Callable[[Handler], Handler]:
    """Decorate a django-ninja operation listing a collection (GET on a list).

    ``@scrudful_list(...)`` must sit *below* the operation decorator, which
    must declare a collection response (as for ninja's ``@paginate``)::

        @router.get("/items", response=List[ItemOut])
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
        ``rel`` of the item schema link. Defaults to ``"describedBy"``.
    schema_type_or_func : str or callable, optional
        ``type`` of the item schema link. Defaults to ``"application/json"``.
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
        queryset.

    Raises
    ------
    TypeError
        If ``href_func`` or a validator callback isn't callable, if
        ``page_size`` or ``max_page_size`` isn't an ``int``, if ``error``
        isn't an :class:`Entity`, if the handler is a generator or already a
        ninja operation (the decorator sits above the operation decorator),
        or if the handler is sync and ``href_func``, a validator callback or
        a link callable (also inside ``error``) is ``async def``.
    ValueError
        If ``page_size`` is less than 1 or ``max_page_size`` less than
        ``page_size``.
    ninja.errors.ConfigError
        When the operation is built, if it declares no collection response
        (e.g. ``response=List[ItemOut]``).
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
            f"max_page_size ({max_page_size}) must be at least page_size "
            f"({page_size})"
        )
    _check_entity("error", error)
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
        items = ScrudfulRouter(
            options_schema_link=(
                "https://example.com/schemas/http-schema-resource.json"
            ),
            options_context_link=(
                "https://example.com/contexts/http-schema-resource.jsonld"
            ),
        )
        api.add_router("/items", items)

    The semantic-http spec's 2019 draft, whose shape the OPTIONS body
    follows (as DRF's ``ScrudfulMetadata``), publishes no resolvable URLs
    for the HTTP Schema Resource's schema and context: point
    ``options_schema_link`` and ``options_context_link`` at copies the
    application serves (the URLs above are placeholders). Alignment with the
    spec's 0.1 revision is planned.

    CORS preflight requests (an OPTIONS with ``Access-Control-Request-Method``)
    are sent by browsers without credentials and are meant to be answered by
    the CORS middleware (e.g. django-cors-headers) before the view runs.
    scrudful's OPTIONS is the other, non-preflight, case: a request that does
    reach it is answered with the description like any other OPTIONS.

    Parameters
    ----------
    options_schema_link : str, optional
        URL of the JSON Schema of the HTTP Schema Resource, sent as the
        OPTIONS response's schema Link (``rel="describedBy"``,
        ``type="application/json"``). Omitted when unset, which the
        semantic-http spec does not allow.
    options_context_link : str, optional
        URL of the JSON-LD context of the HTTP Schema Resource, sent as the
        OPTIONS response's context Link
        (``rel="http://www.w3.org/ns/json-ld#context"``,
        ``type="application/ld+json"``). Omitted when unset.

    The other arguments are ``ninja.Router``'s.
    """

    def __init__(
        self,
        *args: Any,
        options_schema_link: str | None = None,
        options_context_link: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.options_schema_link = options_schema_link
        self.options_context_link = options_context_link

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
                _options_view(self, path, path_view),
                auth=None,
                response=NOT_SET,
                include_in_schema=False,
            )


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
        # The single response code each method's operation declares, if any.
        declared: dict[str, int | None] = {}
        # A list operation's collection response, per method.
        collections: dict[str, _CollectionResponse] = {}

        def flow(request: HttpRequest, values: dict[str, Any]) -> _Flow[Any]:
            """The request flow; each call of a user callable is a ``yield``."""
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

            if config.evaluates_preconditions:
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
            headers, status = _response_and_status(
                content, response, declared.get(method)
            )
            list_etag = list_last_modified = None
            if (
                config.kind == "list"
                and not isinstance(content, HttpResponseBase)
                and method in collections
                and collections[method].includes(status)
            ):
                # The queryset is the authorized object: the collection's
                # preconditions are evaluated now, then the page is built.
                obj = _body(content)
                if config.has_list_validators:
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
                page = yield from _page(config, request, obj, pagination)
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
                if created is not _UNSET:
                    obj = created
                    if status == 201:
                        yield from _created_headers(config, headers, request, obj)
            elif config.kind == "list":
                # None unless the collection's validators were computed.
                if validator_policy(method, status)[1]:
                    set_validator_headers(headers, list_etag, list_last_modified)
            elif config.has_validators:
                refresh, send = validator_policy(method, status)
                if refresh:
                    obj = yield load, (request,), params
                    etag, last_modified = yield from _item_validators(config, obj)
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
        if config.kind == "list":
            wrapper._ninja_contribute_to_operation.append(
                functools.partial(_envelope_response, collections)
            )
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
    callables get ``obj``.
    """
    early = conditional_response(result)
    if result.body is not None and config.error is not None:
        yield from _add_links(early, config.error, request, obj)
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


class _CollectionResponse(NamedTuple):
    """The response of a list operation that ninja validates as the envelope."""

    #: The code whose response model is the envelope (or ``...``).
    code: Any
    #: Every code the operation declares.
    codes: frozenset[Any]

    def includes(self, status: int) -> bool:
        """Whether ninja validates a response with ``status`` as the envelope."""
        return status == self.code if status in self.codes else self.code is ...


def _envelope_response(
    collections: dict[str, _CollectionResponse], operation: Any
) -> None:
    """Replace the operation's collection response by the envelope.

    The first response declared as a collection, e.g. ``List[ItemOut]``,
    becomes ``Scrudful<Item>Page``, as ninja's ``@paginate`` does. Called by
    ninja at the end of ``Operation.__init__``.

    Raises
    ------
    ninja.errors.ConfigError
        If the operation declares no collection response.
    """
    for code, model in operation.response_models.items():
        if model is None or model is NOT_SET:
            continue
        annotation = model.model_fields["response"].annotation
        if is_collection_type(annotation):
            page = _page_model(get_args(annotation)[0])
            operation.response_models[code] = operation._create_response_model(page)
            for method in operation.methods:
                collections[method.upper()] = _CollectionResponse(
                    code, frozenset(operation.response_models)
                )
            return
    raise ConfigError(
        f"{operation.view_func.__qualname__}() has no collection response "
        "(e.g. response=List[ItemOut]), which @scrudful_list requires"
    )


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
    page_count = math.ceil(count / size)
    entries = []
    for item in items:
        href = yield config.href_func, (request, item), {}
        if href is None:
            raise TypeError(
                f"href_func returned None for {item!r}: it must return the "
                "item's URL"
            )
        etag, last_modified = yield from _item_validators(config, item)
        link = yield from _links(config.entity, request, item)
        values = (
            iri_to_uri(str(href)),
            etag,
            None if last_modified is None else format_http_date(last_modified),
            link,
            item,
        )
        entries.append(dict(zip(ENVELOPE_FIELDS, values)))
    url = functools.partial(_page_url, request.build_absolute_uri(), request.GET)
    values = (
        count,
        page_count,
        url(1),
        url(number - 1) if number > 1 else None,
        url(number + 1) if number < page_count else None,
        url(max(page_count, 1)),
        entries,
    )
    return dict(zip(PAGE_FIELDS, values))


def _slice(queryset: Any, number: int, size: int) -> tuple[int, list[Any]]:
    """The item count of ``queryset`` and the items of page ``number``.

    ``count()`` for a ``QuerySet``, else ``len()``. An unordered ``QuerySet``
    gets Django's ``UnorderedObjectListWarning``, as with its ``Paginator``.
    """
    if isinstance(queryset, QuerySet):
        if not queryset.ordered:
            warnings.warn(
                "Pagination may yield inconsistent results with an unordered "
                f"object_list: {queryset.model} {queryset.__class__.__name__}.",
                UnorderedObjectListWarning,
                stacklevel=2,
            )
        count = queryset.count()
    else:
        count = len(queryset)
    if number > max(math.ceil(count / size), 1):
        raise HttpError(404, "Invalid page.")
    start = (number - 1) * size
    return count, list(queryset[start : start + size])


def _page_url(url: str, query: Any, number: int) -> str:
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

#: The methods an OPTIONS body describes, in this order, when the path serves
#: them.
_DESCRIBED_METHODS = ("get", "head", "post", "put", "patch", "delete")
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


def _options_view(
    router: ScrudfulRouter, path: str, path_view: Any
) -> Callable[..., HttpResponseBase]:
    """The view of the automatic OPTIONS operation of ``path``.

    It reads ``path_view``, the router's own (template) ``PathView``, when a
    request comes, so it describes the operations of the path as declared,
    whichever API, prefix or mount the request came through (ninja serves
    clones of them). It declares the path parameters, as ``Any`` (a
    converter such as ``{int:pk}`` passes an ``int``), so that ninja passes
    them and validates nothing.
    """

    def scrudful_options(request: HttpRequest, **params: Any) -> HttpResponseBase:
        body = _describe_path(request, path_view)
        response = JsonResponse(body)
        link = join_links(
            _options_link(
                router.options_schema_link, DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE
            ),
            _options_link(
                router.options_context_link, DEFAULT_CONTEXT_REL, DEFAULT_CONTEXT_TYPE
            ),
        )
        if link is not None:
            response["Link"] = link
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
    return scrudful_options


def _options_link(url: str | None, rel: str, content_type: str) -> str | None:
    return link_content(url, rel, content_type) if url else None


def _describe_path(request: HttpRequest, path_view: Any) -> dict[str, Any]:
    """The HTTP Schema Resource of the path: each method it serves."""
    operations = [
        operation
        for operation in path_view.operations
        if not _is_automatic_options(operation)
    ]
    body = {}
    for method in _DESCRIBED_METHODS:
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
    :func:`~scrudful._adapter.describe_write` (DRF's), with the response
    content of each status's entity and the statuses the operation declares.
    Link callables are called with ``(request, None)``.
    """
    config = getattr(operation.view_func, "_scrudful", None)
    if not isinstance(config, _Config):
        config = None
    read = method in ("get", "head")
    # Whether the If-* headers are evaluated: a list's (after the handler,
    # never required) only matter for GET/HEAD; a create never evaluates them.
    conditional = config is not None and (
        read and config.has_list_validators
        if config.kind == "list"
        else config.evaluates_preconditions
    )
    content = None
    described = set()  # the statuses whose content is known
    if read:
        if config is not None:
            entity = _described_entity(config, 200, conditional)
            content = yield from _media(entity, request)
            described.add("200")
        description = describe_read(content, conditional)
    else:
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
        )

    responses = description["responses"]
    for code, model in operation.response_models.items():
        if (
            isinstance(code, int)
            and not isinstance(code, bool)
            and model is not NOT_SET
        ):
            responses.setdefault(str(code), {"description": _phrase(code)})
    if config is not None:
        for key, response in responses.items():
            if key in described or key in ("204", "304"):
                continue  # no content (204, 304), or already known
            entity = _described_entity(config, int(key), conditional)
            content = yield from _media(entity, request)
            if content:
                response["content"] = content
    return description


def _described_entity(config: _Config, status: int, conditional: bool) -> Any:
    """The entity of the responses with ``status``, for OPTIONS.

    scrudful's own early responses (listed when ``conditional``) get the
    ``error`` entity, if declared; the others the declared entity.
    """
    if conditional and status in _EARLY_STATUSES:
        return (
            config.error if config.error is not None else config.responses.get(status)
        )
    return config.response_entity(status)


def _media(entity: Entity | None, request: HttpRequest) -> _Flow[Any]:
    """The ``content`` of ``entity``: ``{"application/json": {...}}``, or ``None``.

    The values are the schema and context URLs (as DRF's metadata has them);
    an entity without links (e.g. ``Entity()``) has none.
    """
    if entity is None:
        return None
    links = {}
    for key, value_or_func in (
        ("schema", entity.schema_link_or_func),
        ("context", entity.context_link_or_func),
    ):
        value = value_or_func
        if callable(value_or_func):
            value = yield value_or_func, (request, None), {}
        if value:
            links[key] = str(value)
    return {"application/json": links} if links else None


def _phrase(status: int) -> str:
    """The reason phrase of ``status`` (ninja's OpenAPI text when unknown)."""
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "Unknown Status Code"
