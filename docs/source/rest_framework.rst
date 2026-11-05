Django REST Framework
=====================

``scrudful.rest_framework`` adapts the shared conditional-request core to DRF
viewsets, ``APIView`` methods and function views. Install it with
``pip install "scrudful[drf]"``. The example project in
``examples/scrudful_rest_framework`` shows a viewset, a read-only viewset and a
function view. See :doc:`usage` for the behaviour table, the caveats of
``scrudful_viewset``, the concurrency note, and the OPTIONS and HTTP Schema
Resource model the three adapters share.

Setup
-----

Three lines make a project's scrudful views follow Semantic HTTP 0.1: the
``hsr_urls()`` include, which serves every scrudful view's HTTP Schema
Resource (HSR), the base URL, and the exception handler. The 404 and 500
handlers make Django's own error pages JSON too:

.. code-block:: python

    # urls.py
    import scrudful.rest_framework

    urlpatterns = [
        ...,
        path("", include(scrudful.rest_framework.hsr_urls())),
    ]

    PAGE_ERROR = scrudful.rest_framework.Entity(
        "/schemas/error.json", context_link_or_func="/contexts/error.jsonld"
    )
    handler404 = scrudful.rest_framework.handler404(PAGE_ERROR)
    handler500 = scrudful.rest_framework.handler500(PAGE_ERROR)

    # settings.py
    SCRUDFUL = {"BASE_URL": "https://api.example.com/"}
    REST_FRAMEWORK = {
        "EXCEPTION_HANDLER": "scrudful.rest_framework.exception_handler",
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    }

Every response of a scrudful view routed through the URLconf links its HSR, so
**the include is required**: without it (or ``SCRUDFUL["HSR_URL"]``), every
routed request of a scrudful view, not only an OPTIONS, raises
``ImproperlyConfigured``, which names the line to add (``DEBUG`` on or off).
The link is resolved once DRF's ``initial()`` has run, before the handler, so
a link that can't be resolved (the include missing, an ``hsr_url`` that
raises or returns a relative URL) fails the request before anything is
written. A request that comes
through no URL (a view called directly, with ``APIRequestFactory``) gets no
HSR link.

``manage.py check`` (which ``runserver``, ``migrate`` and Django's test runner
run too, and which its errors fail) reports these misconfigurations before any
request, as errors of Django's URL system check:

* ``scrudful.E001``: a scrudful view's route has no URL name (unless the view
  has an ``hsr_url``, which needs none);
* ``scrudful.E002``: one URL name is given to the routes of two different
  scrudful views;
* ``scrudful.E003``: scrudful views rely on the HSR route (no ``Meta.hsr_url``
  or ``SCRUDFUL["HSR_URL"]``) and the ``hsr_urls()`` include is missing;
* ``scrudful.E004``: a view's HSR route is turned off without an ``hsr_url``;
* ``scrudful.E005``: a view's HSR can't be built (a relative link URL without
  a base URL, a schema link without an RDF context, content that isn't JSON
  without a context type, or anything else building it raises but what
  ``scrudful.W001`` reports; a link callable raising a ``NoReverseMatch`` or a
  ``TypeError`` of its signature included). For an exception that isn't
  scrudful's configuration error (a link callable's, the view's, or scrudful's
  own), the hint names the frame that raised it, labelling one inside scrudful
  as what may be its internal error (please report it), and the
  ``scrudful.rest_framework`` logger logs the traceback at ``ERROR``;
* ``scrudful.E006``: the ``SCRUDFUL`` setting is invalid: it isn't a dict of
  its keys, or a key's value is invalid (one error for each key). While it is,
  the checks that depend on it are skipped (no HSR is built);
