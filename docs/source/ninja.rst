django-ninja
============

``scrudful.ninja`` makes django-ninja operations SCRUDful: conditional requests
(304, 400, 412, 428), ``ETag`` / ``Last-Modified``, the schema and JSON-LD
context ``Link`` headers of the semantic-http spec, collections in the scrud
list envelope, an automatic OPTIONS answer with a per-requester ``Allow``, and
each path's "HTTP Schema Resource", served by GET and linked from every
response. Install it with ``pip install "scrudful[ninja]"`` (django-ninja
1.7 or newer, below 2: the adapter relies on django-ninja internals). It does
not need Django REST Framework.

The example project in ``examples/ninja`` uses the three decorators, entities,
error Links, OPTIONS and the 404 and 500 handlers on a ``Role`` resource scoped
by an ``rbac_context``; its tests (``examples/ninja/tests``) show the HTTP
exchanges and run the wire-conformance suite's checks.

The rule everything follows: **no precondition result and no validator is
computed from anything but the object (or queryset) authorization already
approved.** The decorators run inside the operation, after ninja's
authentication, throttling and parameter validation, and an item operation's
validators come from the object its ``load`` callable returns.

Setup
-----

Declare scrudful operations on a :class:`~scrudful.ninja.ScrudfulRouter`: it
adds an OPTIONS operation and an HTTP Schema Resource route to every path that
has one (see `OPTIONS and the HTTP Schema Resource`_). For operations declared
on the API itself, make the API's default router one too. Set the base URL the
relative link URLs resolve against, and install the 404 and 500 handlers in
the root URLconf:

.. code-block:: python

    # api.py
    from ninja import NinjaAPI

    from scrudful.ninja import ScrudfulRouter, add_error_links

    api = NinjaAPI(auth=..., default_router=ScrudfulRouter())
    roles = ScrudfulRouter()
    api.add_router("/roles", roles)
    add_error_links(api, ERROR)  # see "Error responses"

    # settings.py
    SCRUDFUL = {"BASE_URL": "https://api.example.com/"}

    # urls.py
    import scrudful.ninja

    urlpatterns = [path("api/", api.urls)]
    PAGE_ERROR = scrudful.ninja.Entity(
        "/api/schemas/error", context_link_or_func="/api/contexts/error"
    )
    handler404 = scrudful.ninja.handler404(PAGE_ERROR)
    handler500 = scrudful.ninja.handler500(PAGE_ERROR)

Scrudful operations on a plain ``ninja.Router`` work, but get no OPTIONS
operation (ninja answers 405), no HTTP Schema Resource and no HSR link.

Every operation on a ``ScrudfulRouter`` is described in its path's HTTP Schema
Resource, so each needs an entity: a scrudful decorator's, or, for a plain
operation, the one :func:`~scrudful.ninja.described` gives it. Adding a plain
operation without one raises ``ConfigError``:

.. code-block:: python

    @roles.get("/health")
    @described(Entity("/schemas/health", context_link_or_func="/contexts/health"))
    def health(request): ...

A ``@described`` operation's responses get the entity's Links and the HSR
link, as a scrudful operation's do; its link callables get ``(request,
None)``.

HEAD is routed like any method: ninja answers 405 to HEAD on an operation
declared with ``get``. Declare ``api_operation(["GET", "HEAD"], ...)`` for the
paths that should serve it, as the example does for the item and the list.

.. note::

   The schema Link's default relation is ``describedby``, which is what
   Semantic HTTP 0.1 requires of a Schema Link Header. If you override it
   (``schema_rel_or_func="profile"``), the response's Link is no longer a Schema
   Link Header, while the HTTP Schema Resource still describes that schema
   as the response's schema: the API is then not conformant. Use the default.

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
(``message/http``), not the item: the HTTP Schema Resource describes them
without content.
``responses={204: Entity(...)}`` (``responses={200: Entity(...)}`` for a TRACE)
or ``ScrudfulResponse.entity`` still put Links on them if you want them.

ninja routes HEAD only to operations that list it, as above.

Handlers in modules with ``from __future__ import annotations`` work, including
when the hidden parameter's annotation is imported only under
``TYPE_CHECKING``: each annotation is evaluated on its own.

