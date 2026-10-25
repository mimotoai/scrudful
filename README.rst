========
scrudful
========

Helpers for building *SCRUDful* REST APIs (Search, Create, Read, Update,
Delete) that get HTTP conditional requests right, for Django REST Framework,
django-ninja and FastAPI.

A decorated view or endpoint:

* evaluates ``If-Match``, ``If-None-Match``, ``If-Modified-Since`` and
  ``If-Unmodified-Since`` as specified by RFC 9110 section 13, answering 304,
  400, 412 or 428 without calling your code when appropriate;
* sets ``ETag`` and ``Last-Modified`` on the responses that should carry them;
* adds ``Link`` headers pointing at the JSON schema (``rel="describedby"``) and
  the JSON-LD context of the response body (RFC 8288), as Semantic HTTP 0.1
  asks, and answers ``OPTIONS`` with the 0.1 HTTP Schema Resource;
* exposes ``ETag``, ``Link`` and ``Location`` to browsers through
  ``Access-Control-Expose-Headers``;
* with DRF, can compute validators from ``get_object()`` (``object_validators``),
  send them on a 201, and list in the same scrud envelope as django-ninja
  (``ScrudfulPagination``);
* can replace the body of its own 400, 412 and 428 (``error_body_func``, with
  RFC 9457 ``scrudful.problem.problem_details``).

The HTTP logic is framework-agnostic and lives in ``scrudful.conditional`` and
``scrudful.utils``; ``scrudful.rest_framework``, ``scrudful.ninja`` and
``scrudful.fastapi`` are thin adapters. The package has no required
dependencies.

Installation
============

.. code-block:: console

    $ pip install "scrudful[drf]"      # Django >= 4.2, djangorestframework >= 3.15
    $ pip install "scrudful[ninja]"    # Django >= 4.2, django-ninja >= 1.7, < 2
    $ pip install "scrudful[fastapi]"  # fastapi >= 0.100

Python 3.10 or newer is required.

How it works
============

You give the decorator *validator callbacks*: ``etag_func`` and/or
``last_modified_func``, which return the validators of the current
representation of the target resource (or ``None`` if there is none). With
them configured:

======  ==========================================================================
Status  When
======  ==========================================================================
304     GET/HEAD: ``If-None-Match`` matches the ETag (weak comparison), or, with
        no ``If-None-Match``, the resource is not newer than
        ``If-Modified-Since``. The response carries ``ETag`` and
        ``Last-Modified``.
400     ``If-Match`` or ``If-None-Match`` is present but malformed (for example
        an unquoted tag).
412     ``If-Match`` does not match (strong comparison), or, with no
        ``If-Match``, the resource is newer than ``If-Unmodified-Since``; or
        ``If-None-Match`` matches on a method other than GET/HEAD (for example
        ``If-None-Match: *``, "create only", on a PUT to an existing resource).
428     PUT, PATCH or DELETE on an existing resource without ``If-Match``,
        without ``If-None-Match: *`` and without a valid
        ``If-Unmodified-Since``. ``If-Match`` or ``If-Unmodified-Since`` is
        enough to update; the body lists them.
======  ==========================================================================

The error responses are JSON (``{"detail": ...}``), and none of them calls your
view. ``error_body_func(request, result)`` replaces the body of the 400, 412 and
428 (see below); ``scrudful.problem.problem_details`` makes it RFC 9457
``application/problem+json``.

Rules worth knowing:

* **Validators are opt-in.** Preconditions, and the 428 requirement, apply only
  when ``etag_func`` or ``last_modified_func`` is configured. Without them a
  decorated view behaves like an undecorated one, apart from the ``Link`` and
  expose headers.
* **TRACE, OPTIONS and CONNECT ignore preconditions.** Their response is not
  a representation of the target resource (a TRACE echoes the request), so
  the ``If-*`` headers are not evaluated, the validator callbacks are not
  called for them and the response gets no ``ETag`` or ``Last-Modified``
  (RFC 9110 §13.2.1). Their OPTIONS entry lists no 304, 400, 412 or 428.
* **Invalid dates are ignored.** An invalid ``If-Modified-Since`` or
  ``If-Unmodified-Since`` is treated as absent, so a lone invalid
  ``If-Unmodified-Since`` on a write is answered with 428.
* **Callbacks returning ``None``** mean "no current representation": the
  preconditions are skipped and your view produces its own 404. The exception
  is PUT, which may create the resource: ``If-None-Match: *`` passes and
  ``If-Match`` gives 412. POST is not an exception: when both callbacks
  return ``None`` its preconditions are skipped too, so even a failing
  ``If-Match`` reaches your view.
* **Callbacks run first**, before your view's own lookup and permission
  checks, so they must apply the same scoping (for example
  ``view.get_queryset()`` with DRF, or the current user from a dependency with
  FastAPI). Otherwise a resource the view would hide reveals its validators
  (304) and its existence (412 or 428 instead of 404). The django-ninja
  adapter avoids this by design: its callbacks get the object your ``load``
  callable authorized.
* **Validators on responses.** 2xx responses to GET/HEAD get ``ETag`` and
  ``Last-Modified``; 2xx responses to PUT/PATCH get freshly recomputed ones
  (the callbacks run again, so they describe the new representation); POST,
  DELETE and non-2xx responses get none. Headers your view sets itself are
  never overwritten.
* **ETags.** ``str()`` of the callback's result is used. A valid entity-tag
  (``'"abc"'``, ``'W/"abc"'``) is sent as is, anything else is wrapped in double
  quotes. A value that cannot be an entity-tag (it contains a double quote, a
  space, a control character or a character above U+00FF) raises
  ``ValueError``. No library version is appended. Weak tags are allowed, but
  ``If-Match`` uses the strong comparison, which a weak tag never passes: with
  a weak ``etag_func`` only ``If-Unmodified-Since`` (or ``If-Match: *``) can
  make a write succeed.
* **Datetimes.** ``last_modified_func`` returns a ``datetime`` or an epoch
  timestamp. Aware datetimes are converted to UTC and sub-second precision is
  discarded. A *naive* datetime is taken to be UTC by the FastAPI adapter and
  to be in ``settings.TIME_ZONE`` by the DRF and django-ninja adapters (the
  same thing when ``TIME_ZONE`` is ``"UTC"``). Prefer aware datetimes.
