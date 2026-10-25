django-ninja
============

``scrudful.ninja`` makes django-ninja operations SCRUDful: conditional requests
(304, 400, 412, 428), ``ETag`` / ``Last-Modified``, the schema and JSON-LD
context ``Link`` headers of the semantic-http spec, collections in the scrud
list envelope, and an automatic OPTIONS answer (the spec's "HTTP Schema
Resource"). Install it with ``pip install "scrudful[ninja]"`` (django-ninja
1.7 or newer, below 2: the adapter relies on django-ninja internals). It does
not need Django REST Framework.

The example project in ``examples/ninja`` uses the three decorators, entities,
error Links and OPTIONS on a ``Role`` resource scoped by an ``rbac_context``;
its tests (``examples/ninja/tests``) show the HTTP exchanges.

The rule everything follows: **no precondition result and no validator is
computed from anything but the object (or queryset) authorization already
approved.** The decorators run inside the operation, after ninja's
authentication, throttling and parameter validation, and an item operation's
validators come from the object its ``load`` callable returns.

Setup
-----

Declare scrudful operations on a :class:`~scrudful.ninja.ScrudfulRouter`: it
adds an OPTIONS operation to every path that has one. For operations declared
on the API itself, make the API's default router one too:

.. code-block:: python

    from ninja import NinjaAPI

    from scrudful.ninja import ScrudfulRouter, add_error_links

    api = NinjaAPI(auth=..., default_router=ScrudfulRouter())
    roles = ScrudfulRouter()
    api.add_router("/roles", roles)
    add_error_links(api, ERROR)  # see "Error responses"

Scrudful operations on a plain ``ninja.Router`` work, but get no OPTIONS
operation (ninja answers 405).

The Links of the OPTIONS response are fixed: the published JSON Schema and
JSON-LD context of the Semantic HTTP 0.1 HTTP Schema Resource, at
``https://w3id.org/semantic-http/0.1/``. The OPTIONS body follows that
revision (see the "Semantic HTTP conformance" section of :doc:`usage`).

HEAD is routed like any method: ninja answers 405 to HEAD on an operation
declared with ``get``. Declare ``api_operation(["GET", "HEAD"], ...)`` for the
paths that should serve it, as the example does for the item and the list.

.. note::

   The schema Link's default relation is ``describedby``, which is what
   Semantic HTTP 0.1 requires of a Schema Link Header. If you override it
   (``schema_rel_or_func="profile"``), the response's Link is no longer a Schema
   Link Header, while the OPTIONS body still describes that schema as the
   response's schema: the API is then not conformant. Use the default.

Single resources
----------------

``@scrudful(...)`` sits **directly below** the operation decorator (above it,
ninja would register the undecorated function, so it raises ``TypeError``;
see :ref:`ninja-placement` for other decorators). ``load``
is the fetch-if-allowed callable; the validator callbacks are called with what
it returns, and the handler receives it in the ``load_as`` parameter (``obj``
by default), hidden from ninja's signature and OpenAPI:

.. code-block:: python

    from django.shortcuts import get_object_or_404
    from ninja import Schema

    from scrudful.ninja import (
        CONDITIONAL_READ_RESPONSES,
        CONDITIONAL_WRITE_RESPONSES,
        Entity,
        scrudful,
    )


    def authorized_role(request, pk: int, **params):
        return get_object_or_404(Role, pk=pk, rbac_context=request.auth)


    ROLE = dict(
        etag_func=lambda role: role.etag,
        last_modified_func=lambda role: role.modified_at,
        schema_link_or_func="/schemas/role",
        context_link_or_func="/contexts/role",
    )
    ERROR = Entity(schema_link_or_func="/schemas/error",
                   context_link_or_func="/contexts/error")


    @roles.api_operation(
        ["GET", "HEAD"],
        "/{int:pk}",
        response=RoleOut,
        openapi_extra={"responses": CONDITIONAL_READ_RESPONSES},
    )
    @scrudful(load=authorized_role, error=ERROR, **ROLE)
    def get_role(request, pk: int, obj: Role):
        return obj


    @roles.put(
        "/{int:pk}",
        response=RoleOut,
        openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES},
    )
    @scrudful(load=authorized_role, error=ERROR, **ROLE)
    def replace_role(request, pk: int, payload: RoleIn, obj: Role):
        return save_role(obj, **payload.dict())