``@scrudful`` and ``@scrudful_create`` take the hooks of ``@scrudful_list``
too (since 0.8.0), with the same contract and guards (see
:ref:`before= and after_throttles= <ninja-hooks>`): ``before(request,
**path_params)`` runs where DRF checks permissions (``has_permission``),
after ninja's authentication and before its throttles, and
``after_throttles(request, **path_params)`` after the throttles. Both run
before ninja validates any parameter or the body, so before ``load`` and the
preconditions: a client a hook refuses never gets a 304, 412, 428 or 422, or
a validator, and a throttled client gets 429 only once ``before`` allowed it,
as on DRF. The path parameters are the URL's values, not validated yet:
strings, unless the path uses a converter (``pk`` is an ``int`` below). Never
deny by testing a raw string for a value: ``"07"`` is 7 to an ``int``
parameter (see :ref:`below <ninja-hooks>`). Called directly (outside ninja,
e.g. in a unit test), the decorated function of an operation with a hook
raises ``RuntimeError``, since the hooks didn't run: test it through ninja's
``TestClient``. A hook gets the raw request, and may read the body as the
operation's parameters do:

- a JSON body through ``request.body``, which ninja then reads again from
  memory. A body over Django's ``DATA_UPLOAD_MAX_MEMORY_SIZE`` fails there as
  when ninja reads it: Django's 400 (with ``DEBUG``, ninja's 500 traceback).
- a form or an upload (``Form`` and ``File`` parameters) through
  ``request.POST`` and ``request.FILES``, never ``request.body``. A multipart
  body may already be parsed when the hook runs: on a POST by ninja's CSRF
  check (an ``APIKeyCookie`` or ``SessionAuth`` such as ``django_auth``, which
  check CSRF by default), on a PUT, PATCH or DELETE by
  ``fix_request_files_middleware``. ``request.body`` then raises Django's
  ``RawPostDataException`` (a 500). Otherwise it would hold every uploaded
  file in memory, failing beyond ``DATA_UPLOAD_MAX_MEMORY_SIZE``, a limit
  Django doesn't apply to uploaded files.

An item operation refusing, before its throttles, a client that can edit
nowhere:

.. code-block:: python

    def may_edit_somewhere(request, **path_params):
        if not can_edit_in_some_context(getattr(request, "auth", None)):
            raise HttpError(403, "Forbidden")

    @roles.put("/{int:pk}", response=RoleOut,
               openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES})
    @scrudful(load=authorized_role, error=ERROR, before=may_edit_somewhere, **ROLE)
    def replace_role(request, pk: int, payload: RoleIn, obj: Role):
        return save_role(obj, **payload.dict())

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
``load``: the handler authorizes from the parsed body. A plain return value
gets **201**, with ``Location``
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

A create's target is the collection, so its preconditions are the
collection's. Pass ``list_load(request, **params)``, returning what the list
operation's handler returns for the same request, and the list operation's
``list_etag_func`` or ``list_last_modified_func``: for a POST with
``If-Match``, ``If-None-Match`` or ``If-Unmodified-Since``, the list validators
are computed from the collection before the handler runs and the
preconditions are evaluated against them. ``If-Match`` with an ETag the list
doesn't have (a strong comparison: a weak list ETag never matches), a past
``If-Unmodified-Since``, ``If-None-Match: *`` or ``If-None-Match`` with the
list's ETag is a **412**, a malformed ``If-Match`` or ``If-None-Match`` a
**400**, with ``error``'s Links and ``error_body_func``'s body, and nothing is
created. A ``list_load`` returning ``None`` means there is no collection (e.g.
its parent is missing): no precondition can fail and the handler answers, as
with ``load``, though a malformed header is still a 400. A create without a
precondition proceeds without calling ``list_load``: never a 428. The HTTP
Schema Resource lists the 400 and 412. The list validators are evaluated for a
POST only, so an operation without POST raises ``ConfigError`` when it is
built. ``list_load``
must apply the list operation's authorization: the check runs before the
handler, so before any authorization the handler decides from the body.

.. code-block:: python

    def authorized_roles(request, **params):
        return Role.objects.filter(rbac_context=request.auth)

    @roles.post("", response={201: RoleOut, 403: Message})
    @scrudful_create(
        location_func=role_url,
        **ROLE,
        list_load=authorized_roles,
        list_etag_func=roles_etag,
        list_last_modified_func=roles_modified,
        error=ERROR,
    )
    def create_role(request, payload: RoleIn): ...