* **Link headers.** The schema and context links are added to every success
  response with content (a 2xx other than 204; never a TRACE, whose content
  is the echoed request) your view returns, and are appended to any ``Link``
  value it set. Another response your view returns (an error, a 204, a
  redirect) gets no Links from scrudful, unless an entity is declared for
  that status (django-ninja's ``responses=`` or ``ScrudfulResponse.entity``);
  a ``Link`` it set itself is kept as is. *Raise* errors, rather than return
  them, to have them carry the error entity's Links (see "Semantic HTTP
  conformance"). Each
  ``*_or_func`` argument is a value or a callable. The schema link defaults to
  ``rel="describedby"`` and ``type="application/schema+json"``, the context
  link to ``rel="http://www.w3.org/ns/json-ld#context"`` and
  ``type="application/ld+json"``. Relative values are resolved to absolute URIs
  against the request, and a Link that your view set itself with the relation
  of scrudful's schema link suppresses that link: ``describedby`` with the
  defaults, ``profile`` with ``schema_rel_or_func="profile"``. The JSON-LD
  context and ``rdf-context`` relations are one slot (a response has at most
  one RDF Context Link Header): your Link with either suppresses scrudful's
  context Link of either. An overridden context relation is matched exactly.
  Relations are compared case-insensitively, each of a multi-relation ``rel``.

Concurrency
-----------

Preconditions are evaluated *before* your view runs, so two concurrent requests
carrying the same ``If-Match`` can both pass. For strict lost-update protection
the write itself must re-check atomically: with Django, run the request in a
transaction (``ATOMIC_REQUESTS = True`` or ``transaction.atomic``) and lock the
row with ``select_for_update()`` (the example project does it in its validator
callbacks); with any storage, make the write conditional, for example
``UPDATE ... WHERE version = <expected>``.

CORS
----

``ETag``, ``Link`` and ``Location`` are added to
``Access-Control-Expose-Headers`` (merged with an existing value). CORS
middleware that sets that header itself replaces it on cross-origin requests,
so list the names in the middleware's own configuration: ``expose_headers`` for
Starlette's ``CORSMiddleware``, ``CORS_EXPOSE_HEADERS`` for django-cors-headers.

Django REST Framework
=====================

Callbacks are called as ``func(view, request, **url_kwargs)``, with every URL
kwarg, DRF's ``format`` among them on the format-suffix routes a router adds:
accept ``**kwargs``. ``@scrudful_viewset`` reads its configuration from an inner
``Meta`` class (``list_*`` options configure the list response):

.. code-block:: python

    from django.urls import reverse
    from rest_framework import viewsets

    from scrudful.rest_framework import scrudful_viewset

    from items.models import Item
    from items.serializers import ItemSerializer


    # The callbacks run before the view's lookup: use its queryset.
    def item_etag(view, request, pk, **kwargs):
        items = view.get_queryset().filter(pk=pk)
        version = items.values_list("version", flat=True).first()
        return None if version is None else f"v{version}"


    def item_last_modified(view, request, pk, **kwargs):
        items = view.get_queryset().filter(pk=pk)
        return items.values_list("updated_at", flat=True).first()


    # A path: scrudful makes it absolute. DRF's reverse(..., request=request)
    # would keep the request's ?format= in it.
    def item_schema_url(view, request, **kwargs):
        return reverse("item-schema")


    @scrudful_viewset
    class ItemViewSet(viewsets.ModelViewSet):
        queryset = Item.objects.all()
        serializer_class = ItemSerializer

        class Meta:
            etag_func = item_etag
            last_modified_func = item_last_modified
            schema_link_or_func = item_schema_url
            schema_type_or_func = "application/schema+json"

For single methods use ``@scrudful(...)`` on a method of an ``APIView`` or
viewset, and ``@scrudful_api_view(["GET", "PUT"], ...)`` for a function view,
in place of ``@api_view``. OPTIONS requests are answered with the Semantic HTTP
0.1 HTTP Schema Resource of the methods the route serves (``ScrudfulMetadata``)
by ``scrudful_viewset`` viewsets, by classes with a ``@scrudful`` method and by
``scrudful_api_view`` views. ``@scrudful`` below a plain ``@api_view`` also
makes the function view SCRUDful, but there is no class to install
``ScrudfulMetadata`` on when it runs: that view keeps DRF's ``SimpleMetadata``
and gets no HTTP Schema Resource, and the errors it raises get no error Links
(only scrudful's own 400, 412 and 428 do), so use ``scrudful_api_view``
instead. Set ``error=`` (or ``Meta.error``) and name
``scrudful.rest_framework.exception_handler`` as DRF's ``EXCEPTION_HANDLER`` so
that the errors DRF raises itself for those views carry the error entity's
Links too. See ``examples/scrudful_rest_framework`` for a complete project.

A list in the scrud envelope (``count``, ``page_count``, ``first``,
``previous``, ``next``, ``last`` and ``content``; each entry has ``href``,
``etag``, ``last_modified``, ``link`` and ``content``), the one django-ninja's
``scrudful_list`` returns, comes from ``ScrudfulPagination``. It is opt-in as the
viewset's ``pagination_class``; other views are unchanged. It needs
``Meta.href_func(view, request, obj)``. With ``Meta.object_validators`` the
entries' ``etag`` and ``last_modified`` come from ``Meta.etag_func(obj)`` and
``Meta.last_modified_func(obj)`` (otherwise they are ``null``), and ``link``
holds the item schema and context links of ``Meta`` (their callbacks get the
object's lookup value as the URL keyword). ``page`` (at least 1, default 1) and
``page_size`` (1 to ``max_page_size``; default 100, maximum 1000) outside their
range are a 400, and a page past the last one a 404. Give the view an ordered
queryset: an unordered one warns, as with Django's ``Paginator``.

.. code-block:: python

    from scrudful.rest_framework import ScrudfulPagination


    def item_href(view, request, obj):
        return request.build_absolute_uri(f"/items/{obj.pk}")


    @scrudful_viewset
    class ItemViewSet(viewsets.ReadOnlyModelViewSet):
        queryset = Item.objects.order_by("pk")
        pagination_class = ScrudfulPagination

        class Meta:
            href_func = item_href
            object_validators = True
            etag_func = item_etag
            last_modified_func = item_last_modified

Everything below is opt-in; a viewset that sets none of it behaves as before.

**Validators from the object (``object_validators``).** By default the
callbacks run before the view looks the object up. With
``Meta.object_validators = True`` (or ``object_validators=True`` on a
``@scrudful`` method of a generic view) ``etag_func(obj)`` and
``last_modified_func(obj)`` take the object ``view.get_object()`` returns, after
DRF's queryset filtering and object permissions, so a missing or forbidden
object is a 404 or 403 that never reveals a validator, a 412 or a 428. The
handler's own ``get_object()`` returns the same instance, and after a
successful PUT or PATCH the object is loaded again for the response's validators
(none are sent if that fails). A handler that calls ``get_object()`` *after*
saving gets the pre-save instance. On a function view
``@scrudful(object_validators=True)`` raises ``TypeError`` when the request
arrives, as there is no ``get_object()``.

**Creates.** With ``object_validators`` (which needs ``etag_func`` or
``last_modified_func``), ``scrudful_viewset`` sets ``creates=True`` for
``create``; without it, ``create`` gets only the links and error entity. Set
``creates=True`` on an explicit ``@scrudful`` create (it needs
``object_validators``). No precondition is
evaluated and the 201 gets ``ETag`` and ``Last-Modified`` from the saved
instance, plus ``Location`` from ``Meta.location_func(view, request, obj)``
(``location_func=`` on the decorator; it requires ``creates=True`` and
``object_validators``). A header
the view set is never replaced. The 201 is built in ``perform_create``, so a
custom ``create()`` that bypasses it gets no validators. OPTIONS describes the
201 without the validator headers, as django-ninja does.

**Error bodies (``error_body_func``).** ``error_body_func(request, result)``,
also ``Meta.error_body_func`` and ``Meta.list_error_body_func`` (the list
response's), returns the dict used as the body of scrudful's own 400, 412 and
428. It must be a plain function. ``scrudful_viewset`` does not apply it to
``create``. Status, headers, Links and Content-Type are unchanged and it is not
called for a 304.

.. code-block:: python

    from scrudful.problem import PROBLEM_JSON, problem_details
    from scrudful.rest_framework import Entity


    def item_location(view, request, obj):
        return f"/items/{obj.pk}"


    @scrudful_viewset
    class ItemViewSet(viewsets.ModelViewSet):
        class Meta:
            object_validators = True
            etag_func = item_etag
            location_func = item_location
            error_body_func = problem_details
            error = Entity(media_type=PROBLEM_JSON)

Caveats of ``scrudful_viewset``:

* The item link callbacks also run for ``create`` and for OPTIONS and POST on the
  list route, where there is no ``pk``. Accept ``**kwargs``, as
  ``item_schema_url`` does above.
* An explicit ``@scrudful`` on ``update`` applies to PATCH when
  ``partial_update`` is DRF's stock one, in OPTIONS and for the errors
  ``exception_handler`` answers too (decorate a subclass of a decorated
  viewset with ``scrudful_viewset`` too, or it inherits the parent's wrapper).
  With a user-defined ``partial_update`` that calls ``update``, the
  ``Meta``-configured wrapper of ``partial_update`` wins: decorate
  ``partial_update`` explicitly to override it.
* OPTIONS uses ``ScrudfulMetadata`` only when the viewset's ``metadata_class``
  is DRF's default ``SimpleMetadata`` (so does an ``APIView`` with a
  ``@scrudful`` method). A custom ``metadata_class`` (or ``None``,
  or a project-wide ``DEFAULT_METADATA_CLASS``) is kept; subclass
  ``ScrudfulMetadata`` to get its output with your own additions.
* Methods the class does not have are not added (a read-only viewset answers
  405 to writes).

FastAPI
=======

Callbacks are called with the keyword arguments FastAPI resolved for the
endpoint (path, query, body and dependency values, plus the endpoint's own
``Request``/``Response`` parameters), so accept ``**kwargs`` for the ones you
don't need. Callbacks and endpoints may be ``def`` (run in the threadpool) or
``async def``.

.. code-block:: python

    from datetime import datetime, timezone
    from typing import Any

    from fastapi import FastAPI, HTTPException, Request

    from scrudful.fastapi import (
        CONDITIONAL_READ_RESPONSES,
        CONDITIONAL_WRITE_RESPONSES,
        scrudful,
    )

    app = FastAPI()
    users = {
        1: {
            "name": "John",
            "version": 1,
            "updated_at": datetime(2021, 5, 25, tzinfo=timezone.utc),
        }
    }


    def user_etag(user_id: int, **kwargs: Any) -> str | None:
        user = users.get(user_id)
        return None if user is None else f"user-{user_id}-v{user['version']}"


    def user_last_modified(user_id: int, **kwargs: Any) -> datetime | None:
        user = users.get(user_id)
        return None if user is None else user["updated_at"]


    @app.get("/users/{user_id}", responses=CONDITIONAL_READ_RESPONSES)
    @scrudful(etag_func=user_etag, last_modified_func=user_last_modified)
    async def get_user(user_id: int) -> dict:
        if user_id not in users:
            raise HTTPException(404, "User not found")
        return users[user_id]


    @app.put("/users/{user_id}", responses=CONDITIONAL_WRITE_RESPONSES)
    @scrudful(etag_func=user_etag, last_modified_func=user_last_modified)
    async def replace_user(user_id: int, user: dict) -> dict:
        if user_id not in users:
            raise HTTPException(404, "User not found")
        record = users[user_id]
        record.update(
            user, version=record["version"] + 1, updated_at=datetime.now(timezone.utc)
        )
        return record

Notes:

* ``@scrudful(...)`` must sit **below** the route decorator. Above it, FastAPI
  registers the undecorated function and the decorator silently has no effect.
  All arguments are keyword-only.
* The 304/400/412/428 responses are produced without calling the endpoint, so
  FastAPI doesn't know about them. Add ``CONDITIONAL_READ_RESPONSES``
  (GET/HEAD) or ``CONDITIONAL_WRITE_RESPONSES`` (PUT/PATCH/DELETE) to the route's
  ``responses=`` to document them in OpenAPI; merge them with your own:
  ``responses={**CONDITIONAL_READ_RESPONSES, 404: {...}}``.
* FastAPI answers 405 to HEAD on a route declared with ``@app.get``. Stack
  ``@app.head(path)`` with ``@app.get(path)`` above ``@scrudful(...)`` (the
  example does), or use ``@app.api_route(path, methods=["GET", "HEAD"])``;
  newer FastAPI versions warn about the duplicate operation id of the latter.
* ``add_error_links(app, ERROR)`` gives the errors FastAPI and Starlette answer
  (422, 404, 405, ``HTTPException``) the error entity's Links, and
  ``add_options(app)`` answers ``OPTIONS`` on every scrudful URL with its HTTP
  Schema Resource. Call both once the routes are declared. FastAPI request
  bodies are described there with the endpoint's response links: the adapter
  has no request entity.
* To have a link callback build absolute URLs, declare a ``request: Request``
  parameter on the endpoint (``request.url_for(...)``).

See ``examples/fastapi/api.py`` for a complete application (run it with
``python examples/fastapi/api.py``).

django-ninja
============

``scrudful.ninja`` follows one rule: validators and precondition results are
computed only from what authorization already approved. Its decorators run
inside the operation, after ninja's authentication, throttling and parameter
validation; an item operation's ``load(request, **params)`` is the
fetch-if-allowed callable, the validator callbacks get the object it returns,
and so does the handler. It does not need Django REST Framework.

.. code-block:: python

    from typing import List

    from django.shortcuts import get_object_or_404
    from ninja import NinjaAPI

    from scrudful.ninja import (
        CONDITIONAL_READ_RESPONSES,
        CONDITIONAL_WRITE_RESPONSES,
        Entity,
        ScrudfulRouter,
        add_error_links,
        scrudful,
        scrudful_create,
        scrudful_list,
    )

    api = NinjaAPI(auth=..., default_router=ScrudfulRouter())
    roles = ScrudfulRouter()  # adds OPTIONS to every path with a scrudful operation
    api.add_router("/roles", roles)
    ERROR = Entity(schema_link_or_func="/schemas/error",
                   context_link_or_func="/contexts/error")
    add_error_links(api, ERROR)  # the 422, 401, ... get the error entity's Links


    def authorized_role(request, pk: int, **params):
        # Missing and hidden are both 404, for every method and If-* header.
        return get_object_or_404(Role, pk=pk, rbac_context=request.auth)


    ROLE = dict(
        etag_func=lambda role: role.etag,
        last_modified_func=lambda role: role.modified_at,
        schema_link_or_func="/schemas/role",
        context_link_or_func="/contexts/role",
    )


    @roles.api_operation(["GET", "HEAD"], "/{int:pk}", response=RoleOut,
                         openapi_extra={"responses": CONDITIONAL_READ_RESPONSES})
    @scrudful(load=authorized_role, **ROLE)
    def get_role(request, pk: int, obj: Role):
        return obj


    @roles.put("/{int:pk}", response=RoleOut,
               openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES})
    @scrudful(load=authorized_role, **ROLE)
    def replace_role(request, pk: int, payload: RoleIn, obj: Role):
        ...


    @roles.post("", response={201: RoleOut})  # 201, Location, item validators
    @scrudful_create(location_func=lambda request, role: f"/roles/{role.pk}", **ROLE)
    def create_role(request, payload: RoleIn):
        return Role.objects.create(**payload.dict())


    @roles.get("", response=List[RoleOut])  # the scrud list envelope, paginated
    @scrudful_list(href_func=role_url, **ROLE)
    def list_roles(request):
        return Role.objects.filter(rbac_context=request.auth).order_by("pk")

Notes:

* ``@scrudful(...)``, ``@scrudful_create(...)`` and ``@scrudful_list(...)``
  must sit **below** the operation decorator (above it, they raise
  ``TypeError``).
* ``load`` returns the object, ``None`` ("no current representation", RFC
  9110 semantics, e.g. for a PUT that may create) or raises, through ninja's
  exception handlers; an object it returns exists even if its validators are
  ``None``. Whether PUT may create and how hidden rows are reported is yours
  to choose; the docs show the four patterns.
* Use ``ScrudfulRouter`` (and ``NinjaAPI(default_router=ScrudfulRouter(...))``
  for operations on the API itself): it answers OPTIONS with the
  Semantic HTTP 0.1 "HTTP Schema Resource", the same for every client, without
  ``auth``. Scrudful operations on a plain ``Router`` get no OPTIONS.
* ``Entity(...)`` declares request and per-status response entities and the
  ``error`` entity whose Links scrudful's 400, 412 and 428 bodies carry
  (``add_error_links(api, ERROR)`` gives the errors ninja answers itself,
  the 422 and 401 for example, the same Links); a
  handler parameter annotated ``ScrudfulResponse`` sets the status,
  ``Location``, validators or Links of one response.
* ``async def`` handlers are supported; sync callables then run through
  ``sync_to_async``.
* ``scrudful.collection`` builds the JSON Schema and JSON-LD context of the
  list envelope.

See ``examples/ninja`` for a complete project, and the documentation's
django-ninja page for the details.

Semantic HTTP conformance
=========================

scrudful follows `Semantic HTTP 0.1 <https://w3id.org/semantic-http/0.1/>`_
(issue #4) in all three adapters. By default it guarantees that:

* every response scrudful makes with content has a schema Link
  (``rel="describedby"``, ``type="application/schema+json"``) and, for a
  non-in-band media type, a context Link, and that every ``HEAD`` carries the
  Links of the ``GET``;
* Link header targets and every URI in the OPTIONS document are absolute,
  link relations are de-duplicated, and a ``Link`` your view set with the
  same relation is kept instead of scrudful's. A ``Location`` (which may be
  relative) and a list entry's ``href`` are the application's, sent as given:
  return absolute ones from ``location_func`` / ``href_func`` if you want
  them;
* ``OPTIONS`` answers 200 with the 0.1 HTTP Schema Resource as
  ``application/json``, the published HTTP Schema Resource Links, an ``Allow``
  listing exactly the body's methods and an ``options`` entry (plus a
  content-less ``head`` entry where HEAD is served);
* scrudful's own 400, 412 and 428 carry the Links of the ``error`` entity you
  pass, and are served with its ``media_type``, which must be JSON content
  (``application/json`` or ``+json``, other than JSON-LD). The same holds for
  every JSON error response that gets an error entity's Links (from the
  error-links helpers below, or raised inside a ninja scrudful operation): it
  is served with that entity's ``media_type``.

The application must still:

* **Install the error-links helper:** ``add_error_links(api, ERROR)`` for
  django-ninja and FastAPI, so the errors the framework answers (422, 401,
  404, 405, ...) carry Links. For DRF, name ``exception_handler`` as
  ``EXCEPTION_HANDLER``: it adds the view's ``error`` Links to the errors DRF
  raises, per view, for the views whose scrudful handler has an ``error``
  (DRF has no app-wide error entity).
* **Raise errors, don't return them:** a 4xx or 5xx your view returns gets
  no Links from scrudful (the view's links describe its 2xx content only),
  unless an entity is declared for that status (django-ninja's
  ``responses=`` or ``ScrudfulResponse.entity``); one it raises goes through
  the error-links helper.
* **Give every conditional decorator** ``error=`` (``Meta.error`` and
  ``Meta.list_error`` for a DRF viewset): the helpers cover the errors the
  framework raises, not scrudful's own 400, 412 and 428, which carry Links
  only from the decorator's ``error``.
* **Install the OPTIONS helper in FastAPI:** ``add_options(app)``.
* **Use** ``scrudful_api_view`` **for DRF function views:** a plain
  ``@api_view`` with ``@scrudful`` keeps DRF's OPTIONS, which is no HTTP
  Schema Resource, and ``exception_handler`` can't find its ``error``: the
  errors it raises get no Links, only scrudful's own 400, 412 and 428 do.
* **Decorate the HTTP method handlers of a DRF generic view** (``get``,
  ``put``, ``patch`` ...), or use a viewset with ``scrudful_viewset``: a
  ``@scrudful`` ``update`` behind the stock ``put`` of a
  ``RetrieveUpdateAPIView`` is evaluated, but OPTIONS describes ``put`` and
  ``patch`` as bare 200s.
* **Accept JSON request bodies only (DRF):** DRF's default parsers also take
  form and multipart bodies, which the HTTP Schema Resource doesn't list (no
  lifting to RDF covers form data). Set ``DEFAULT_PARSER_CLASSES`` (or the
  view's ``parser_classes``) to ``JSONParser`` only, as the example does.
* **Leave the browsable API out (DRF):** its HTML responses (``Accept:
  text/html``) carry the Links of the JSON content, so they are not
  conformant. For full conformance, drop ``BrowsableAPIRenderer`` from
  ``DEFAULT_RENDERER_CLASSES`` (or the view's ``renderer_classes``), as the
  example does. An OPTIONS with ``Accept: text/html`` then gets DRF's 406, an
  error carrying the error Links, not an HTTP Schema Resource.
* **Give every entity a schema and a context** (only a schema for in-band
  types such as JSON-LD or Turtle). The spec requires the context of JSON
  content to be a JSON-LD 1.1 context. Keep the default ``describedby``
  relation: another one is no Schema Link Header while the OPTIONS body still
  describes it as the schema.
* **Serve the linked documents:** scrudful sends the URLs, not the documents.
* **Make item contexts map every member** of the item schema, so the item is
  RDF, and the collection context the envelope.
* **Handle 5xx pages and URLs no route serves** (Django's 404 and 500 pages, a
  ninja 405 for an unrouted method): no exception handler of the framework
  reaches them, so give them Links in a handler or middleware of your own.
* **Expose the headers to browsers:** list ``OPTIONS`` in the CORS middleware's
  ``Access-Control-Allow-Methods`` and ``Link`` in its expose list
  (``expose_headers``, ``CORS_EXPOSE_HEADERS``).
* **Answer CORS preflights with the CORS middleware.** On a plain ninja
  ``Router``, or behind ``add_decorator`` permission or throttle gates,
  scrudful's ``OPTIONS`` never sees the preflight (nor does it, in FastAPI,
  under a ``Mount`` or ``Router`` with ``middleware``).

The three example projects (``examples/``) are conformant: their tests check
every response against the rules above.

Development
===========

.. code-block:: console

    $ uv sync
    $ uv run pytest
    $ uv run --group docs make -C docs html

Releasing
---------

A merge request that changes the package (``scrudful/``, or the published parts
of ``pyproject.toml``) sets the version to release; the ``version-check`` job
fails until it does. Pick the bump and commit ``pyproject.toml`` and
``uv.lock``:

.. code-block:: console

    $ uv version --bump minor    # or --bump patch / --bump major, or: uv version 1.0.0

Merging to ``master`` publishes that version to PyPI and creates the
``vX.Y.Z`` tag and GitLab release. Merges that leave the version unchanged (docs,
tests, CI) publish nothing.

License
=======

MIT. The conditional-request core adapts code from Django (BSD-3-Clause); its
license is in ``LICENSES/Django-BSD-3-Clause.txt``.