* ``scrudful.E007``: an action of a ``scrudful_viewset`` class without
  ``@scrudful`` (an ``@action``, or a method its ``mapping`` adds).
  ``scrudful_viewset`` wraps only ``list``, ``create``, ``retrieve``,
  ``update``, ``partial_update`` and ``destroy``: an extra action needs its
  own ``@scrudful(entity=...)`` (or link options). Without it nothing
  describes its responses: they still answer, but get no HSR link (nor
  ``Meta``'s entity and Links), and a route of such actions alone has no HSR;

and, as a warning:

* ``scrudful.W001``: a failure that may depend on the data: a link callable
  raises a ``django.db.DatabaseError`` (``OperationalError``,
  ``ProgrammingError`` ...), a ``django.http.Http404`` (``get_object_or_404``,
  ``get_list_or_404``) or a ``django.core.exceptions.ObjectDoesNotExist`` (a
  model's ``DoesNotExist``), subclasses included, when called as
  ``func(view, None)``, or returns nothing for a context link (beside a
  schema link) or for its type. The warning names the route, the callable or
  entry, and the error. There is one for each callable that fails so, and
  the check goes on past it (describing that entry without content), so a
  warning hides no ``scrudful.E005`` of the same view.

These are only a warning because ``migrate`` runs the checks before it
migrates: a callable that reads the database fails on a database not migrated
yet, and finds no row in a table not filled yet (by a data migration or a
fixture), and an error would stop
``migrate`` (and ``makemigrations`` and ``runserver``) on a fresh database.
The HSR is built again, and the callables called again, when it is first
served; if it fails then too, that request fails, so don't leave the warning
standing. Any other exception a callable raises is ``scrudful.E005``.

The check builds each view's HSR as the route does (without a request; link
callables are called) and keeps none. With the include, it runs in Django's own
URL check, which loads the URLconf. Without the include (a project serving
its HSRs elsewhere, with ``SCRUDFUL["HSR_URL"]`` or ``Meta.hsr_url``, or one
that forgot it), the checks run only when ``scrudful.rest_framework`` was
imported before they started, which registers them: import it in an
``AppConfig.ready()`` of yours, or ``manage.py check`` reports none of them,
``scrudful.E003`` included.

``scrudful_viewset``, ``scrudful_api_view`` and a ``@scrudful`` method in the
body of an ``APIView`` (or viewset) class install scrudful's OPTIONS and HSR
link, through :class:`~scrudful.rest_framework.ScrudfulMetadata`, but only when
the view's ``metadata_class`` is DRF's default ``SimpleMetadata``. A custom
class, ``None``, or a project-wide ``DEFAULT_METADATA_CLASS`` is kept, and
OPTIONS is then DRF's (its 200 with a body, or a 405); make a custom class
subclass ``ScrudfulMetadata`` (or mix it in first) to switch scrudful's on. So
does a ``@scrudful`` method in the body of a mixin: each ``APIView``
subclassing the mixin (``class ItemView(ItemMixin, APIView)``) gets it when it
is created, by the same rules (scrudful chains the mixin's
``__init_subclass__`` to do so). A ``metadata_class`` or ``options`` that the
view, or a class between it and the mixin, sets is kept.

Subclassing ``ScrudfulMetadata`` does nothing else: OPTIONS has no body and the
HSR is built without the request, so a subclass may not define
``determine_metadata`` or one of 0.9's hooks (``determine_metadata_for_<method>``,
``_write``, ``required_conditional_headers``), nor ``get_method``, which
scrudful never calls: each raises ``TypeError`` when the class is created.
Shape the document with ``statuses=``, ``entity=`` and ``Meta`` instead (see
`The HTTP Schema Resource`_). A view that only sets
``metadata_class = ScrudfulMetadata``, keeping DRF's ``options``, answers
OPTIONS with the HSR as DRF's 200 body, which is not Semantic HTTP's OPTIONS.

A function view with ``@scrudful`` below a plain ``@api_view`` is SCRUDful,
but there is no class to install it on when ``@scrudful`` runs: it keeps DRF's
``SimpleMetadata`` and gets no HSR, and its responses no HSR link. Nor can
``exception_handler`` find its ``error`` entity (the view's methods are
``api_view``'s own), so the errors it raises (a serializer's 400, a
``NotFound``) get no error Links: only scrudful's own 400, 412 and 428 carry
them. Use ``scrudful_api_view`` in place of ``@api_view`` + ``@scrudful``:

.. code-block:: python

    # OPTIONS: DRF's SimpleMetadata, no HTTP Schema Resource; raised errors
    # get no error Links.
    @api_view(["GET", "PUT"])
    @scrudful(etag_func=item_etag, error=ERROR)
    def item(request, pk): ...


    # OPTIONS: scrudful's 204; raised errors get ERROR's Links.
    @scrudful_api_view(["GET", "PUT"], etag_func=item_etag, error=ERROR)
    def item(request, pk): ...