GET and HEAD answer 304 to a matching ``If-None-Match`` / ``If-Modified-Since``
and send the validators on 2xx. PUT, PATCH and DELETE get 428 without a
precondition, 412 when it is stale, 400 for a malformed ``If-Match`` /
``If-None-Match``, and fresh validators on 2xx. With ``error=Entity(...)``
scrudful's own 400, 412 and 428 bodies carry that entity's Links, and so do
the errors the operation raises (see :ref:`ninja-errors`). ``load`` is
required when ``etag_func`` or ``last_modified_func`` is set.

The ``schema_*`` / ``context_*`` arguments are the default entity: its Links go
on 2xx responses, except a 204, which has no body (so a DELETE answering 204
gets none), and except those to TRACE, whose content is the echoed request
(``message/http``), not the item: OPTIONS describes them without content.
``responses={204: Entity(...)}`` (``responses={200: Entity(...)}`` for a TRACE)
or ``ScrudfulResponse.entity`` still put Links on them if you want them.

ninja routes HEAD only to operations that list it, as above.

Handlers in modules with ``from __future__ import annotations`` work, including
when the hidden parameter's annotation is imported only under
``TYPE_CHECKING``: each annotation is evaluated on its own.

Choosing what ``load`` returns
------------------------------

``load`` returns the object, ``None`` ("no current representation") or raises
(e.g. ``Http404``, ``HttpError(404)``), which goes through ninja's exception
handlers. scrudful never tells a missing resource from a hidden one by itself:
what ``load`` does decides what a client can observe. Whether PUT may create,
and how hidden rows are reported, is the application's choice.

.. list-table::
   :header-rows: 1
   :widths: 22 14 14 50

   * - Pattern
     - ``load`` for missing
     - ``load`` for hidden
     - Consequence
   * - **No PUT-create**
     - raise 404
     - raise 404
     - Missing and hidden are indistinguishable for every method. Creation
       goes through POST to the collection.
   * - **PUT-create, nothing hidden**
     - ``None``
     - (n/a)
     - RFC semantics: ``If-None-Match: *`` creates, ``If-Match`` gives 412.
   * - **PUT-create with hidden rows**
     - ``None``
     - raise 404
     - A PUT with ``If-Match`` tells them apart (412 vs 404). Acceptable only
       when identifiers aren't guessable or existence isn't secret.
   * - **PUT-create, hidden treated as missing**
     - ``None``
     - ``None``
     - The handler must cope with a create that collides with a hidden row.
       It must not overwrite the row or 500 on it; a 409 or 404 is a policy
       choice.

When ``load`` returns ``None``, RFC 9110 applies: a PUT with
``If-None-Match: *`` reaches the handler, a PUT with ``If-Match`` is answered
with 412, and the other methods reach the handler, which answers (e.g. 404).

``load`` alone decides whether the resource exists. When it returns an object,
the resource exists even if ``etag_func`` / ``last_modified_func`` return
``None`` for it (RFC 9110 §13.1.1-2): PUT, PATCH and DELETE then need
``If-Match: *`` (428 without a precondition), an ``If-Match`` listing
entity-tags fails with 412 (there is no current entity-tag to match), and
``If-None-Match: *`` fails with 412 (304 for GET and HEAD), so it never
overwrites an existing row.

**No PUT-create** (the example project's choice):

.. code-block:: python

    def authorized_role(request, pk: int, **params):
        # Missing, or in another rbac_context: 404 either way.
        return get_object_or_404(Role, pk=pk, rbac_context=request.auth)

**PUT-create, nothing hidden**: every row is visible to every caller.

.. code-block:: python

    def role_or_none(request, pk: int, **params):
        return Role.objects.filter(pk=pk).first()


    @roles.put("/{int:pk}", response={200: RoleOut, 201: RoleOut})
    @scrudful(load=role_or_none, **ROLE)
    def put_role(request, pk: int, payload: RoleIn, obj: Role | None,
                 meta: ScrudfulResponse):
        if obj is None:
            meta.status = 201
            return Role.objects.create(pk=pk, **payload.dict())
        return save_role(obj, **payload.dict())

**PUT-create with hidden rows**: a hidden row raises, a missing one is
``None``. A PUT with ``If-Match`` gets 412 for a missing row and 404 for a
hidden one.

.. code-block:: python

    def role_or_none_unless_hidden(request, pk: int, **params):
        role = Role.objects.filter(pk=pk).first()
        if role is not None and role.rbac_context != request.auth:
            raise Http404
        return role

    # put_role as above, with load=role_or_none_unless_hidden

**PUT-create, hidden treated as missing**: both are ``None``, so the handler
must handle a create that collides with a hidden row, without overwriting it
and without a 500:

.. code-block:: python

    def visible_role_or_none(request, pk: int, **params):
        return Role.objects.filter(pk=pk, rbac_context=request.auth).first()


    @roles.put("/{int:pk}", response={200: RoleOut, 201: RoleOut, 404: Message})
    @scrudful(load=visible_role_or_none, **ROLE)
    def put_role(request, pk: int, payload: RoleIn, obj: Role | None,
                 meta: ScrudfulResponse):
        if obj is not None:
            return save_role(obj, **payload.dict())
        try:
            with transaction.atomic():
                role = Role.objects.create(pk=pk, **payload.dict())
        except IntegrityError:  # the pk is a hidden row's: a policy choice
            return Status(404, {"detail": "Not Found"})
        meta.status = 201
        return role

After a successful (2xx) PUT or PATCH, ``load`` is called again, so that the
validators and the Links describe the representation just written: the
validators are computed from what it returns, and link callables get it. The
second call happens when ``etag_func`` or ``last_modified_func`` is set or a
value of the response's entity is callable, and not otherwise (static Links
need no object). If it raises then (e.g. the write moved the object out of the
caller's scope), the client gets that error although the write was committed:
make ``load`` find what the handler just wrote.

The preconditions are evaluated before the handler runs, so two concurrent
requests with the same ``If-Match`` can both pass them. For strict lost-update
protection, re-check in the write: ``select_for_update()`` in ``load`` inside a
transaction, or ``UPDATE ... WHERE etag = <loaded etag>`` as the example's
``save_role`` does.

Create
------

``@scrudful_create(...)`` decorates a POST on a collection. There is no
``load`` and no precondition evaluation: the handler authorizes from the
parsed body. A plain return value gets **201**, with ``Location``
(``location_func``), the item's Links and the item's ``ETag`` and
``Last-Modified``, so the client can edit the new item without a GET. Pass the
item operations' callbacks:

.. code-block:: python

    @roles.post("", response={201: RoleOut, 403: Message})
    @scrudful_create(location_func=role_url, **ROLE)
    def create_role(request, payload: RoleIn):
        if payload.rbac_context != request.auth:
            return Status(403, {"detail": "Forbidden"})
        return Role.objects.create(**payload.dict())

The operation must declare ``response={201: ...}`` (or ``...``): with
``response=RoleOut`` (a 200) ninja raises ``ConfigError``.

The created object is the handler's return value, or what it sets as
``ScrudfulResponse.created``. A handler that returns ``None`` (e.g. with
``response={201: None}``) or an ``HttpResponse`` has created no object as far
as scrudful knows: its 201 gets no ``Location`` or validators, and
``location_func`` and the validator callbacks are not called. A body still
gets Links: an ``HttpResponse`` with content (e.g.
``JsonResponse(item, status=201)``) gets the item's Links when none of the six
link values is callable. A link callable would be called with ``None`` in place
of the created item, so with one it is not called and the 201 gets no item
Links. A ``None`` body or an empty ``HttpResponse`` gets no item Links.
``responses[201]`` and ``ScrudfulResponse.entity`` replace the item entity as
usual. Set ``meta.created`` to get them all, or ``meta.location`` for the
``Location`` alone:

.. code-block:: python

    @roles.post("", response={201: None})
    @scrudful_create(location_func=role_url, **ROLE)
    def create_role(request, payload: RoleIn, meta: ScrudfulResponse):
        meta.created = Role.objects.create(**payload.dict())

.. _ninja-placement:

Placement of other decorators
-----------------------------

When ninja builds an operation, the scrudful decorator replaces its
``view_func`` by a wrapper bound to that operation's ``response=``. Where
another decorator goes decides what it runs before:

* **Never put an authorization or caching decorator below the scrudful
  decorator.** A decorator on the handler runs after ``load`` and the
  preconditions: a 304, 412 or 428, or an ``ETag``, may already be decided
  (e.g. an unauthorized ``If-None-Match`` would get a 304 with the real
  entity-tag).
* **Per-operation authorization** goes in ``load`` (fetch-if-allowed) or in
  ninja's ``auth=``.
* **A decorator for every operation of a router or the API**:
  ``router.add_decorator(decorator, mode="operation")`` (or the API's). It
  wraps ``view_func``, so it runs inside ``Operation.run``, after ninja's
  authentication and throttling and before ``load``, and its exceptions go
  through ninja's exception handlers.
* **A wrapper for one operation**, in that same place: a callback registered
  with ``ninja.utils.contribute_operation_callback`` that wraps
  ``operation.view_func``, contributed by a decorator applied *above* the
  scrudful decorator, so that it runs after the binding:

  .. code-block:: python

      import functools

      from ninja.utils import contribute_operation_callback

      def audited(func):
          def wrap(operation):
              inner = operation.view_func

              @functools.wraps(inner)
              def view_func(request, **values):
                  audit(request)
                  return inner(request, **values)

              operation.view_func = view_func

          contribute_operation_callback(func, wrap)
          return func

      @roles.get("/{pk}", response=RoleOut)
      @audited
      @scrudful(load=load_role, **ROLE)
      def get_role(request, pk: int, obj): ...

* ``decorate_view`` (and ``add_decorator`` with ``mode="view"``) wraps
  ``Operation.run``: it runs before ninja's authentication and outside its
  exception handlers. It suits response-level concerns such as
  ``cache_page``, not a check that raises.

A ``view_func`` replaced before the binding would be discarded by it: such a
callback contributed below the scrudful decorator, or any decorator between the
operation decorator and the scrudful one, makes ninja raise
``ninja.errors.ConfigError`` when the operation is built. Stacking two scrudful
decorators on one handler is not supported: it raises ``ConfigError``, or
``TypeError`` when the inner one has ``load``.

General POST
------------

A POST that is not a create, e.g. ``POST /roles/search``, is a plain
``@scrudful``: 200 with the result's Links, no ``Location`` and no validators.
``request=Entity(...)`` describes the query body (for OPTIONS), separately from
the result:

.. code-block:: python

    @roles.post("/search", response=List[RoleOut])
    @scrudful(
        request=Entity(schema_link_or_func="/schemas/role-search",
                       context_link_or_func="/contexts/role-search"),
        schema_link_or_func="/schemas/role-search-result",
        context_link_or_func="/contexts/role-search-result",
    )
    async def search_roles(request, search: RoleSearch):
        roles = Role.objects.filter(rbac_context=request.auth,
                                    name__icontains=search.name)
        return [role async for role in roles]

Collections
-----------

``@scrudful_list(...)`` works like ninja's ``@paginate``: the handler returns
the authorized queryset, the operation declares ``response=List[RoleOut]``, and
the response schema is replaced by a typed envelope (``Scrudful<Item>Page``),
so ninja's validation and OpenAPI describe the real body:

.. code-block:: python

    @roles.get("", response=List[RoleOut])
    @scrudful_list(
        href_func=role_url,
        **ROLE,
        list_etag_func=roles_etag,
        list_last_modified_func=roles_modified,
        list_schema_link_or_func="/schemas/roles",
        list_context_link_or_func="/contexts/roles",
    )
    def list_roles(request):
        return Role.objects.filter(rbac_context=request.auth).order_by("pk")

.. code-block:: json

    {"count": 240, "page_count": 3,
     "first": "…?page=1", "previous": "…?page=1",
     "next": "…?page=3", "last": "…?page=3",
     "content": [{"href": "https://…/roles/17", "etag": "\"…\"",
                  "last_modified": "Thu, 22 Oct 2026 21:09:56 GMT",
                  "link": "</schemas/role>; rel=\"describedby\"; …",
                  "content": {"id": 17, "name": "admin"}}]}

Each entry's ``etag``, ``last_modified`` and ``link`` come from the same item
callbacks as the detail GET. ``page`` and ``page_size`` (default 100, at most
1000) are query parameters. The optional list validators are called with the
queryset, and then a list can answer 304.

The collection response is a list, set or tuple of the item schema
(``List[RoleOut]``, ``list[RoleOut]``). ``Optional[List[RoleOut]]`` or another
union raises ``ConfigError`` when the operation is built, since the envelope is
always sent, and so does a bare ``list``, which names no item schema for the
entries.

* Order the queryset: an unordered one raises Django's
  ``UnorderedObjectListWarning``, as with Django's paginator, because its pages
  are not stable.
* A 2-tuple returned from a list handler is ninja's deprecated
  ``(status, body)``, not a sequence: return a list, or wrap it in ``Status``.
* In an async list operation the queryset is counted and sliced in a thread,
  but ninja serializes the items on the event loop, so the item schema must
  not touch lazy relations: use ``select_related`` / ``prefetch_related`` in
  the handler. ninja's ``@paginate`` has the same limit.
* In an async list operation whose ``href_func``, item validator callbacks and
  item link callables are all sync, the count, the slice and every entry's
  values are computed in one ``sync_to_async`` call per page. An ``async def``
  one among them is awaited, and the others then take a thread hop per call.

The collection documents
~~~~~~~~~~~~~~~~~~~~~~~~

:func:`scrudful.collection.collection_schema` and
:func:`scrudful.collection.collection_context` build the JSON Schema and the
JSON-LD context of the envelope, from the item's schema and context URIs.
Serving them is the application's job (``examples/ninja/roles/documents.py``
serves them next to the item documents).

OPTIONS
-------

The automatic OPTIONS operation answers, for each method the path serves, an
HTTP Schema Resource (Semantic HTTP 0.1), the shape DRF's ``ScrudfulMetadata``
produces too: the request and response ``content`` (per media type, a
``schema`` ``$ref`` and, for a non-in-band type, an ``x-rdf-context`` ``href``),
the statuses and the precondition headers of conditional writes. The statuses
are those the operation answers: the ones it declares in ``response=`` (200
without one), plus 201 for a ``@scrudful_create`` operation, and scrudful's
early responses (304, 400, 412, 428) when it evaluates preconditions. So
``POST /roles/search`` lists 200, where DRF's ``ScrudfulMetadata`` lists 201 for
every POST. It is the same for everyone:

* it runs no ``load``, handler or validator callback, ignores the API's and
  the router's ``auth``, and scrudful runs no permission check for it, so it
  reveals nothing about which items exist;
* ninja still applies the decorators added with ``add_decorator`` on the API
  or a router, and the API's and routers' throttles: "no permission check"
  means scrudful runs none, and a permission decorator added that way gates
  OPTIONS too;
* every automatic OPTIONS operation has the URL name ``scrudful_options``, so
  the name can't be reversed to one path;
* the reason phrases of extra statuses are ``http.HTTPStatus(code).phrase``,
  which can differ between Python versions (e.g. for 422);
* link callables are called with the request and ``obj=None`` (there is no
  loaded object, queryset or created item), so they must accept ``None``;
* the response always has the two fixed HTTP Schema Resource Links, an
  ``Allow`` header listing the body's methods and, for the paths that serve
  it, a ``head`` entry without content.

CORS
~~~~

Browsers send CORS preflight requests (an OPTIONS with
``Access-Control-Request-Method``) without credentials. django-cors-headers
answers them in its middleware, before the view runs. scrudful's OPTIONS is the
non-preflight case: a preflight that does reach it gets the description like
any other OPTIONS. With django-cors-headers, list ``ETag``, ``Link`` and
``Location`` in ``CORS_EXPOSE_HEADERS``: it replaces the
``Access-Control-Expose-Headers`` scrudful sets.

The runtime response
--------------------

A handler parameter annotated :class:`~scrudful.ninja.ScrudfulResponse` is the
one place to set, for this response, the status, ``Location``, ``ETag`` /
``Last-Modified``, the entity whose Links it gets, and (for a create) the
created object. Headers the handler sets on the response itself are never
overwritten. An ``etag`` or ``last_modified`` set there on an operation that
also has ``etag_func`` / ``last_modified_func`` must stay consistent with
them: the preconditions of the next request are evaluated with the computed
values.

.. _ninja-errors:

Error responses
---------------

The semantic-http spec asks for the schema and context Links on every response
with content, error responses included. scrudful adds them in two places.

**Errors a scrudful operation raises.** An ``HttpError`` (or a subclass) or
``Http404`` raised by ``load``, a callback, the handler or a list's paging (the
404 of a page past the last one) propagates as before, through
``add_decorator(mode="operation")`` decorators (so a transaction still rolls
back), and ninja answers it with the API's exception handler: the status and
body are the API's, custom handlers included. That response then gets the Links
of the entity the operation's OPTIONS describes for its status:
``responses[status]`` if declared (``error`` first for scrudful's own 400, 412
and 428 when the operation evaluates the ``If-*`` headers), else ``error``. So a
404 a client gets agrees with the HTTP Schema Resource:

.. code-block:: python

    NOT_FOUND = Entity(schema_link_or_func="/schemas/not-found",
                       context_link_or_func="/contexts/not-found")


    @roles.get("/{int:pk}", response={200: RoleOut, 404: Message})
    @scrudful(load=authorized_role, responses={404: NOT_FOUND}, error=ERROR,
              **ROLE)
    def get_role(request, pk: int, obj: Role):
        return obj

Their link callables are called with the request and ``obj=None``: nothing was
loaded. A status the operation has no entity for gets no Link from it.

**Errors ninja answers before the operation runs**, the 422 of a parameter that
does not validate, the 401 of missing credentials, the 403 of an
``AuthorizationError`` and the 429 of a throttle, come from the API's exception
handlers. :func:`~scrudful.ninja.add_error_links` gives them an API-wide error
entity:

.. code-block:: python

    from scrudful.ninja import add_error_links

    api = NinjaAPI(auth=..., default_router=ScrudfulRouter())
    add_error_links(api, ERROR)

It wraps the handlers registered for ``ninja.errors.ValidationError``,
``AuthenticationError``, ``AuthorizationError`` (when the installed ninja
defines it), ``Throttled``, ``HttpError`` and ``Http404``, so it also covers
the errors of plain ninja operations, and of scrudful operations without an
entity for the status (a scrudful operation's own entity wins). Only a class
with a handler of its own is wrapped: with ninja's defaults,
``AuthenticationError``, ``AuthorizationError`` and ``Throttled`` have none and
fall back to the (wrapped) ``HttpError`` handler, as ninja looks handlers up.
Each wrapped handler keeps the original's status and body and adds the entity's
Links when the response has content. A Link the handler set itself with the
relation of the entity's schema link suppresses that link, and one with either
context relation (JSON-LD or ``rdf-context``, one slot) its context link, so a
response that already has scrudful's Links gets no Link twice, nor two RDF
Context Link Headers. Calling it again
replaces the entity of the earlier call: the wrapped handlers are unwrapped
first, so only the latest entity's Links, media type and link callables apply.
Call it after registering your own exception handlers: it wraps the
ones registered when it is called, and a handler registered later replaces the
wrapped one, for its class and for the subclasses without a handler of their
own (a later ``HttpError`` handler answers the 401, 403 and 429 too, without
Links).
ninja's exception handlers are sync, also for async operations, where they run
on the event loop: the entity's link callables must be sync (``TypeError``
otherwise) and must not use the ORM there.