The check and the create are atomic only if you serialize every write that
changes the collection's validators with conditional creates: lock a row per
collection in ``list_load`` (``select_for_update()``) with a transaction
spanning the whole operation, so the lock is held until the create commits, and
take the same lock in the other writes (unconditional creates, which don't call
``list_load``, and the item operations). Otherwise two requests with the same
``If-Match`` can both create.

* The transaction is ``ATOMIC_REQUESTS = True``, or
  ``router.add_decorator(transaction.atomic, mode="operation")`` on a router
  whose operations are all sync (see :ref:`ninja-placement`). ninja serves a
  path asynchronously when any of its operations is async, and Django refuses
  ``ATOMIC_REQUESTS`` for async views, so a sync create sharing its path with
  an async operation can use neither (``add_decorator`` would wrap the async
  one too). Wrap only the create's ``view_func`` in ``transaction.atomic``,
  with a ``contribute_operation_callback`` callback contributed above the
  scrudful decorator, as in :ref:`ninja-placement`: ninja runs the sync
  operation in one thread, so ``list_load`` and the handler share the
  transaction.
* When the list operation's handler calls the same function as ``list_load``
  (e.g. it returns ``authorized_roles(request)``), lock only when
  ``request.method == "POST"``, or every GET of the list takes the lock too.
* An async operation can't hold the lock: ``list_load`` and the handler are
  called separately, so no transaction spans both. Re-check the collection in
  the handler's own transaction and raise ``HttpError(412)`` if it changed.

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
* **Per-operation authorization** goes in ``load`` (fetch-if-allowed),
  ``before=`` or ninja's ``auth=``: OPTIONS's ``Allow`` sees the last two.
* **A decorator for every operation of a router or the API**:
  ``router.add_decorator(decorator, mode="operation")`` (or the API's). It
  wraps ``view_func``, so it runs inside ``Operation.run``, after ninja's
  authentication and throttling and before ``load``, and its exceptions go
  through ninja's exception handlers. It also wraps the HTTP Schema Resource
  route, and OPTIONS's ``Allow`` doesn't see it: authorization done there
  needs an ``allowed=`` hook too (see `OPTIONS and the HTTP Schema
  Resource`_).
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
``request=Entity(...)`` describes the query body (in the HTTP Schema
Resource), separately from the result:

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
1000) are query parameters; bad values are ninja's 422 validation error,
listing every bad parameter. The optional list validators are called with the
queryset, and then a list can answer 304.

.. _ninja-hooks:

ninja validates the parameters before the operation runs, so a bad ``page``
would be a 422 even for a client the list must refuse. ``before=`` runs where
DRF checks permissions: ``before(request, **path_params)`` is called once per
request after ninja's authentication and before its throttles and its
validation of any parameter, with the URL's path parameters as they are (not
validated yet). They are strings (unless the path uses a converter such as
``{int:pk}``), not the values the function gets: ``"07"``, ``"+7"`` and
``" 7"`` are all 7 to an ``int`` parameter, so a ``before`` denying
``path_params["org"] == "7"`` lets ``/orgs/07/...`` through. Never deny by
testing a raw string for a value: compare the value the function gets
(declare the parameter ``str``, as below), or parse it as its type does and
refuse what doesn't parse. ``before`` returns ``None`` to let the request go
on; anything else it returns (``False``, a response) raises ``TypeError``,
as a refusal is raised, never returned. ``request.auth`` is set only when an
auth callback succeeded (the examples here run under ``NinjaAPI(auth=...)``,
where it always is): on an operation that may run without ``auth``, fail
closed before using it, as below. Raise ``HttpError(401, ...)`` or ``HttpError(403, ...)``
there; the API's exception handler answers it, and the refused request uses
none of a throttle's quota. When it returns, ninja throttles and validates
every parameter as without ``before``: one 422 lists every bad one, ``page``
and ``page_size`` included, and the OpenAPI description is unchanged. Neither
its error nor that 422 passes through the decorators added with
``add_decorator(mode="operation")``. scrudful installs ``before`` through
``Operation.run``: a ``decorate_view`` decorator below ``@scrudful_list``
must use ``functools.wraps`` (``ConfigError`` otherwise, when the operation is
built). ``before`` runs in scrudful's own copy of ninja's per-operation checks
(CSRF flag, authentication, throttles), which relies on ``Operation.run``
answering their response before it reads any parameter. Both are verified
against ninja 1.7.0 and 1.7.1; the ``ninja`` extra allows any ninja from 1.7
below 2, so a later release may be installed before scrudful has verified
it. When the
operation is built, scrudful compares ninja's ``_run_checks`` and ``run``
with those releases': by a digest of their source (comments, docstrings and
blank lines aside), or, where the source isn't installed (``.pyc`` only),
only by the names, local variables and arguments they use. On a ninja whose
code differs, or with an ``Operation`` subclass overriding either method,
``before=`` raises ``ConfigError`` at startup instead of skipping a check:
upgrade scrudful, or keep ninja at a verified release (an app using
``before=`` should lock its ninja version). Without ``before=`` or
``after_throttles=`` the check is never made. Each request is checked too:
one that reaches the operation's function without the hooks having allowed it
(ninja served a copy of the operation without scrudful's checks) fails with
``RuntimeError``, never goes on unchecked.

Here the API's auth callback returns the caller's ``rbac_context`` (a
string, as in the example project), and the path names the context listed.
The path parameter is declared ``str``, so ``before`` compares the value the
handler gets:

.. code-block:: python

    def same_context(request, **path_params):
        context = getattr(request, "auth", None)
        if context is None:  # no auth callback succeeded: fail closed
            raise HttpError(401, "Unauthorized")
        if path_params["context"] != context:
            raise HttpError(403, "Forbidden")

    @roles.get("/contexts/{context}/roles", response=List[RoleOut])
    @scrudful_list(href_func=role_href, before=same_context)
    def list_context_roles(request, context: str):
        return Role.objects.filter(rbac_context=context).order_by("pk")

``after_throttles=`` (since 0.8.0) runs where DRF's ``list()`` starts:
``after_throttles(request, **path_params)`` is called once per request after
ninja's authentication, after ``before`` (if given) and after ninja's
throttles, and still before its validation of any parameter. A throttled
request (429) never calls it, so a client can't force an expensive check
(e.g. a per-row authorization filter that may answer 400) by sending
requests over its quota, and its error beats the 422 of a bad ``page``, as
in DRF, where ``list()`` filters before it paginates. A request it refuses
has used its throttle quota, as in DRF. Its contract is ``before``'s: the
same raw path parameters (each hook gets its own copy), ``None`` to go on
(anything else, or an awaitable in a sync operation, raises ``TypeError``),
an error raised to answer first, answered by the API's exception handler
with the operation's error Links and never passing through the
``add_decorator(mode="operation")`` decorators, ``async def`` in an
``async def`` operation, the same ``ConfigError`` at startup and the same
``RuntimeError`` for a request that reaches the function without it having
allowed it. When it returns, one 422 lists every bad parameter, and the
OpenAPI description is unchanged. Use it alone or with ``before``; with both,
the order is DRF's: authentication, ``before``, throttles,
``after_throttles``, then the parameters:

.. code-block:: python

    def not_too_large(request, **path_params):
        if conditional_rows(request.auth) > LIMIT:
            raise HttpError(400, "conditional_list_too_large")

    @roles.get("/contexts/{context}/roles", response=List[RoleOut])
    @scrudful_list(
        href_func=role_href, before=same_context, after_throttles=not_too_large
    )
    def list_context_roles(request, context: str):
        return Role.objects.filter(rbac_context=context).order_by("pk")

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

OPTIONS and the HTTP Schema Resource
------------------------------------

A :class:`~scrudful.ninja.ScrudfulRouter` gives every path with a scrudful (or
``@described``) operation an HTTP Schema Resource (HSR), serves it by GET on a
route of its own, links it from every response of the path, and answers the
path's OPTIONS with a 204 whose ``Allow`` lists what the requester may use.
:doc:`usage` describes the model shared by the three adapters ("OPTIONS and
the HTTP Schema Resource"); this section is django-ninja's part.

Settings
~~~~~~~~

Each argument of ``ScrudfulRouter`` defaults (``None``) to the key of the
optional ``SCRUDFUL`` Django setting, then to scrudful's default:

.. list-table::
   :header-rows: 1
   :widths: 22 22 56

   * - ``ScrudfulRouter(...)``
     - ``SCRUDFUL[...]``
     - Meaning
   * - ``base_url``
     - ``"BASE_URL"``
     - The absolute URL relative link URLs resolve against, in the HSR and on
       the Link headers (see "The base URL" in :doc:`usage`). Without one, a
       relative link URL is a ``ConfigError`` when the HSR is built, and the
       Link headers and the HSR link resolve against the request.
   * - ``hsr_route``
     - ``"HSR_ROUTE"``
     - Whether the router serves each path's HSR (default ``True``).
       ``False`` needs an ``hsr_url`` (``ConfigError`` when the router is
       created).
   * - ``hsr_url``
     - ``"HSR_URL"``
     - ``hsr_url(path)``: the absolute URL of the HSR of ``path`` (the
       router's path, as given to the operation decorator), for a document
       served elsewhere. The responses link to it as it returns it, even when
       the router serves the route too.
   * - ``hsr_cache_control``
     - ``"HSR_CACHE_CONTROL"``
     - The ``Cache-Control`` of the HSR route (default ``public,
       max-age=300``).
   * - ``allowed``
     - (none)
     - ``allowed(request, method, obj)``, the ``Allow`` hook of the router's
       paths (see `OPTIONS`_ below).

``SCRUDFUL`` must be a dict of those keys (``"ERROR"`` is DRF's; django-ninja's
API-wide error entity is :func:`~scrudful.ninja.add_error_links`'s): another
value, or an unknown key, raises ``ImproperlyConfigured``. A ``base_url`` that
isn't absolute is a ``ConfigError``.

The document
~~~~~~~~~~~~

The HSR of a path (an OpenAPI 3.1 Path Item, see
:func:`~scrudful.ninja.http_schema_resource`) describes each method the path
serves, in the order ``get``, ``put``, ``post``, ``delete``, ``options``,
``head``, ``patch``, ``trace``, in the shape DRF's
:func:`~scrudful.rest_framework.http_schema_resource` gives too: the request
and response ``content`` (per media type, a ``schema`` ``$ref`` and, for a
non-in-band type, an ``x-rdf-context`` ``href``), the statuses and the
precondition headers of conditional writes. The statuses are those the
operation answers: the ones it declares in ``response=`` (200 without one),
plus 201 for a ``@scrudful_create`` operation, and scrudful's early responses
(304, 400, 412, 428) when it evaluates preconditions. So ``POST
/roles/search`` lists 200, where DRF lists 201 for a POST without
``statuses=``. Each method also has a ``default`` entry for its errors, with
the content of the operation's ``error``, else of the API-wide entity
(:func:`~scrudful.ninja.add_error_links`); when the operation's ``error``
differs from the API-wide one, the 401, 422 and 429 that ninja answers itself,
with the API-wide entity, have entries of their own. ``head`` (where an
operation routes it) is described like ``get``, without content, and
``options`` is exactly ``{"responses": {"204": {"description": "No
Content"}}}``.

* It is built once per path template, without a request: on the first GET of
  its route (or :func:`~scrudful.ninja.http_schema_resource` call), and again
  after an operation is added to the router or ``add_error_links`` is
  called. It is the same for every requester and ``Host``.
* Link callables are called in *describe* mode, ``func(None, None)``: they
  must return the target they return on the responses, without request or
  object, or the HSR disagrees with the Links. One that raises is a
  ``ConfigError`` naming the operation. Those of an async operation may be
  ``async def``; they are awaited with ``asgiref.sync.async_to_sync``. The
  entity of ``add_error_links`` is described the same way.
* Relative link URLs resolve against the router's base URL; without one, a
  relative URL is a ``ConfigError`` when the HSR is built.
* A schema link without an RDF context link, for a non-in-band media type,
  would give a Media Type Object without ``x-rdf-context``, which the spec's
  JSON Schema rejects: ``Entity(...)`` and the decorators raise
  ``scrudful._hsr.HsrConfigError`` (a ``ValueError``) for a plain value, and
  building the HSR raises ``ConfigError`` for a callable one.
* The reason phrases of extra statuses are ``http.HTTPStatus(code).phrase``,
  which can differ between Python versions (e.g. for 422).
* A path whose HSR link can't be computed (no route and no ``hsr_url``, a
  route that can't be reversed) raises ``ConfigError`` on every response of
  the path, not only on OPTIONS.

The HSR route
~~~~~~~~~~~~~

The router serves each path's document by GET and HEAD at ``/_hsr/<name>``
under its prefix, ``<name>`` being the path's segments joined with ``.``:
``"/{pk}"`` gives ``/roles/_hsr/pk`` on a router added at ``/roles``,
``"/{kind}/{pk}"`` gives ``_hsr/kind.pk``, ``"/search"`` gives
``_hsr/search`` and ``""`` gives ``_hsr/_root``. It answers ``application/json``
(the compact JSON of the document, whatever the API's renderer), the Links of
the HSR's own JSON Schema and JSON-LD context, a strong ``ETag`` (304 for a
matching ``If-None-Match``) and the router's ``Cache-Control``; another method
gets the router's JSON 405 (``Allow: GET, HEAD``). It has ``auth=None`` and no
throttles, is CSRF-exempt and is not in the OpenAPI document. It is public
under Django's ``LoginRequiredMiddleware`` (Django 5.1+): the view ninja routes
the route's URL to is ``login_required = False``, as is
:func:`~scrudful.ninja.http_schema_resource_view`'s. (ninja's other views,
your operations included, are not: that middleware gates them.)

* **The** ``_hsr`` **segment is reserved.** The routes come before the
  router's other paths, so ``"/{kind}/{pk}"`` doesn't match them; an
  operation under ``/_hsr/``, or two paths with one name (``"/a/{x}"`` and
  ``"/a/{x}/"``), is a ``ConfigError``. The routes' URL names are private:
  don't ``reverse()`` them.
* **The** ``add_decorator`` **decorators still wrap it.** ninja applies the
  API's and routers' ``add_decorator`` decorators (either mode) to every
  operation, the HSR route included: a gate added that way (a permission
  check, a throttle) also gates the HSR, which then is no longer public.
  Put such checks in ``auth=``, ``before=`` or ``load``, or serve the HSR
  elsewhere.
* **Serving it elsewhere.** With ``hsr_route=False`` and an ``hsr_url``,
  mount :func:`~scrudful.ninja.http_schema_resource_view`, which answers as
  the route does::

      HSR_URLS = {
          "/{pk}": "https://api.example.com/schemas/items/item",
          "": "https://api.example.com/schemas/items/list",
      }
      items = ScrudfulRouter(hsr_route=False, hsr_url=HSR_URLS.__getitem__)

      urlpatterns = [
          path("api/", api.urls),
          path("schemas/items/item", http_schema_resource_view(items, "/{pk}")),
          path("schemas/items/list", http_schema_resource_view(items, "")),
      ]

  Like the route, the view runs no authentication, permission check or
  throttle, is CSRF-exempt and is public under ``LoginRequiredMiddleware``;
  ``path`` is checked on its first request.

OPTIONS
~~~~~~~

The router adds an OPTIONS operation to every path with a scrudful (or
``@described``) operation, unless the path has one; an operation you add later
whose methods include ``"OPTIONS"`` replaces it. It is registered with
``auth=None``, ``throttle=[]`` and ``include_in_schema=False``, is CSRF-exempt,
and has the URL name ``scrudful_options`` on every path (don't ``reverse()``
it). It is answered by an ``Operation.run`` decorator, before ninja's checks:
the ``add_decorator(mode="operation")`` decorators never gate it, while the
``add_decorator(mode="view")`` ones wrap ``Operation.run`` and so run first,
for a preflight too. In this order:

1. **A CORS preflight** (``Origin`` and ``Access-Control-Request-Method``) is
   answered 204, without content or Links, before any auth, throttle, hook or
   load.
2. **The request is throttled once**, as ninja would throttle the operation:
   with the API's throttles, or the mount's (the router's, or
   ``add_router(..., throttle=...)``'s). A refused one is ninja's 429.
3. **On an item path, the object is loaded**, with the ``load`` of the GET
   operation, else of the first ``@scrudful`` operation with one: on a copy of
   the request with that operation's method, only once its ``auth=`` callbacks
   and ``before`` allow the requester (so a requester refused there can't
   learn which objects exist), with the path parameters validated as the
   operation validates them (no object for one it would refuse with a 422),
   and only them: give ``load``'s other parameters defaults, or take
   ``**kwargs``. ``None`` is no object, and the object-level checks are
   skipped. An ``HttpError`` or ``Http404`` it raises (the 404 of a missing
   or hidden object) is the answer, by the API's exception handler, with
   ``add_error_links``'s Links and the HSR link: return ``None`` for a 204 on
   a missing object (a PUT that may create, say).
4. **Each method is checked**, on a copy of the request with that method:

   * with an ``allowed`` hook (the operation's ``allowed=``, else the
     router's), ``allowed(request, method, obj)`` alone decides. It returns a
     ``bool`` (``TypeError`` otherwise) and may be ``async def``. The copy is
     authenticated by the operation's ``auth=`` callbacks when they
     authenticate the requester (``request.auth``), and as it came otherwise;
   * without one, the method is allowed when the operation's ``auth=``
     callbacks authenticate the requester and its ``before`` (if any) returns
     ``None``.

   A raised ``HttpError`` with status 401, 403 or 404 (as ninja's
   ``AuthenticationError`` and ``AuthorizationError`` are), ``Http404`` or
   ``PermissionDenied`` refuses the method: a ``before`` that signals denial
   with a 404 refuses it, it doesn't answer the OPTIONS. Anything else raised
   is answered by the API's exception handler. Throttles, ``after_throttles``
   and the handlers never run, so computing ``Allow`` uses no throttle quota;
   the CSRF check of a cookie ``auth`` is skipped (the request using the
   method is checked).
5. **The answer** is a 204 without content or ``Content-Type``, with ``Allow``
   (those methods and ``OPTIONS``; ``HEAD`` only where an operation lists it,
   since ninja answers an unrouted HEAD with 405), one HSR link and
   ``Access-Control-Expose-Headers``.

Things to know:

* OPTIONS isn't gated: an anonymous requester of a locked path gets a 204
  with ``Allow: OPTIONS``, not a 401. Bad credentials make the requester
  anonymous (ninja's auth callbacks return ``None`` for them, without telling
  them from missing ones), so they get the anonymous ``Allow``, not a 401.
* ``Allow`` sees only ``auth=``, ``before=`` and ``allowed=``: authorization
  done in an ``add_decorator`` decorator, in ``after_throttles`` or in the
  handler doesn't show in it (OPTIONS can list a GET that then answers 403).
  Give the router or operation ``allowed=`` for such checks.
* The ``auth=`` callbacks are those of the mount the request came through: a
  router mounted twice with different ``auth`` gets each mount's ``Allow``.
* An error answering OPTIONS (the 404 of ``load``, a 429) has no entry of its
  own in the HSR, whose ``options`` entry is exactly the 204's.

CORS
~~~~

Browsers send CORS preflights (an OPTIONS with ``Access-Control-Request-Method``)
without credentials. django-cors-headers answers them in its middleware,
before the view runs; one that reaches scrudful's OPTIONS gets the 204
without Links, before any check. For conformance, configure
django-cors-headers as "CORS" in :doc:`usage` says:

.. code-block:: python

    from scrudful.utils import EXPOSE_HEADERS

    CORS_EXPOSE_HEADERS = list(EXPOSE_HEADERS)  # it replaces scrudful's
    CORS_ALLOW_CREDENTIALS = True  # Allow depends on the credentials
    # CORS_ALLOW_METHODS keeps OPTIONS (its default has it), and a
    # CORS_URLS_REGEX must also match the HSR routes (/_hsr/).

The HSR route sets no CORS headers of its own and answers OPTIONS (a preflight
too) with the router's JSON 405, so the middleware must cover it. On a plain
``Router``, or behind an ``add_decorator(mode="view")`` gate, scrudful's
OPTIONS never sees the preflight: let the middleware answer it.

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
of the entity the operation's HTTP Schema Resource describes for its
status:
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
media type of the entity whose Links it carries, as the HTTP Schema Resource
lists it:
scrudful's own 400, 412 and 428, and the responses the API's exception
handlers make for the errors raised inside a scrudful operation and for those
:func:`~scrudful.ninja.add_error_links` covers. ninja renders those
``application/json``, and scrudful replaces that ``Content-Type`` with the
entity's ``media_type`` whatever the handler chose, with the ``charset``
parameter of the bytes (the renderer's; one the entity's ``media_type``
names is replaced), so every wrapped handler's JSON bodies must be content
the entity describes. A handler's response with
other content (e.g. ``text/plain``) keeps its ``Content-Type``.

**The API's renderer.** scrudful's own bodies (the 400, 412 and 428) are
written by the renderer of the API
serving the request (``NinjaAPI(renderer=...)``), as every other response, so
a renderer with other separators or escapes gives them the same bytes (a
JSON-LD renderer, ``application/ld+json``, too). The renderer writes them as
it writes the API's other data: one that changes the data's shape (renames
keys, e.g. to camelCase, or wraps each body, e.g. in ``{"data": ...}``)
changes them too, as it changes ninja's own 422 bodies. Use a renderer that
changes only how the data is written. (The HTTP Schema Resource route writes
its compact JSON itself, whatever the renderer.) Their
``Content-Type`` is still the media type described here (``application/json``,
or the ``error`` entity's media type), not the renderer's, with the
renderer's ``charset`` parameter, as ninja's own responses carry it
(``application/json; charset=utf-8`` by default). Unless the renderer is
ninja's ``JSONRenderer`` with a ``DjangoJSONEncoder`` (its default
``NinjaJSONEncoder`` is one), which writes them its own way, the body is first
reduced to JSON values as ninja's ``NinjaJSONEncoder`` (a
``DjangoJSONEncoder``) writes them, so a renderer of plain JSON (ORJSON,
``json.dumps``) writes a datetime, ``Decimal``, ``UUID`` or lazy string in an
``error_body_func`` body as ``JsonResponse`` did. A renderer whose
``media_type`` is not JSON content, or a ``decorate_view`` decorator below
scrudful that wraps ``Operation.run`` without ``functools.wraps``, gets them
written as Django's ``JsonResponse`` writes them.

**Error bodies.** ``error_body_func(request, result)`` on ``@scrudful`` and
``@scrudful_list`` returns the dict that replaces the default
``{"detail": ...}`` body of scrudful's own 400, 412 and 428 (``result`` is the
:class:`~scrudful.conditional.ConditionalResult`; its default body is
``result.body``). It must be a plain function, also in an ``async`` operation.
The status, headers, Links and media type are unchanged, and a 304 never calls
it. :func:`scrudful.problem.problem_details` gives RFC 9457 problem details;
pair it with an ``error`` entity of media type ``PROBLEM_JSON``:

.. code-block:: python

    from scrudful.problem import PROBLEM_JSON, problem_details

    @scrudful(error_body_func=problem_details,
              error=Entity(media_type=PROBLEM_JSON), **ROLE)

**Problem details.** With an ``application/problem+json`` entity, ninja's
request validation 422 (``{"detail": [errors]}``, whose ``detail`` problem
details want a string) is rewritten as RFC 9457 problem details, ninja's
errors as the ``errors`` member:
``problem_from_errors(422, errors, "Request validation failed")``
(:func:`scrudful.problem.problem_from_errors`). The w3id problem-details
context doesn't map ``errors``: publish a context that imports it
(``@import``) and maps ``errors``, and link that one.

**Responses no exception handler makes.** A method no operation of a
``ScrudfulRouter`` path routes (e.g. a POST on a path with only a GET) gets a
JSON 405 instead of ninja's ``text/html`` one: the API's exception handler's
answer to ``HttpError(405, "Method not allowed")``, so it carries
:func:`~scrudful.ninja.add_error_links`'s Links and media type, with RFC
9110's ``Allow`` (every method the path serves) and the HSR link. A plain
``Router``'s 405 stays ninja's. Django's 404 for a URL no pattern matches and
its 500 page come from Django: install :func:`~scrudful.ninja.handler404` and
:func:`~scrudful.ninja.handler500` in the root URLconf (see `Setup`_). They
take an ``Entity`` of plain (non-callable) link values, answer JSON (problem
details for an ``application/problem+json`` entity, else ``{"detail":
<reason phrase>}``) with its Links, resolved against
``SCRUDFUL["BASE_URL"]``, unless ``Accept`` prefers ``text/html``, and always
send ``Vary: Accept``. Django uses them with ``DEBUG`` off; with ``DEBUG =
True`` it shows its debug pages (and ninja its traceback page). They get no
HSR link: a URL no route serves is no described path.

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
   :members: collection_schema, collection_context, page_envelope, envelope_entry, InvalidPage

.. automodule:: scrudful.problem
   :members:
