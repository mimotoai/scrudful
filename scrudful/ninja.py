"""SCRUDful conditional requests and Link headers for django-ninja operations.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to django-ninja's operations. Validators and
precondition results are computed only from the object the operation's
``load`` callable returns, i.e. from what authorization already approved (for
a create, the preconditions from the collection ``list_load`` returns, the
response's validators from the object the handler created).

Order of execution
    ``@scrudful`` wraps the operation's function, so everything it does runs
    inside the operation, after ninja's own steps:

    1. ninja's authentication and throttling (401, 429 as usual), with the
       optional ``before`` and ``after_throttles`` hooks, which scrudful runs
       in its copy of ninja's checks: authentication, ``before``, throttling,
       ``after_throttles`` (DRF's order; see :func:`scrudful_list`);
    2. ninja's parameter parsing and validation (422 as usual);
    3. ``load(request, **params)`` (a create's ``list_load``, for a POST
       carrying a precondition), with the parameters ninja validated;
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

    - per-operation authorization goes in ``before=`` (before the throttles,
      as DRF's permissions), ``load`` (fetch-if-allowed) or ninja's ``auth=``;
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
    gets none, and without ``error`` they carry no entity Links and are
    ``application/json`` (on a :class:`ScrudfulRouter`, each carries the HTTP
    Schema Resource link). Describe them in OpenAPI with
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
    body: the HTTP Schema Resource describes it, and it puts no header on the
    response. The Links of a response the handler produces are those of, in
    this order: the
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
    the Links of the entity the HTTP Schema Resource describes for its
    status: ``responses[status]`` (``error`` first for scrudful's own 400,
    412 and 428 when the operation evaluates the If-* headers); else, for a
    401 or 403 when :func:`add_error_links`'s entity is set and differs from
    ``error``, that entity; else ``error``; else none. Link callables get
    ``None``. A response with JSON content (ninja's renderer's
    ``application/json``) is then served as that entity's media type when it
    is JSON content too, e.g. ``application/problem+json``, as the HTTP
    Schema Resource lists it. The errors ninja answers before the operation
    runs (401, 403, 422, 429, ...) get ``responses[status]`` when the
    operation declares the status (a problem-details 422 then reshapes
    ninja's validation body, as :func:`add_error_links` does), else the
    Links of :func:`add_error_links`, the API-wide error entity, as do those
    of operations without an entity for them. On a
    :class:`ScrudfulRouter`, each also gets the HTTP Schema Resource link,
    and a method no operation of the path routes is a JSON 405.

Create
    :func:`scrudful_create` decorates a POST on a collection that creates an
    item. It is the flow above with a preset, not a separate one: there is no
    ``load``, so authorization is the handler's, decided from the parsed
    body. Its target is the collection: with ``list_load`` and
    ``list_etag_func`` or ``list_last_modified_func`` (the list operation's),
    a POST carrying If-Match, If-None-Match or If-Unmodified-Since calls
    ``list_load(request, **params)`` for the collection, the list
    validators are computed from it and the preconditions are evaluated
    against them before the handler runs, with the rules of the other
    methods. A failed If-Match (a strong comparison: a weak list entity-tag
    never matches), a past If-Unmodified-Since or ``If-None-Match: *`` (an
    existing collection has a representation, even empty) is answered with
    412 and a malformed If-Match or If-None-Match with 400 (``error``'s
    Links, whose callables get the collection, and ``error_body_func``'s
    body), and If-Modified-Since is ignored. When ``list_load`` returns
    ``None`` there is no collection, as for ``load``: no precondition can
    fail and the handler answers (e.g. 404), though a malformed header is
    still a 400 (whose link callables get ``None``). A create without a
    precondition proceeds without calling ``list_load``: it is never
    answered with 428. Without the list validators the If-* headers are
    ignored. The list validators are evaluated for POST only: an operation
    without POST raises ``ConfigError`` when it is built. ``list_load`` must
    apply the list operation's authorization; the check runs before the
    handler, so before any authorization it decides from the body.

    The check and the create are atomic only if the application serializes
    every write that changes the collection's validators with conditional
    creates: lock a row per collection (``select_for_update()``) in
    ``list_load``, with a transaction spanning the whole operation, so that
    the lock is held until the create commits, and take the same lock in
    the other writes (unconditional creates, which don't call
    ``list_load``, and the item operations). Otherwise two requests carrying
    the same If-Match can both create. The transaction is
    ``ATOMIC_REQUESTS = True``, or ``transaction.atomic`` wrapping the
    operation's ``view_func`` (``router.add_decorator(transaction.atomic,
    mode="operation")`` on a router whose operations are all sync; see
    "Placement"). Django refuses ``ATOMIC_REQUESTS`` for an async view, and
    ninja serves a path asynchronously when any of its operations is async,
    so a sync create sharing its path with an async operation can use
    neither (``add_decorator`` would wrap the async one too): wrap only its
    ``view_func``, with a ``contribute_operation_callback`` callback
    contributed above the scrudful decorator (ninja runs the sync operation
    in one thread, so ``list_load`` and the handler share the transaction). If
    ``list_load`` is shared with the list operation, lock only for a POST
    (``request.method == "POST"``), or every GET of the list takes the lock
    too. An async operation can't do this: ``list_load`` and the handler
    are called separately, so no transaction spans both. Re-check the
    collection in the handler's own transaction instead (e.g. lock the row,
    compare its version with the If-Match the precondition check passed,
    and raise ``HttpError(412)`` if it moved).

    A plain return value gets status 201: the
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
      item entity's Links of a detail GET given the same arguments (whose
      ``Link`` also carries its own path's HTTP Schema Resource link). Each
      is ``null`` without its callbacks.
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
    path wins. It answers 204, without content or ``Content-Type``, with an
    ``Allow`` listing the methods of the path that *this requester* may use,
    the link to the path's HTTP Schema Resource
    (``rel="https://w3id.org/semantic-http/ns#http-schema-resource-document"``,
    ``type="application/json"``, see :class:`ScrudfulRouter` for its target)
    and ``Access-Control-Expose-Headers``. It is not in the OpenAPI document,
    and has the URL name ``scrudful_options`` on every path, so don't
    ``reverse()`` it. In this order:

    It is answered by an ``Operation.run`` decorator of the operation, so
    before ninja's checks and before the operation's function, which the
    API's and routers' ``add_decorator(mode="operation")`` decorators wrap:
    those never gate OPTIONS. The ``add_decorator(mode="view")`` decorators
    wrap ``Operation.run`` itself, so they run first, as for any operation
    (a CORS preflight too).

    1. A CORS preflight (an OPTIONS with ``Origin`` and
       ``Access-Control-Request-Method``) is answered 204, without content or
       Links, before anything runs: no auth, throttle, hook or load, whatever
       the API's and the routers'. A CORS middleware in front
       (django-cors-headers) adds its headers, or answers it itself.
    2. The request is throttled once, as ninja would throttle the operation:
       with the API's throttles, or the mount's (the router's, or
       ``add_router(..., throttle=...)``'s) when it has some. A refused one
       is ninja's 429. Computing ``Allow`` (step 4) runs no throttle.
    3. On an item path, the object is loaded, with the ``load`` of the GET
       operation, else of the first :func:`scrudful` operation with one; a
       collection has none. It is loaded as that operation would load it: on
       a copy of the request with its method, once its ``auth=`` callbacks
       and ``before`` allowed the requester (else there is no object), with
       the path parameters, validated as the operation validates them (no
       object for one it would refuse with a 422), and only them: give
       ``load``'s other parameters defaults, or take ``**kwargs``. ``None``
       is no object, and only ``None``: an ``HttpError`` or ``Http404`` it
       raises (the 404 of a missing or hidden object) is the answer, that
       status by the API's exception handler, with the Links of
       :func:`add_error_links` and the HSR link (for a 204 on a missing
       object, return ``None``).
    4. Each method the path serves is checked for this requester, on a copy
       of the request with that method. With an ``allowed`` hook (the
       operation's ``allowed=``, else the router's), what
       ``allowed(request, method, obj)`` returns decides: a ``bool``, else
       ``TypeError``; the copy is authenticated by the operation's ``auth=``
       callbacks when they authenticate the requester, and as it came
       otherwise. Without one, the method is allowed when the operation's
       ``auth=`` callbacks authenticate the requester and its ``before`` (if
       any) returns ``None``. A raised ``HttpError`` with status 401, 403 or
       404 (as ninja's ``AuthenticationError`` and ``AuthorizationError``
       are), ``Http404`` or ``PermissionDenied`` refuses the method, except
       a 404 (``HttpError`` or ``Http404``) an ``allowed`` hook raises, which
       answers the OPTIONS, as ``load``'s does; anything else raised is
       answered by the API's exception handler, as ninja answers what an
       operation raises (re-raised without a handler).
       What an ``async def`` callback returns is awaited
       (``asgiref.sync.async_to_sync``), never taken for an answer; an
       awaitable returned by the ``before`` of a sync operation, which the
       operation can't await, refuses the method. Throttles,
       ``after_throttles`` and the handlers never run, so computing
       ``Allow`` uses no throttle quota, and the CSRF check of a cookie
       ``auth`` is skipped: ``Allow`` is about authorization, and the
       request using the method is checked.
    5. ``Allow`` lists those methods and ``OPTIONS`` always
       (:func:`scrudful._hsr.allow_value`). ``HEAD`` is a method like the
       others: listed only when an operation of the path lists it (e.g.
       ``@router.api_operation(["GET", "HEAD"], ...)``), checked with that
       operation's checks, since ninja answers an unrouted HEAD with 405.

    So OPTIONS isn't gated itself: an anonymous requester on a locked path
    gets a 204 with ``Allow: OPTIONS``, not a 401. The ``auth=`` callbacks
    are those of the mount the request came through: a router mounted twice
    with different ``auth`` gets each mount's ``Allow``.

    The semantic-http 0.1 "HTTP Schema Resource" (an OpenAPI 3.1 Path Item,
    see :func:`http_schema_resource`) describes the path, built once per path
    template (and API-wide error entity: each API mounting the router serves
    the document of its own :func:`add_error_links` entity) and without a
    request: the same document for every requester.
    For each method the path serves, in the order ``get``,
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
    - each error status ``responses=`` declares, with its entity's
      ``content``: it describes every response with that status, ninja's
      own (a 401 of its authentication, a 422) included;
    - a ``default`` response, the errors: with the content of the
      operation's ``error``, else of the API-wide error entity
      (:func:`add_error_links`). When the operation's ``error`` differs from
      the API-wide entity, the 401, 403, 422 and 429 ninja answers itself,
      which get the API-wide entity, are listed with its content (every 401
      and 403 of the operation then carries it, raised by ninja's
      authentication, a hook or the operation), unless ``responses=``
      declares the status; without an API-wide entity, the 401, 422 and
      429, without content;
    - ``parameters``: the precondition headers of a conditional write.

    ``head`` (when an operation routes it) is described like ``get``, but
    without ``content``: a HEAD response has none. ``options`` is
    ``{"responses": {"204": {"description": "No Content"}}}``.

    A ``content`` maps the entity's ``media_type`` to its Media Type Object:
    ``{"schema": {"$ref": <schema URL>}}`` for a JSON Schema (the schema
    type ``application/schema+json``, the default), else ``{"x-schema":
    {"href": <schema URL>, "type": <schema type>}}``, and, unless the media
    type is in-band RDF, ``"x-rdf-context": {"href": <context URL>, "type":
    <context type>}`` when the entity has a context link. An entity without
    a schema link (such as ``Entity()``) has no ``content``. Relative URLs
    are resolved against the router's base URL (``base_url=``, else
    ``SCRUDFUL["BASE_URL"]``), as those of the Links are, so the body
    agrees with the Links of the responses it describes; without one, a
    relative URL is a ``ConfigError``. Link callables are called in
    describe mode, ``func(None, None)``: there is neither a request nor a
    loaded object, so they must return the same target without them, and
    one that raises is a ``ConfigError`` naming the operation. Those of an
    async operation may be ``async def``: they are awaited with
    ``asgiref.sync.async_to_sync``. A plain (not scrudful) operation is
    described with the entity :func:`described` gives it, without
    preconditions.

    The router serves the document by GET, the same for every requester and
    cacheable, at the path's HSR route ``/_hsr/<name>`` (see
    :class:`ScrudfulRouter`), as ``application/json`` with the Links of the
    HTTP Schema Resource's JSON Schema
    (``https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json``,
    ``rel="describedby"``) and JSON-LD context
    (``https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld``);
    or :func:`http_schema_resource_view` serves it wherever it is mounted.

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
    The Link URLs are resolved against the base URL of the
    :class:`ScrudfulRouter` serving the operation (``base_url=``, else
    ``SCRUDFUL["BASE_URL"]``), as in the HTTP Schema Resource, else against
    the request's absolute URI (``request.build_absolute_uri()``), so a
    relative one such as ``"/schemas/item"`` is sent absolute. On a
    :class:`ScrudfulRouter`, the link to the path's HTTP Schema Resource
    follows them, on every response (see there).
    ``Access-Control-Expose-Headers`` is extended with ``Link`` and
    ``Allow``, and whichever of ``ETag`` and ``Location`` the response
    carries (django-cors-headers replaces that header when
    ``CORS_EXPOSE_HEADERS`` is set; list the names there instead).

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
import copy
import functools
import hashlib
import inspect
import io
import itertools
import json
import re
import textwrap
import tokenize
import types
import weakref
from collections.abc import (
    Awaitable,
    Callable,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
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

import pydantic
from asgiref.sync import async_to_sync, sync_to_async
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder
from django.core.signals import setting_changed
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
)
from django.urls import NoReverseMatch, URLResolver, reverse
from django.urls.resolvers import RegexPattern
from django.utils.encoding import iri_to_uri
from ninja import NinjaAPI, Query, Router, Schema, Status
from ninja import errors as ninja_errors
from ninja import router as ninja_router
from ninja.constants import NOT_SET
from ninja.errors import ConfigError, HttpError
from ninja.operation import AsyncOperation, Operation, PathView
from ninja.renderers import JSONRenderer
from ninja.responses import NinjaJSONEncoder
from ninja.signature.details import is_collection_type
from ninja.signature.utils import get_path_param_names
from ninja.throttling import BaseThrottle
from pydantic import Field, create_model

from scrudful import _adapter
from scrudful._adapter import (
    BODY_HEADERS,
    METHOD_KEYS,
    SCHEMA_REL,
    SCHEMA_TYPE,
    HsrConfigError,
    check_error_body_func,
    check_callables,
    check_error_media_type,
    check_validators,
    context_defaults,
    context_link_type,
    describe_content,
    describe_read,
    describe_write,
    essence,
    evaluates_create_preconditions,
    evaluates_preconditions,
    expose_headers,
    has_default_entity,
    is_json,
    is_json_syntax,
    merge_links,
    reason_phrase,
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
    error_handler,
    hsr_not_allowed,
    hsr_response,
    no_content,
    page_url,
    precondition_headers,
    scrudful_setting,
    setting_base_url,
    setting_hsr_cache_control,
    setting_hsr_route,
    setting_hsr_url,
    slice_page,
)
from scrudful._hsr import (
    HSR_ROUTE_SEGMENT,
    absolute_for_hsr,
    allow_value,
    check_base_url,
    check_hsr_cache_control,
    check_hsr_url_result,
    hsr_link,
    hsr_route_name,
    is_preflight,
    options_operation,
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
from scrudful.problem import PROBLEM_JSON, problem_from_errors
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
    "described",
    "handler404",
    "handler500",
    "http_schema_resource",
    "http_schema_resource_view",
    "scrudful",
    "scrudful_create",
    "scrudful_list",
]


def handler404(error: Entity) -> Callable[..., HttpResponse]:
    """A Django ``handler404`` that answers JSON unless the client prefers HTML.

    In ``urls.py``: ``handler404 = scrudful.ninja.handler404(ERROR)``, with
    ``ERROR`` an :class:`Entity` of plain (non-callable) link values.
    """
    return error_handler(404, error)


def handler500(error: Entity) -> Callable[..., HttpResponse]:
    """A Django ``handler500`` that answers JSON unless the client prefers HTML.

    In ``urls.py``: ``handler500 = scrudful.ninja.handler500(ERROR)``.
    """
    return error_handler(500, error)


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


#: The hooks scrudful runs in its copy of ninja's checks, in DRF's order,
#: and where each runs (see :func:`_install_hooks`).
_HOOKS = {
    "before": "between authentication and throttling",
    "after_throttles": "after throttling",
}


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
    #: The request body's entity (described by the HTTP Schema Resource; no
    #: header).
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
    #: queryset the handler returned), or of a create operation's (called
    #: with what :attr:`list_load` returned).
    list_etag_func: Callable[[Any], Any] | None = None
    list_last_modified_func: Callable[[Any], Any] | None = None
    #: ``list_load(request, **params)`` of a create operation: the collection
    #: it creates in, for its preconditions.
    list_load: Callable[..., Any] | None = None
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
    #: ``allowed(request, method, obj)``: whether the requester may use the
    #: operation's methods, for the ``Allow`` of OPTIONS (see
    #: :func:`_options_view`); the router's when ``None``.
    allowed: Callable[..., Any] | None = None

    @functools.cached_property
    def hooks(self) -> tuple[str, ...]:
        """The names of the hooks given (see :data:`_HOOKS`), in DRF's order;
        empty without either. Computed once: each request checks it."""
        return tuple(name for name in _HOOKS if getattr(self, name) is not None)

    @property
    def hooks_label(self) -> str:
        """:attr:`hooks` as errors name them (``"before="``,
        ``"after_throttles="`` or both)."""
        return " and ".join(f"{name}=" for name in self.hooks)

    @property
    def entity(self) -> Entity:
        """The item entity (see the class docstring)."""
        return Entity(**_entity_values(self))

    @property
    def default_status(self) -> int | None:
        """The status of a plain return value, unless the handler sets one."""
        return 201 if self.kind == "create" else None

    @property
    def evaluates_preconditions(self) -> bool:
        """Whether the If-* headers may be evaluated before the handler runs.

        An item's, against the loaded object's validators; a create's,
        against the collection's (the flow evaluates them only for a POST
        carrying a precondition).
        """
        if self.kind == "create":
            return self.has_list_validators
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
    allowed: Callable[..., Any] | None = None,
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
        The schema and context of the request body. Described by the HTTP
        Schema Resource; it puts no header on the response.
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
        for :func:`scrudful_list`: the path parameters are the URL's values,
        not validated yet (strings, unless the path uses a converter such as
        ``{int:pk}``; never deny by testing a raw string for a value, see
        :func:`scrudful_list`), it returns ``None`` to let the request go on
        (anything else, or an awaitable in a sync operation, raises
        ``TypeError``) and
        raises to answer first (the API's exception handler answers, with the
        operation's error Links, and the request uses none of a throttle's
        quota); neither its error nor a later 422 passes through the
        ``add_decorator(mode="operation")`` decorators. It may read the body
        as the operation's parameters do: ``request.body`` for a JSON body,
        ``request.POST`` and ``request.FILES`` for a form or an upload. Never
        ``request.body`` of a multipart body: ninja's CSRF check or
        ``fix_request_files_middleware`` may have parsed it already (Django
        then raises ``RawPostDataException``), and it would hold the
        uploaded files in memory, within ``DATA_UPLOAD_MAX_MEMORY_SIZE``.
    after_throttles : callable, optional
        ``after_throttles(request, **path_params)``, the same, called after
        ``before`` and ninja's throttles: a throttled request (429) never
        calls it, and one it refuses has used its quota, as in DRF. With
        either hook, scrudful runs its own copy of ninja's per-operation
        checks, with the guards described for :func:`scrudful_list`
        (``ConfigError`` when the operation is built, ``RuntimeError`` for a
        request reaching the function without the hooks having allowed it,
        as does a direct call of the decorated function: test the operation
        through ninja's ``TestClient``).
    allowed : callable, optional
        ``allowed(request, method, obj)``: whether the requester may use
        ``method`` (this operation's) on ``obj``, for the ``Allow`` of the
        path's OPTIONS (see "OPTIONS" in the module docstring). It wins over
        the router's ``allowed``, and alone decides: without either, the
        operation's ``auth=`` callbacks and ``before`` decide. It may be
        ``async def`` whatever the handler is, and must return a ``bool``.
        A 401 or 403 ``HttpError`` (``AuthenticationError``,
        ``AuthorizationError``) or ``PermissionDenied`` it raises refuses the
        method; a 404 it raises is the OPTIONS's answer.

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
        Also when decorating, if a schema link has no RDF context link (for a
        media type that isn't in-band RDF), chained to the
        :class:`~scrudful.entity.HsrConfigError` of the entity.
    """
    check_validators(etag_func, last_modified_func)
    _check_entity("request", request)
    _check_error(error)
    check_error_body_func(error_body_func)
    check_callables(
        load=load, before=before, after_throttles=after_throttles, allowed=allowed
    )
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
        allowed=allowed,
    )
    if config.has_validators and load is None:
        raise TypeError(
            "etag_func and last_modified_func require load: the validators are "
            "computed from the object load returns"
        )
    _link_entity("@scrudful", **_entity_values(config))
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
    list_load: Callable[..., Any] | None = None,
    list_etag_func: Callable[[Any], Any] | None = None,
    list_last_modified_func: Callable[[Any], Any] | None = None,
    error: Entity | None = None,
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None = None,
    before: Callable[..., Any] | None = None,
    after_throttles: Callable[..., Any] | None = None,
    allowed: Callable[..., Any] | None = None,
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

    It is :func:`scrudful` with a preset: no ``load``, preconditions
    evaluated against the collection's validators (with ``list_load``, never
    a 428), a default status of 201, and on a 201 the ``Location``, the
    Links and the validators of the created item (see "Create" in the module
    docstring). Pass the same validator callbacks and link arguments as the
    item operations, so the 201 carries the validators a detail GET sends,
    and the list validators of the list operation, so that If-Match takes
    the ETag a GET of the list sends. The handler may be ``async def``, and
    then the callables may be too.

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
        The schema and context of the request body, described by the HTTP
        Schema Resource. Defaults to the item entity (the body is an item, as
        for DRF's create).
    responses : mapping of int to Entity, optional
        The response entity of each status, e.g. ``{400: Entity(...)}`` for a
        ``Status(400, ...)`` the handler returns, or an ``HttpError(400)`` it
        raises (see "Error responses" in the module docstring).
        ``responses[201]`` replaces the item entity's Links on 201 responses.
    list_load : callable, optional
        ``list_load(request, **params)`` returns the collection the item is
        created in: what the list operation's handler returns for the same
        request (e.g. the authorized queryset), so the list validators equal
        those of a GET of the list. ``params`` are the parameters ninja
        validated for the operation (path, query, the body ...), as for
        :func:`scrudful`'s ``load``; accept ``**kwargs`` to ignore those you
        don't need. It must apply the list operation's authorization. It
        runs before the handler, only for a POST carrying If-Match,
        If-None-Match or If-Unmodified-Since, and returns ``None`` when there
        is no collection (e.g. its parent is missing): no precondition can
        then fail and the handler answers, though a malformed If-Match or
        If-None-Match is still a 400. Required with a list validator, and
        needs one.
    list_etag_func : callable, optional
        ``list_etag_func(collection)``: the entity-tag of the collection
        ``list_load`` returned, as for :func:`scrudful_list`. The request's
        If-Match (strong comparison: a weak entity-tag never matches) and
        If-None-Match are compared with it.
    list_last_modified_func : callable, optional
        ``list_last_modified_func(collection)``: the modification time of
        the collection, as for :func:`scrudful_list`, compared with the
        request's If-Unmodified-Since.
    error : Entity, optional
        As for :func:`scrudful`: the schema and context of scrudful's own 400
        and 412 bodies (there is no 428), and of the response to an
        ``HttpError`` or ``Http404`` the operation raises, unless
        ``responses`` declares its status. Link callables get the collection
        on the 400 and 412 (``None`` if ``list_load`` returned none), else
        ``None``.
    error_body_func : callable, optional
        As for :func:`scrudful`: the body of scrudful's own 400 and 412. An
        ``HttpError(412)`` the handler raises gets the API's body, not this.
    before : callable, optional
        ``before(request, **path_params)``, called once per request where DRF
        checks permissions (``has_permission``): after ninja's
        authentication, before its throttles and before it validates any
        parameter or the body. As for :func:`scrudful_list`: the path
        parameters are the URL's values, not validated yet (strings, unless
        the path uses a converter such as ``{int:pk}``; never deny by testing
        a raw string for a value, see :func:`scrudful_list`), it returns
        ``None`` to let the request go on (anything else, or an awaitable in a
        sync operation, raises ``TypeError``) and raises to answer first (the
        API's exception handler answers, with the Links of
        ``responses[status]``, if any, and the request uses none of a
        throttle's quota); neither its error nor a later 422 passes through
        the ``add_decorator(mode="operation")`` decorators. It may read the
        body as the operation's parameters do: ``request.body`` for a JSON
        body, ``request.POST`` and ``request.FILES`` for a form or an upload.
        Never ``request.body`` of a multipart body: ninja's CSRF check or
        ``fix_request_files_middleware`` may have parsed it already (Django
        then raises ``RawPostDataException``), and it would hold the
        uploaded files in memory, within ``DATA_UPLOAD_MAX_MEMORY_SIZE``.
    after_throttles : callable, optional
        ``after_throttles(request, **path_params)``, the same, called after
        ``before`` and ninja's throttles: a throttled request (429) never
        calls it, and one it refuses has used its quota, as in DRF. With
        either hook, scrudful runs its own copy of ninja's per-operation
        checks, with the guards described for :func:`scrudful_list`
        (``ConfigError`` when the operation is built, ``RuntimeError`` for a
        request reaching the function without the hooks having allowed it,
        as does a direct call of the decorated function: test the operation
        through ninja's ``TestClient``).
    allowed : callable, optional
        ``allowed(request, method, obj)``: whether the requester may use
        ``method`` (this operation's) on ``obj``, for the ``Allow`` of the
        path's OPTIONS (see "OPTIONS" in the module docstring). It wins over
        the router's ``allowed``, and alone decides: without either, the
        operation's ``auth=`` callbacks and ``before`` decide. It may be
        ``async def`` whatever the handler is, and must return a ``bool``.

    Raises
    ------
    TypeError
        If ``location_func``, ``list_load``, a hook, a validator callback or
        ``error_body_func`` isn't callable, if ``error_body_func`` is
        ``async def``, if a list validator is set
        without ``list_load`` or ``list_load`` without one, if ``request``,
        ``error`` or a value of ``responses`` isn't an :class:`Entity` or a
        key of ``responses`` isn't an HTTP status code, if the handler is a
        generator or already a ninja operation (the decorator sits above the
        operation decorator), or if the handler is sync and
        ``location_func``, ``list_load``, a hook, a validator callback or a
        link callable (also inside an entity) is ``async def``.
    ValueError
        If ``error``'s media type is not JSON content.
    ninja.errors.ConfigError
        When the operation is built, if something replaced its ``view_func``
        before the decorator bound it: a ``_ninja_contribute_to_operation``
        callback contributed below the decorator, or a decorator between it
        and the operation decorator (see "Placement" in the module
        docstring). Also with a hook, as for :func:`scrudful_list`, and with
        list validators on an operation without POST.
        Also when decorating, if a schema link has no RDF context link (for a
        media type that isn't in-band RDF), chained to the
        :class:`~scrudful.entity.HsrConfigError` of the entity.
    """
    if not callable(location_func):
        raise TypeError(
            f"location_func must be callable, not {type(location_func).__name__}"
        )
    check_validators(etag_func, last_modified_func)
    _check_entity("request", request)
    _check_error(error)
    check_error_body_func(error_body_func)
    check_callables(
        list_load=list_load,
        list_etag_func=list_etag_func,
        list_last_modified_func=list_last_modified_func,
        before=before,
        after_throttles=after_throttles,
        allowed=allowed,
    )
    has_list_validators = (
        list_etag_func is not None or list_last_modified_func is not None
    )
    if has_list_validators and list_load is None:
        raise TypeError(
            "list_etag_func and list_last_modified_func require list_load: the "
            "collection's validators are computed from what it returns"
        )
    if list_load is not None and not has_list_validators:
        raise TypeError("list_load needs list_etag_func or list_last_modified_func")
    item = _link_entity(
        "@scrudful_create",
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
        list_load=list_load,
        list_etag_func=list_etag_func,
        list_last_modified_func=list_last_modified_func,
        error=error,
        error_body_func=error_body_func,
        before=before,
        after_throttles=after_throttles,
        allowed=allowed,
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
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None = None,
    before: Callable[..., Any] | None = None,
    after_throttles: Callable[..., Any] | None = None,
    allowed: Callable[..., Any] | None = None,
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
    allowed : callable, optional
        ``allowed(request, method, obj)``: whether the requester may use
        ``method`` (this operation's) on ``obj``, for the ``Allow`` of the
        path's OPTIONS (see "OPTIONS" in the module docstring). It wins over
        the router's ``allowed``, and alone decides: without either, the
        operation's ``auth=`` callbacks and ``before`` decide. It may be
        ``async def`` whatever the handler is, and must return a ``bool``.

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
        ``Operation`` subclass overrides either. Also when decorating, if a
        schema link (the item's or the collection's) has no RDF context
        link, chained to the :class:`~scrudful.entity.HsrConfigError` of
        the entity.
    """
    if not callable(href_func):
        raise TypeError(f"href_func must be callable, not {type(href_func).__name__}")
    check_validators(etag_func, last_modified_func)
    check_callables(
        list_etag_func=list_etag_func,
        list_last_modified_func=list_last_modified_func,
        before=before,
        after_throttles=after_throttles,
        allowed=allowed,
    )
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
        collection=_link_entity(
            "@scrudful_list",
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
        allowed=allowed,
    )
    _link_entity("@scrudful_list", **_entity_values(config))
    return _decorator(config, "@scrudful_list")


class ScrudfulRouter(Router):
    """A ninja ``Router`` answering OPTIONS on every path with a scrudful operation.

    The first scrudful operation added to a path (one decorated with
    :func:`scrudful`, :func:`scrudful_create` or :func:`scrudful_list`), or
    :func:`described` one, also adds an OPTIONS operation to it, unless the
    path already has one. It is
    registered with ``auth=None``, ``throttle=[]`` and
    ``include_in_schema=False``, and is CSRF-exempt, so it needs no
    credentials whatever the API's or router's ``auth`` and is not in the
    OpenAPI document; scrudful throttles a non-preflight OPTIONS once itself,
    as ninja would. It answers a 204 whose ``Allow`` lists the methods this
    requester may use, with the link to the path's HTTP Schema Resource (see
    "OPTIONS" in the module docstring). An operation the implementer adds
    later whose methods include ``"OPTIONS"`` replaces it. It is answered by
    an ``Operation.run`` decorator, before the operation's function: the
    API's and routers' ``add_decorator(mode="operation")`` decorators don't
    apply to it, while their ``add_decorator(mode="view")`` decorators wrap
    ``Operation.run`` and so run first, even for a CORS preflight. Its URL
    name is ``scrudful_options`` on every path.

    The first scrudful or :func:`described` operation of a path also adds
    the path's HTTP Schema Resource route: GET and HEAD of
    ``/_hsr/<name>`` under the router's prefix, ``<name>`` being the path's
    segments joined with ``.`` (``"/{pk}"`` gives ``/_hsr/pk``,
    ``"/{kind}/{pk}"`` gives ``/_hsr/kind.pk``, ``""`` gives
    ``/_hsr/_root``). It answers with the document of
    :func:`http_schema_resource` as compact JSON (``application/json``,
    whatever the API's renderer), the HSR's own schema and context Links, a
    strong ``ETag`` (304 for a matching ``If-None-Match``) and the router's
    ``Cache-Control``. It is the same for every requester: it has
    ``auth=None`` and no throttles, is CSRF-exempt and is not in the OpenAPI
    document; the ``add_decorator`` decorators still apply. The routes come
    before the router's other paths, so ``"/{kind}/{pk}"`` doesn't match
    them, and ``/_hsr/`` is reserved: an operation under it, or two paths
    with one name (``"/a/{x}"`` and ``"/a/{x}/"``), is a ``ConfigError``.
    Their URL names are private (unique per router), so don't ``reverse()``
    them. With ``hsr_route=False``, serve the document elsewhere with
    :func:`http_schema_resource_view` and say where with ``hsr_url``.

    Every operation on a ``ScrudfulRouter`` is described in its path's HTTP
    Schema Resource, so each needs an entity: a scrudful decorator's, or,
    for a plain operation, the one :func:`described` (or ``scrudful_entity=``
    of :meth:`add_api_operation`) gives it. Adding a plain operation without
    one raises ``ConfigError``; one routing only OPTIONS needs none.

    Every response an operation of a described path answers carries the
    link to the path's HTTP Schema Resource (Semantic HTTP 0.1, "Finding the
    HTTP Schema Resource"): its 2xx, 304 and HEAD responses, scrudful's
    early 400, 412 and 428, the errors it raises and those ninja answers
    for it (401, 422, 429, ...), with or without :func:`add_error_links`,
    after their other Links; a link with that relation the handler set
    replaces it. A CORS preflight gets none. ``Link`` and ``Allow`` are
    exposed on each (``Access-Control-Expose-Headers``).

    A request with a method no operation of a path routes is answered with
    a JSON 405 instead of ninja's ``text/html`` one: the API's exception
    handler's answer to ``HttpError(405, "Method not allowed")``, so
    ``{"detail": "Method not allowed"}`` (``application/json``), with the
    Links and media type of :func:`add_error_links`'s entity when it was
    called; an ``Allow`` of the methods the path serves (RFC 9110: not per
    requester, unlike OPTIONS'; ``HEAD`` only when an operation routes it);
    and the HSR link. scrudful replaces ninja's ``PathView`` of each path
    for it, relying on ninja 1.7's ``PathView.clone``, ``get_view``,
    ``_sync_view`` and ``_async_view`` (a ``ConfigError`` when the installed
    ninja's differ).

    Use it for the routers holding scrudful operations, nested or not, and
    for the API's own operations (``default_router``); scrudful operations
    on a plain ``ninja.Router`` get no OPTIONS (ninja answers 405)::

        api = NinjaAPI(auth=..., default_router=ScrudfulRouter())
        items = ScrudfulRouter(base_url="https://api.example/")
        api.add_router("/items", items)

    CORS preflight requests (an OPTIONS with ``Origin`` and
    ``Access-Control-Request-Method``) are sent by browsers without
    credentials and are meant to be answered by the CORS middleware (e.g.
    django-cors-headers) before the view runs. One that reaches scrudful's
    OPTIONS is answered 204, without content or Links, before any
    authentication, throttle, hook or load (only ``add_decorator(mode="view")``
    decorators run before it).

    It takes ``ninja.Router``'s arguments, and these, each of which
    defaults (``None``) to the key of the ``SCRUDFUL`` Django setting, then
    to scrudful's default:

    Parameters
    ----------
    base_url : str, optional
        The absolute URL the relative link URLs of the router's operations
        are resolved against (``SCRUDFUL["BASE_URL"]``): in the HTTP Schema
        Resource, which never depends on the request, and in the Link headers
        of the operations' responses. Without one, a relative link URL is a
        ``ConfigError`` when the HTTP Schema Resource is built, and the Link
        headers resolve it against the request.
    hsr_route : bool, optional
        Whether the router serves each path's HTTP Schema Resource by GET
        (``SCRUDFUL["HSR_ROUTE"]``, default ``True``). Turning it off needs
        an ``hsr_url``. It is read when each path's first scrudful or
        :func:`described` operation is added, which adds the path's route
        or not for good: the responses of a path with a route link to it,
        whatever the setting says later.
    hsr_url : callable, optional
        ``hsr_url(path)``: the URL of the HTTP Schema Resource of ``path``
        (``SCRUDFUL["HSR_URL"]``), for one served elsewhere; the responses
        link to it, even when the router serves the route too.
    hsr_cache_control : str, optional
        The ``Cache-Control`` of the HTTP Schema Resource
        (``SCRUDFUL["HSR_CACHE_CONTROL"]``, default ``public, max-age=300``).
    allowed : callable, optional
        ``allowed(request, method, obj)``: whether the requester may use
        ``method`` on ``obj`` (the object OPTIONS loaded, or ``None``), for
        the ``Allow`` of OPTIONS on the router's paths; an operation's own
        ``allowed=`` wins over it. It returns a ``bool`` and may be ``async
        def``. ``request`` is a copy with ``method``, authenticated by the
        operation's ``auth=`` callbacks when they authenticate the requester
        (``request.auth``), else as it came. ``obj`` was loaded only once the
        loading operation's ``auth=`` and ``before`` allowed the requester.
        Without a hook, the operations' ``auth=`` callbacks and ``before``
        decide (see "OPTIONS" in the module docstring).

    Each is also an attribute of the router, checked as the argument is
    when set (``hsr_route`` only before the router's first operation); a
    new ``base_url`` discards the built HTTP Schema Resources, as a change
    of ``SCRUDFUL`` (``override_settings``) does.

    Raises
    ------
    TypeError
        If one of these has the wrong type.
    ConfigError
        If ``base_url`` is not an absolute URL; ``SCRUDFUL["HSR_ROUTE"]`` or
        ``SCRUDFUL["HSR_URL"]`` has the wrong type; or the HSR route is off
        without an ``hsr_url``.
    """

    def __init__(
        self,
        *args: Any,
        base_url: str | None = None,
        hsr_route: bool | None = None,
        hsr_url: Callable[[str], str] | None = None,
        hsr_cache_control: str | None = None,
        allowed: (
            Callable[[HttpRequest, str, Any], bool | Awaitable[bool]] | None
        ) = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        #: The ``PathView`` of each path, as given to the operation decorator.
        self._scrudful_paths: dict[str, Any] = {}
        #: The HTTP Schema Resource of each path, once built.
        self._scrudful_hsrs: dict[Any, dict[str, Any]] = {}
        #: The URL name of the HSR route of each path that has one.
        self._scrudful_hsr_url_names: dict[str, str] = {}
        #: The path of each HSR route name, to refuse two paths one name.
        self._scrudful_hsr_names: dict[str, str] = {}
        #: Unique for the process, so the HSR routes' URL names are too.
        self._scrudful_serial = next(_ROUTER_SERIALS)
        #: The ``PathView`` clones of the router's paths, one per path and
        #: mount: their operations' ``api`` is the API of the mount.
        self._scrudful_mounts: weakref.WeakSet[Any] = weakref.WeakSet()
        # The options, each checked by its property's setter; hsr_route last,
        # once hsr_url is set, since the route off needs an hsr_url.
        self._scrudful_base_url: str | None = None
        self._scrudful_hsr_route: bool | None = None
        self._scrudful_hsr_url: Callable[[str], str] | None = None
        self._scrudful_hsr_cache_control: str | None = None
        self._scrudful_allowed: Any = None
        _check_option_types(
            hsr_route=hsr_route,
            hsr_cache_control=hsr_cache_control,
            hsr_url=hsr_url,
            allowed=allowed,
        )
        self.base_url = base_url
        self.hsr_cache_control = hsr_cache_control
        self.allowed = allowed
        self._scrudful_hsr_url = hsr_url
        # Reads both settings, so that a wrong one fails here.
        self.hsr_route = hsr_route
        _SCRUDFUL_ROUTERS.add(self)

    @property
    def base_url(self) -> str | None:
        """The ``base_url`` option (see the class docstring); ``None`` reads
        ``SCRUDFUL["BASE_URL"]``. Setting it is checked as the argument is,
        and discards the built HTTP Schema Resources."""
        return self._scrudful_base_url

    @base_url.setter
    def base_url(self, value: str | None) -> None:
        if value is not None:
            _check_base_url(value, "base_url")
        self._scrudful_base_url = value
        self._scrudful_hsrs.clear()

    @property
    def hsr_route(self) -> bool | None:
        """The ``hsr_route`` option; ``None`` reads ``SCRUDFUL["HSR_ROUTE"]``.

        A path's HSR route is added with its first scrudful or
        :func:`described` operation, as the option (or the setting) is then,
        and stays: setting the option once the router has a path is a
        ``ConfigError``, and so is turning the route off without an
        ``hsr_url``.
        """
        return self._scrudful_hsr_route

    @hsr_route.setter
    def hsr_route(self, value: bool | None) -> None:
        _check_option_types(hsr_route=value)
        if self._scrudful_paths:
            raise ConfigError(
                "set a ScrudfulRouter's hsr_route before adding operations: a "
                "path's HTTP Schema Resource route is added (or not) with its "
                "first operation"
            )
        _check_route_and_url(value, self._scrudful_hsr_url)
        self._scrudful_hsr_route = value

    @property
    def hsr_url(self) -> Callable[[str], str] | None:
        """The ``hsr_url`` option; ``None`` reads ``SCRUDFUL["HSR_URL"]``.
        Setting it is checked as the argument is (the route off needs one)."""
        return self._scrudful_hsr_url

    @hsr_url.setter
    def hsr_url(self, value: Callable[[str], str] | None) -> None:
        _check_option_types(hsr_url=value)
        _check_route_and_url(self._scrudful_hsr_route, value)
        self._scrudful_hsr_url = value

    @property
    def hsr_cache_control(self) -> str | None:
        """The ``hsr_cache_control`` option; ``None`` reads
        ``SCRUDFUL["HSR_CACHE_CONTROL"]``. Setting it is checked as the
        argument is."""
        return self._scrudful_hsr_cache_control

    @hsr_cache_control.setter
    def hsr_cache_control(self, value: str | None) -> None:
        _check_option_types(hsr_cache_control=value)
        if value is not None:
            _check_hsr_cache_control(value, "hsr_cache_control")
        self._scrudful_hsr_cache_control = value

    @property
    def allowed(
        self,
    ) -> Callable[[HttpRequest, str, Any], bool | Awaitable[bool]] | None:
        """The ``allowed`` hook of the router's paths, or ``None``. Setting it
        is checked as the argument is."""
        return self._scrudful_allowed  # type: ignore[no-any-return]

    @allowed.setter
    def allowed(
        self,
        value: Callable[[HttpRequest, str, Any], bool | Awaitable[bool]] | None,
    ) -> None:
        _check_option_types(allowed=value)
        self._scrudful_allowed = value

    def add_api_operation(
        self,
        path: str,
        methods: list[str],
        view_func: Callable[..., Any],
        *,
        scrudful_entity: Entity | None = None,
        **kwargs: Any,
    ) -> None:
        """Add the operation, as ``ninja.Router.add_api_operation`` does.

        ``scrudful_entity`` is the :class:`Entity` describing a plain
        operation (see :func:`described`, the way to give it through
        ``router.get(...)`` and the other operation decorators, which don't
        pass it on).

        The first scrudful or :func:`described` operation of a path also adds
        the path's HTTP Schema Resource route (see :class:`ScrudfulRouter`).

        Raises
        ------
        ConfigError
            If the operation is plain (not scrudful) and has no entity, unless
            it routes only OPTIONS, or if it is given an entity twice or is
            scrudful and given one; if ``path`` is under ``/_hsr/``; or if
            ``path`` would have the HTTP Schema Resource route of another
            path of the router.
        """
        if ("/" + path.lstrip("/")).startswith(f"/{HSR_ROUTE_SEGMENT}/"):
            raise ConfigError(
                f"'/{HSR_ROUTE_SEGMENT}/' is reserved for scrudful's HTTP "
                "Schema Resources"
            )
        entity = _operation_entity(path, methods, view_func, scrudful_entity)
        is_scrudful = isinstance(getattr(view_func, "_scrudful", None), _Config)
        is_described = is_scrudful or entity is not None
        _check_path_view()
        hsr_name = None
        if (
            is_described
            and path not in self._scrudful_hsr_url_names
            and self._hsr_route_on()
        ):
            hsr_name = hsr_route_name(path)
            other = self._scrudful_hsr_names.get(hsr_name)
            if other is not None:
                raise ConfigError(
                    f"{path!r} and {other!r} of this ScrudfulRouter have the "
                    f"same HTTP Schema Resource route name {hsr_name!r} "
                    f"('/{HSR_ROUTE_SEGMENT}/{hsr_name}'): use one path "
                    "template, or put them on different routers"
                )
        super().add_api_operation(path, methods, view_func, **kwargs)
        path_view = _path_view(self, view_func)
        _ScrudfulPathView.adopt(path_view, self, path)
        operation = view_func._ninja_operation
        operation._scrudful_entity = entity
        # ninja re-applies them to the clone of each mount, in this order.
        if entity is not None:
            _add_run_decorator(operation, _described_run(entity))
        _add_run_decorator(operation, _serving_router_run(self, path))
        self._scrudful_paths[path] = path_view
        self._scrudful_hsrs.clear()
        # ninja routes a request to the operations listing its method as is.
        if "OPTIONS" in methods:
            # The implementer's OPTIONS replaces the automatic one.
            path_view.operations = [
                operation
                for operation in path_view.operations
                if not _is_automatic_options(operation)
            ]
        elif is_described and not any(
            "OPTIONS" in operation.methods for operation in path_view.operations
        ):
            options_view = _options_view(self, path, path_view)
            super().add_api_operation(
                path,
                ["OPTIONS"],
                options_view,
                auth=None,
                throttle=[],
                response=NOT_SET,
                include_in_schema=False,
            )
            options_operation = getattr(options_view, "_ninja_operation")
            # ninja's clones (one per mount) get the run decorators again.
            _check_ninja_internal(
                type(options_operation),
                "Operation",
                "clone",
                "scrudful's OPTIONS (answered in an Operation.run decorator)",
            )
            # Inner: answers OPTIONS before ninja's checks and view_func (and
            # the add_decorator(mode="operation") wrappers of view_func).
            _add_run_decorator(options_operation, _options_run(self, path, path_view))
            # Outer: the router and operation serving, for _hsr_target.
            _add_run_decorator(options_operation, _serving_router_run(self, path))
        if hsr_name is not None:
            self._add_hsr_route(path, hsr_name)

    def _add_hsr_route(self, path: str, name: str) -> None:
        """Serve ``path``'s HTTP Schema Resource by GET and HEAD at
        ``/_hsr/<name>``, ahead of every other path of the router.

        Django tries a router's paths in the order of ninja's
        ``Router.path_operations``, so the HSR routes are moved to its start:
        a path such as ``"/{kind}/{pk}"``, added before or after, would
        otherwise match ``/_hsr/<name>``. That order is kept by ninja's
        ``BoundRouter._bind_operations`` (which clones the paths for each
        mount) and ``BoundRouter.urls_paths`` (which yields their URL
        patterns, naming them by an explicit ``url_name`` as is), checked to
        be ninja 1.7.0's and 1.7.1's.

        Raises
        ------
        ConfigError
            If the installed ninja's ``BoundRouter`` or either method differs.
        """
        bound_router = getattr(ninja_router, "BoundRouter", None)
        for method in ("_bind_operations", "urls_paths"):
            _check_ninja_internal(
                bound_router,
                "BoundRouter",
                method,
                "scrudful's HTTP Schema Resource routes",
            )
        _check_ninja_internal(
            Operation,
            "Operation",
            "clone",
            "scrudful's HTTP Schema Resource routes (served with the API of "
            "their mount, through an Operation.run decorator)",
        )
        url_name = f"scrudful_hsr_{self._scrudful_serial}_{name}"
        route = f"/{HSR_ROUTE_SEGMENT}/{name}"
        view = _hsr_route_view(self, path)
        super().add_api_operation(
            route,
            ["GET", "HEAD"],
            view,
            auth=None,
            throttle=[],
            response=NOT_SET,
            include_in_schema=False,
            url_name=url_name,
        )
        _ScrudfulPathView.adopt(_path_view(self, view), self, route, hsr_route=True)
        # Each mount's clone serves the document of its API.
        _add_run_decorator(view._ninja_operation, _serving_run)
        self._scrudful_hsr_url_names[path] = url_name
        self._scrudful_hsr_names[name] = path
        hsr_routes = {f"/{HSR_ROUTE_SEGMENT}/{n}" for n in self._scrudful_hsr_names}
        for key in list(self.path_operations):
            if key not in hsr_routes:
                self.path_operations[key] = self.path_operations.pop(key)

    def _hsr_route_on(self) -> bool:
        """``hsr_route``, else ``SCRUDFUL["HSR_ROUTE"]``, else ``True``."""
        return _route_on(self._scrudful_hsr_route)

    def _hsr_url(self) -> Callable[[str], str] | None:
        """``hsr_url``, else ``SCRUDFUL["HSR_URL"]``, else ``None``."""
        if self._scrudful_hsr_url is not None:
            return self._scrudful_hsr_url
        return _setting(setting_hsr_url)  # type: ignore[no-any-return]

    def _hsr_cache_control(self) -> str:
        """``hsr_cache_control``, else ``SCRUDFUL["HSR_CACHE_CONTROL"]``, else
        ``public, max-age=300``."""
        if self._scrudful_hsr_cache_control is not None:
            return self._scrudful_hsr_cache_control
        return _setting(setting_hsr_cache_control)  # type: ignore[no-any-return]

    def _hsr(self, path: str, api: NinjaAPI | None = None) -> dict[str, Any]:
        """The HTTP Schema Resource of ``path`` as ``api`` serves it, built
        once (not a copy): with ``api``'s :func:`add_error_links` entity,
        else (no ``api``) the one of the APIs mounting the router
        (:func:`_api_error`).

        Kept per path and API-wide entity, which is looked up first: an API
        mounting the router later (ninja's ``add_router``, which clears
        nothing) gets the document of its own entity.
        """
        api_error = _api_error(self) if api is None else _API_ERRORS.get(api)
        key = (path, api_error)
        hsr = self._scrudful_hsrs.get(key)
        if hsr is None:
            path_view = self._scrudful_paths.get(path)
            if path_view is None:
                raise ConfigError(
                    f"{path!r} is not a path of this ScrudfulRouter (its paths "
                    f"are {sorted(self._scrudful_paths)}): give the path as "
                    "given to the operation decorator"
                )
            hsr = _describe_path(self, path_view, api_error)
            self._scrudful_hsrs[key] = hsr
        return hsr

    def _base_url(self) -> str | None:
        """``base_url``, else ``SCRUDFUL["BASE_URL"]``, else ``None``."""
        if self._scrudful_base_url is not None:
            return self._scrudful_base_url
        return _setting_base_url()

    def _describes(self, path: str) -> bool:
        """Whether an HTTP Schema Resource describes ``path`` (a path as given
        to the operation decorator): whether it has a scrudful or
        :func:`described` operation. Its responses then link to it."""
        path_view = self._scrudful_paths.get(path)
        return path_view is not None and any(
            _scrudful_config(operation) is not None
            or getattr(operation, "_scrudful_entity", None) is not None
            for operation in path_view.operations
        )


#: Every ``ScrudfulRouter``, whose built HTTP Schema Resources
#: :func:`add_error_links` discards.
_SCRUDFUL_ROUTERS: weakref.WeakSet[ScrudfulRouter] = weakref.WeakSet()


def _forget_router_hsrs(setting: str, **kwargs: Any) -> None:
    """Discard every router's built HTTP Schema Resources when ``SCRUDFUL``
    changes (``override_settings``): they hold its base URL."""
    if setting == "SCRUDFUL":
        for router in list(_SCRUDFUL_ROUTERS):
            router._scrudful_hsrs.clear()


setting_changed.connect(_forget_router_hsrs)

#: The serial number of each ``ScrudfulRouter``, in its HSR routes' URL names.
_ROUTER_SERIALS = itertools.count(1)

#: The entity :func:`add_error_links` gave each API, for the HTTP Schema
#: Resources of the paths of the routers it mounts.
_API_ERRORS: weakref.WeakKeyDictionary[NinjaAPI, Entity] = weakref.WeakKeyDictionary()

#: The ``ScrudfulRouter`` of the operation running (see
#: :func:`_serving_router_run`); ``None`` outside one.
_SERVING_ROUTER: ContextVar[ScrudfulRouter | None] = ContextVar(
    "scrudful_serving_router", default=None
)

#: That operation (ninja's ``Operation``, whose ``api`` :func:`_hsr_target`
#: reads); ``None`` outside one, or when it can't be found (see
#: :func:`_operation_of`).
_SERVING_ROUTER_OPERATION: ContextVar[Any] = ContextVar(
    "scrudful_serving_router_operation", default=None
)

#: A resolver of each API's own URL patterns (see :func:`_hsr_route_path`).
_API_RESOLVERS: weakref.WeakKeyDictionary[NinjaAPI, URLResolver] = (
    weakref.WeakKeyDictionary()
)


def _check_base_url(url: Any, what: str) -> None:
    """Raise ``ConfigError`` unless ``url`` is an absolute URL (a str)."""
    try:
        check_base_url(url, what)
    except HsrConfigError as exc:
        raise ConfigError(str(exc)) from exc


def _check_hsr_cache_control(value: Any, what: str) -> str:
    """``value`` if it is a usable ``Cache-Control`` value, else ``ConfigError``."""
    try:
        return check_hsr_cache_control(value, what)
    except HsrConfigError as exc:
        raise ConfigError(str(exc)) from exc


def _setting_base_url() -> str | None:
    """``SCRUDFUL["BASE_URL"]``, checked, or ``None``."""
    return _setting(setting_base_url)  # type: ignore[no-any-return]


def _setting(accessor: Callable[[], Any]) -> Any:
    """What ``accessor``, one of :mod:`scrudful._django`'s checked accessors
    of a ``SCRUDFUL`` key, returns.

    Raises
    ------
    django.core.exceptions.ImproperlyConfigured
        If ``SCRUDFUL`` itself is not a dict of scrudful's keys.
    ConfigError
        If the key's value is invalid (chained to the accessor's
        ``ImproperlyConfigured``), as for the router's own arguments.
    """
    # The setting's shape first: that error is the setting's own, as is.
    scrudful_setting("BASE_URL", None)
    try:
        return accessor()
    except ImproperlyConfigured as exc:
        raise ConfigError(str(exc)) from exc


def _route_on(hsr_route: bool | None) -> bool:
    """``hsr_route``, else ``SCRUDFUL["HSR_ROUTE"]`` (default ``True``)."""
    if hsr_route is not None:
        return hsr_route
    return _setting(setting_hsr_route)  # type: ignore[no-any-return]


def _check_route_and_url(
    hsr_route: bool | None, hsr_url: Callable[[str], str] | None
) -> None:
    """Raise ``ConfigError`` if the route is off (``hsr_route``, else the
    setting) without an ``hsr_url`` (``hsr_url``, else the setting). Reads
    both settings, so that a wrong one fails."""
    route_on = _route_on(hsr_route)
    url = hsr_url if hsr_url is not None else _setting(setting_hsr_url)
    if not route_on and url is None:
        raise ConfigError("hsr_route=False needs hsr_url=")


#: The type of each :class:`ScrudfulRouter` option that isn't callable.
_OPTION_TYPES = {"hsr_route": ("a bool", bool), "hsr_cache_control": ("a str", str)}


def _check_option_types(**options: Any) -> None:
    """Raise ``TypeError`` unless each of the :class:`ScrudfulRouter`
    ``options`` given is ``None`` or of its type (``hsr_url`` and
    ``allowed``: a callable)."""
    for name, value in options.items():
        if value is None:
            continue
        if name not in _OPTION_TYPES:
            if not callable(value):
                raise TypeError(
                    f"{name} must be a callable or None, not {type(value).__name__}"
                )
            continue
        kind, accepted = _OPTION_TYPES[name]
        if not isinstance(value, accepted):
            raise TypeError(
                f"{name} must be {kind} or None, not {type(value).__name__}"
            )


def _operation_entity(
    path: str,
    methods: list[str],
    view_func: Callable[..., Any],
    scrudful_entity: Entity | None,
) -> Entity | None:
    """The entity describing a plain operation of a ``ScrudfulRouter``.

    ``None`` for a scrudful operation, described by its decorator, and for
    one routing only OPTIONS, which the HTTP Schema Resource describes
    itself. See :meth:`ScrudfulRouter.add_api_operation`.
    """
    marked = getattr(view_func, "_scrudful_entity", None)
    name = getattr(view_func, "__qualname__", repr(view_func))
    if scrudful_entity is not None:
        if not isinstance(scrudful_entity, Entity):
            raise TypeError(
                "scrudful_entity must be an Entity, not "
                f"{type(scrudful_entity).__name__}"
            )
        if marked is not None:
            raise ConfigError(
                f"{name}() has an entity from @described already: don't also "
                "pass scrudful_entity="
            )
        marked = scrudful_entity
    if isinstance(getattr(view_func, "_scrudful", None), _Config):
        if marked is not None:
            raise ConfigError(
                f"{name}() is described by its scrudful decorator: don't pass "
                "scrudful_entity="
            )
        return None
    if marked is None and any(method.upper() != "OPTIONS" for method in methods):
        raise ConfigError(
            f"{name}() on {path!r} of a ScrudfulRouter is described in the "
            "path's HTTP Schema Resource, so it needs an entity: decorate it "
            "with a scrudful decorator, or with @described(Entity(...)) below "
            "the operation decorator (or pass scrudful_entity= to "
            "add_api_operation)"
        )
    return marked


def described(entity: Entity) -> Callable[[Handler], Handler]:
    """Give a plain (not scrudful) operation on a :class:`ScrudfulRouter` its entity.

    The HTTP Schema Resource then describes the operation's request body
    (POST, PUT, PATCH) and its 2xx responses but 204 with ``entity``'s
    ``content``. Those responses (and HEAD's, as GET's) get ``entity``'s
    schema and context Links, as a scrudful operation's do, so that they
    agree with the description; link callables get ``(request, None)``
    (there is no loaded object), and may be ``async def`` only in an async
    operation. Every response of the operation gets the HTTP Schema
    Resource link, and its path answers OPTIONS (see
    :class:`ScrudfulRouter`). ninja's operation decorators don't pass
    unknown arguments on, so it is a decorator, below the operation
    decorator::

        @router.get("/health")
        @described(Entity(schema_link_or_func=..., context_link_or_func=...))
        def health(request): ...

    Raises
    ------
    TypeError
        If ``entity`` isn't an :class:`Entity`, or the function is already a
        ninja operation (the decorator sits above the operation decorator)
        or scrudful; when the operation is added, if a value of ``entity``
        is an ``async def`` callable and the operation is sync.
    """
    if not isinstance(entity, Entity):
        raise TypeError(f"described() takes an Entity, not {type(entity).__name__}")

    def decorator(view_func: Handler) -> Handler:
        name = getattr(view_func, "__qualname__", repr(view_func))
        if hasattr(view_func, "_ninja_operation"):
            raise TypeError(
                "@described must sit below the operation decorator: "
                f"{name}() is already a ninja operation"
            )
        if isinstance(getattr(view_func, "_scrudful", None), _Config):
            raise TypeError(
                f"{name}() is described by its scrudful decorator: drop @described"
            )
        view_func._scrudful_entity = entity  # type: ignore[attr-defined]
        return view_func

    return decorator


def http_schema_resource(
    router: ScrudfulRouter, path: str, *, api: NinjaAPI | None = None
) -> dict[str, Any]:
    """The HTTP Schema Resource (an OpenAPI 3.1 Path Item) of ``path``.

    ``path`` is the router-relative path template, as given to the
    operation decorator (e.g. ``"/{pk}"``). The document is the same for
    every requester: it is built without a request, once per path and
    API-wide error entity (the first call, or GET of its route, builds it;
    adding an operation to the router, calling :func:`add_error_links`,
    changing ``SCRUDFUL`` or the router's ``base_url`` discards it), and a
    copy is returned. See "OPTIONS" in the module docstring for its
    content.

    Its ``default`` and framework error entries take the
    :func:`add_error_links` entity of ``api``, as the HSR route of a mount of
    the router on ``api`` serves it. Without ``api``, of the APIs mounting
    the router (those :func:`add_error_links` was called for, and those
    whose URL patterns were built): pass ``api`` for a router mounted on
    APIs given different entities (or on one with and one without).

    Link callables are called in *describe* mode, ``func(None, None)``: with
    neither request nor object. They must return the same target as on the
    responses they describe, or the description disagrees with the
    responses. Relative URLs are resolved against the router's base URL
    (``base_url=``, else ``SCRUDFUL["BASE_URL"]``).

    Raises
    ------
    TypeError
        If ``router`` isn't a :class:`ScrudfulRouter`, or ``api`` (if given)
        a ``NinjaAPI``.
    ConfigError
        If ``path`` is not one of the router's; a link URL is relative and
        there is no base URL; a link callable raises when called as
        ``func(None, None)``; or, without ``api``, the router is mounted on
        APIs that :func:`add_error_links` gave different entities.
    """
    _check_hsr_arguments(router, api)
    return copy.deepcopy(router._hsr(path, api))


def _check_hsr_arguments(router: Any, api: Any) -> None:
    """Raise ``TypeError`` unless ``router`` is a :class:`ScrudfulRouter` and
    ``api`` ``None`` or a ``NinjaAPI``."""
    if not isinstance(router, ScrudfulRouter):
        raise TypeError(f"router must be a ScrudfulRouter, not {type(router).__name__}")
    if api is not None and not isinstance(api, NinjaAPI):
        raise TypeError(f"api must be a NinjaAPI or None, not {type(api).__name__}")


def http_schema_resource_view(
    router: ScrudfulRouter, path: str, *, api: NinjaAPI | None = None
) -> Callable[[HttpRequest], HttpResponseBase]:
    """A plain Django view serving the HTTP Schema Resource of ``path``.

    It answers GET and HEAD as the router's own HSR route does (see
    :class:`ScrudfulRouter`), and any other method with 405. It is for
    serving the document elsewhere, e.g. with ``hsr_route=False`` and an
    ``hsr_url`` naming where it is mounted::

        path("schemas/items/item", http_schema_resource_view(items, "/{pk}"))

    Like the route, it runs no authentication, permission check or throttle,
    is CSRF-exempt, and is public under Django's ``LoginRequiredMiddleware``
    (``login_required = False``). ``path`` is checked on the first request,
    as :func:`http_schema_resource` checks it, and ``api`` is what that
    function takes: the API whose :func:`add_error_links` entity the error
    entries describe.

    Raises
    ------
    TypeError
        If ``router`` isn't a :class:`ScrudfulRouter`, or ``api`` (if given)
        a ``NinjaAPI``.
    """
    _check_hsr_arguments(router, api)

    def scrudful_hsr_view(
        request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponseBase:
        if request.method not in ("GET", "HEAD"):
            return hsr_not_allowed()
        return _hsr_response(request, router, path, api)

    scrudful_hsr_view.csrf_exempt = True  # type: ignore[attr-defined]
    # Public under Django's LoginRequiredMiddleware (Django >= 5.1).
    scrudful_hsr_view.login_required = False  # type: ignore[attr-defined]
    return scrudful_hsr_view


def _hsr_route_view(
    router: ScrudfulRouter, path: str
) -> Callable[..., HttpResponseBase]:
    """The view of ``path``'s HSR route (see :meth:`ScrudfulRouter._add_hsr_route`).

    It declares no parameters, so ninja passes and validates none (a prefix
    such as ``"/orgs/{org}/items"`` has some), and is CSRF-exempt. The view
    ninja routes the path to is public under Django's
    ``LoginRequiredMiddleware`` (see :meth:`_ScrudfulPathView.get_view`).
    """

    def scrudful_hsr(request: HttpRequest, **params: Any) -> HttpResponseBase:
        # The API of the mount serving it (see ScrudfulRouter._add_hsr_route).
        return _hsr_response(request, router, path, _SERVING_API.get())

    scrudful_hsr.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    )
    scrudful_hsr.csrf_exempt = True  # type: ignore[attr-defined]
    return scrudful_hsr


def _hsr_response(
    request: HttpRequest,
    router: ScrudfulRouter,
    path: str,
    api: NinjaAPI | None,
) -> HttpResponseBase:
    """The answer to a GET or HEAD of ``path``'s HTTP Schema Resource as
    ``api`` serves it (see :meth:`ScrudfulRouter._hsr`), with the router's
    ``Cache-Control`` (see :func:`scrudful._django.hsr_response`)."""
    hsr = router._hsr(path, api)
    return hsr_response(request, hsr, router._hsr_cache_control())


def _hsr_target(request: HttpRequest, router: ScrudfulRouter, path: str) -> str:
    """The URL of the HTTP Schema Resource of ``router``'s ``path``: the target
    of the HSR link of the responses (and OPTIONS) of ``path``.

    Resolved in this order:

    1. ``hsr_url(path)``, with the router's ``hsr_url``, else
       ``SCRUDFUL["HSR_URL"]``, as it returns it;
    2. else, with the HSR route turned off (``hsr_route=False`` or
       ``SCRUDFUL["HSR_ROUTE"] = False``), a ``ConfigError``;
    3. else the path of ``path``'s HSR route, reversed by its URL name (unique
       per router), with the URL parameters the request matched outside
       ``path`` (an API's or router's prefix, such as ``"/orgs/{org}/items"``):

       a. in the URL namespace the request was resolved in (Django's URL
          resolver, so the route of the API and mount the request came
          through);
       b. when the request has no namespace (ninja's ``TestClient`` resolves
          it among the API's patterns alone), among the URL patterns of the
          API of the ``ScrudfulRouter`` operation running, relative to the
          API's root;

    4. made absolute against the router's base URL (``base_url``, else
       ``SCRUDFUL["BASE_URL"]``) when it has one, else with
       ``request.build_absolute_uri()``.

    A router mounted twice in one API (``url_name_prefix``) has one URL name
    per route for both mounts: 3a gives one of them, which serves the same
    document.

    Raises
    ------
    ConfigError
        If the route is off and there is no ``hsr_url``; ``path`` has no HSR
        route on ``router``; the route can't be reversed (3b outside a
        ``ScrudfulRouter`` operation, or a request not routed to ``router``);
        or, for 3b, the installed ninja's ``Operation.api``,
        ``NinjaClientBase._resolve`` or ``NinjaAPI.urls`` isn't ninja 1.7's.
    """
    hsr_url = router._hsr_url()
    if hsr_url is not None:
        try:
            return check_hsr_url_result(hsr_url(path), f"hsr_url({path!r})")
        except HsrConfigError as exc:
            raise ConfigError(str(exc)) from exc
    route = _hsr_route_path(request, router, path)
    base_url = router._base_url()
    if base_url is not None:
        return resolve_uri(route, base_url)
    return request.build_absolute_uri(route)


def _hsr_route_path(request: HttpRequest, router: ScrudfulRouter, path: str) -> str:
    """The absolute path of ``path``'s HSR route (steps 3a and 3b of
    :func:`_hsr_target`)."""
    url_name = router._scrudful_hsr_url_names.get(path)
    if url_name is None:
        raise ConfigError(
            f"{path!r} has no HTTP Schema Resource route on this ScrudfulRouter "
            f"(the paths with one are {sorted(router._scrudful_hsr_url_names)}): "
            "give the path as given to the operation decorator, of a scrudful "
            "or @described operation added while the route was on"
        )
    match = getattr(request, "resolver_match", None)
    kwargs: dict[str, Any] = {}
    if match is not None:
        own = get_path_param_names(path)
        kwargs = {k: v for k, v in match.kwargs.items() if k not in own}
        if match.namespace:
            try:
                return reverse(f"{match.namespace}:{url_name}", kwargs=kwargs)
            except NoReverseMatch as exc:
                raise _unreversed(path, exc) from exc
    if _SERVING_ROUTER.get() is None:
        raise ConfigError(
            f"can't find the HTTP Schema Resource route of {path!r}: the request "
            "was resolved without a URL namespace, outside an operation of a "
            "ScrudfulRouter, so scrudful doesn't know its API"
        )
    api = _operation_api(_SERVING_ROUTER_OPERATION.get())
    resolver = _API_RESOLVERS.get(api)
    if resolver is None:
        # ninja's TestClient routes a request among ``api.urls[0]`` alone and
        # sets the namespace-less ``resolver_match`` of the pattern matched.
        try:
            from ninja.testing import client as ninja_client
        except ImportError:  # pragma: no cover - ninja 1.7 has it
            ninja_client = None
        _check_ninja_internal(
            getattr(ninja_client, "NinjaClientBase", None),
            "NinjaClientBase",
            "_resolve",
            "scrudful's HTTP Schema Resource links under ninja's TestClient",
        )
        resolver = _API_RESOLVERS[api] = URLResolver(
            RegexPattern(r"^/"), _api_patterns(api)
        )
    try:
        return "/" + resolver.reverse(url_name, **kwargs)
    except NoReverseMatch as exc:
        raise _unreversed(path, exc) from exc


def _operation_api(operation: Any) -> NinjaAPI:
    """The API of ninja's ``operation`` (ninja 1.7's ``Operation.api``).

    Raises
    ------
    ConfigError
        If ``operation`` is ``None`` (it can't be found) or has no API.
    """
    if operation is None:
        raise ConfigError(
            "scrudful can't find the ninja operation through Operation.run, "
            "because a decorate_view decorator wraps it without "
            "functools.wraps, so it doesn't know the API serving the request "
            "for its HTTP Schema Resource link. Use functools.wraps in that "
            "decorator, or apply decorate_view above the operation decorator."
        )
    api = getattr(operation, "api", None)
    if not isinstance(api, NinjaAPI):
        raise ConfigError(
            "scrudful reads the API serving the request from ninja's "
            "Operation.api, which the installed ninja's "
            f"{type(operation).__qualname__} doesn't have as ninja 1.7.0 and "
            "1.7.1 do: upgrade scrudful, or keep ninja at one of those releases"
        )
    return api


def _api_patterns(api: NinjaAPI) -> list[Any]:
    """``api``'s URL patterns: the first item of ``NinjaAPI.urls`` (ninja 1.7's
    ``(patterns, app_name, namespace)``).

    Raises
    ------
    ConfigError
        If the installed ninja's ``NinjaAPI.urls`` has another shape.
    """
    urls = api.urls
    if not (isinstance(urls, tuple) and len(urls) == 3 and isinstance(urls[0], list)):
        raise ConfigError(
            "scrudful reverses the HTTP Schema Resource routes of an API among "
            "its patterns, the first item of ninja's NinjaAPI.urls, which the "
            "installed ninja doesn't give as ninja 1.7.0 and 1.7.1 do, a "
            "(patterns, app_name, namespace) tuple: upgrade scrudful, or keep "
            "ninja at one of those releases"
        )
    return urls[0]


def _unreversed(path: str, exc: NoReverseMatch) -> ConfigError:
    return ConfigError(
        f"can't reverse the HTTP Schema Resource route of {path!r} (is the "
        f"request routed to its ScrudfulRouter?): {exc}"
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
    content keeps its Content-Type. For an ``application/problem+json``
    ``error``, the body of ninja's request validation 422,
    ``{"detail": [errors]}``, whose ``detail`` problem details want a
    string, is rewritten as RFC 9457 problem details with ninja's errors as
    the ``errors`` member
    (``problem_from_errors(422, errors, "Request validation failed")``,
    :func:`scrudful.problem.problem_from_errors`), by the API's renderer; a
    custom ``ValidationError`` handler's body without a ``detail`` list is
    kept.

    An error raised inside a scrudful operation gets that operation's
    entity for the status, if it has one (``responses[status]``, else its
    ``error``), and its media type: it describes the response, as the
    OPTIONS of the operation does, and ``error`` here applies only otherwise.
    A 401 or 403 is the exception: one the operation (or its ``before`` or
    ``after_throttles``) raises gets ``error`` here when the operation's
    ``error`` differs (and no ``responses[status]`` is declared), as the
    401 and 403 ninja's authentication answers (a failed CSRF check, an
    ``AuthorizationError``) get it: each 401 and 403 of the operation agrees
    with the HTTP Schema Resource's 401 and 403 entries. A status the
    operation declares in ``responses=`` gets that entity instead, on the
    errors ninja answers for it too (its authentication's 401, its
    validation's 422).

    ``error`` is also the API-wide error entity of the HTTP Schema Resources
    of the paths of the :class:`ScrudfulRouter` routers ``api`` mounts (see
    :func:`http_schema_resource`): the ``default`` response entry of an
    operation without an ``error`` of its own, and the explicit 401, 403,
    422 and 429 entries (ninja's own errors) of one whose ``error`` differs. Its
    relative link URLs resolve against the router's base URL there and, on
    the responses of a ``ScrudfulRouter`` operation, here too.

    The JSON 405 a :class:`ScrudfulRouter` answers for a method no
    operation of the path routes goes through the ``HttpError`` handler, so
    it is covered too; ninja's own ``text/html`` 405 of a plain ``Router``
    is not. Neither are Django's 404 for an unknown URL and its 500 page:
    install :func:`handler404` and :func:`handler500` for those.

    On a path of a :class:`ScrudfulRouter`, the responses also get the HTTP
    Schema Resource link (see there).

    Parameters
    ----------
    api : NinjaAPI
        The API whose exception handlers are wrapped.
    error : Entity
        The schema, context and media type of the error bodies. Its link
        callables are called with the request and ``obj=None``, and with
        ``(None, None)`` when an HTTP Schema Resource is built. ninja's
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
            api.add_exception_handler(exc_class, _error_handler(handler, error, api))
    # The HTTP Schema Resources describe it (see _api_error): built ones
    # are rebuilt.
    _API_ERRORS[api] = error
    for router in list(_SCRUDFUL_ROUTERS):
        router._scrudful_hsrs.clear()


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

            if config.kind == "create" and config.evaluates_preconditions:
                headers = precondition_headers(request)
                # Only a POST carrying a precondition: list_load and the list
                # validators aren't called for one without (never a 428).
                if evaluates_create_preconditions(method, headers):
                    obj = yield config.list_load, (request,), params
                    etag, last_modified = yield from _validators(
                        config.list_etag_func, config.list_last_modified_func, obj
                    )
                    result = evaluate_preconditions(
                        method,
                        headers,
                        etag=etag,
                        last_modified=last_modified,
                        require_precondition=False,
                        # As for load: None is no collection (the handler
                        # answers, e.g. 404), else it exists, even empty.
                        exists=obj is not None,
                    )
                    if result is not None:
                        return (
                            yield from _early_response(config, result, request, obj)
                        )
            elif config.evaluates_preconditions and evaluates_preconditions(method):
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
                        content = await _run_async(flow(request, values, answers))
                    except Exception as exc:
                        _note_raised(config, exc)
                        raise
                    _note_answered()
                    return content

            else:

                @functools.wraps(handler)
                def wrapper(request: HttpRequest, **values: Any) -> Any:
                    _check_hooks_allowed(config, label)
                    try:
                        content = _run_sync(flow(request, values, answers))
                    except Exception as exc:
                        _note_raised(config, exc)
                        raise
                    _note_answered()
                    return content

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
                    "authorization in before=, load or auth=, and wrap a router's or "
                    "the API's operations with add_decorator(mode='operation'). "
                    "Stacking two scrudful decorators is not supported. See "
                    '"Placement of other decorators" in the scrudful.ninja '
                    "documentation."
                )
            if (
                config.kind == "create"
                and config.has_list_validators
                and "POST" not in operation.methods
            ):
                raise ConfigError(
                    f"{name}: operation {'/'.join(operation.methods)} "
                    f"{operation.path!r} ({handler.__qualname__}()): the "
                    "collection's preconditions (list_etag_func, "
                    "list_last_modified_func) are evaluated for a POST "
                    "only, and the operation takes none. Register it with "
                    "router.post, or drop list_load and the list validators."
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
            run_decorator = _raised_links_run(asynchronous, config, name, label)
            _add_run_decorator(operation, run_decorator)

        # Registered with ninja, which replaces it in each operation by the
        # wrapper :func:`bind_operation` binds (called directly, it declares
        # nothing: a plain return value is a 200; with a hook it raises
        # ``RuntimeError``, the hooks not having run).
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
    #: The configuration of the operation running.
    operation: _Config | None = None
    #: Whether its wrapper returned (its response then has its Links).
    answered: bool = False

    def declared(self, status: int) -> _Config | None:
        """The operation's configuration, if ``responses=`` declares the
        error ``status`` of a response its wrapper didn't answer (ninja's
        authentication, throttling or validation; a decorator's): that
        entity describes it, whatever the API-wide one."""
        config = self.operation
        if (
            config is not None
            and not self.answered
            and status >= 400
            and status in config.responses
        ):
            return config
        return None


#: Set by :func:`_raised_links_run` while ``Operation.run`` runs. A mutable
#: holder, so the wrapper's note reaches it from a copied context too.
_RAISED: ContextVar[_Raised | None] = ContextVar("scrudful_raised", default=None)


def _api_error_of(operation: Any) -> Entity | None:
    """The :func:`add_error_links` entity of the API serving ``operation``
    (ninja's ``Operation.api``, as :func:`_serving` reads it), if any."""
    api = getattr(operation, "api", None)
    return _API_ERRORS.get(api) if isinstance(api, NinjaAPI) else None


#: The API serving the scrudful operation running, set by its
#: ``Operation.run`` decorator (:func:`_raised_links_run`); ``None`` outside
#: one.
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


def _serving_run(run: Callable[..., Any]) -> Callable[..., Any]:
    """The ``Operation.run`` decorator of a sync operation setting
    :data:`_SERVING_API` to its API (see :func:`_serving`) while it runs."""
    operation = _operation_of(run)

    @functools.wraps(run)
    def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
        with _serving(operation):
            return run(request, *args, **kw)

    return scrudful_run


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


@contextlib.contextmanager
def _serving_router(router: ScrudfulRouter, operation: Any) -> Iterator[None]:
    """:data:`_SERVING_ROUTER` is ``router`` and
    :data:`_SERVING_ROUTER_OPERATION` is ``operation`` within."""
    router_token = _SERVING_ROUTER.set(router)
    operation_token = _SERVING_ROUTER_OPERATION.set(operation)
    try:
        yield
    finally:
        _SERVING_ROUTER_OPERATION.reset(operation_token)
        _SERVING_ROUTER.reset(router_token)


def _is_async_run(run: Callable[..., Any], operation: Any) -> bool:
    """Whether ``run``, the ``Operation.run`` of ``operation``, is async."""
    return bool(getattr(operation, "is_async", inspect.iscoroutinefunction(run)))


def _serving_router_run(
    router: ScrudfulRouter, path: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The ``Operation.run`` decorator of each operation of ``router``'s
    ``path``, setting :data:`_SERVING_ROUTER` (see :func:`_live_base`) and
    :data:`_SERVING_ROUTER_OPERATION`, the operation (see
    :func:`_hsr_target`), while it runs, and giving the response it returns
    the HTTP Schema Resource link (:func:`_add_hsr_link`).

    The link is resolved (:func:`_resolved_hsr_link`) before ``run``: a
    misconfiguration it raises (an ``hsr_url`` raising or returning a
    relative URL, a route that can't be reversed) fails the request before
    ninja's checks and the operation run, so before any write, rather than
    after one.

    Outermost of scrudful's, so the API's exception handlers ninja calls in
    ``run`` see them too, and every response the operation answers gets the
    link: the operation's own, scrudful's early ones, and those of ninja's
    checks and exception handlers (401, 422, 429, a raised error).
    """

    def decorator(run: Callable[..., Any]) -> Callable[..., Any]:
        operation = _operation_of(run)

        if _is_async_run(run, operation):

            @functools.wraps(run)
            async def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
                with _serving_router(router, operation):
                    link = _resolved_hsr_link(request, router, path)
                    response = await run(request, *args, **kw)
                _add_hsr_link(response, link)
                return response

        else:

            @functools.wraps(run)
            def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
                with _serving_router(router, operation):
                    link = _resolved_hsr_link(request, router, path)
                    response = run(request, *args, **kw)
                _add_hsr_link(response, link)
                return response

        return scrudful_run

    return decorator


def _resolved_hsr_link(
    request: HttpRequest, router: ScrudfulRouter, path: str
) -> str | None:
    """The link to the HTTP Schema Resource of ``router``'s ``path``
    (:func:`_hsr_target`), for ``request``'s response; ``None`` when the path
    has no HTTP Schema Resource (no scrudful or :func:`described`
    operation), or for a CORS preflight, whose answer carries no Links.
    Called within :func:`_serving_router`, which :func:`_hsr_target` reads
    under ninja's ``TestClient``."""
    if is_preflight(request.method or "", request.headers.get):
        return None
    if not router._describes(path):
        return None
    return hsr_link(_hsr_target(request, router, path))


def _add_hsr_link(response: HttpResponseBase, link: str | None) -> None:
    """Give ``response`` the HTTP Schema Resource ``link``
    (:func:`_resolved_hsr_link`) and the exposed headers; nothing when
    ``link`` is ``None``. A Link with the HSR relation the response already
    has (the handler's) replaces it (see
    :func:`scrudful._adapter.merge_links`)."""
    if link is None:
        return
    merged = merge_links(response.get("Link"), None, None, link)
    if merged is not None:
        response["Link"] = merged
    expose_headers(response)


def _described_run(
    entity: Entity,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The ``Operation.run`` decorator of a plain operation that
    :func:`described` gives ``entity``: its responses get ``entity``'s Links
    where the HTTP Schema Resource describes ``entity``'s content (a 2xx but
    204, to any method but TRACE; and HEAD, as GET), as a scrudful
    operation's do. Link callables get ``(request, None)``: there is no
    loaded object. Inside :func:`_serving_router_run`, which adds the HSR
    link and the exposed headers.

    Raises
    ------
    TypeError
        If a value of ``entity`` is an ``async def`` callable and the
        operation is sync.
    """

    def decorator(run: Callable[..., Any]) -> Callable[..., Any]:
        operation = _operation_of(run)
        if _is_async_run(run, operation):

            @functools.wraps(run)
            async def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
                response = await run(request, *args, **kw)
                if has_default_entity(request.method or "", response.status_code):
                    await _run_async(_add_links(response, entity, request, None))
                return response

            return scrudful_run

        for entity_field in fields(Entity):
            if _is_async_callable(getattr(entity, entity_field.name)):
                raise TypeError(
                    f"@described: {entity_field.name} is async, but the operation "
                    "is not: async callables need an async def handler"
                )

        @functools.wraps(run)
        def scrudful_sync_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
            response = run(request, *args, **kw)
            if has_default_entity(request.method or "", response.status_code):
                _run_sync(_add_links(response, entity, request, None))
            return response

        return scrudful_sync_run

    return decorator


def _live_base(request: HttpRequest) -> str:
    """The URL the relative link URLs of a response resolve against.

    The base URL of the :class:`ScrudfulRouter` serving it (its
    ``base_url``, else ``SCRUDFUL["BASE_URL"]``), as in the HTTP Schema
    Resource; else (a scrudful operation on a plain router, or an error
    answered outside a ``ScrudfulRouter`` operation) ``SCRUDFUL["BASE_URL"]``;
    else the request's absolute URI.
    """
    router = _SERVING_ROUTER.get()
    base = router._base_url() if router is not None else _setting_base_url()
    return base if base is not None else request.build_absolute_uri()


def _render(request: HttpRequest) -> Render | None:
    """How the API serving ``request`` writes a JSON body, for scrudful's own
    responses (the early 400, 412 and 428).

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


def _note_answered() -> None:
    """Note that the operation's wrapper returned (see :class:`_Raised`)."""
    raised = _RAISED.get()
    if raised is not None:
        raised.answered = True


def _note_raised(config: _Config, exc: Exception) -> None:
    """Note ``exc`` if it is an ``HttpError`` or ``Http404`` the operation of
    ``config`` raised (its wrapper, or a hook): its response gets the
    Links of the operation's entity for the status (see
    :func:`_raised_links_run`)."""
    raised = _RAISED.get()
    if raised is not None and isinstance(exc, (HttpError, Http404)):
        raised.config = config


def _described_by(
    raised: _Raised, request: HttpRequest, response: HttpResponseBase, operation: Any
) -> _Config | None:
    """The configuration whose entity describes ``response``, which the
    operation's ``Operation.run`` returned, if it isn't the wrapper's own
    answer: an error the operation raised (:func:`_note_raised`), or one
    whose status its ``responses=`` declares (:meth:`_Raised.declared`),
    such as ninja's own 401. The body of ninja's request validation 422 is
    then problem details when that entity is (:func:`_problem_422`)."""
    if raised.config is not None:
        return raised.config
    declared = raised.declared(response.status_code)
    if declared is not None and response.status_code == 422:
        entity = declared.responses[422]
        api = getattr(operation, "api", None)
        if essence(entity.media_type) == PROBLEM_JSON and isinstance(api, NinjaAPI):
            _problem_422(response, request, api)
    return declared


def _raised_links_run(
    asynchronous: bool, config: _Config, name: str, label: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The ``Operation.run`` decorator adding the Links of a raised error.

    The exception propagates out of the wrapper as before (through the
    decorators added with ``add_decorator(mode="operation")``, e.g. one
    rolling a transaction back), and ninja answers it with the API's
    exception handler. The response ``run`` returns then gets the Links of
    the operation's entity for its status, see :func:`_raised_entity`; so
    does a response the wrapper didn't answer (ninja's authentication,
    throttling or request validation) whose error status ``responses=``
    declares (see :func:`_described_by`).

    While ``run`` runs, :data:`_SERVING_API` is the operation's API, whose
    renderer writes scrudful's own bodies (see :func:`_render`). The
    operation's hooks (``before``, ``after_throttles``) are installed on the
    operation, and on each clone ninja decorates again (see
    :func:`_install_hooks`; ``name`` is the scrudful decorator and ``label``
    names the operation in its errors).
    """

    def decorator(run: Callable[..., Any]) -> Callable[..., Any]:
        operation = _operation_of(run)
        if config.hooks:
            _install_hooks(operation, config, asynchronous, name, label)
        if asynchronous:

            @functools.wraps(run)
            async def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
                raised = _Raised(path_params=kw, operation=config)
                token = _RAISED.set(raised)
                try:
                    with _serving(operation):
                        response = await run(request, *args, **kw)
                finally:
                    _RAISED.reset(token)
                described = _described_by(raised, request, response, operation)
                if described is not None:
                    links = _raised_links(
                        described, request, response, _api_error_of(operation)
                    )
                    await _run_async(links)
                return response

        else:

            @functools.wraps(run)
            def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
                raised = _Raised(path_params=kw, operation=config)
                token = _RAISED.set(raised)
                try:
                    with _serving(operation):
                        response = run(request, *args, **kw)
                finally:
                    _RAISED.reset(token)
                described = _described_by(raised, request, response, operation)
                if described is not None:
                    links = _raised_links(
                        described, request, response, _api_error_of(operation)
                    )
                    _run_sync(links)
                return response

        return scrudful_run

    return decorator


#: The digests (:func:`_source_digest`) of the methods of ninja's
#: ``Operation`` and ``AsyncOperation`` that scrudful's copy of the checks
#: (:func:`_install_hooks`) was written from: ``_run_checks``, which it
#: copies, and ``run``, which answers the response it returns before reading
#: any parameter. ninja 1.7.0 and 1.7.1 on Python 3.10 to 3.14 (``run``'s
#: f-string is tokenized differently from Python 3.12 on). Also those the HTTP
#: Schema Resource routes and links rely on (:meth:`ScrudfulRouter._add_hsr_route`,
#: :func:`_hsr_route_path`): ``BoundRouter._bind_operations`` and
#: ``BoundRouter.urls_paths`` (the routes' order and URL names) and ninja's
#: ``TestClient`` resolving a request (``NinjaClientBase._resolve``), each
#: with its digest before Python 3.12 and from it on. And those the
#: per-requester ``Allow`` of OPTIONS relies on (:func:`_served_operations`,
#: :func:`_authenticated`, :func:`_path_values`): ``NinjaAPI._get_bound_routers``
#: (the operations of the mount serving the request), the
#: ``_run_authentication`` of ``Operation`` (which differs between ninja
#: 1.7.0 and 1.7.1) and ``AsyncOperation`` (what scrudful runs as they run
#: the ``auth`` callbacks), ``ParamModel.resolve`` and
#: ``PathModel.get_request_data`` (the validation of the path parameters);
#: and ``Operation.clone`` (which re-applies the run decorator answering
#: OPTIONS to each clone) and ``Operation._check_throttles`` (which throttles
#: it, :func:`_options_throttled`); each tokenizes alike on Python 3.10 to
#: 3.14.
_NINJA_SOURCES = {
    "Operation.clone": frozenset(
        {"419b2415bfa634071e7df7d2e9173da511f951e68465770106c3c8175277cd70"}
    ),
    "Operation._check_throttles": frozenset(
        {"12d6d65ac18a38775f1dbf350efd8b2716cd46fbc7e3f9e96ff49ac722000b39"}
    ),
    "NinjaAPI._get_bound_routers": frozenset(
        {"41498c0b58d36097dc76dc36d81d0b0bfcb177d303339a2bd53260f5962012de"}
    ),
    "Operation._run_authentication": frozenset(
        {
            # 1.7.0, then 1.7.1 (which awaits a coroutine through a helper).
            "f8f3bff9009cdc09a5e651bf23168060d084061bf2a64eca975af09b00363517",
            "a07ef4988adebe087dd596db4df8540e67b65b55285c1667fc8cce154a49f298",
        }
    ),
    "AsyncOperation._run_authentication": frozenset(
        {"e403cc7bfd6e5e87e66aa993830a9cf40ae12eec5bb6be58a90c516826d4230b"}
    ),
    "ParamModel.resolve": frozenset(
        {"febf455ac9585728764f10f251480f5d938a91e4a06a6d41486614341d60525e"}
    ),
    "PathModel.get_request_data": frozenset(
        {"ff2c2a3213646b380b9ce2d8fe331b0e660015e8f0d240e98bf5b3216b8f8296"}
    ),
    "BoundRouter._bind_operations": frozenset(
        {
            "873e51ad8cace9e07c15f2819ec99ac3cf53d0659705b15bcc8621edfb052fed",
            "27088996948fe2a5fadbec87c86f96c701f33752d4d594b94eb4ef06134d5af2",
        }
    ),
    "BoundRouter.urls_paths": frozenset(
        {
            "85c4f63c7cd88350cc2ac5af93f0b024c66487ee4f1b5188ec6bc8c5f5f41f3b",
            "9cacc722ce0ab6834f0c20a5de1363293c9a3d0c13050431d1ae8cdade7d24b0",
        }
    ),
    "NinjaClientBase._resolve": frozenset(
        {
            "45f93bff824798198140d0b3311bb4bb0b2d58c6544fcd1d0860814dcbfce7bf",
            "1067efbf84d27a64f0017013877ea6abd94bd812c36b91c68f40f770ec545901",
        }
    ),
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
    # The JSON 405 (_ScrudfulPathView); each tokenizes alike on Python 3.10
    # to 3.14, in ninja 1.7.0 and 1.7.1.
    "PathView.clone": frozenset(
        {"7635be8d39a8461cbdffc9c5d1352cc54f5196c1e34cccf401b8f2ee7bbc7315"}
    ),
    "PathView.get_view": frozenset(
        {"b676b307a9b8b4d65c5ad4a37295d96ebe6f5345b1d433dee9ad8750ca945bdf"}
    ),
    "PathView._sync_view": frozenset(
        {"2ac2decb57289da4c5ef11985668446740247a66beb700d1835ce3fe65ea476a"}
    ),
    "PathView._async_view": frozenset(
        {"9fb4a1e9a18cef1d7916c95b877413ccd1ad970b965359084d75eea6e4e18ed6"}
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
#: The names of ``BoundRouter.urls_paths`` (see :data:`_NINJA_CODE`).
_URLS_PATHS_NAMES = (
    "replace_path_param_notation",
    "path_operations",
    "items",
    "join",
    "normalize_path",
    "lstrip",
    "operations",
    "getattr",
    "api",
    "get_operation_url_name",
    "template",
    "url_name_prefix",
    "django_path",
    "get_view",
)
#: The locals of ``NinjaAPI._get_bound_routers`` before Python 3.12 (see
#: :data:`_NINJA_CODE`).
_BOUND_ROUTERS_LOCALS = (
    "self",
    "all_mounts",
    "prefix",
    "router",
    "auth",
    "throttle",
    "tags",
    "url_name_prefix",
    "api_decorators",
    "mounts",
    "first_mount",
    "mount",
)
#: Where the source isn't available: the names, local variables and argument
#: count of each (the same in ninja 1.7.0 and 1.7.1 on Python 3.10 to 3.14,
#: but where noted), or a set of them. Weaker: a body using the same names in
#: another order passes.
_NINJA_CODE: dict[str, Any] = {
    "Operation.clone": (
        (
            "object",
            "__new__",
            "__class__",
            "is_async",
            "path",
            "list",
            "methods",
            "view_func",
            "cast",
            "api",
            "csrf_exempt",
            "hasattr",
            "url_name",
            "auth_param",
            "auth_callbacks",
            "throttle_param",
            "throttle_objects",
            "signature",
            "models",
            "stream_format",
            "stream_item_model",
            "dict",
            "response_models",
            "operation_id",
            "summary",
            "description",
            "tags",
            "deprecated",
            "include_in_schema",
            "openapi_extra",
            "by_alias",
            "exclude_unset",
            "exclude_defaults",
            "exclude_none",
            "_run_decorators",
            "run",
            "append",
        ),
        ("self", "cloned", "deco"),
        1,
    ),
    # Before Python 3.12, then from it on (PEP 709 inlines the comprehension).
    "Operation._check_throttles": frozenset(
        (
            (
                "throttle_objects",
                "allow_request",
                "append",
                "wait",
                "max",
                "api",
                "on_exception",
                "Throttled",
                "str",
                "math",
                "ceil",
            ),
            ("self", "request", "throttle_durations", "throttle", *names, "response"),
            2,
        )
        for names in (("durations", "duration"), ("duration", "durations"))
    ),
    # Before Python 3.12, then from it on, when the comprehensions' names are
    # the function's (PEP 709).
    "NinjaAPI._get_bound_routers": frozenset(
        {
            (
                (
                    "_bound_routers_cache",
                    "_router_registrations",
                    "default_router",
                    "_decorators",
                    "build_routers",
                    "NOT_SET",
                    "auth",
                    "throttle",
                    "tags",
                    "url_name_prefix",
                    "extend",
                    "template",
                    "_freeze",
                    "_routers",
                ),
                _BOUND_ROUTERS_LOCALS,
                1,
            ),
            (
                (
                    "_bound_routers_cache",
                    "_router_registrations",
                    "default_router",
                    "_decorators",
                    "build_routers",
                    "NOT_SET",
                    "auth",
                    "throttle",
                    "tags",
                    "url_name_prefix",
                    "extend",
                    "BoundRouter",
                    "template",
                    "_freeze",
                    "prefix",
                    "_routers",
                ),
                _BOUND_ROUTERS_LOCALS + ("m",),
                1,
            ),
        }
    ),
    "Operation._run_authentication": frozenset(
        (
            (
                "auth_callbacks",
                "is_async_callable",
                "getattr",
                "inspect",
                "iscoroutine",
                "async_to_sync",
                *helper,
                "Exception",
                "api",
                "on_exception",
                "auth",
                "AuthenticationError",
            ),
            ("self", "request", "callback", "result", "exc"),
            2,
        )
        # 1.7.0, then 1.7.1.
        for helper in ((), ("_await_coroutine",))
    ),
    "AsyncOperation._run_authentication": (
        (
            "auth_callbacks",
            "is_async_callable",
            "getattr",
            "sync_to_async",
            "Exception",
            "api",
            "on_exception",
            "auth",
            "AuthenticationError",
        ),
        ("self", "request", "callback", "cor", "result", "exc"),
        2,
    ),
    "ParamModel.resolve": (
        ("get_request_data", "_map_data_paths", "model_validate"),
        ("cls", "request", "api", "path_params", "data"),
        4,
    ),
    "PathModel.get_request_data": ((), ("cls", "request", "api", "path_params"), 4),
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
    "BoundRouter._bind_operations": (
        (
            "mount",
            "inherited_decorators",
            "template",
            "_decorators",
            "path_operations",
            "items",
            "clone",
            "operations",
            "api",
            "auth_param",
            "NOT_SET",
            "auth",
            "_set_auth",
            "throttle_param",
            "throttle",
            "isinstance",
            "BaseThrottle",
            "throttle_objects",
            "tags",
            "run",
            "view_func",
            "ValueError",
        ),
        (
            "self",
            "effective_decorators",
            "path",
            "path_view",
            "cloned_view",
            "operation",
            "throttle",
            "decorator",
            "mode",
        ),
        1,
    ),
    # Several fingerprints: from Python 3.12 on, the comprehension's ``i`` is
    # a local variable of the function (PEP 709).
    "BoundRouter.urls_paths": frozenset(
        (
            _URLS_PATHS_NAMES,
            ("self", "prefix", "path", "path_view", *comprehension, "route")
            + ("operation", "url_name"),
            2,
        )
        for comprehension in ((), ("i",))
    ),
    "NinjaClientBase._resolve": (
        (
            "split",
            "lstrip",
            "urls",
            "resolve",
            "_build_request",
            "resolver_match",
            "func",
            "kwargs",
            "Exception",
        ),
        (
            "self",
            "method",
            "path",
            "data",
            "request_params",
            "url_path",
            "url",
            "match",
            "request",
        ),
        5,
    ),
    # Before Python 3.12, then from it on (PEP 709 inlines the comprehension).
    "PathView.clone": frozenset(
        {
            (("PathView", "is_async", "url_name", "operations"), ("self", "cloned"), 1),
            (
                ("PathView", "is_async", "url_name", "operations", "clone"),
                ("self", "cloned", "op"),
                1,
            ),
        }
    ),
    # Before Python 3.14, then from it on (PEP 649 defers the annotations).
    "PathView.get_view": frozenset(
        (names, ("self", "async_view_wrapper", "sync_view_wrapper"), 1)
        for names in (
            ("is_async", "HttpRequest", "Any", "HttpResponseBase", "csrf_exempt"),
            ("is_async", "csrf_exempt"),
        )
    ),
    "PathView._sync_view": (
        ("_find_operation", "_not_allowed", "run"),
        ("self", "request", "a", "kw", "operation"),
        2,
    ),
    "PathView._async_view": (
        (
            "_find_operation",
            "_not_allowed",
            "is_async",
            "cast",
            "AsyncOperation",
            "run",
            "sync_to_async",
        ),
        ("self", "request", "a", "kw", "operation"),
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
    ``"Operation.run"``) scrudful was written against: by the digest of its
    source, else (no source) by its names, locals and arguments. Memoized:
    each function is read once, whatever the number of operations and
    mounts."""
    digest = _source_digest(func)
    if digest is not None:
        return digest in _NINJA_SOURCES[name]
    code = getattr(func, "__code__", None)
    if code is None:
        return False
    expected = _NINJA_CODE[name]
    fingerprints = expected if isinstance(expected, frozenset) else {expected}
    return (code.co_names, code.co_varnames, code.co_argcount) in fingerprints


def _check_ninja_internal(owner: Any, owner_name: str, method: str, use: str) -> None:
    """Raise ``ConfigError`` unless ``owner`` (ninja's class ``owner_name``,
    ``None`` when the installed ninja has none) has ninja 1.7.0's and
    1.7.1's ``method``, which ``use`` relies on."""
    name = f"{owner_name}.{method}"
    func = getattr(owner, method, None) if owner is not None else None
    if func is None or not _is_ninja_code(func, name):
        raise ConfigError(
            f"{use}: scrudful relies on ninja's {name} here, and the installed "
            "ninja's differs from ninja 1.7.0's and 1.7.1's, which scrudful is "
            "verified against: upgrade scrudful, or keep ninja at one of those "
            "releases"
        )


def _check_ninja_checks(
    operation: Any, config: _Config, asynchronous: bool, name: str, label: str
) -> None:
    """Raise ``ConfigError`` unless ``operation`` can run ``config``'s hooks
    (``before``, ``after_throttles``) in scrudful's copy of ninja's checks
    (:func:`_install_hooks`).

    So an opaque decorator hiding the operation, an ``Operation`` subclass
    overriding ``_run_checks`` or ``run``, or a ninja whose checks (or the
    ``run`` calling them) changed fails when the operation is built instead
    of losing a hook or a check.
    """
    hooks = config.hooks_label
    if operation is None:
        raise ConfigError(
            f"{label}: {hooks} can't be installed: scrudful can't find the "
            "ninja operation through Operation.run, because a decorate_view "
            f"decorator below {name} wraps it without functools.wraps. "
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


def _verb(config: _Config, one: str, several: str) -> str:
    """``one`` for a single hook, ``several`` for more."""
    return one if len(config.hooks) == 1 else several


def _checks_error(label: str, config: _Config, reason: str) -> ConfigError:
    """The ``ConfigError`` of an operation that can't run ``config``'s hooks."""
    where = " and ".join(f"{name} {_HOOKS[name]}" for name in config.hooks)
    return ConfigError(
        f"{label}: {config.hooks_label} {_verb(config, 'needs', 'need')} ninja's "
        "own Operation._run_checks and the Operation.run calling it (as in "
        f"ninja 1.7.0 and 1.7.1), as scrudful runs {where}; {reason}."
    )


def _install_hooks(
    operation: Any, config: _Config, asynchronous: bool, name: str, label: str
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
    _check_ninja_checks(operation, config, asynchronous, name, label)

    def current() -> _Raised:
        raised = _RAISED.get()
        if raised is None:
            raise RuntimeError(
                f"{label}: {config.hooks_label} can't get the path parameters: "
                "the operation's checks ran outside its Operation.run"
            )
        return raised

    def answer(request: HttpRequest, exc: Exception) -> HttpResponseBase:
        """The API's answer to what a hook raised."""
        _note_raised(config, exc)
        return operation.api.on_exception(request, exc)  # type: ignore[no-any-return]

    def allowed(hook: str, result: Any) -> None:
        """Fail closed unless hook ``hook`` returned ``None``."""
        if result is not None:
            _refuse_hook_result(hook, result, asynchronous)

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

        async def call(
            hook: str, request: HttpRequest, raised: _Raised
        ) -> HttpResponseBase | None:
            """Call hook ``hook``, if given: ``None`` once it allowed the
            request, else the API's answer to what it raised."""
            func = getattr(config, hook)
            if func is None:
                return None
            try:
                result = await _call_async(func, (request,), raised.path_params)
            except Exception as exc:
                return answer(request, exc)
            allowed(hook, result)
            return None

        async def run_checks(request: HttpRequest) -> HttpResponseBase | None:
            mark_csrf_exempt(request)
            if operation.auth_callbacks:
                error = await operation._run_authentication(request)
                if error:
                    return error  # type: ignore[no-any-return]
            raised = current()
            # DRF's order: before, throttles, after_throttles.
            error = await call("before", request, raised)
            if error is None:
                error = throttle(request)
            if error is None:
                error = await call("after_throttles", request, raised)
            if error is None:
                raised.hooks_allowed = True
            return error

    else:

        def call(  # type: ignore[misc]
            hook: str, request: HttpRequest, raised: _Raised
        ) -> HttpResponseBase | None:
            """Call hook ``hook``, if given: ``None`` once it allowed the
            request, else the API's answer to what it raised."""
            func = getattr(config, hook)
            if func is None:
                return None
            try:
                result = func(request, **raised.path_params)
            except Exception as exc:
                return answer(request, exc)
            allowed(hook, result)
            return None

        def run_checks(  # type: ignore[misc]
            request: HttpRequest,
        ) -> HttpResponseBase | None:
            mark_csrf_exempt(request)
            if operation.auth_callbacks:
                error = operation._run_authentication(request)
                if error:
                    return error  # type: ignore[no-any-return]
            raised = current()
            # DRF's order: before, throttles, after_throttles.
            error = call("before", request, raised)
            if error is None:
                error = throttle(request)
            if error is None:
                error = call("after_throttles", request, raised)
            if error is None:
                raised.hooks_allowed = True
            return error

    operation._run_checks = run_checks


def _check_hooks_allowed(config: _Config, label: str) -> None:
    """Raise ``RuntimeError`` unless the operation's hooks (``before``,
    ``after_throttles``; if any) ran and allowed this request.

    So a request never reaches the function without them: if ninja served an
    operation without scrudful's checks (e.g. a copy it made without
    re-applying ``Operation.run``'s decorators), the request fails instead
    of going on unchecked.
    """
    if not config.hooks:
        return
    raised = _RAISED.get()
    if raised is None or not raised.hooks_allowed:
        raise RuntimeError(
            f"{label}: {config.hooks_label} didn't run for this request: the operation "
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
    config: _Config,
    request: HttpRequest,
    response: HttpResponseBase,
    api_error: Entity | None,
) -> _Flow[None]:
    """Add the Links of an error the operation raised to ``response``.

    ``response`` is the API's answer to it; when it has content, it gets
    the entity :func:`_raised_entity` picks for its status (``api_error``
    is the serving API's :func:`add_error_links` entity), whose link
    callables get ``None``, and its media type (see :func:`_error_links`).
    It exposes ``Link`` and ``Allow`` either way.
    """
    expose_headers(response)
    if not _carries_body(response):
        return
    entity = _raised_entity(
        config, request.method.lower(), response.status_code, api_error
    )
    if entity is not None:
        yield from _error_links(response, entity, request)


def _raised_entity(
    config: _Config, method: str, status: int, api_error: Entity | None
) -> Entity | None:
    """The entity of the response to an error the operation raised.

    The one the HTTP Schema Resource describes for ``status`` (see
    :func:`_described_entity`: ``responses[status]``, but ``error`` first
    for scrudful's own early statuses); else, for a 401 or 403 when the
    API-wide entity ``api_error`` (:func:`add_error_links`) is set and
    differs from ``error``, ``api_error``, as ninja's own 401 and 403 carry
    it; else ``error``. None below 400: an operation decorator turned
    the error into another response, which is its own to describe (a link
    callable of the default entity would get ``None``, not the object).
    """
    if status < 400:
        return None
    entity = _described_entity(config, method, status, _is_conditional(config, method))
    if entity is not None:
        return entity
    if (
        status in _AUTH_ERRORS
        and api_error is not None
        and config.error is not None
        and api_error != config.error
    ):
        return api_error
    return config.error


def _error_handler(
    handler: Callable[[HttpRequest, Exception], HttpResponseBase],
    error: Entity,
    api: NinjaAPI,
) -> Callable[[HttpRequest, Exception], HttpResponseBase]:
    """``handler``, an exception handler of ``api``, adding ``error``'s Links.

    With a problem-details ``error``, ninja's request validation 422 is
    reshaped as problem details (:func:`_problem_422`).
    """

    @functools.wraps(handler)
    def scrudful_handler(request: HttpRequest, exc: Exception) -> HttpResponseBase:
        response = handler(request, exc)
        raised = _RAISED.get()
        if raised is not None and (
            (
                raised.config is not None
                and _raised_entity(
                    raised.config, request.method.lower(), response.status_code, error
                )
                is not None
            )
            or (raised.config is None and raised.declared(response.status_code))
        ):
            # A scrudful operation raised it, or declares its status: its
            # entity describes the response, and :func:`_raised_links_run`
            # adds it.
            return response
        if _carries_body(response):
            if isinstance(exc, ninja_errors.ValidationError) and (
                essence(error.media_type) == PROBLEM_JSON
            ):
                _problem_422(response, request, api)
            _run_sync(_error_links(response, error, request))
        return response

    scrudful_handler._scrudful_error_handler = handler  # type: ignore[attr-defined]
    return scrudful_handler


def _problem_422(
    response: HttpResponseBase, request: HttpRequest, api: NinjaAPI
) -> None:
    """Rewrite ninja's request validation body, ``{"detail": [errors]}``, as
    RFC 9457 problem details (:func:`scrudful.problem.problem_from_errors`,
    the errors as the ``errors`` extension member), written by ``api``'s
    renderer, so that it is the content the problem-details entity
    describes (``detail`` is a string there).

    Only that body: JSON content (ninja's renderer's) whose ``detail`` is a
    list. A custom handler's body is its own, and kept.
    """
    content_type = response.get("Content-Type")
    if not (
        content_type
        and is_json_syntax(content_type)
        and isinstance(response, HttpResponse)
    ):
        return
    try:
        body = json.loads(response.content)
    except ValueError:
        return
    if not (isinstance(body, dict) and isinstance(body.get("detail"), list)):
        return
    status = response.status_code
    problem = problem_from_errors(status, body["detail"], "Request validation failed")
    renderer = api.renderer
    if is_json_syntax(getattr(renderer, "media_type", None) or ""):
        response.content = renderer.render(request, problem, response_status=status)
    else:  # JSON content, though the API renders another media type
        response.content = json.dumps(problem)


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
    yield "list_load", config.list_load
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


def _link_entity(name: str, **values: Any) -> Entity:
    """The :class:`Entity` of a decorator's link arguments.

    Built when decorating, so that a schema link without an RDF context link
    (the HTTP Schema Resource's ``x-rdf-context`` would be missing) is
    refused before the operation is built.

    Raises
    ------
    ConfigError
        Chained to the :class:`~scrudful.entity.HsrConfigError` the entity
        raises; ``name`` is the decorator's.
    """
    try:
        return Entity(**values)
    except HsrConfigError as exc:
        raise ConfigError(f"{name}: {exc}") from exc


def _entity_values(config: _Config) -> dict[str, Any]:
    """The item entity's values of ``config`` (see :attr:`_Config.entity`)."""
    return {
        entity_field.name: getattr(config, entity_field.name)
        for entity_field in fields(Entity)
    }


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

    The URL is resolved against :func:`_live_base`: the base URL of the
    HTTP Schema Resource (see :func:`_media`) when there is one, else the
    request's absolute URI. The context link of ``context_of`` content
    takes its type from :func:`scrudful._adapter.context_link_type`, as the
    HTTP Schema Resource does.
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
    return link_content(resolve_uri(url, _live_base(request)), rel, content_type)


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


# --- The JSON 405 --------------------------------------------------------------

#: The methods of ninja's ``PathView`` that :class:`_ScrudfulPathView` relies
#: on: ``clone`` (one per mount, whose class it keeps), ``get_view`` (whose
#: wrappers call the two others, and whose view an HSR route marks
#: ``login_required = False``) and ``_sync_view`` / ``_async_view`` (which
#: it overrides with ninja 1.7's code but for the 405).
_PATH_VIEW_METHODS = ("clone", "get_view", "_sync_view", "_async_view")


def _check_path_view() -> None:
    """Raise ``ConfigError`` unless ninja's ``PathView`` has ninja 1.7.0's
    and 1.7.1's :data:`_PATH_VIEW_METHODS` (memoized, see
    :func:`_is_ninja_code`)."""
    for method in _PATH_VIEW_METHODS:
        _check_ninja_internal(
            PathView,
            "PathView",
            method,
            "scrudful's JSON 405 (a method no operation of a ScrudfulRouter "
            "path routes) and its Allow",
        )


class _ScrudfulPathView(PathView):
    """A path of a :class:`ScrudfulRouter`: ninja's ``PathView``, but a
    request with a method none of its operations routes is answered by
    :func:`_method_not_allowed` (JSON), not by ninja's ``text/html``
    ``PathView._not_allowed``.

    ``_sync_view`` and ``_async_view`` are ninja 1.7's but for that answer;
    ``clone`` keeps the class, so that the ``PathView`` of each mount
    (``BoundRouter._bind_operations`` clones the router's), whose
    ``get_view`` serves the URL pattern, is one too. All four are
    version-guarded (:func:`_check_path_view`).
    """

    #: The router and the path (as given to the operation decorator).
    _scrudful_router: ScrudfulRouter
    _scrudful_path: str
    #: Whether the path is an HTTP Schema Resource route
    #: (:meth:`ScrudfulRouter._add_hsr_route`).
    _scrudful_hsr_route: bool = False
    #: What :meth:`adopt` sets, and :meth:`clone` copies.
    _SCRUDFUL_ATTRIBUTES = ("_scrudful_router", "_scrudful_path", "_scrudful_hsr_route")

    @classmethod
    def adopt(
        cls,
        path_view: Any,
        router: ScrudfulRouter,
        path: str,
        *,
        hsr_route: bool = False,
    ) -> None:
        """Make ``path_view``, the ``PathView`` ninja made for ``router``'s
        ``path``, one of these (once); ``hsr_route`` marks an HTTP Schema
        Resource route's.

        Raises
        ------
        ConfigError
            If ninja made something else than its own ``PathView``.
        """
        if isinstance(path_view, cls):
            return
        if type(path_view) is not PathView:
            raise ConfigError(
                "scrudful's JSON 405 replaces ninja's PathView of each path of a "
                "ScrudfulRouter, but the installed ninja made a "
                f"{type(path_view).__qualname__}, where ninja 1.7.0 and 1.7.1 "
                "make a PathView: upgrade scrudful, or keep ninja at one of "
                "those releases"
            )
        path_view.__class__ = cls
        values = (router, path, hsr_route)
        for name, value in zip(cls._SCRUDFUL_ATTRIBUTES, values, strict=True):
            setattr(path_view, name, value)

    def clone(self) -> PathView:
        cloned = super().clone()
        cloned.__class__ = type(self)
        for name in self._SCRUDFUL_ATTRIBUTES:
            setattr(cloned, name, getattr(self, name))
        # A mount's: its operations get the API mounting it (see _api_error).
        self._scrudful_router._scrudful_mounts.add(cloned)
        return cloned

    def get_view(self) -> Callable[..., Any]:
        """ninja's view of the path, the one its URL pattern routes to
        (``BoundRouter.urls_paths``); an HSR route's is public under
        Django's ``LoginRequiredMiddleware`` (Django >= 5.1), which reads
        ``login_required`` from it."""
        view = super().get_view()
        if self._scrudful_hsr_route:
            view.login_required = False  # type: ignore[attr-defined]
        return view  # type: ignore[no-any-return]

    def _sync_view(self, request: HttpRequest, *a: Any, **kw: Any) -> HttpResponseBase:
        operation = self._find_operation(request)
        if operation is None:
            return _method_not_allowed(self, request)
        return operation.run(request, *a, **kw)  # type: ignore[no-any-return]

    async def _async_view(
        self, request: HttpRequest, *a: Any, **kw: Any
    ) -> HttpResponseBase:
        operation = self._find_operation(request)
        if operation is None:
            return _method_not_allowed(self, request)
        run: Any = operation.run
        if operation.is_async:
            return await run(request, *a, **kw)  # type: ignore[no-any-return]
        response = await sync_to_async(run)(request, *a, **kw)
        return response  # type: ignore[no-any-return]


def _method_not_allowed(
    path_view: _ScrudfulPathView, request: HttpRequest
) -> HttpResponseBase:
    """The answer to a method none of the operations of ``path_view`` (a
    mount's) routes: the API's answer to ``HttpError(405, "Method not
    allowed")``, by its exception handler, so ``{"detail": "Method not
    allowed"}`` as JSON with ninja's, and the API-wide error entity's Links
    and media type with :func:`add_error_links`; with an ``Allow`` of the
    methods the path serves (RFC 9110 §15.5.6: not per requester, unlike
    OPTIONS), the HSR link of a described path and the exposed headers.

    It runs outside any operation, so the router and the API (of the
    mount's operations, ninja's ``Operation.api``) are set as an operation's
    are (:func:`_serving_router`), for the Links and :func:`_hsr_target`.
    """
    router = path_view._scrudful_router
    operation = path_view.operations[0]
    api = _operation_api(operation)
    with _serving_router(router, operation):
        link = _resolved_hsr_link(request, router, path_view._scrudful_path)
        response = api.on_exception(request, HttpError(405, "Method not allowed"))
    _add_hsr_link(response, link)
    served = {
        method.upper()
        for operation in path_view.operations
        for method in operation.methods
    }
    # allow_value's order; OPTIONS only when served (an HSR route doesn't).
    response["Allow"] = ", ".join(
        method for method in allow_value(served).split(", ") if method in served
    )
    expose_headers(response)
    return response  # type: ignore[no-any-return]


def _options_view(
    router: ScrudfulRouter, path: str, path_view: Any
) -> Callable[..., HttpResponseBase]:
    """The view of the automatic OPTIONS operation of ``path``, which never
    answers: :func:`_options_run`, its ``Operation.run`` decorator, does,
    before ninja's checks and ``view_func`` (and so before the
    ``add_decorator(mode="operation")`` wrappers of ``view_func``).

    It declares the path parameters, as ``Any``, as ninja expects, is marked
    CSRF-exempt and :func:`_is_automatic_options`. Reaching it means the run
    decorator was lost (ninja served an operation without it): it raises
    ``RuntimeError`` instead of answering without the checks.
    """

    def scrudful_options(request: HttpRequest, **params: Any) -> HttpResponseBase:
        raise RuntimeError(
            f"scrudful's OPTIONS of {path!r} reached its view: ninja served the "
            "operation without scrudful's Operation.run decorator, which answers "
            "it. Refusing the request."
        )

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
    scrudful_options.csrf_exempt = True  # type: ignore[attr-defined]
    return scrudful_options


def _options_run(
    router: ScrudfulRouter, path: str, path_view: Any
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The ``Operation.run`` decorator of the automatic OPTIONS operation of
    ``path`` (``path_view`` is the router's own, template, ``PathView``).

    It answers the request (:func:`_options_answer`) without calling ``run``:
    so before ninja's ``_run_checks`` and the operation's ``view_func``,
    which the API's and routers' ``add_decorator(mode="operation")``
    decorators wrap, and OPTIONS is not gated by them. The
    ``add_decorator(mode="view")`` decorators wrap ``run`` itself, so they
    still run first. ninja re-applies it to each clone of the operation
    (``Operation.clone``, version-guarded where it is added).
    """

    def decorator(run: Callable[..., Any]) -> Callable[..., Any]:
        operation = _operation_of(run)

        @functools.wraps(run)
        def scrudful_run(request: HttpRequest, *args: Any, **kw: Any) -> Any:
            return _options_answer(request, router, path, path_view, operation)

        return scrudful_run

    return decorator


def _options_answer(
    request: HttpRequest,
    router: ScrudfulRouter,
    path: str,
    path_view: Any,
    operation: Any,
) -> HttpResponseBase:
    """The answer to an OPTIONS of ``path``, served by ``operation`` (ninja's
    clone of the automatic OPTIONS operation, in the mount the request came
    through). In the order of "OPTIONS" in the module docstring:

    1. a CORS preflight (:func:`scrudful._hsr.is_preflight`) is answered 204,
       with no content and no Links, before anything else;
    2. the request is throttled once, with the throttles ninja would bind to
       the operation (:func:`_options_throttled`): a 429 is ninja's;
    3. the object is loaded (:func:`_options_object`); an ``HttpError`` or
       ``Http404`` the loader raises is answered by the API's exception
       handler;
    4. each method of the path is checked for this requester
       (:func:`_may_use`), with the operations of the mount the request came
       through (:func:`_served_operations`), ninja's clones of the template's;
    5. the answer is a 204 with no content and no ``Content-Type``, and the
       ``Allow`` of the methods allowed (:func:`scrudful._hsr.allow_value`).

    Anything else raised is answered by the API's exception handler, as
    ninja's ``Operation.run`` answers it (re-raised without a handler).
    Every answer but the preflight's gets the HSR link and the exposed
    headers from the operation's outer run decorator
    (:func:`_serving_router_run`), which resolved the link before.
    """
    if is_preflight(request.method or "", request.headers.get):
        return no_content()
    api = _operation_api(operation)
    # As ninja's _run_checks marks the request of a CSRF-exempt operation.
    request._ninja_csrf_exempt = True  # type: ignore[attr-defined]
    try:
        bound_router, bound_view = _bound_mount(api, operation)
        throttled = _options_throttled(api, operation, bound_router, request)
        if throttled is not None:
            return throttled
        served = _served_operations(bound_view, path_view)
        # The URL's parameters, as the operations' hooks get them (also those
        # of a prefix).
        match = getattr(request, "resolver_match", None)
        url_params = dict(match.kwargs) if match is not None else {}
        try:
            obj = _options_object(request, api, served, url_params)
        except (HttpError, Http404) as exc:
            return api.on_exception(request, exc)  # type: ignore[no-any-return]
        allowed = [
            method
            for method, template, bound in served
            if _may_use(request, router, method, template, bound, obj, url_params)
        ]
    except Exception as exc:
        return api.on_exception(request, exc)  # type: ignore[no-any-return]
    response = no_content()
    response["Allow"] = allow_value(allowed)
    return response


def _options_throttled(
    api: NinjaAPI, operation: Any, bound_router: Any, request: HttpRequest
) -> HttpResponseBase | None:
    """ninja's 429 if the throttles ninja would bind to ``operation`` refuse
    ``request``, else ``None``.

    The automatic OPTIONS operation is registered with ``throttle=[]``, so
    that a preflight runs none; a non-preflight OPTIONS is throttled once
    here, as ninja would throttle the operation without its own
    ``throttle``: ``BoundRouter._bind_operations`` gives it the API's
    throttles, replaced by the mount's (``BoundRouter.throttle``) when it has
    some. They are evaluated by ninja's ``Operation._check_throttles``, on
    ``operation``'s ``throttle_objects``, set once: ninja's own checks of the
    operation never run (:func:`_options_run`).

    Raises
    ------
    ConfigError
        If the installed ninja's ``Operation._check_throttles`` differs from
        ninja 1.7.0's and 1.7.1's, or its ``BoundRouter`` has no ``throttle``.
    """
    _check_ninja_internal(
        type(operation),
        "Operation",
        "_check_throttles",
        "scrudful's throttling of OPTIONS",
    )
    if not getattr(operation, "_scrudful_throttles_bound", False):
        if not hasattr(bound_router, "throttle"):
            raise ConfigError(
                "scrudful throttles OPTIONS with ninja's BoundRouter.throttle, "
                "which the installed ninja's BoundRouter doesn't have as ninja "
                "1.7.0 and 1.7.1 do: upgrade scrudful, or keep ninja at one of "
                "those releases"
            )
        throttle: Any = NOT_SET
        if api.throttle != NOT_SET:
            throttle = api.throttle
        if bound_router.throttle != NOT_SET:
            throttle = bound_router.throttle
        if throttle == NOT_SET:
            throttle = []
        elif isinstance(throttle, BaseThrottle):
            throttle = [throttle]
        operation.throttle_objects = list(throttle)
        operation._scrudful_throttles_bound = True
    if operation.throttle_objects:
        return operation._check_throttles(request)  # type: ignore[no-any-return]
    return None


#: What the per-method checks of OPTIONS (:func:`_may_use`) and loading
#: (:func:`_options_object`) take for "not allowed": an ``HttpError`` (as
#: ninja's ``AuthenticationError`` and ``AuthorizationError`` are) with one
#: of these statuses, ``Http404`` or ``PermissionDenied``.
_DENIAL_STATUSES = frozenset({401, 403, 404})


def _is_denial(exc: Exception) -> bool:
    if isinstance(exc, HttpError):
        return exc.status_code in _DENIAL_STATUSES
    return isinstance(exc, (Http404, PermissionDenied))


def _is_hook_denial(exc: Exception) -> bool:
    """Whether ``exc``, raised by an ``allowed`` hook, means "not allowed"
    (as with DRF's and FastAPI's hooks): a 401 or 403 ``HttpError`` (as
    ninja's ``AuthenticationError`` and ``AuthorizationError`` are) or
    ``PermissionDenied``. A 404 (``HttpError`` or ``Http404``) is the hook's
    answer to the OPTIONS, as ``load``'s is."""
    if isinstance(exc, HttpError):
        return exc.status_code in (401, 403)
    return isinstance(exc, PermissionDenied)


def _settled(result: Any) -> Any:
    """``result``, awaited (``async_to_sync``) when it is awaitable: an
    ``async def`` check's coroutine is truthy, never its answer."""
    if inspect.isawaitable(result):
        return async_to_sync(_await)(result)
    return result


def _discard(awaitable: Any) -> None:
    """Close a coroutine or cancel a future, so neither runs on unawaited."""
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    elif asyncio.isfuture(awaitable):
        awaitable.cancel()


def _served_operations(bound_view: Any, template: Any) -> list[tuple[str, Any, Any]]:
    """``(method, template operation, bound operation)`` of each method the
    path serves, OPTIONS aside: the first operation listing it, as ninja
    routes it.

    The template operation (the router's) carries the scrudful
    configuration; the bound one, ninja's clone of it in ``bound_view``, the
    ``PathView`` of the mount the request came through (:func:`_bound_mount`),
    the ``auth`` callbacks that mount gives it. The clones are in the
    template's order (ninja's ``PathView.clone``).

    Raises
    ------
    ConfigError
        If the clones don't match the template's operations.
    """
    operations = getattr(bound_view, "operations", None)
    if not isinstance(operations, list) or len(operations) != len(template.operations):
        raise _bound_path_error()
    served: list[tuple[str, Any, Any]] = []
    for template_operation, bound in zip(template.operations, operations):
        if getattr(bound, "methods", None) != template_operation.methods:
            raise _bound_path_error()
        if _is_automatic_options(template_operation):
            continue
        for method in template_operation.methods:
            method = method.upper()
            if method != "OPTIONS" and all(method != m for m, _, _ in served):
                served.append((method, template_operation, bound))
    return served


def _bound_mount(api: NinjaAPI, running: Any) -> tuple[Any, Any]:
    """The ``BoundRouter`` of ``api`` holding ``running``, and its ``PathView``
    holding it (ninja 1.7's ``NinjaAPI._get_bound_routers`` and the
    ``BoundRouter.path_operations`` that ``BoundRouter._bind_operations``
    fills), found once per operation.

    Raises
    ------
    ConfigError
        If the installed ninja's ``NinjaAPI._get_bound_routers`` or
        ``BoundRouter._bind_operations`` differ from ninja 1.7.0's and
        1.7.1's, or ``BoundRouter.path_operations`` doesn't hold ``running``.
    """
    _check_ninja_internal(
        type(api),
        "NinjaAPI",
        "_get_bound_routers",
        "scrudful's per-requester Allow of OPTIONS",
    )
    _check_ninja_internal(
        getattr(ninja_router, "BoundRouter", None),
        "BoundRouter",
        "_bind_operations",
        "scrudful's per-requester Allow of OPTIONS",
    )
    found = getattr(running, "_scrudful_bound", None)
    if found is not None:
        return found  # type: ignore[no-any-return]
    for bound_router in api._get_bound_routers():
        path_operations = getattr(bound_router, "path_operations", None)
        if not isinstance(path_operations, dict):
            raise _bound_path_error()
        for path_view in path_operations.values():
            if any(op is running for op in getattr(path_view, "operations", ())):
                running._scrudful_bound = bound_router, path_view
                return bound_router, path_view
    raise _bound_path_error()


def _bound_path_error() -> ConfigError:
    return ConfigError(
        "scrudful's per-requester Allow of OPTIONS finds the operations of the "
        "mount serving the request through ninja's BoundRouter.path_operations "
        "(PathView clones of the router's, in its order), which the installed "
        "ninja doesn't have as ninja 1.7.0 and 1.7.1 do: upgrade scrudful, or "
        "keep ninja at one of those releases"
    )


def _scrudful_config(operation: Any) -> _Config | None:
    """The scrudful decorator's configuration of a template operation."""
    config = getattr(operation.view_func, "_scrudful", None)
    return config if isinstance(config, _Config) else None


def _options_object(
    request: HttpRequest,
    api: NinjaAPI,
    served: list[tuple[str, Any, Any]],
    url_params: dict[str, Any],
) -> Any:
    """The object of OPTIONS: what the loading operation's ``load`` returns.

    The loading operation is the GET operation, if it has a ``load``, else
    the first item operation (:func:`scrudful`) with one; ``None`` (no
    object) without one, as for a collection. It loads as it would: on a
    copy of the request with its method, once its ``auth`` callbacks and
    ``before`` allowed the requester (else there is no object: a requester
    who may not use it isn't shown it), with the path parameters validated
    as it validates them (none it would refuse with a 422: no object). The
    loader gets only those: give its other parameters defaults, or take
    ``**kwargs``.

    Raises
    ------
    HttpError, Http404
        What the loader raises: the caller answers it.
    """
    loading = None
    candidates = [entry for entry in served if entry[0] == "GET"] + [
        entry for entry in served if entry[0] != "GET"
    ]
    for method, template, bound in candidates:
        config = _scrudful_config(template)
        if (
            config is not None
            and config.load is not None
            and (method == "GET" or config.kind == "item")
        ):
            loading = method, config, bound
            break
    if loading is None:
        return None
    method, config, bound = loading
    requester, authenticated = _authenticated(request, bound, method)
    if not authenticated or not _before_allows(config, bound, requester, url_params):
        return None
    values = _path_values(requester, api, bound, url_params)
    if values is None:
        return None
    return _settled(config.load(requester, **values))


def _path_values(
    request: HttpRequest, api: NinjaAPI, operation: Any, url_params: dict[str, Any]
) -> dict[str, Any] | None:
    """``operation``'s path parameters, validated by its path models as
    ninja's ``Operation._get_values`` validates them (ninja 1.7's
    ``Operation.models``, ``ParamModel.resolve`` and
    ``PathModel.get_request_data``); ``None`` if they are invalid.

    Raises
    ------
    ConfigError
        If the installed ninja's differ.
    """
    try:
        from ninja.params import models as ninja_params
    except ImportError:  # pragma: no cover - ninja 1.7 has it
        ninja_params = None  # type: ignore[assignment]
    use = "scrudful's loading for OPTIONS"
    param_model = getattr(ninja_params, "ParamModel", None)
    path_model = getattr(ninja_params, "PathModel", None)
    _check_ninja_internal(param_model, "ParamModel", "resolve", use)
    _check_ninja_internal(path_model, "PathModel", "get_request_data", use)
    models = getattr(operation, "models", None)
    if not isinstance(models, list) or not all(
        isinstance(model, type) and issubclass(model, param_model) for model in models
    ):
        raise ConfigError(
            f"{use} validates the path parameters with ninja's Operation.models, "
            "which the installed ninja doesn't have as ninja 1.7.0 and 1.7.1 do "
            "(a list of ParamModel classes): upgrade scrudful, or keep ninja at "
            "one of those releases"
        )
    values: dict[str, Any] = {}
    for model in models:
        if getattr(model, "__ninja_param_source__", None) != "path":
            continue
        if not issubclass(model, path_model) or (
            _class_function(model, "resolve")
            is not _class_function(param_model, "resolve")
            or _class_function(model, "get_request_data")
            is not _class_function(path_model, "get_request_data")
        ):
            raise ConfigError(
                f"{use} validates the path parameters with ninja's PathModel, "
                f"which {model.__qualname__} doesn't resolve as ninja 1.7 does"
            )
        try:
            values.update(model.resolve(request, api, url_params))
        except pydantic.ValidationError:
            return None
    return values


def _class_function(cls: type, name: str) -> Any:
    """The function of the classmethod ``cls.<name>``."""
    return getattr(getattr(cls, name, None), "__func__", None)


def _authenticated(
    request: HttpRequest, operation: Any, method: str
) -> tuple[HttpRequest, bool]:
    """A copy of ``request`` with ``method``, and whether ``operation``'s
    ``auth`` callbacks authenticate it (always, without any).

    The callbacks run as ninja 1.7's ``Operation._run_authentication`` runs
    them (the first one returning a truthy value authenticates, and its
    value is the copy's ``auth``), on the copy, which is CSRF-exempt: ``Allow``
    is about authorization, and the method itself checks CSRF. An
    ``async def`` callback is awaited. A denial (:func:`_is_denial`) is not
    authenticated; anything else a callback raises propagates.

    Raises
    ------
    ConfigError
        If the installed ninja's ``_run_authentication`` (of the operation's
        class, ``Operation`` or ``AsyncOperation``) differs from ninja
        1.7.0's and 1.7.1's, or its ``Operation.auth_callbacks`` isn't a
        sequence of callables.
    """
    ninja_class = AsyncOperation if getattr(operation, "is_async", False) else Operation
    _check_ninja_internal(
        type(operation),
        ninja_class.__name__,
        "_run_authentication",
        "scrudful's per-requester Allow of OPTIONS",
    )
    callbacks = getattr(operation, "auth_callbacks", None)
    if not isinstance(callbacks, Sequence) or not all(map(callable, callbacks)):
        raise ConfigError(
            "scrudful's per-requester Allow of OPTIONS runs the auth callbacks "
            "of ninja's Operation.auth_callbacks, which the installed ninja "
            "doesn't have as ninja 1.7.0 and 1.7.1 do (a sequence of "
            "callables): upgrade scrudful, or keep ninja at one of those releases"
        )
    # A copy of the OPTIONS request, which _options_answer marked CSRF-exempt.
    requester = copy.copy(request)
    requester.method = method
    if not callbacks:
        return requester, True
    for callback in callbacks:
        try:
            result = _settled(callback(requester))
        except Exception as exc:
            if _is_denial(exc):
                return requester, False
            raise
        if result:
            requester.auth = result  # type: ignore[attr-defined]
            return requester, True
    return requester, False


def _before_allows(
    config: _Config | None,
    operation: Any,
    requester: HttpRequest,
    url_params: dict[str, Any],
) -> bool:
    """Whether the operation's ``before`` (if any) allows ``requester``.

    It allows by returning ``None``: anything else, or a denial
    (:func:`_is_denial`), doesn't; anything else it raises propagates. An
    awaitable it returns is awaited in an async operation; in a sync one,
    which can't await it (the operation raises ``TypeError``), it is
    discarded and doesn't allow.
    """
    if config is None or config.before is None:
        return True
    try:
        result = config.before(requester, **url_params)
        if inspect.isawaitable(result):
            if not getattr(operation, "is_async", False):
                _discard(result)
                return False
            result = _settled(result)
    except Exception as exc:
        if _is_denial(exc):
            return False
        raise
    return result is None


def _may_use(
    request: HttpRequest,
    router: ScrudfulRouter,
    method: str,
    template: Any,
    bound: Any,
    obj: Any,
    url_params: dict[str, Any],
) -> bool:
    """Whether the requester of the OPTIONS ``request`` may use ``method``,
    which the template operation ``template`` serves (``bound`` is its clone
    serving the request), on ``obj``.

    With an ``allowed`` hook (the operation's, else ``router``'s), what it
    returns, on a copy of the request with ``method``, authenticated by the
    operation's ``auth`` callbacks when they authenticate it (else as is:
    the hook alone decides); a denial it raises (:func:`_is_hook_denial`)
    is not allowed, and anything else it raises (a 404 included)
    propagates. Otherwise, whether those callbacks authenticate it and the
    operation's ``before`` allows it (see :func:`_authenticated` and
    :func:`_before_allows`). Neither throttles, ``after_throttles`` nor
    ``load`` run.

    Raises
    ------
    TypeError
        If the hook returns something else than a ``bool``.
    """
    config = _scrudful_config(template)
    hook = config.allowed if config is not None else None
    if hook is None:
        hook = router.allowed
    requester, authenticated = _authenticated(request, bound, method)
    if hook is not None:
        try:
            result = _settled(hook(requester, method, obj))
        except Exception as exc:
            if _is_hook_denial(exc):
                return False
            raise
        if not isinstance(result, bool):
            raise TypeError(
                f"allowed returned {result!r} for {method}: it must return a bool"
            )
        return result
    return authenticated and _before_allows(config, bound, requester, url_params)


#: The description of each method's ``default`` response entry: the errors.
_DEFAULT_ERROR = "Error."
#: The errors ninja answers before an operation runs (authentication, its
#: CSRF check and ``AuthorizationError``, request validation, throttling),
#: whose bodies get the API-wide error entity (:func:`add_error_links`),
#: whatever the operation's ``error``.
_FRAMEWORK_ERRORS = {
    401: "Unauthorized: the request lacks valid authentication credentials.",
    403: "Forbidden: the requester may not perform this request.",
    422: "Unprocessable Content: the request's parameters or body are invalid.",
    429: "Too Many Requests: the request was throttled.",
}
#: The statuses of :data:`_FRAMEWORK_ERRORS` that also carry the API-wide
#: entity when the operation (or a hook) raises them: an operation
#: answers each 401 and 403 alike, whoever raised it.
_AUTH_ERRORS = frozenset({401, 403})


def _describe_path(
    router: ScrudfulRouter, path_view: Any, api_error: Entity | None
) -> dict[str, Any]:
    """The HTTP Schema Resource of the path of ``path_view``: each method it
    serves, described without a request (see :func:`http_schema_resource`).

    In the order of :data:`~scrudful._adapter.METHOD_KEYS`, with ``options``
    (:func:`scrudful._hsr.options_operation`) among them. Relative link URLs
    are resolved against ``router``'s base
    URL, and the ``default`` error entries take the API-wide entity
    ``api_error`` (see :meth:`ScrudfulRouter._hsr`).
    """
    base_url = router._base_url()
    operations = [
        operation
        for operation in path_view.operations
        if not _is_automatic_options(operation)
    ]
    body = {}
    for method in METHOD_KEYS:
        if method == "options":
            body[method] = options_operation()
            continue
        # The operation ninja routes the method to: the first that lists it.
        operation = next(
            (op for op in operations if method.upper() in op.methods), None
        )
        if operation is None:
            continue
        name = f"{getattr(operation.view_func, '__qualname__', 'operation')}()"
        flow = _describe_operation(
            method, operation, _described_config(operation), base_url, api_error, name
        )
        body[method] = _run_describing(flow, name)
    return body


def _described_config(operation: Any) -> _Config | None:
    """The configuration describing ``operation``: its scrudful decorator's,
    else, for a plain operation given an entity (:func:`described`), an item
    configuration of that entity alone (no validators, no ``error``);
    ``None`` for a plain operation without one."""
    config = getattr(operation.view_func, "_scrudful", None)
    if isinstance(config, _Config):
        return config
    entity = getattr(operation, "_scrudful_entity", None)
    if entity is None:
        return None
    return _Config(
        kind="item",
        **{
            entity_field.name: getattr(entity, entity_field.name)
            for entity_field in fields(Entity)
        },
    )


def _run_describing(flow: _Flow[T], name: str) -> T:
    """Run a describe-mode ``flow`` (:func:`_describe_operation`).

    Each callable is called directly, and an awaitable it returns (an
    ``async def`` link callable's) is awaited with
    ``asgiref.sync.async_to_sync``. An exception a callable raises is
    re-raised as ``ConfigError``, naming the operation ``name``, and so is
    a :class:`~scrudful._adapter.HsrConfigError` of the description.
    """
    result = None
    while True:
        try:
            func, args, kwargs = flow.send(result)
        except StopIteration as stop:
            return stop.value
        except HsrConfigError as exc:
            raise ConfigError(str(exc)) from exc
        try:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                result = async_to_sync(_await)(result)
        except Exception as exc:
            raise ConfigError(
                f"{name}: link callables must work without a request or object "
                f"when describing (called as func(None, None)): {exc}"
            ) from exc


def _api_error(router: ScrudfulRouter) -> Entity | None:
    """The API-wide error entity of ``router``'s paths: the one
    :func:`add_error_links` gave the APIs mounting it, if any.

    Those APIs are the ones :func:`add_error_links` was called for that
    mount the router (nested or not, see :func:`_mounted_routers`), and
    those whose URL patterns were built with it (ninja binds a router to an
    API then, cloning its paths), whose entity is ``None`` without
    :func:`add_error_links`.

    Raises
    ------
    ConfigError
        If APIs mounting ``router`` were given different entities (or one
        was and another wasn't): its HTTP Schema Resources differ between
        them.
    """
    apis = [
        api
        for api in list(_API_ERRORS.keys())
        if any(mounted is router for mounted in _mounted_routers(api))
    ]
    for mount in list(router._scrudful_mounts):
        for operation in mount.operations:
            api = getattr(operation, "api", None)
            if isinstance(api, NinjaAPI) and all(api is not seen for seen in apis):
                apis.append(api)
    errors: list[Entity | None] = []
    for api in apis:
        error = _API_ERRORS.get(api)
        if error not in errors:
            errors.append(error)
    if len(errors) > 1:
        raise ConfigError(
            "a ScrudfulRouter is mounted on APIs that add_error_links gave "
            "different error entities, so its HTTP Schema Resources differ "
            "between them: pass api= for the one to describe"
        )
    return errors[0] if errors else None


def _mounted_routers(api: NinjaAPI) -> list[Router]:
    """The routers ``api`` mounts, nested ones too, read from ninja's
    ``NinjaAPI._router_registrations`` and ``Router._routers`` (ninja 1.7):
    lists of tuples whose second item is the router, filled by
    ``add_router``, before the API is bound.

    Raises
    ------
    ConfigError
        If the installed ninja has either of them in another shape.
    """
    pending = _routers_in(api, "_router_registrations", "NinjaAPI")
    routers: list[Router] = []
    while pending:
        router = pending.pop()
        if any(router is seen for seen in routers):
            continue
        routers.append(router)
        pending.extend(_routers_in(router, "_routers", "Router"))
    return routers


def _routers_in(owner: Any, attribute: str, owner_name: str) -> list[Router]:
    """The routers of ``owner.<attribute>``, see :func:`_mounted_routers`."""
    entries = getattr(owner, attribute, None)
    if isinstance(entries, list) and all(
        isinstance(entry, tuple) and len(entry) >= 2 and isinstance(entry[1], Router)
        for entry in entries
    ):
        return [entry[1] for entry in entries]
    raise _internal_error(f"{owner_name}.{attribute}")


def _internal_error(internal: str) -> ConfigError:
    return ConfigError(
        "scrudful finds the APIs mounting a ScrudfulRouter (for the error "
        f"entity of add_error_links) through ninja's {internal}, which the "
        "installed ninja doesn't have as ninja 1.7 does: upgrade scrudful, or "
        "keep ninja at 1.7"
    )


def _describe_operation(
    method: str,
    operation: Any,
    config: _Config | None,
    base_url: str | None,
    api_error: Entity | None,
    name: str,
) -> _Flow[dict[str, Any]]:
    """Describe ``operation`` as ``method``; each link callable is a ``yield``.

    The description of :func:`scrudful._adapter.describe_read` /
    :func:`~scrudful._adapter.describe_write` (DRF's), with the statuses the
    operation answers (see :func:`_answered_statuses`), an entry for each
    error status ``responses=`` declares, and the response content of each
    status's entity, but for HEAD, whose responses have no content, and the
    error entries (see :func:`_error_entries`). ``config``
    is :func:`_described_config`'s. Link callables are called with
    ``(None, None)``, and the URLs resolved against ``base_url`` (``name``
    is the operation's, for errors).
    """
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
            content = yield from _media(entity, base_url, f"{name}'s request body")
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
            content = yield from _media(entity, base_url, f"{name}'s {key} response")
            if content:
                response["content"] = content
    if config is not None:
        # The error statuses declared in ``responses=``: an entry each, which
        # Agreement picks before ``default`` and the framework's entries.
        for status in sorted(config.responses):
            key = str(status)
            if status < 400 or key in responses:
                continue
            entry: dict[str, Any] = {"description": reason_phrase(status)}
            if method != "head":
                entity = _described_entity(config, method, status, conditional)
                where = f"{name}'s {key} response"
                content = yield from _media(entity, base_url, where)
                if content:
                    entry["content"] = content
            responses[key] = entry
    yield from _error_entries(
        responses,
        None if config is None else config.error,
        api_error,
        base_url,
        name,
        with_content=method != "head",
    )
    return description


def _error_entries(
    responses: dict[str, Any],
    error: Entity | None,
    api_error: Entity | None,
    base_url: str | None,
    name: str,
    with_content: bool,
) -> _Flow[None]:
    """Add the error entries to ``responses``.

    The ``default`` entry has the content of the entity the operation's own
    errors get: its ``error``, else ``api_error`` (:func:`add_error_links`).
    When ``error`` is set and differs from ``api_error``, ninja's own 401,
    422 and 429, which get ``api_error`` whatever the operation, have
    explicit entries with its content (Agreement picks an exact status
    before ``default``), unless the operation declares them (in
    ``response=`` or ``responses=``, whose entries ``responses`` already
    has); so has the 403
    when ``api_error`` is set (every 401 and 403 answered for the operation
    then carries it, see :func:`_raised_entity`). Without ``with_content``
    (HEAD), the same entries without content.
    """

    def entry(description: str, entity: Entity | None, key: str) -> _Flow[Any]:
        described: dict[str, Any] = {"description": description}
        if with_content:
            content = yield from _media(entity, base_url, f"{name}'s {key} response")
            if content:
                described["content"] = content
        return described

    if error is not None and error != api_error:
        for status, description in _FRAMEWORK_ERRORS.items():
            key = str(status)
            # Without an API-wide entity, a 403 is the operation's (ninja's
            # own carries no entity), under ``default``.
            if key not in responses and (status != 403 or api_error is not None):
                responses[key] = yield from entry(description, api_error, key)
    responses["default"] = yield from entry(
        _DEFAULT_ERROR, api_error if error is None else error, "default"
    )


def _is_conditional(config: _Config, method: str) -> bool:
    """Whether the operation evaluates the If-* headers for ``method``.

    A list's (after the handler, never required) only matter for GET/HEAD; a
    create's, the collection's, for POST. ``method`` is lowercase.
    """
    if config.kind == "list":
        return method in ("get", "head") and config.has_list_validators
    if config.kind == "create":
        return method == "post" and config.has_list_validators
    return config.evaluates_preconditions and evaluates_preconditions(method.upper())


def _answered_statuses(operation: Any, config: _Config | None) -> list[int]:
    """The statuses ``operation`` answers with, for the HTTP Schema Resource.

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
    """The entity of the ``method`` responses with ``status``, for the HSR.

    scrudful's own early responses (listed when ``conditional``) get the
    ``error`` entity, if declared; the others the declared entity.
    """
    if conditional and status in _EARLY_STATUSES:
        return (
            config.error if config.error is not None else config.responses.get(status)
        )
    return config.response_entity(method, status)


def _media(entity: Entity | None, base_url: str | None, where: str) -> _Flow[Any]:
    """The ``content`` of ``entity``: its Media Type Object, or ``None``.

    Built by :func:`scrudful._adapter.describe_content` from the entity's
    schema and context URLs and types, which are evaluated as for its Links,
    in describe mode (each callable called with ``(None, None)``; the
    context only when its media type takes one), with relative URLs
    resolved against ``base_url`` (:func:`scrudful._hsr.absolute_for_hsr`),
    as the Links of the responses it describes are (see :func:`_live_base`).
    ``None`` without a schema URL (e.g. ``Entity()``). ``where`` names the
    entry, for errors.

    Raises
    ------
    HsrConfigError
        If a URL is relative and there is no ``base_url``, a schema has no
        RDF context, or a context of a media type that isn't JSON has no
        type (its callable returned none).
    """
    if entity is None:
        return None
    schema = yield from _value(entity.schema_link_or_func, None, None)
    if not schema:
        return None
    schema_type = yield from _value(entity.schema_type_or_func, None, None)
    context = context_type = None
    rel, default_type = context_defaults(entity.media_type)
    if rel is not None:  # not in-band RDF
        context = yield from _value(entity.context_link_or_func, None, None)
    if context:
        context_type = (
            yield from _value(entity.context_type_or_func, None, None)
        ) or default_type
    schema_url = absolute_for_hsr(str(schema), base_url, f"{where} schema link")
    context_url = (
        absolute_for_hsr(str(context), base_url, f"{where} context link")
        if context
        else None
    )
    try:
        return describe_content(
            entity.media_type,
            schema_url,
            str(schema_type or SCHEMA_TYPE),
            context_url,
            None if context_type is None else str(context_type),
        )
    except ValueError as exc:
        # Also the ValueError of a context without a type, whose callable
        # couldn't be checked when decorating.
        raise HsrConfigError(f"{where}: {exc}") from exc
