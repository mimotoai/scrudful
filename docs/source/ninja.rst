django-ninja
============

``scrudful.ninja`` makes django-ninja operations SCRUDful: conditional requests
(304, 400, 412, 428), ``ETag`` / ``Last-Modified``, the schema and JSON-LD
context ``Link`` headers of the semantic-http spec, collections in the scrud
list envelope, and an automatic OPTIONS answer (the spec's "HTTP Schema
Resource"). Install it with ``pip install "scrudful[ninja]"`` (django-ninja
1.7 or newer). It does not need Django REST Framework.

The example project in ``examples/ninja`` uses every feature on a ``Role``
resource scoped by an ``rbac_context``; its tests (``examples/ninja/tests``)
show the HTTP exchanges.

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

    from scrudful.ninja import ScrudfulRouter

    SCHEMA_RESOURCE = dict(
        options_schema_link=(
            "https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json"
        ),
        options_context_link=(
            "https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld"
        ),
    )

    api = NinjaAPI(auth=..., default_router=ScrudfulRouter(**SCHEMA_RESOURCE))
    roles = ScrudfulRouter(**SCHEMA_RESOURCE)
    api.add_router("/roles", roles)

Scrudful operations on a plain ``ninja.Router`` work, but get no OPTIONS
operation (ninja answers 405).

Single resources
----------------

``@scrudful(...)`` sits **below** the operation decorator. ``load`` is the
fetch-if-allowed callable; the validator callbacks are called with what it
returns, and the handler receives it in the ``load_as`` parameter (``obj`` by
default), hidden from ninja's signature and OpenAPI:

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
scrudful's own 400, 412 and 428 bodies carry that entity's Links. ``load`` is
required when ``etag_func`` or ``last_modified_func`` is set.

ninja routes HEAD only to operations that list it, as above.

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

After a successful PUT or PATCH, ``load`` is called again to compute the
validators of the representation just written. If it raises then (e.g. the
write moved the object out of the caller's scope), the client gets that error
although the write was committed.

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
                  "link": "</schemas/role>; rel=\"describedBy\"; …",
                  "content": {"id": 17, "name": "admin"}}]}

Each entry's ``etag``, ``last_modified`` and ``link`` come from the same item
callbacks as the detail GET. ``page`` and ``page_size`` (default 100, at most
1000) are query parameters. The optional list validators are called with the
queryset, and then a list can answer 304.

* Order the queryset: an unordered one raises Django's
  ``UnorderedObjectListWarning``, as with Django's paginator, because its pages
  are not stable.
* A 2-tuple returned from a list handler is ninja's deprecated
  ``(status, body)``, not a sequence: return a list, or wrap it in ``Status``.
* In an async list operation the queryset is counted and sliced in a thread,
  but ninja serializes the items on the event loop, so the item schema must
  not touch lazy relations: use ``select_related`` / ``prefetch_related`` in
  the handler. ninja's ``@paginate`` has the same limit.

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
OpenAPI-like description in the shape DRF's ``ScrudfulMetadata`` produces: the
request and response ``content`` (``{"schema": <url>, "context": <url>}``),
the statuses (scrudful's early responses and those the operation declares) and
the precondition headers of conditional writes. It is the same for everyone:

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
* without ``options_schema_link`` / ``options_context_link`` the response has
  no Link, which the semantic-http spec does not allow.

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

.. automodule:: scrudful.collection
   :members: collection_schema, collection_context