scrudful's own 400, 412 and 428 are returned by the operation, not raised, so
no exception handler sees them: they carry Links only from the decorator's
``error``. Give every conditional ``@scrudful`` and ``@scrudful_list`` an
``error=``, also with :func:`~scrudful.ninja.add_error_links` installed.

**The error media type.** An error entity describes JSON bodies: its
``media_type`` must be ``application/json`` or another ``+json`` type (e.g.
``application/problem+json``), other than JSON-LD; anything else raises
``ValueError``, from the decorators and from
:func:`~scrudful.ninja.add_error_links`. An error response is served with the
media type of the entity whose Links it carries, as the OPTIONS body lists it:
scrudful's own 400, 412 and 428, and the responses the API's exception
handlers make for the errors raised inside a scrudful operation and for those
:func:`~scrudful.ninja.add_error_links` covers. ninja renders those
``application/json``, and scrudful replaces that ``Content-Type`` with the
entity's ``media_type`` whatever the handler chose, so every wrapped handler's
JSON bodies must be content the entity describes. A handler's response with
other content (e.g. ``text/plain``) keeps its ``Content-Type``.

Not covered, since no ninja exception handler makes them:

* ninja's own 405 for a method that no operation of the path routes (e.g. a
  POST on a path with only a GET);
* Django's 404 for a URL no pattern matches, and Django's 500 page (or ninja's
  traceback page for other exceptions with ``DEBUG = True``).

Give those Links in Django: custom ``handler404`` / ``handler500`` views, or a
middleware adding them to error responses.

Async operations
----------------

An ``async def`` handler makes an async operation; the steps run in the same
order with the same results. Sync ``load``, validator and link callables run
through ``sync_to_async``, so they may use the ORM; ``async def`` ones are
awaited. With a sync handler every callable must be sync (``TypeError`` at
decoration time otherwise).

Reference
---------

.. automodule:: scrudful.ninja
   :members:
   :member-order: bysource
   :exclude-members: Entity

.. automodule:: scrudful.collection
   :members: collection_schema, collection_context
