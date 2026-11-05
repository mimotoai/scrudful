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
  the JSON-LD context of the response body (RFC 8288), as `Semantic HTTP 0.1
  <https://w3id.org/semantic-http/0.1/>`_ asks (as merged at spec commit
  ``37ec1ce``);
* answers ``OPTIONS`` with a 204 whose ``Allow`` lists the methods *this*
  requester may use, and a link to the path's HTTP Schema Resource, which it
  serves by ``GET``: one cacheable document, the same for every requester,
  that every response of the path links to;
* exposes ``Link`` and ``Allow``, and ``ETag`` and ``Location`` when present, to
  browsers through ``Access-Control-Expose-Headers``;
* with DRF, can compute validators from ``get_object()`` (``object_validators``),
  send them on a 201, and list in the same scrud envelope as django-ninja
  (``ScrudfulPagination``);
* can replace the body of its own 400, 412 and 428 (``error_body_func``, with
  RFC 9457 ``scrudful.problem.problem_details``), and makes the framework's
  validation errors problem details too for an ``application/problem+json``
  error entity;
* gives the framework's own error responses (a 404 for an unknown URL, a 405,
  a 500) JSON content with Links (for Django's 404 and 500 pages, unless
  ``Accept`` prefers HTML).

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
  when ``etag_func`` or ``last_modified_func`` is configured (for a create, the
  collection's ``list_etag_func`` or ``list_last_modified_func``). Without them
  a decorated view behaves like an undecorated one, apart from the ``Link`` and
  expose headers.
* **TRACE, OPTIONS and CONNECT ignore preconditions.** Their response is not
  a representation of the target resource (a TRACE echoes the request), so
  the ``If-*`` headers are not evaluated, the validator callbacks are not
  called for them and the response gets no ``ETag`` or ``Last-Modified``
  (RFC 9110 §13.2.1). Their HTTP Schema Resource entry lists no 304, 400, 412
  or 428.
* **Invalid dates are ignored.** An invalid ``If-Modified-Since`` or
  ``If-Unmodified-Since`` is treated as absent, so a lone invalid
  ``If-Unmodified-Since`` on a write is answered with 428.
* **Callbacks returning ``None``** mean "no current representation": the
  preconditions are skipped and your view produces its own 404. The exception
  is PUT, which may create the resource: ``If-None-Match: *`` passes and
  ``If-Match`` gives 412. A general POST is not an exception: when both
  callbacks return ``None`` its preconditions are skipped too, so even a
  failing ``If-Match`` reaches your view. A DRF create's list validators are:
  the collection always has a representation, so ``If-None-Match: *`` gives
  412 even when they return ``None``; raise ``Http404`` in the callback for a
  missing collection. A django-ninja ``list_load`` returning ``None`` is a
  missing collection, as for ``load``.
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
  redirect) gets no schema or context Links from scrudful, unless an entity
  is declared for that status (django-ninja's ``responses=`` or
  ``ScrudfulResponse.entity``); a ``Link`` it set itself is kept as is. *Raise*
  errors, rather than return them, to have them carry the error entity's
  Links (see "Semantic HTTP conformance"). Every response of a described path
  (a 304, a 204, an error and a HEAD too) also carries the link to the path's
  HTTP Schema Resource (see below). Each
  ``*_or_func`` argument is a value or a callable. The schema link defaults to
  ``rel="describedby"`` and ``type="application/schema+json"``, the context
  link to ``rel="http://www.w3.org/ns/json-ld#context"`` and
  ``type="application/ld+json"``. Relative values are resolved to absolute URIs
  against the base URL when one is set (see "The base URL"), else against the
  request, and a Link that your view set itself with the relation
  of scrudful's schema link suppresses that link: ``describedby`` with the
  defaults, ``profile`` with ``schema_rel_or_func="profile"``. The JSON-LD
  context and ``rdf-context`` relations are one slot (a response has at most
  one RDF Context Link Header): your Link with either suppresses scrudful's
  context Link of either. An overridden context relation is matched exactly.
  Relations are compared case-insensitively, each of a multi-relation ``rel``.
  A Link your view set with the HTTP Schema Resource relation replaces
  scrudful's.

Concurrency
-----------

Preconditions are evaluated *before* your view runs, so two concurrent requests
carrying the same ``If-Match`` can both pass. For strict lost-update protection
the write itself must re-check atomically: with Django, run the request in a
transaction (``ATOMIC_REQUESTS = True`` or ``transaction.atomic``) and lock the
row with ``select_for_update()`` (the example project does it in its validator
callbacks); with any storage, make the write conditional, for example
``UPDATE ... WHERE version = <expected>``.

OPTIONS and the HTTP Schema Resource
------------------------------------

Every path with a scrudful view, operation or endpoint has an *HTTP Schema
Resource* (HSR), its Semantic HTTP 0.1 description: an OpenAPI 3.1 Path Item
listing each method the path serves, with the media types, schemas and
contexts of the content it accepts and returns, its success statuses,
scrudful's 304, 400, 412 and 428 where it evaluates preconditions, and a
``default`` entry for the errors, with the content of the error entity they
carry. ``options`` is ``{"responses": {"204": {"description": "No
Content"}}}``. The document is built once per path template, *without a
request*: it is the same for every requester and every ``Host``.

**The HSR is served by GET**, on a route scrudful adds (for DRF, one
``include()`` line in the root URLconf). The route has a reserved ``_hsr``
segment, which no path of yours may use:

==================  ====================================  ==========================================
Adapter             Route                                 ``<name>``
==================  ====================================  ==========================================
django-ninja        ``<router prefix>/_hsr/<name>``       the router's path, segments joined with
                                                          ``.``: ``"/{pk}"`` gives ``pk``,
                                                          ``""`` gives ``_root``
DRF                 ``/_hsr/<name>``, from                the route's URL name, namespace
                    ``include(hsr_urls())``               included: ``item-detail``
FastAPI             ``/_hsr/<name>`` (under the prefix    the path's segments joined with ``.``,
                    of an ``APIRouter`` given to          a convertor but ``str`` joined with
                    ``add_options``)                      ``-``: ``users.id``, ``users.id-int``
==================  ====================================  ==========================================

It answers GET and HEAD with a 200, ``application/json``, the compact JSON of
the document, the Links of the HSR's own JSON Schema and JSON-LD context, a
strong ``ETag`` (a 304 for a matching ``If-None-Match``) and ``Cache-Control:
public, max-age=300`` by default; any other method with a 405 (``Allow: GET,
HEAD``). It runs no authentication, permission check, throttle or CSRF check
(on django-ninja, the decorators added with ``add_decorator`` still wrap it).
Turn it off (``hsr_route=False``) to serve the document elsewhere, with the
adapter's ready-made view and an ``hsr_url`` naming where it is: see each
adapter's page.

**OPTIONS** (other than a CORS preflight) answers a 204 without content, with
an ``Allow`` that lists exactly the methods *this* requester may use, one link
to the HSR (``rel="https://w3id.org/semantic-http/ns#http-schema-resource-document"``,
``type="application/json"``, no ``anchor``) and the expose headers. In this
order:

1. A CORS preflight (an OPTIONS with ``Origin`` and
   ``Access-Control-Request-Method``) is answered 204 without content or
   Links, before any authentication, throttle, hook, permission check or
   load of scrudful's or the framework's (the middleware, and django-ninja's
   ``add_decorator(mode="view")`` decorators, still run first).
2. OPTIONS itself is not gated: an anonymous requester of a locked resource
   gets a 204 with ``Allow: OPTIONS``, not a 401. What runs for it depends on
   the adapter:

   * DRF authenticates the requester and throttles the request as its
     ``initial()`` does, so bad credentials get DRF's 401 (or 403);
   * django-ninja throttles the request once, as ninja would throttle the
     operation, but runs no authentication of its own: each method's
     ``auth=`` callbacks run in step 4, and bad credentials make the requester
     anonymous;
   * FastAPI runs no dependency for OPTIONS itself: only the per-method
     security checks of step 4.
3. On an item URL, the object is loaded with the view's own loader, only once
   a check allows the requester, so that a requester refused everything can't
   tell which objects exist:

   * DRF: ``get_object()``, once at least one method passes its view-level
     permissions (a method with an ``allowed_func`` always passes);
   * django-ninja: the ``load`` of the GET operation (else of the first with
     one), once that operation's own ``auth=`` and ``before=`` allow the
     requester, with or without an ``allowed=`` hook;
   * FastAPI: ``add_options(load=...)``, once at least one method passes its
     security dependencies (every method does with ``allowed=``).

   Without an object (a collection, a loader returning ``None``, or a gate
   that refused) the object-level checks get ``obj=None``; a 404 the loader
   raises is the answer, with the error entity's Links and the HSR link.
4. Each method the path serves is checked for this requester, on a copy of
   the request with that method. A hook, when given, alone decides; without
   one, the framework's own declared authorization does:

   ============  ===========================================  ==========================================
   Adapter       Hook                                         Default checks
   ============  ===========================================  ==========================================
   django-ninja  ``allowed(request, method, obj)``, on the    the operation's ``auth=`` callbacks and
                 operation or the ``ScrudfulRouter``          its ``before=``
   DRF           ``allowed_func(view, request, method,        the permission classes:
                 obj)``, on the handler or ``Meta``           ``check_permissions``, then, with an
                                                              object, ``check_object_permissions``
   FastAPI       ``allowed(request, method, obj)``, on        the routes' security dependencies only
                 ``add_options``                              (``Security(...)``, or a ``Depends``
                                                              over a security scheme)
   ============  ===========================================  ==========================================

   No throttle and no handler runs per method, so computing ``Allow`` uses
   no throttle quota. The checks themselves run per method: django-ninja's
   ``auth=`` callbacks and ``before``, DRF's permission classes and, for
   FastAPI, each security dependency whole, with the plain dependencies
   nested inside it (a database session, a throttle a ``get_current_user``
   depends on). Whatever they write, they write for each method checked.
5. ``Allow`` lists the allowed methods and ``OPTIONS``; ``HEAD`` only when the
   path serves it. A 405 keeps RFC 9110's ``Allow`` for django-ninja and DRF:
   every method the path serves, whoever asks (FastAPI's 405 is
   Starlette's). An error answering OPTIONS (a loader's 404, a 429) has no
   entry of its own in the HSR, whose ``options`` entry is exactly the 204's.

**Every response links the HSR**: the 2xx, 304 and HEAD responses, scrudful's
own 400, 412 and 428, a 204 or an error your view returns, and the errors the
framework answers for the path through scrudful's error helpers. A preflight,
and an error for a URL no route serves, get none. A Link with that relation
that your view sets wins.

The base URL
------------

Set the absolute URL the API is served at:
``SCRUDFUL = {"BASE_URL": "https://api.example.com/"}`` in Django's settings
(or ``base_url=`` on a ``ScrudfulRouter``, a viewset's ``Meta`` or a DRF
decorator), ``add_options(app, base_url="https://api.example.com/")`` for
FastAPI. Relative link URLs (``"/schemas/item"``, a ``reverse()`` path)
resolve against it, which is needed because:

* the HSR is the same for every requester, so it can't resolve a relative URL
  against the request: without a base URL, a relative link URL in it is a
  configuration error (``ninja.errors.ConfigError``, Django's
  ``ImproperlyConfigured``, ``ValueError`` for FastAPI) when the document is
  built;
* the Link headers resolve against it too, so they agree with the HSR
  whichever ``Host`` the request came through;
* the HSR link's target is made absolute against it. Without one, it follows
  the request's ``Host``: set it so that every hostname the API answers on
  links the same document.

Absolute link URLs need no base URL. A base URL that isn't absolute, or isn't
a ``str``, is a configuration error.

Link callables in describe mode
-------------------------------

The HSR is built without a request, so scrudful calls the link callables (the
schema and context URL, ``rel`` and ``type`` of every entity, error entities
included) in *describe* mode:

* django-ninja: ``func(None, None)``, neither request nor object;
* DRF: ``func(view, None)``, with a request-less view instance: the route's
  view class built with its ``initkwargs`` (as DRF's ``as_view`` builds it),
  ``action`` set for a viewset, no URL kwargs;
* FastAPI: ``func()``, with no arguments.

On responses they are called as before. **A link callable must return the
same target in both modes**, or the HSR disagrees with the responses' Links.
A link that depends on the request, the object or a URL argument can't be
described; a callable that raises in describe mode is a configuration error
naming it, when the document is built. Return a path (Django's
``reverse()``, FastAPI's ``app.url_path_for()``) and set the base URL.

CORS
----

scrudful adds ``Link`` and ``Allow`` to ``Access-Control-Expose-Headers`` on
every response it makes or decorates, and ``ETag`` and ``Location`` when the
response has them (merged with an existing value): a cross-origin script needs
``Link`` for the schema, context and HSR links, and ``Allow`` for the methods
available to it. Configure the CORS middleware so that it keeps this:

* **Expose the headers.** A middleware that sets
  ``Access-Control-Expose-Headers`` itself replaces scrudful's on cross-origin
  responses: give it ``scrudful.utils.EXPOSE_HEADERS``
  (``("ETag", "Link", "Location", "Allow")``):
  ``CORS_EXPOSE_HEADERS = list(EXPOSE_HEADERS)`` for django-cors-headers,
  ``CORSMiddleware(..., expose_headers=list(EXPOSE_HEADERS))`` for Starlette.
* **Allow OPTIONS**, and the resources' methods, in
  ``Access-Control-Allow-Methods``: a script's own OPTIONS is preflighted.
  django-cors-headers' default ``CORS_ALLOW_METHODS`` has it; Starlette's
  ``CORSMiddleware`` defaults to ``GET`` only, which answers the preflight of a
  PUT or an OPTIONS with a 400: pass ``allow_methods=["*"]`` (or a list with
  ``OPTIONS``).
* **Credentials.** ``Allow`` reflects the requester, so a script sends its
  OPTIONS with credentials: answer them with
  ``Access-Control-Allow-Credentials: true`` and a named origin
  (``CORS_ALLOW_CREDENTIALS = True``; ``allow_credentials=True`` with
  ``allow_origins`` listing the origins).
* **Cover the HSR routes** (``/_hsr/``) with the same origins as the
  resources (a ``CORS_URLS_REGEX`` that matches them): the route sets no CORS
  headers of its own, and answers OPTIONS, a preflight too, with a 405, so the
  middleware must answer its preflights.

A preflight that reaches scrudful's OPTIONS is answered 204 without content or
Links, before anything runs; a CORS middleware in front answers it itself, or
adds its headers to scrudful's 204.

Django REST Framework
=====================

Callbacks are called as ``func(view, request, **url_kwargs)``, with every URL
kwarg, DRF's ``format`` among them on the format-suffix routes a router adds:
accept ``**kwargs``. The link callbacks are also called as ``func(view, None)``
to build the HTTP Schema Resource (see "Link callables in describe mode").
``@scrudful_viewset`` reads its configuration from an inner ``Meta`` class
(``list_*`` options configure the list response). The root URLconf includes
``hsr_urls()``, which serves every scrudful view's HTTP Schema Resource, and
the settings give the base URL:

.. code-block:: python

    # urls.py
    import scrudful.rest_framework

    urlpatterns = [
        ...,
        path("", include(scrudful.rest_framework.hsr_urls())),
    ]

    # settings.py
    SCRUDFUL = {"BASE_URL": "https://api.example.com/"}

A viewset:

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


    # A path, the same with and without the request (the HTTP Schema Resource
    # calls it as func(view, None)): scrudful resolves it against
    # SCRUDFUL["BASE_URL"]. DRF's reverse(..., request=request) would depend on
    # the request, and keep its ?format=.
    def item_schema_url(view, request, **kwargs):
        return reverse("item-schema")


    def item_context_url(view, request, **kwargs):
        return reverse("item-context")


    @scrudful_viewset
    class ItemViewSet(viewsets.ModelViewSet):
        queryset = Item.objects.all()
        serializer_class = ItemSerializer

        class Meta:
            etag_func = item_etag
            last_modified_func = item_last_modified
            schema_link_or_func = item_schema_url
            context_link_or_func = item_context_url

For single methods use ``@scrudful(...)`` on a method of an ``APIView`` or
viewset, and ``@scrudful_api_view(["GET", "PUT"], ...)`` for a function view,
in place of ``@api_view``. OPTIONS requests are answered with the 204 and its
per-requester ``Allow`` (see "OPTIONS and the HTTP Schema Resource"), by
``ScrudfulMetadata``, on ``scrudful_viewset`` viewsets, classes with a
``@scrudful`` method and ``scrudful_api_view`` views. ``@scrudful`` below a
plain ``@api_view`` also makes the function view SCRUDful, but there is no
class to install ``ScrudfulMetadata`` on when it runs: that view keeps DRF's
``SimpleMetadata`` OPTIONS, has no HTTP Schema Resource and its responses no
HSR link, and the errors it raises get no error Links (only scrudful's own
400, 412 and 428 do), so use ``scrudful_api_view`` instead. Set ``error=`` (or
``Meta.error``) and name ``scrudful.rest_framework.exception_handler`` as DRF's
``EXCEPTION_HANDLER`` so that the errors DRF raises itself for those views
carry the error entity's Links too. See ``examples/scrudful_rest_framework``
for a complete project, and the documentation's Django REST Framework page
for the settings, ``allowed_func``, ``statuses=`` and ``entity=``.

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
``page_size`` (1 to ``max_page_size``; default 100, maximum 1000) that are not
integers or are outside their range are one 400 naming each bad parameter, with
DRF's ``IntegerField`` codes (``invalid``, ``min_value``, ``max_value``) on the
errors, and a page past the last one is a 404. An integer is what django-ninja's
``scrudful_list`` accepts (pydantic's lax ``int``: ``2.0`` is 2, a non-ASCII
digit is not an integer), so both front ends answer the same values alike. Give the view an ordered
queryset: an unordered one warns, as with Django's ``Paginator``.

.. code-block:: python

    from scrudful.rest_framework import ScrudfulPagination


    def item_href(view, request, obj):
        return request.build_absolute_uri(f"/items/{obj.pk}")


    # With object_validators the callbacks take the object, not the URL kwargs.
    def item_object_etag(obj):
        return f"v{obj.version}"


    def item_object_modified(obj):
        return obj.updated_at


    @scrudful_viewset
    class ItemViewSet(viewsets.ReadOnlyModelViewSet):
        queryset = Item.objects.order_by("pk")
        pagination_class = ScrudfulPagination

        class Meta:
            href_func = item_href
            object_validators = True
            etag_func = item_object_etag
            last_modified_func = item_object_modified

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
``create``; without it, the 201 gets only the links. Set
``creates=True`` on an explicit ``@scrudful`` create (it needs
``object_validators``). The 201 gets ``ETag`` and ``Last-Modified`` from the saved
instance, plus ``Location`` from ``Meta.location_func(view, request, obj)``
(``location_func=`` on the decorator; it requires ``creates=True`` and
``object_validators``). A header
the view set is never replaced. The 201 is built in ``perform_create``, so a
custom ``create()`` that bypasses it gets no validators, and neither does a
bulk create (``many=True``) that saves several objects. The HTTP Schema
Resource describes the 201 without the validator headers, as django-ninja's
does.

A create's target is the collection, so its preconditions are evaluated
against the list validators (``Meta.list_etag_func`` and
``Meta.list_last_modified_func``; ``list_etag_func=`` and
``list_last_modified_func=`` on an explicit ``@scrudful``), as a GET of the
list computes them for the same request: ``If-Match`` with an ETag the list
doesn't have, a past ``If-Unmodified-Since``, ``If-None-Match: *`` or
``If-None-Match`` with the list's ETag is a 412, a malformed ``If-Match`` or
``If-None-Match`` a 400, and nothing is created. The 400 and 412 carry the
create's ``Meta.error`` and ``Meta.error_body_func``, as its serializer 400
does. The callbacks are called only for a POST with ``If-Match``,
``If-None-Match`` or ``If-Unmodified-Since``: a create without a precondition
proceeds (never a 428). They run before the view, so before any permission it
decides from the body. Two misplacements raise ``TypeError``: list validators on
a class-body handler named after a standard non-POST handler (``get``,
``list``, ``retrieve`` ...), and an explicit ``@scrudful`` create without list
validators of its own when ``Meta`` sets them (an explicit decorator replaces
the ``Meta`` configuration). On any other handler that doesn't answer a POST
(e.g. a GET ``@action``) they are ignored.

The check and the create are atomic only if you serialize every write that
changes the collection's validators with conditional creates: lock a row per
collection (``select_for_update()``) in a list validator callback, with a
transaction spanning the request (``ATOMIC_REQUESTS``) so the lock is held until
the create commits, and take the same lock in the other writes (unconditional
creates, which call no list callback, and item PUT, PATCH and DELETE). The list
callbacks also serve the ``list`` action: lock only when
``request.method == "POST"``, or every GET of the list takes the lock too.

**Error bodies (``error_body_func``).** ``error_body_func(request, result)``,
also ``Meta.error_body_func`` and ``Meta.list_error_body_func`` (the list
response's), returns the dict used as the body of scrudful's own 400, 412 and
428. It must be a plain function. ``scrudful_viewset`` gives ``create`` the
item's (``Meta.error_body_func``), paired with the ``Meta.error`` its 400 and
412 carry. Status, headers, Links and Content-Type are unchanged and it is not
called for a 304.

.. code-block:: python

    from scrudful.problem import PROBLEM_JSON, problem_details
    from scrudful.rest_framework import Entity


    def item_object_etag(obj):
        return f"v{obj.version}"


    def item_location(view, request, obj):
        return f"/items/{obj.pk}"


    @scrudful_viewset
    class ItemViewSet(viewsets.ModelViewSet):
        class Meta:
            object_validators = True
            etag_func = item_object_etag
            location_func = item_location
            error_body_func = problem_details
            error = Entity(media_type=PROBLEM_JSON)

Caveats of ``scrudful_viewset``:

* The item link callbacks also run for ``create`` and POST on the list route,
  where there is no ``pk``, and as ``func(view, None)`` for the HTTP Schema
  Resource. Accept ``**kwargs``, and return the same target without the
  request, as ``item_schema_url`` does above.
* An explicit ``@scrudful`` on ``update`` applies to PATCH when
  ``partial_update`` is DRF's stock one, in the HTTP Schema Resource and for
  the errors
  ``exception_handler`` answers too (decorate a subclass of a decorated
  viewset with ``scrudful_viewset`` too, or it inherits the parent's wrapper).
  With a user-defined ``partial_update`` that calls ``update``, the
  ``Meta``-configured wrapper of ``partial_update`` wins: decorate
  ``partial_update`` explicitly to override it.
* OPTIONS is scrudful's only when the viewset's ``metadata_class`` is DRF's
  default ``SimpleMetadata``, which ``ScrudfulMetadata`` replaces (so does an
  ``APIView`` with a ``@scrudful`` method). A custom ``metadata_class`` (or
  ``None``, or a project-wide ``DEFAULT_METADATA_CLASS``) is kept, and OPTIONS
  is then DRF's. A subclass of ``ScrudfulMetadata`` switches scrudful's
  OPTIONS on, and nothing else: it may not override ``determine_metadata``
  or the 0.9 hooks (``TypeError``); shape the document with ``statuses=`` and
  ``entity=`` instead.
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
  (422, 404, 405, ``HTTPException``) the error entity's Links, and answers an
  unhandled exception with a JSON 500. ``add_options(app,
  base_url="https://api.example.com/")`` serves every scrudful path's HTTP
  Schema Resource at ``/_hsr/<name>`` and answers its ``OPTIONS`` with the 204
  and its per-requester ``Allow``; ``load=`` (``load(request,
  **path_params)``) gives OPTIONS the object of an item URL, and
  ``allowed=`` decides each method (by default, the routes' security
  dependencies do). Call both once the routes are declared, before the app
  serves requests. FastAPI request bodies are described in the HTTP Schema
  Resource with the endpoint's response links: the adapter has no request
  entity.
* Link callbacks are also called as ``func()``, with no arguments, to build
  the HTTP Schema Resource: accept ``**kwargs`` (or give the parameters
  defaults) and return a path, e.g. ``app.url_path_for("user_schema")``,
  which resolves against ``add_options``'s ``base_url``.

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
    # Answers OPTIONS on every path with a scrudful operation and serves its
    # HTTP Schema Resource (/roles/_hsr/pk, /roles/_hsr/_root). Relative link
    # URLs resolve against base_url (by default SCRUDFUL["BASE_URL"]).
    roles = ScrudfulRouter(base_url="https://api.example.com/")
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
  for operations on the API itself): it answers OPTIONS with the 204 and its
  per-requester ``Allow`` (the operations' ``auth=`` and ``before=`` decide,
  or an ``allowed=`` hook), serves each path's HTTP Schema Resource, links it
  from every response, and answers a method no operation routes with a JSON
  405. Every operation on it needs an entity: a plain one takes
  ``@described(Entity(...))``. Scrudful operations on a plain ``Router`` get
  none of this.
* Link callables are also called as ``func(None, None)`` to build the HTTP
  Schema Resource: return the same target without the request or object.
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

scrudful follows `Semantic HTTP 0.1 <https://w3id.org/semantic-http/0.1/>`_ as
merged at spec commit ``37ec1ce`` (issue #4) in all three adapters. Once the
application does what the next list says, scrudful guarantees that:

* every response scrudful makes with content has a schema Link
  (``rel="describedby"``, ``type="application/schema+json"``) and, for a
  non-in-band media type, a context Link, and that every ``HEAD`` carries the
  Links of the ``GET``;
* every response of a described path carries exactly one HTTP Schema Resource
  link, with one target for all of them and no ``anchor``;
* ``OPTIONS`` answers a 204 without content, with an ``Allow`` that lists
  exactly the methods the requester may use (as the framework's declared
  authorization, or your hook, decides) and the HTTP Schema Resource link;
* the HTTP Schema Resource is served by ``GET`` as ``application/json``, with
  the published HTTP Schema Resource Links, the same bytes for every
  requester, a strong ``ETag`` and ``Cache-Control: public``; it lists every
  method of the path with its success statuses and media types, a ``default``
  error entry, and ``options`` as a 204 without content;
* Link header targets and every URI in the HTTP Schema Resource are absolute,
  link relations are de-duplicated, and a ``Link`` your view set with the
  same relation is kept instead of scrudful's. A ``Location`` (which may be
  relative) and a list entry's ``href`` are the application's, sent as given:
  return absolute ones from ``location_func`` / ``href_func`` if you want
  them;
* scrudful's own 400, 412 and 428 carry the Links of the ``error`` entity you
  pass (for FastAPI, else of the ``add_error_links`` entity), and are served
  with its ``media_type``, which must be JSON content
  (``application/json`` or ``+json``, other than JSON-LD). The same holds for
  every JSON error response that gets an error entity's Links (from the
  error-links helpers below, or raised inside a ninja scrudful operation): it
  is served with that entity's ``media_type``. With an
  ``application/problem+json`` entity, the framework's validation errors (a
  DRF serializer's 400, ninja's and FastAPI's 422) are rewritten as problem
  details, the framework's error list as the ``errors`` member
  (``scrudful.problem.problem_from_errors``);
* ``Link`` and ``Allow`` are always exposed to cross-origin scripts, and a CORS
  preflight is answered before any authentication, throttle or load of
  scrudful's or the framework's (the middleware, and django-ninja's
  ``add_decorator(mode="view")`` decorators, run first);
* a schema link without an RDF context link (for a non-in-band media type) is
  refused, since the HTTP Schema Resource would fail the spec's JSON Schema.

The application must still:

* **Set the base URL** (``SCRUDFUL["BASE_URL"]``, ``add_options(base_url=)``)
  when its link URLs are relative, and **make link callables work in describe
  mode** (see "The base URL" and "Link callables in describe mode").
* **Mount the HTTP Schema Resources:** for DRF, add
  ``path("", include(scrudful.rest_framework.hsr_urls()))`` to the root
  URLconf (without it, or ``SCRUDFUL["HSR_URL"]``, every routed request of a
  scrudful view raises ``ImproperlyConfigured``); for FastAPI, call
  ``add_options(app, ...)``. django-ninja's ``ScrudfulRouter`` does it itself.
* **Install the error-links helper:** ``add_error_links(api, ERROR)`` for
  django-ninja and FastAPI, so the errors the framework answers (422, 401,
  404, 405, ...) carry Links. For DRF, name ``exception_handler`` as
  ``EXCEPTION_HANDLER``: it adds the view's ``error`` Links to the errors DRF
  raises, and ``SCRUDFUL["ERROR"]``'s to those of a view without one.
* **Install the 404 and 500 handlers (Django):** in the root URLconf,
  ``handler404 = scrudful.ninja.handler404(PAGE_ERROR)`` and ``handler500 =
  scrudful.ninja.handler500(PAGE_ERROR)`` (or ``scrudful.rest_framework``'s),
  with ``PAGE_ERROR`` an ``Entity`` of plain link values. Django's 404 for a
  URL no pattern matches and its 500 page are then JSON (problem details for
  an ``application/problem+json`` entity) with the entity's Links, unless
  ``Accept`` prefers ``text/html``, always with ``Vary: Accept``. They apply
  with ``DEBUG`` off. FastAPI's ``add_error_links`` installs a JSON 500 itself.
* **Raise errors, don't return them:** a 4xx or 5xx your view returns gets
  no error Links from scrudful (the view's links describe its 2xx content
  only), just the HTTP Schema Resource link, unless an entity is declared for
  that status (django-ninja's ``responses=`` or ``ScrudfulResponse.entity``);
  one it raises goes through the error-links helper.
* **Give every conditional decorator** ``error=`` (``Meta.error`` and
  ``Meta.list_error`` for a DRF viewset): the helpers cover the errors the
  framework raises, not scrudful's own 400, 412 and 428, which, for DRF and
  django-ninja, carry Links only from the decorator's ``error`` (FastAPI's
  fall back to the ``add_error_links`` entity).
* **Give the authorization scrudful can't see to the** ``Allow`` **hook:**
  checks made in a ninja ``add_decorator`` decorator, in a FastAPI dependency
  that uses no security scheme or in a view's own code, and the object-level
  rules of a FastAPI app, don't show in ``Allow``: give ``allowed=`` /
  ``allowed_func`` (and FastAPI's ``load=``).
* **Keep scrudful's OPTIONS (DRF):** leave ``metadata_class`` at DRF's
  default, or subclass ``ScrudfulMetadata``. A custom metadata class, or a
  kept ``APIView.options`` (a view that only sets ``metadata_class =
  ScrudfulMetadata``), answers DRF's 200 with a body, which the HTTP Schema
  Resource's ``options`` entry (a 204) doesn't describe.
* **Use** ``scrudful_api_view`` **for DRF function views:** a plain
  ``@api_view`` with ``@scrudful`` keeps DRF's OPTIONS and has no HTTP
  Schema Resource, and ``exception_handler`` can't find its ``error``: the
  errors it raises get no Links, only scrudful's own 400, 412 and 428 do.
* **Decorate the HTTP method handlers of a DRF generic view** (``get``,
  ``put``, ``patch`` ...), or use a viewset with ``scrudful_viewset``: a
  ``@scrudful`` ``update`` behind the stock ``put`` of a
  ``RetrieveUpdateAPIView`` is evaluated, but the HTTP Schema Resource
  describes ``put`` and ``patch`` as bare 200s.
* **Describe the real success statuses and media types:** for DRF,
  ``statuses=`` (``{"post": [200]}`` for a POST action answering 200) and
  ``entity=`` (an ``Entity`` with the media type the view renders); FastAPI
  reads ``status_code=`` and ``responses=``, and takes ``entity=`` for a media
  type other than JSON.
* **Drop DRF's browsable API in production:** its HTML is allowed for a
  request whose ``Accept`` asks for ``text/html`` (scrudful sends ``Vary:
  Accept`` on a view with a renderer that isn't JSON), but DRF also serves it
  for ``?format=api`` without ``Accept``, which is undescribed content. Drop
  ``BrowsableAPIRenderer`` from ``DEFAULT_RENDERER_CLASSES`` (or the view's
  ``renderer_classes``), as the example does, or keep it reachable only
  through ``Accept: text/html``.
* **Form bodies (DRF) are allowed:** a resource MAY accept request media types
  its HTTP Schema Resource doesn't list, so DRF's default form and multipart
  parsers don't break conformance; the HTTP Schema Resource lists JSON only,
  and no lifting to RDF covers form data. The example accepts JSON only
  (``JSONParser``).
* **Give every entity a schema and a context** (only a schema for in-band
  types such as JSON-LD or Turtle). The spec requires the context of JSON
  content to be a JSON-LD 1.1 context. Keep the default ``describedby``
  relation: another one is no Schema Link Header while the HTTP Schema
  Resource still describes it as the schema.
* **Serve the linked documents:** scrudful sends their URLs, and serves only
  the HTTP Schema Resources.
* **Make item contexts map every member** of the item schema, so the item is
  RDF, and the collection context the envelope. For problem details with an
  ``errors`` member, publish a context that imports the w3id one
  (``@import``) and maps ``errors``.
* **Configure the CORS middleware** as "CORS" above says: expose
  ``scrudful.utils.EXPOSE_HEADERS``, allow ``OPTIONS`` and credentials, and
  cover ``/_hsr/``.

The three example projects (``examples/``) are conformant: their tests run the
wire-conformance suites' checks on their responses.

Migrating from 0.9
==================

0.10.0 is a breaking release: Semantic HTTP 0.1 changed its OPTIONS model
between spec commits ``bcc338e`` and ``37ec1ce``. For a project on 0.9
(projects on 0.8, such as django-scoped-rbac and scrud-django at the time of
this release, also need the 0.9.0 changes in the changelog):

1. **Move the pin** to ``scrudful>=0.10,<0.11``.
2. **Set the base URL:** ``SCRUDFUL = {"BASE_URL": "https://api.example.com/"}``
   (Django), ``add_options(app, base_url=...)`` (FastAPI). Without it, a
   relative link URL can't go into the HTTP Schema Resource.
3. **DRF: mount the HTTP Schema Resources.** Add
   ``path("", include(scrudful.rest_framework.hsr_urls()))`` to the root
   URLconf, and give every route of a scrudful view a URL name. Or, to serve
   a document elsewhere (beside ``collections-json-schema/<slug>/``, say), set
   ``Meta.hsr_route = False`` (or ``SCRUDFUL["HSR_ROUTE"] = False``) with
   ``Meta.hsr_url`` (or ``SCRUDFUL["HSR_URL"]``), ``hsr_url(view_name)``
   returning its absolute URL, and mount ``http_schema_resource_view(view_func)``
   there.
4. **Make the link callables request-free.** They are called in describe mode
   to build the HTTP Schema Resource: ``func(view, None)`` for DRF (the view
   still has the attributes its route's ``initkwargs`` set, such as a
   ``resource_type_name`` or a ``resource_type_iri``), ``func(None, None)`` for
   django-ninja, ``func()`` for FastAPI. Return the same target as on a
   response: a ``django.urls.reverse(...)`` path, which resolves against the
   base URL, rather than DRF's ``reverse(..., request=request)``.
5. **Implement the** ``Allow`` **hook** for an exact ``Allow``: for DRF,
   ``Meta.allowed_func`` (or ``allowed_func=`` on a decorator),
   ``allowed_func(view, request, method, obj) -> bool``; for django-ninja,
   ``ScrudfulRouter(allowed=...)`` (or ``allowed=`` on a decorator),
   ``allowed(request, method, obj) -> bool``; for FastAPI,
   ``add_options(app, allowed=..., load=...)``, with
   ``allowed(request, method, obj) -> bool`` and
   ``load(request, **path_params)`` returning the object (``None`` for none).
   Decide whether the requester may use ``method`` (``"GET"``, ``"PUT"``
   ...), on ``obj`` when there is one (for django-scoped-rbac, its
   ``http.<METHOD>`` permission against the policy).
   ``obj`` is ``None`` for a collection, and, under django-ninja, also on an
   item URL when the GET operation's ``auth=`` or ``before=`` refused the
   requester (the object is then not loaded). Under DRF and FastAPI, a hook
   makes OPTIONS load the object for every requester, so ``get_object()``
   (DRF) or ``load`` (FastAPI) must hide, with a 404 or ``None``, the objects
   a requester may not see. A denial the hook raises (a 401 or 403) refuses
   the method; a 404 it raises answers the OPTIONS. Without a hook, the
   permission classes (DRF), ``auth=`` and ``before=`` (ninja) or the security
   dependencies (FastAPI) decide.
6. **Update the tests that assert the 200 OPTIONS body.** OPTIONS is now a 204
   without content, ``Allow`` depends on the requester, and the document is
   at the target of the response's HTTP Schema Resource link: GET it there. It
   has a ``default`` entry per method and ``options`` as a 204; ``HEAD`` is in
   ``Allow`` only where it is served. A middleware that strips ``Allow`` from
   OPTIONS must now keep it.
7. **Replace the removed APIs:** ``ScrudfulMetadata``'s 0.9 hooks
   (``determine_metadata_for_<method>``, ``_write``,
   ``required_conditional_headers``) and a ``determine_metadata`` override
   raise ``TypeError``: shape the document with ``statuses=``, ``entity=`` and
   ``Meta``. ``scrudful.utils.EXPOSABLE_HEADERS`` is now ``EXPOSE_HEADERS``,
   and the private ``scrudful._adapter.options_links``, ``describe_options``
   and ``allow_header`` are gone.
8. **Install** ``handler404`` / ``handler500`` (Django), and configure the CORS
   middleware (``EXPOSE_HEADERS``, ``OPTIONS``, the ``/_hsr/`` routes).
9. **Also check what now fails at startup or on configuration:**

   * a schema link without a context link (for a non-in-band media type, such
     as JSON) is refused. A plain value raises
     ``scrudful.entity.HsrConfigError`` (a ``ValueError``) when an ``Entity``
     is created or a view is decorated: DRF's ``scrudful``,
     ``scrudful_api_view``, ``ScrudfulViewFunc`` and ``scrudful_viewset``
     (for ``Meta``'s item and ``list_`` links) and FastAPI's ``scrudful``;
     ninja's decorators raise ``ConfigError`` chained to it. A callable one
     raises the adapter's configuration error when the HSR is built. Give
     every schema link its ``context_link_or_func`` (0.9's README DRF
     snippet had a schema only);
   * a base URL that isn't an absolute URL, a ``str`` (bytes, say) included,
     raises the adapter's configuration error: ``ConfigError`` (ninja),
     ``ImproperlyConfigured`` (DRF, the 404 and 500 handlers) or
     ``HsrConfigError``, a ``ValueError`` (FastAPI);
   * DRF: ``manage.py check``, and so ``runserver``, ``migrate`` and Django's
     test runner, which run it, now fail with the errors ``scrudful.E001``
     to ``E007``: an unnamed route of a scrudful view, a URL name shared by
     two scrudful views, a missing ``hsr_urls()`` include, a route turned off
     without ``hsr_url``, an HTTP Schema Resource that can't be built, an
     invalid ``SCRUDFUL`` setting, and an action of a ``scrudful_viewset``
     class without ``@scrudful`` (an ``@action``, or a method its
     ``mapping`` adds: decorate it, with ``entity=`` or link options). Only
     what may depend on the data is the warning ``scrudful.W001``, so that
     ``migrate`` runs on a fresh database: a link callable raising a
     ``django.db.DatabaseError`` when called as ``func(view, None)``, or
     returning nothing for a context link or its type. Anything else a
     callable raises (a ``NoReverseMatch``, a ``TypeError`` of its
     signature) is ``E005``. Run ``manage.py check`` before deploying;
   * DRF: ``Meta.hsr_route = False`` without ``Meta.hsr_url`` or
     ``SCRUDFUL["HSR_URL"]`` raises ``ImproperlyConfigured`` when the class
     is decorated;
   * django-ninja: every plain (not scrudful) operation on a
     ``ScrudfulRouter`` needs ``@described(Entity(...))`` (or
     ``scrudful_entity=``), or adding it raises ``ConfigError``;
   * django-ninja: the API's and routers' ``add_decorator`` decorators now
     also wrap the public HSR route, so a gate added that way makes it
     non-public; and ``add_decorator(mode="operation")`` gates no longer gate
     OPTIONS, nor show in its ``Allow`` (give ``allowed=``);
   * django-ninja: a ``ScrudfulRouter``'s options are checked when set as
     attributes too, and ``hsr_route`` can only be set before its first
     operation (``ConfigError``). The ninja internals OPTIONS relies on are
     checked when a path gets its OPTIONS, at startup (``ConfigError`` for
     an unsupported ninja); where ninja's source isn't installed, a
     ``RuntimeWarning`` says the check is weaker, once;
   * FastAPI: ``add_options`` raises ``RuntimeError`` on a FastAPI whose
     internals differ from those scrudful supports, when a route has
     security dependencies to check (no ``allowed=``), and ``ValueError``
     (0.9: ``TypeError``) for a link callable that can't be called as
     ``func()``;
   * FastAPI: a security dependency that needs the request body (a body,
     form or file parameter, at any depth) can't run on OPTIONS: its method
     is left out of ``Allow`` for every requester, and ``add_options`` warns
     (``RuntimeWarning``, which fails a test suite run with warnings as
     errors). Give ``allowed=`` to decide such a method.

10. **Check what now behaves differently:**

    * DRF and django-ninja resolve the HSR link before the handler runs, as
      FastAPI does: a link that can't be resolved (the ``hsr_urls()``
      include missing, an ``hsr_url`` that raises or returns a relative URL)
      fails the request before anything is written;
    * DRF: OPTIONS loads the object as the first method that passed its
      view-level checks loads it (GET, when it did): with a request of that
      method and, on a viewset, its action (``retrieve``), so a
      ``get_queryset`` scoped by ``self.action`` scopes it as a GET does. A
      ``get_object`` of the view's own is called on a viewset's item action,
      and on a generic view's route with a URL kwarg other than ``format``,
      whatever the kwarg is named: it must handle each route it serves
      (raise ``Http404`` for a missing object). A plain ``APIView`` is loaded
      only when the URL kwargs have its ``lookup_url_kwarg``;
    * DRF: an undecorated action of a ``scrudful_viewset`` (``E007``) still
      answers, but no HSR describes it, and its responses get no HSR link;
    * django-ninja: ``SCRUDFUL["HSR_URL"]`` is called with the route Django
      resolved the request to (``request.resolver_match.route``, the full
      path template with every prefix: ``"api/items/<pk>"``), the same under
      ninja's ``TestClient`` for an API the root URLconf includes; a router's
      own ``hsr_url`` gets the router's path (``"/{pk}"``). ``http_schema_resource`` and
      ``http_schema_resource_view`` take ``api=``: pass it for a router
      mounted on several APIs;
    * django-ninja, with ``add_error_links``: a 401 or 403 of a scrudful
      operation (ninja's authentication's, ``before=``'s,
      ``after_throttles=``'s or one it raises) carries the API-wide entity,
      not the operation's ``error``, unless the operation declares that
      status in ``responses=``: a declared error status has its own HSR entry
      and its entity wins, on every response with that status;
    * FastAPI: scrudful's 400, 412 and 428 of an endpoint without ``error=``
      carry the Links and media type of the ``add_error_links`` entity;
    * FastAPI: ``add_options(router)`` wraps the router's ``add_api_route``
      and ``include_router`` (and those of the routers included in it), so a
      resource added afterwards gets its OPTIONS and HSR routes as it is
      added. A route appended to ``routes`` directly gets them only with the
      next route added, or another ``add_options`` call, and a
      ``RuntimeWarning`` names it. A ``copy.copy`` of such a router shares
      the wrapped methods (a route added to the copy goes to the original),
      and ``copy.deepcopy`` fails: copy a router before ``add_options``.

The changelog lists every change.

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