(The callbacks of the first get ``(request, **url_kwargs)``, those of the
second ``(view, request, **url_kwargs)``.)

The view's schema and context links describe its success content: they go on
a 2xx response other than 204 that the view returns, and on no other (nor on
a TRACE's, whose content is the echoed request). A 204 (a DELETE's), a
redirect, or an error the view *returns* (``Response(serializer.errors,
status=400)``, a 404 ``Response``) gets no schema or context Links from
scrudful, only the HSR link; a ``Link`` the view set on it is kept as is. To
give an error the error entity's Links, *raise* it
(``serializers.ValidationError``, ``NotFound``, ...) and install
``exception_handler`` (below).

Settings
--------

The optional ``SCRUDFUL`` Django setting is a dict of these keys; a viewset's
``Meta`` and the decorators override some of them per view:

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - ``SCRUDFUL[...]``
     - Per view
     - Meaning
   * - ``"BASE_URL"``
     - ``base_url=`` (decorators), ``Meta.base_url``
     - The absolute URL relative link URLs resolve against, in the HSR, on
       the Link headers and for the HSR link (see "The base URL" in
       :doc:`usage`). Without one, a relative link URL is an
       ``ImproperlyConfigured`` when the HSR is built, and the responses
       resolve their Links against the request.
   * - ``"HSR_ROUTE"``
     - ``Meta.hsr_route``
     - Whether :func:`~scrudful.rest_framework.hsr_urls` serves the view's
       HSR (default ``True``). ``False`` needs an ``hsr_url``:
       ``Meta.hsr_route = False`` without ``Meta.hsr_url`` or
       ``SCRUDFUL["HSR_URL"]`` raises ``ImproperlyConfigured`` when the class
       is decorated (``scrudful.E004`` reports the setting's).
   * - ``"HSR_URL"``
     - ``Meta.hsr_url``
     - ``hsr_url(view_name)``: the absolute URL of the HSR of the route named
       ``view_name`` (namespace-qualified, as
       ``request.resolver_match.view_name``: ``"api:item-detail"``), for a
       document served elsewhere. The responses link to it as it returns
       it. django-ninja calls the setting's callable with a route instead
       (``"api/items/<pk>"``): a project using both adapters sets
       ``Meta.hsr_url`` here, or the ninja routers' ``hsr_url=``, so that
       one callable doesn't get both kinds.
   * - ``"HSR_CACHE_CONTROL"``
     - ``Meta.hsr_cache_control``
     - The ``Cache-Control`` of the HSR route (default ``public,
       max-age=300``).
   * - ``"ERROR"``
     - ``error=``, ``Meta.error``, ``Meta.list_error``
     - The API-wide error ``Entity``: the errors DRF answers itself for a
       scrudful view (see `Errors DRF raises`_).

Another value than a dict, or an unknown key, raises ``ImproperlyConfigured``,
as does a base URL that isn't absolute. The per-handler options of the HSR,
``statuses=``, ``entity=`` and ``allowed_func=``, are described below and in
:class:`~scrudful.rest_framework.ScrudfulViewFunc`.

The HTTP Schema Resource
------------------------

:func:`~scrudful.rest_framework.http_schema_resource` builds a view's HSR from
its view function (what ``as_view(...)`` or ``scrudful_api_view`` returns),
once, without a request: a *describe-mode* view instance,
``view_func.cls(**view_func.initkwargs)``, as DRF's view function builds one
but without dispatching (``request`` is ``None``, ``args`` and ``kwargs`` are
empty, and a viewset has its ``action`` set for each method). It is the same
for every requester and every ``Host``. It describes each method the view
allows:

* the request body and success responses with the content of the handler's
  ``entity`` (by default ``application/json`` with the handler's schema and
  context links). ``entity=Entity(..., media_type="text/csv")`` (or
  ``Meta.entity`` / ``Meta.list_entity``) describes another media type: the
  view's responses get its Links, and the HSR its content under that key;
* the success statuses: ``statuses=`` (or ``Meta.statuses``), a mapping of
  lowercase methods to lists of 2xx codes, e.g. ``{"post": [200]}`` for a
  POST ``@action`` answering 200; else DRF's: 201 for POST, 204 for DELETE,
  200 for the others. A status that isn't a 2xx raises ``ValueError``;
* scrudful's 304, 400, 412 and 428 for a conditional method, the 400, 412
  and 428 with the ``error`` entity's content, else ``SCRUDFUL["ERROR"]``'s,
  and the precondition headers;
* a ``default`` entry for the errors :func:`~scrudful.rest_framework.exception_handler`
  answers, with the content of the handler's ``error`` (the view-wide one for
  a handler that isn't scrudful), else ``SCRUDFUL["ERROR"]``'s. When
  ``SCRUDFUL["ERROR"]`` is set and differs from the handler's ``error``, the
  401, 403, 406 and 415 that DRF answers itself, which carry
  ``SCRUDFUL["ERROR"]``, have entries of their own. Without scrudful's
  exception handler (or one carrying its marker, below), the raised errors
  have no Links and ``default`` no content;
* ``head`` like ``get`` without content, and ``options`` as a 204.

Link callables are called as ``func(view, None)``: they get the describe-mode
view, so the attributes its route's ``initkwargs`` set (a ``resource_type_name``,
say), but neither the request nor the URL kwargs. They must return the target
they return on the responses, or the HSR disagrees with the Links. One that
needs the request or a URL kwarg raises when called so, which is an
``ImproperlyConfigured`` naming it when the HSR is built; so is a relative URL
without a base URL, and a callable schema link without an RDF context link
(for a non-in-band media type). A plain schema link without a context link is
refused sooner, when decorating (``scrudful``, ``scrudful_api_view``, and
``scrudful_viewset`` for ``Meta``'s item and ``list_`` links), as
``Entity(...)`` refuses it: ``scrudful.entity.HsrConfigError``, a
``ValueError``. Return a ``django.urls.reverse(...)`` path: DRF's
``reverse(..., request=request)`` keeps the request's host and ``?format=``.

The HSR route
~~~~~~~~~~~~~

:func:`~scrudful.rest_framework.hsr_urls` serves ``_hsr/<view_name>`` (named
``scrudful_hsr``), with ``view_name`` the route's namespace-qualified URL name:
``/_hsr/item-detail``, ``/_hsr/api:item-detail``. On its first request it
scans the URLconf, through every ``include()`` and namespace, for the routes
of scrudful views (a ``scrudful_viewset`` class, a view with a
``ScrudfulViewFunc`` handler, a ``scrudful_api_view``), however they are
mounted (a router, ``path()``). It answers GET and HEAD with the document as
compact ``application/json``, the HSR's own schema and context Links, a strong
``ETag`` (a 304 for a matching ``If-None-Match``) and ``Cache-Control``;
another method with a 405 (``Allow: GET, HEAD``, no content); a name that is
no scrudful route's, or whose view has its route turned off, with a JSON 404,
``{"detail": "Not found."}``, without Links. It runs no authentication,
permission check or throttle, is CSRF-exempt, and is public under Django's
``LoginRequiredMiddleware`` (Django 5.1+): the route and
:func:`~scrudful.rest_framework.http_schema_resource_view` are
``login_required = False``.

* Give every route of a scrudful view a URL name: an unnamed one raises
  ``ImproperlyConfigured`` on its requests, and two scrudful views sharing one
  name make a GET of that name's ``/_hsr/`` raise it. Either breaks only its
  own requests: the other names are served (``manage.py check`` reports both).
* The ``_hsr`` segment is the route's: don't put a route of yours under
  ``/_hsr/``. Place the include before any catch-all pattern (a single-page
  app's ``re_path(r"^.*$", ...)`` fallback, a ``<path:slug>`` page view):
  Django tries the URLconf's patterns in order, so one placed before the
  include that matches ``/_hsr/<name>`` shadows the route, and the HSR link
  targets that view instead. The system checks don't detect it.
* **Serving it elsewhere.** Turn the route off for a view
  (``Meta.hsr_route = False``, or ``SCRUDFUL["HSR_ROUTE"] = False`` for all,
  with ``Meta.hsr_route = True`` to keep one), say where the document is
  (``Meta.hsr_url`` or ``SCRUDFUL["HSR_URL"]``) and mount
  :func:`~scrudful.rest_framework.http_schema_resource_view` there, which
  answers as the route does:

  .. code-block:: python

      @scrudful_viewset
      class CollectionViewSet(viewsets.ModelViewSet):
          class Meta:
              hsr_route = False

              def hsr_url(view_name):
                  return f"https://api.example.com/collections-hsr/{view_name}/"

      collection = CollectionViewSet.as_view(
          {"get": "retrieve", "put": "update", "delete": "destroy"}
      )
      urlpatterns = [
          path("collections/<slug>/", collection, name="collection"),
          path("collections-hsr/collection/", http_schema_resource_view(collection)),
      ]

OPTIONS and Allow
-----------------

With ``ScrudfulMetadata`` installed, OPTIONS is answered in this order:

1. **A CORS preflight** (``Origin`` and ``Access-Control-Request-Method``) is a
   204 without content, ``Content-Type`` or Links, answered before DRF's
   ``dispatch``: no content negotiation, authentication (so no CSRF check),
   permission check, throttle or loading runs. Django middleware runs first,
   so a CORS middleware adds its headers, or answers it itself.
2. **The request is authenticated and throttled** as DRF does in
   ``initial()``, but OPTIONS itself is not gated by the permissions: an
   anonymous requester of a resource it may not use gets a 204 with ``Allow:
   OPTIONS``. Bad credentials are answered as DRF answers them (a 401, or a
   403 when the first authenticator has no ``WWW-Authenticate``), a refused
   throttle with DRF's 429.
3. **Each method's view-level permissions** (``check_permissions``) run, on a
   request of that method (``rest_framework.request.clone_request``, also the
   view's ``request``, with the method's action as a viewset's ``action``,
   meanwhile). A method with an ``allowed_func`` skips this pass: the hook
   alone decides, with the object.
4. **Only if a method passed is the object loaded**, on an item's route, with
   ``get_object()``, its ``check_object_permissions`` suppressed, first as the
   first method that passed (GET, when it did) loads it: a request of that
   method is the view's ``request`` meanwhile and, on a viewset, that method's
   action (``retrieve`` for GET) its ``action``, so a ``get_queryset`` that
   scopes by ``self.action`` gives the queryset that method gets (not
   OPTIONS's ``metadata`` action's). Then it is loaded once for each other
   action of a method that passed, as that action loads it; on a view that
   isn't a viewset (it has no actions), once for each other method that
   passed, on a request of that method, so a ``get_queryset`` scoped by
   ``self.request.method`` scopes it. A 401, 403 or 404 such a load raises (a
   ``get_queryset`` that lets only the owner update or delete, say) leaves out
   only the methods it was loaded for, as their requests would answer it.
   **The cost:** one ``get_object()`` (usually one query) per distinct action
   (or method) of an allowed method, so an item OPTIONS of a ``ModelViewSet``
   may run four (``retrieve``, ``update``, ``partial_update``, ``destroy``).
   An item's route is one whose ``lookup_url_kwarg``,
   else ``lookup_field``, is in the URL kwargs; with a ``get_object`` of the
   view's own, whatever the URL kwarg is named: on a viewset, a route whose
   action is ``retrieve``, ``update``, ``partial_update``, ``destroy`` or an
   ``@action(detail=True)``; on a generic view (``GenericAPIView``) that
   overrides ``get_object``, a route with a URL kwarg (but DRF's ``format``).
   A plain ``APIView`` is loaded only on its lookup kwarg: declare it with
   ``lookup_url_kwarg``. A ``get_object`` called so must handle every route
   it is called on (or raise ``Http404``): what else it raises is the
   OPTIONS's answer. So a requester refused every method gets ``Allow:
   OPTIONS`` whether the object exists or not, as a GET of either is refused
   alike. The first load's ``get_object()`` that raises (the ``Http404`` of a
   missing object, or of one the requester may not read) is the answer,
   through the view's exception handler, with the HSR link.
5. **Each method that passed is checked against the object** loaded for
   it: ``check_object_permissions``, on the same request of that method
   (with no object, a collection's, the method is allowed). DRF's
   ``PermissionDenied`` or ``NotAuthenticated``, any other 401 or 403
   ``APIException`` (``AuthenticationFailed``, or one of yours), or Django's
   ``PermissionDenied`` means not allowed; anything else raised propagates,
   answered as in step 4. An ``Http404`` (or ``NotFound``) raised there by the
   first loaded method's check (``DjangoObjectPermissions`` for an object the
   requester may not read) is the OPTIONS's 404, as a GET's would be, not a
   method refused; raised by another method's check, it leaves out only that
   method.
6. **The answer** is a 204 without content or ``Content-Type`` (whatever
   renderer DRF's content negotiation picked), with ``Allow`` (the methods
   allowed and ``OPTIONS``; ``HEAD``, which DRF dispatches to the GET handler,
   when the view serves it and GET is allowed), one HSR link and
   ``Access-Control-Expose-Headers``. No throttle and no handler runs per
   method.

``allowed_func(view, request, method, obj) -> bool`` (on a handler, or
``Meta.allowed_func`` for every handler of a viewset), when given, alone decides
its method: ``request`` is a request of ``method``, ``obj`` the loaded object
or ``None``. It returns a ``bool`` (``TypeError`` otherwise), and may raise
DRF's ``PermissionDenied`` or ``NotAuthenticated``, any other 401 or 403
``APIException`` (``AuthenticationFailed``, or one of yours), or Django's
``PermissionDenied`` for ``False``, as ninja's and FastAPI's hooks may raise
their 401 and 403. An ``Http404`` (or ``NotFound``) it raises is the OPTIONS's
404 for the first method loaded (GET, when it passed); for another method, once
that one's request reaches the object, it leaves out only that method.
Anything else propagates. Use it for
authorization the permission classes don't hold (a policy checked in the
view's code, say). A method with an ``allowed_func`` passes step 3 for every
requester, since the hook has no object-free check: on such a view the object
is loaded for anyone, and a missing one is a 404 whoever asks, so OPTIONS tells
anyone which objects exist, unless ``get_object()`` itself hides those a
requester may not see (with a 404).

**A 404 from a per-method check is the OPTIONS's answer.** A view-level
permission (``has_permission``), or the first loaded method's object permission
(``has_object_permission``) or ``allowed_func``, that raises ``Http404`` or
``NotFound`` for a method answers the whole OPTIONS with that 404 (error Links
and HSR link), as the method's own request would be answered, so ``Allow``
never reveals a resource a check hides. Once the first method's request
reaches the object, another method's object permission or ``allowed_func``
raising it, or that method's own load, leaves out only that method.
This differs from ninja and FastAPI, where a 404 that a method's ``auth=`` /
``before`` or security dependency raises refuses only that method, and their
hooks' 404 is always the answer.
An error other than a 401, 403 or 404 from another method's load (a
``ValidationError``, or a ``Throttled`` a ``get_queryset`` raises) answers the
whole OPTIONS, where ninja leaves only that method out of ``Allow``.

A 405 keeps DRF's ``Allow``, every method the view serves. An ``Accept`` that
none of the view's renderers serves (e.g. ``text/html`` on a view without the
browsable API) still gets DRF's 406, raised before OPTIONS is handled. With
``metadata_class = None`` OPTIONS is DRF's 405.

Errors DRF raises
-----------------

An ``error`` entity gives scrudful's own 400, 412 and 428 its Links; without
one, they carry ``SCRUDFUL["ERROR"]``'s, the API-wide entity, and the HSR's
400, 412 and 428 entries describe its content. The errors
DRF itself raises (a serializer's 400, the 404 of ``get_object()``, a 405, a 401
or 403) need ``exception_handler``, which settings must name::

    REST_FRAMEWORK = {
        "EXCEPTION_HANDLER": "scrudful.rest_framework.exception_handler",
    }

It calls DRF's handler and, for a view whose handler for the request has an
``error`` entity, adds that entity's Links and serves the response with the
entity's media type, whatever the request's ``Accept``. An error with no
scrudful handler of its own (one raised while answering OPTIONS, such as a 403
or an unsupported ``?format=``, the 405 for a method the view doesn't serve,
or an error a handler that isn't scrudful raises) gets the view-wide entity:
the ``error`` the view's scrudful handlers share. When they have different
ones (a viewset list route whose ``Meta.list_error`` differs from
``Meta.error``, say), it gets the GET handler's, else the first one in the
order ``get``, ``put``, ``post``, ``delete``, ``options``, ``head``, ``patch``,
``trace``. Entities are compared by equality, their callables by identity:
give the handlers one ``Entity`` object (or equal ones with the same
callables) for them to share it.

``SCRUDFUL["ERROR"]`` is the API-wide error entity: the errors of a scrudful
view without an ``error`` of its own get it. When it is set, the 401, 403, 406
and 415 of a scrudful view (the errors DRF answers itself, for authentication,
permissions, content negotiation and parsing) get it whatever the handler's
``error``, as the HSR's explicit entries for them say. Without it, those
errors get the handler's ``error``, as in 0.9.

With an ``application/problem+json`` entity, a ``ValidationError``'s 400 (a
serializer's) is RFC 9457 problem details, DRF's error data as the ``errors``
member: ``problem_from_errors(400, errors, "Request validation failed")``
(:func:`scrudful.problem.problem_from_errors`). The w3id problem-details
context doesn't map ``errors``: publish a context that imports it
(``@import``) and maps ``errors``, and link that one. Every response of a
scrudful view exposes ``Link`` and ``Allow`` and carries the HSR link,
whatever the exception handler.

The HSR's ``default`` entries describe these errors only when the view's
exception handler is this one, which it knows by the handler's
``scrudful_error_links = True`` attribute. A project handler that delegates to
scrudful's must carry it too: set ``handler.scrudful_error_links = True``, or
use ``functools.wraps(exception_handler)``, which copies it:

.. code-block:: python

    import functools

    from scrudful.rest_framework import exception_handler


    @functools.wraps(exception_handler)
    def logging_exception_handler(exc, context):
        logger.info("error: %r", exc)
        return exception_handler(exc, context)

Without the marker, such a handler's errors still carry the error Links, but
the HSR doesn't describe them. Without the setting, or without an ``error``
entity and ``SCRUDFUL["ERROR"]``, such errors are DRF's own, with no Link but
the HSR link. The example
project (``examples/scrudful_rest_framework``) is conformant on the wire: its
tests run the wire-conformance suite's checks on its responses.

The responses served as the error entity's media type (scrudful's own 400,
412 and 428, and this handler's) bypass DRF's content negotiation: the first
of the view's ``renderer_classes`` with a JSON media type (``application/json``
or ``+json``) renders them, so a project's own JSON renderer (camel-case keys,
orjson ...) formats them as it formats the rest, and DRF's ``JSONRenderer``
does when the view has none.

**Django's 404 and 500.** A URL no pattern matches, and an exception DRF
doesn't handle, are answered by Django's ``handler404`` and ``handler500``:
:func:`~scrudful.rest_framework.handler404` and
:func:`~scrudful.rest_framework.handler500` (see `Setup`_) take an ``Entity``
of plain (non-callable) link values, of a JSON media type (checked when the
handler is made: ``TypeError``, ``ValueError``, ``ImproperlyConfigured`` for
a link URL that can't be parsed), and answer JSON (problem
details for an ``application/problem+json`` entity, else ``{"detail": <reason
phrase>}``) with its Links, resolved against ``SCRUDFUL["BASE_URL"]``, unless
``Accept`` prefers ``text/html``, and always send ``Vary: Accept``. Django uses
them with ``DEBUG`` off. They get no HSR link. A Link they can't build for the
settings, the request or the URLconf (an invalid ``SCRUDFUL["BASE_URL"]``, a
relative link without a base URL on a request whose host ``ALLOWED_HOSTS``
refuses, or a ``reverse_lazy`` link that doesn't reverse) is logged on the ``scrudful._django`` logger, with the request's path, and the
error served without the Links, so Django still logs the error it answers.

Callbacks
---------

Every callback gets ``(view, request, **url_kwargs)``: every URL kwarg, among
them DRF's ``format`` on the format-suffix routes a router adds
(``/items/1.json``). Accept ``**kwargs`` (``def item_etag(view, request, pk,
**kwargs)``), or ``/items/1.json`` answers 500. The link callables are also
called as ``func(view, None)`` when the HSR is built (see `The HTTP Schema
Resource`_): give the parameters only a response passes defaults, or accept
``**kwargs``.

For a schema or context URL, return a path from ``django.urls.reverse(name,
kwargs=...)``: scrudful makes it absolute against the base URL (else the
request). DRF's ``reverse(..., request=request)`` keeps the request's host and
``?format=`` in the URL, so the linked document would answer 404, and the
target differs between the HSR and the responses.

The browsable API and form bodies
---------------------------------

DRF's browsable API is allowed by Semantic HTTP 0.1 for a request whose
``Accept`` asks for ``text/html``: the HTML is undescribed content, which may
go to a client that accepts it. scrudful sends ``Vary: Accept`` on the
responses of a view with a renderer that isn't JSON, so that a shared cache
doesn't give that HTML to a JSON client. But DRF also serves it for
``?format=api`` whatever the ``Accept``, so a request without ``Accept`` gets
undescribed HTML, which the spec forbids. For production, drop
``BrowsableAPIRenderer`` from ``DEFAULT_RENDERER_CLASSES`` (or from the view's
``renderer_classes``), or keep it reachable only through ``Accept:
text/html``:

.. code-block:: python

    REST_FRAMEWORK = {
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
        "EXCEPTION_HANDLER": "scrudful.rest_framework.exception_handler",
    }

An ``Accept`` none of the view's renderers serves then gets DRF's 406, for
OPTIONS too. scrudful's own 400, 412 and 428 and the errors of
``exception_handler`` are JSON whatever the ``Accept``.

DRF's default parsers also accept ``application/x-www-form-urlencoded`` and
``multipart/form-data`` request bodies, which the HSR doesn't list: it
describes JSON bodies only. Semantic HTTP 0.1 lets a resource accept request
media types it doesn't list (a MAY), so they don't break conformance; no
lifting to RDF covers form data, though, so a client reading the HSR sends
JSON. The example project accepts JSON only (``JSONParser``); another body
then gets DRF's 415.

An explicit ``@scrudful`` on ``update`` also applies to PATCH when
``partial_update`` is DRF's stock ``UpdateModelMixin.partial_update``, which
calls ``update``: the HSR describes PATCH as that ``update``, and
``exception_handler`` gives a PATCH's errors its ``error`` Links. A
``partial_update`` of your own is described, and handled, only as itself.

On DRF's generic views (``GenericAPIView``, ``RetrieveUpdateAPIView`` ...) the
stock ``get``, ``put`` and ``patch`` delegate to ``retrieve``, ``update`` and
``partial_update``. A ``@scrudful`` on ``update`` there is evaluated on the
wire (a PUT without ``If-Match`` gets the 428), but the HSR looks at the HTTP
method handlers and describes ``put`` and ``patch`` as bare 200s. Decorate the
HTTP method handlers (``get``, ``put``, ``patch`` ...) of a generic view, or
use a viewset with ``scrudful_viewset``.

.. note::

   The schema Link's default relation is ``describedby``, which Semantic HTTP
   0.1 requires of a Schema Link Header. Overriding it
   (``schema_rel_or_func="profile"``) makes the response's Link no longer a
   Schema Link Header, while the HSR still describes that schema as the
   response's schema: the API is then not conformant. Use the default.

Set ``error=`` on every conditional decorator (or ``Meta.error`` /
``Meta.list_error``), or ``SCRUDFUL["ERROR"]``, which those without one fall
back to, so that scrudful's 400, 412 and 428 carry the error entity's Links
and are served as JSON with its ``media_type``, which must be JSON content
(``application/json`` or ``+json``, other than JSON-LD). Without either they
carry no error Links. ``exception_handler`` does not answer them: scrudful
returns them, it does not raise them, and they fall back to
``SCRUDFUL["ERROR"]`` with or without it.

CORS
----

With django-cors-headers, configure the middleware as "CORS" in :doc:`usage`
says: it replaces scrudful's ``Access-Control-Expose-Headers``, so give it
``EXPOSE_HEADERS``; keep ``OPTIONS`` in ``CORS_ALLOW_METHODS`` (its default
has it); allow credentials, since ``Allow`` depends on them; and have
``CORS_URLS_REGEX`` (if you set one) match the ``/_hsr/`` route, which sets no
CORS headers of its own and answers OPTIONS, a preflight too, with a 405:

.. code-block:: python

    from scrudful.utils import EXPOSE_HEADERS

    CORS_EXPOSE_HEADERS = list(EXPOSE_HEADERS)
    CORS_ALLOW_CREDENTIALS = True

django-cors-headers answers preflights in its middleware; one that reaches a
scrudful view gets the 204 without Links, before any authentication,
permission check or throttle.

.. automodule:: scrudful.rest_framework
   :members:
   :member-order: bysource
   :exclude-members: Entity
