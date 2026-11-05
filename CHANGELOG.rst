=========
Changelog
=========

0.10.0 (since 0.9.0)
====================

Semantic HTTP 0.1 at spec commit ``37ec1ce`` (breaking). scrudful 0.5–0.9
followed the spec at commit ``bcc338e``; its 0.1 merge, ``37ec1ce``, replaced
the OPTIONS model: OPTIONS now has no content and links to an HTTP Schema
Resource (HSR) served by GET, the HSR must be the same for every requester,
``Allow`` lists the methods available to *this* requester, and every response
should link the HSR. All three adapters now follow it. Clients and tests that
read the OPTIONS body must GET the HSR instead; the README's "Migrating from
0.9" lists the steps (django-scoped-rbac and scrud-django pin
``scrudful>=0.8,<0.9``, so this release doesn't reach them until they move the
pin to ``>=0.10,<0.11``).

Behaviour change (all adapters)
-------------------------------

* OPTIONS (other than a CORS preflight) answers **204 without content or
  Content-Type** (was a 200 ``application/json`` body, the HSR, with the HSR's
  own schema and context Links), with ``Allow``, exactly one HSR link
  (``rel="https://w3id.org/semantic-http/ns#http-schema-resource-document"``,
  ``type="application/json"``, no ``anchor``) and the expose headers.
* ``Allow`` lists exactly the methods **this requester** may use (was the
  HSR's methods, the same for everyone): each method is checked on a copy of
  the request with that method, by a hook (ninja ``allowed=``, DRF
  ``allowed_func``, FastAPI ``add_options(allowed=)``), else by the framework's
  declared authorization (ninja ``auth=`` and ``before=``; DRF's permission
  classes; FastAPI's security dependencies). OPTIONS itself is not gated: an
  anonymous requester of a locked resource gets a 204 with ``Allow:
  OPTIONS``. No throttle or handler runs per method. ``HEAD`` is listed only
  where a route serves it.
* On an item URL, OPTIONS loads the object with the view's own loader (ninja
  ``load``, DRF ``get_object()``, FastAPI ``add_options(load=)``), only once a
  check allows the requester, so a requester refused everything can't tell
  which objects exist: for DRF and FastAPI, once a method passes its
  object-free checks (every method does with a hook); for ninja, once the
  loading operation's own ``auth=`` and ``before`` allow the requester, hook
  or not (else the hook gets ``obj=None``). A 404 the loader raises is the OPTIONS's
  answer, with the error Links and the HSR link; ``None`` is no object.
* A denial an ``Allow`` hook raises refuses that method, in every adapter:
  DRF's ``PermissionDenied`` or ``NotAuthenticated``, any other 401 or 403
  ``APIException`` (``AuthenticationFailed``, a project's own), or Django's
  ``PermissionDenied`` from ``allowed_func`` (or a permission check); ninja's ``HttpError`` 401 or
  403 (``AuthenticationError``, ``AuthorizationError``) or ``PermissionDenied``
  from ``allowed``; FastAPI's ``HTTPException`` 401 or 403 from
  ``add_options(allowed=)`` (ninja and FastAPI failed the whole OPTIONS). A
  404 a hook raises is the OPTIONS's answer; anything else propagates.
* The HSR route and views are public under Django's
  ``LoginRequiredMiddleware`` (Django 5.1+): DRF's ``hsr_urls()`` view and
  ``http_schema_resource_view``, ninja's HSR routes and
  ``http_schema_resource_view``, and the ``handler404`` / ``handler500`` views
  are ``login_required = False`` (they redirected to the login page).
* A CORS preflight (an OPTIONS with ``Origin`` and
  ``Access-Control-Request-Method``) that reaches scrudful is answered 204
  without content or Links, before any authentication, throttle, hook,
  permission check or load (was answered as any OPTIONS: a ninja throttle's
  429, a DRF permission's 401 or 403).
* The HSR is served by **GET** (and HEAD), on a route scrudful adds
  (``/_hsr/<name>``; for DRF, ``include(hsr_urls())`` in the root URLconf):
  ``application/json``, compact JSON, the HSR's schema and context Links, a
  strong ``ETag`` (304 for ``If-None-Match``), ``Cache-Control: public,
  max-age=300`` (configurable), no authentication, permission check, throttle
  or CSRF check; another method is a 405 (``Allow: GET, HEAD``). The ``_hsr``
  path segment is reserved.
* The HSR is built once per path template **without a request**: the same
  bytes for every requester and ``Host``. Relative link URLs in it resolve
  against the configured base URL (``SCRUDFUL["BASE_URL"]``, ``base_url=``);
  without one they are a configuration error when it is built (were resolved
  against the request).
* Link callables are called in **describe mode** to build the HSR (one that
  raises then is a configuration error naming it): ninja ``func(None, None)``
  (was ``(request, None)``), DRF ``func(view, None)`` with a request-less view
  built from the route's ``cls`` and ``initkwargs`` (was ``func(view,
  request, **url_kwargs)``), FastAPI ``func()`` (was the URL's path
  parameters and the request). On responses they are called as before, and
  must return the same target.
* The HSR's ``options`` entry is ``{"responses": {"204": {"description": "No
  Content"}}}`` (was a 200 with the HSR as content).
* Every method but OPTIONS has a ``default`` response entry, with the content
  of the error entity its raised errors get (the operation's ``error``, else
  the API-wide one: ninja and FastAPI ``add_error_links``, DRF
  ``SCRUDFUL["ERROR"]``); without an error helper it has no content. Where the
  API-wide entity differs from the operation's, the errors the framework
  answers before the operation (ninja 401, 403, 422 and 429; DRF 401, 403, 406
  and 415) have explicit entries with its content.
* Every response of a described path carries the **HSR link**: the 2xx, 304
  and HEAD responses, scrudful's early 400, 412 and 428, a 204 or an error the
  view returns, and the errors the framework answers through scrudful's error
  helpers; after the other Links. A Link the view sets with that relation
  replaces it. A preflight, and an error for a URL no route serves, get none.
* A link URL with a scheme but no authority (``urn:``, ``tag:``) is absolute
  in the HSR, as RFC 3986 has it: it needs no base URL.
* The Link headers resolve relative URLs against the base URL when one is set
  (were always resolved against the request), so they agree with the HSR on
  every ``Host``.
* ``Access-Control-Expose-Headers`` always lists ``Link`` and ``Allow`` (was
  ``ETag``, ``Link`` and ``Location``, each only when present); ``ETag`` and
  ``Location`` still only when present.
* A schema link without an RDF context link, for a non-in-band media type, is
  refused (its Media Type Object would fail the spec's JSON Schema): an
  ``Entity`` with a plain schema link raises ``scrudful.entity.HsrConfigError``,
  a ``ValueError``, when created, and the ninja decorators when given one, as
  do the DRF decorators (``scrudful``, ``scrudful_api_view``,
  ``ScrudfulViewFunc``, and ``scrudful_viewset`` for ``Meta``'s item and
  ``list_`` links, whatever methods the class has); a callable one raises the
  adapter's configuration error when the HSR is built (was accepted).
  ``ScrudfulPagination`` names the view's ``Meta`` in an
  ``ImproperlyConfigured`` for an undecorated view's.
* Problem details: with an ``application/problem+json`` error entity, the
  framework's validation errors are rewritten as RFC 9457 problem details,
  ``problem_from_errors(status, errors, "Request validation failed")``, its
  error list as the ``errors`` member (ninja's and FastAPI's 422 were served
  as ``application/problem+json`` with their ``{"detail": [...]}`` body; DRF's
  serializer 400 kept its body).
* The HSR's configuration errors (a relative URL without a base URL, a link
  callable raising in describe mode, a schema without a context) carry one
  message, raised as ``ninja.errors.ConfigError``, Django's
  ``ImproperlyConfigured`` (DRF) and ``ValueError`` (FastAPI).
* ``SCRUDFUL`` (Django) must be a dict of the keys ``BASE_URL``,
  ``HSR_ROUTE``, ``HSR_URL``, ``HSR_CACHE_CONTROL`` and ``ERROR``: another
  value, or an unknown key, raises ``ImproperlyConfigured``. A base URL that
  isn't absolute is a configuration error (``ConfigError`` for ninja,
  ``ImproperlyConfigured`` for DRF and the 404 and 500 handlers,
  ``ValueError`` for FastAPI). A ``base_url`` that isn't a ``str`` is refused
  the same way (ninja and FastAPI raised ``TypeError``).
* A ``hsr_url`` callable must return an absolute URL (a non-empty ``str`` with
  a scheme): ``None``, an empty string or a relative URL is a configuration
  error, raised when the link is built (it was sent as a broken link).
  ``hsr_cache_control`` (and ``SCRUDFUL["HSR_CACHE_CONTROL"]``) must not be
  empty (an empty ``Cache-Control`` field was sent). A base URL that doesn't
  make a relative link absolute is refused too.
* ``scrudful.entity.HsrConfigError`` is the public name of the error the
  shared HSR code raises.

Behaviour change (ninja)
------------------------

* ``ScrudfulRouter``'s OPTIONS runs the operations' checks: the loading
  operation's ``auth=`` and ``before`` (then ``load``), and each method's
  ``auth=`` and ``before`` (or ``allowed``). Bad credentials make the
  requester anonymous (ninja's auth callbacks return ``None``), so they get
  the anonymous ``Allow``. A raised ``HttpError`` 401, 403 or 404,
  ``Http404`` or ``PermissionDenied`` refuses a method; anything else raised
  is answered by the API's exception handler.
* OPTIONS is answered by an ``Operation.run`` decorator, so the API's and
  routers' ``add_decorator(mode="operation")`` decorators no longer gate it
  (they did in 0.9); ``mode="view"`` ones still run first. A non-preflight
  OPTIONS is throttled once with the API's or the mount's throttles.
* ``ScrudfulRouter`` serves each path's HSR at ``/_hsr/<name>`` under its
  prefix (``"/{pk}"`` → ``_hsr/pk``, ``""`` → ``_hsr/_root``), before its other
  paths; the API's and routers' ``add_decorator`` decorators wrap that route
  too. An operation under ``/_hsr/``, or two paths with one name, is a
  ``ConfigError``.
* Every operation on a ``ScrudfulRouter`` needs an entity: adding a plain
  operation without ``@described(Entity(...))`` (or ``scrudful_entity=``)
  raises ``ConfigError`` (it was described without content). One routing
  only OPTIONS needs none.
* A method no operation of a ``ScrudfulRouter`` path routes is answered with a
  **JSON 405**, the API's exception handler's answer to ``HttpError(405,
  "Method not allowed")`` (``add_error_links``'s Links and media type), with
  ``Allow`` (every method the path serves) and the HSR link (was ninja's
  ``text/html`` 405). It relies on ninja 1.7's ``PathView`` internals
  (``ConfigError`` when the installed ninja's differ).
* The HSR is compact JSON written by scrudful, whatever the API's renderer
  (0.7–0.9 had the OPTIONS body written by the renderer); OPTIONS has no body.
* With ``add_error_links``, every 401 and 403 answered for a scrudful
  operation whose ``error`` differs from the API-wide entity carries the
  **API-wide entity** (was the operation's ``error`` for those the operation,
  ``before=`` or ``after_throttles=`` raised): those of ninja's authentication
  (a failed CSRF check, an ``AuthorizationError``) always did, so each 401 and
  403 agrees with the HSR, which lists a 403 entry beside the 401, 422 and 429
  (unless ``responses=`` declares the status). Without an API-wide entity,
  nothing changes.
* A path whose HSR link can't be computed (the route off without
  ``hsr_url``, a route that can't be reversed) raises ``ConfigError`` on
  every response of the path.
* ``add_error_links``'s entity is also the HSR's API-wide error entity, and
  calling it discards the built HSRs; its link callables are called as
  ``(None, None)`` there.

Behaviour change (DRF)
----------------------

* **The root URLconf must include** ``hsr_urls()`` (``path("",
  include(scrudful.rest_framework.hsr_urls()))``): every routed request of a
  scrudful view, not only an OPTIONS, raises ``ImproperlyConfigured`` without
  it (or ``SCRUDFUL["HSR_URL"]``, or ``Meta.hsr_url``). So does a routed
  request of a scrudful view whose route has no URL name; two scrudful views
  sharing a URL name make a GET of that name's ``/_hsr/`` raise it. Each
  breaks only its own requests: the HSRs of the other names are served. A
  view called without URL resolution (``APIRequestFactory``) gets no HSR link.
* ``manage.py check`` reports those misconfigurations before any request, as
  errors of Django's URL system check: ``scrudful.E001`` (an unnamed route of
  a scrudful view), ``E002`` (one URL name for two scrudful views), ``E003``
  (no ``hsr_urls()`` include where a view needs it), ``E004`` (a route turned
  off without ``hsr_url``), ``E005`` (an HSR that can't be built: a relative
  link without a base URL, a schema without a context, a non-JSON context
  without a type, or whatever else its build raises, so that one view never
  stops the check) and ``E006`` (an invalid ``SCRUDFUL`` setting: one
  error for each invalid key, ``HSR_CACHE_CONTROL`` and ``ERROR`` included,
  reported beside the routes' errors), ``E007`` (an action of a
  ``scrudful_viewset`` class without ``@scrudful``); and, as the warning
  ``scrudful.W001``,
  a link callable raising when called as ``func(view, None)``, so that
  ``migrate`` runs on a fresh database whose tables a callable reads. The
  ``hsr_urls()`` pattern runs the check in Django's URL check; ``E003`` needs
  ``scrudful.rest_framework`` imported before the checks start, which
  registers the check (an ``AppConfig.ready()``).
* OPTIONS is no longer gated by the view's permissions (DRF's ``initial()``
  checked them for OPTIONS too); authentication and throttles still run. Its
  ``Allow`` runs each method's ``check_permissions`` and, with the object,
  ``check_object_permissions``; an ``Http404`` an object check raises
  (``DjangoObjectPermissions`` for an object the requester may not read) is
  the OPTIONS's 404. A method with an ``allowed_func`` passes the first pass
  for every requester, so on such a view the object is loaded for anyone.
* An action of a ``scrudful_viewset`` class without ``@scrudful`` (an
  ``@action``, or a method its ``mapping`` adds) gets no HSR link on its
  responses, which no HSR describes, and a route of such actions alone has no
  HSR route and no HSR link on its OPTIONS (they were linked to an HSR
  describing them without content). ``scrudful.E007`` reports them.
* OPTIONS loads the object as the first method that passed its view-level
  checks loads it (GET, when it did): a request of that method and, on a
  viewset, its action (``retrieve``), not OPTIONS's ``metadata``, so a
  ``get_queryset`` scoped by ``self.action`` scopes it as a GET does. A
  ``get_object`` of the view's own is called whatever the URL kwarg is named
  (on a viewset's item action, or an ``APIView`` route with a URL kwarg), so
  its object checks run; only the lookup kwarg did.
* The view class's ``finalize_response`` adds the HSR link to every response
  of a routed scrudful view, OPTIONS's 401, 406 and 429 included; a view with
  a renderer that isn't JSON (the browsable API) also sends ``Vary: Accept``.
  The link is resolved in ``initial()``, before the handler runs: a link that
  can't be resolved fails the request before the handler writes anything.
* ``exception_handler``: with ``SCRUDFUL["ERROR"]`` set, a scrudful view's
  401, 403, 406 and 415 get that entity whatever the handler's ``error``, and
  the other errors of a view without an ``error`` get it too (unchanged from
  0.9 when it is unset); every error of a scrudful view exposes ``Link`` and
  ``Allow``; with a problem-details entity, a ``ValidationError``'s 400 is
  problem details.
* The HSR's success statuses come from ``statuses=`` / ``Meta.statuses``
  (2xx only, ``ValueError`` otherwise), else DRF's defaults (201 POST, 204
  DELETE, 200), and its content's media type from ``entity=`` / ``Meta.entity``
  (was always ``application/json``).
* A link callable that needs a URL kwarg or the request raises
  ``ImproperlyConfigured`` when the HSR is built, since it is called as
  ``func(view, None)``; accept ``**kwargs`` and return a ``reverse()`` path.
* ``ScrudfulMetadata.determine_metadata`` returns the HSR built without the
  request; a view that only sets ``metadata_class = ScrudfulMetadata``,
  keeping DRF's ``options``, answers OPTIONS with it as DRF's 200 body. A
  ``ScrudfulMetadata`` subclass may not define ``determine_metadata``,
  ``get_method`` (which scrudful never calls) or a 0.9 hook (see "Removed"):
  ``TypeError`` when the class is created.
* The built HSRs are discarded when ``SCRUDFUL``, ``REST_FRAMEWORK`` or
  ``ROOT_URLCONF`` change (``override_settings``).

Behaviour change (FastAPI)
--------------------------

* ``add_options`` takes keyword-only ``base_url``, ``hsr_route``, ``hsr_url``,
  ``hsr_cache_control``, ``allowed`` and ``load``; calling it again replaces
  them all. It serves the HSRs from one ``/_hsr/{name}`` route put before every
  other route (an ``APIRouter`` gets one route per resource), named after the
  template with each convertor but ``str`` joined by ``-`` (``users.id``,
  ``users.id-int``).
* ``add_options`` raises **ValueError** (was ``TypeError``) for a link callable
  of a scrudful endpoint, or of its ``error``, that can't be called as
  ``func()``, checked up front for all of them; and for a base URL that isn't
  absolute, ``hsr_route=False`` without ``hsr_url``, a path under ``/_hsr/``
  and two resources with one route name.
* Resources are keyed by template shape: parameter names are ignored and
  convertors kept, so ``/items/{id}`` and ``/items/{item_id}``, even on
  different routers, are one resource with one HSR and one ``Allow`` (were
  separate path strings). Literal and convertor overlaps (``/items/new``
  beside ``/items/{id}``, ``{id:int}`` beside ``{name}``) stay separate.
* ``Allow`` runs only the routes' security dependencies (``Security(...)``,
  and a ``Depends`` with a security scheme below it), resolved without the
  body; a plain dependency, a throttle in ``dependencies=`` say, is not run.
  ``load(request, **path_params)`` is called only for a URL with path
  parameters, with all of them.
* The HSR describes the route's ``status_code=`` and the statuses of its
  ``responses=`` (was the status code only), the endpoint's ``entity=`` media
  type (was always ``application/json``); a declared success media type
  other than the entity's is not described.
* ``add_error_links`` wraps the app's ``500`` / ``Exception`` handler, or
  installs a **JSON 500** (``{"detail": "Internal Server Error"}``, problem
  details with a problem-details entity) with the Links, in place of
  Starlette's ``text/plain`` one (only with ``debug`` off; the exception is
  raised again once the response is sent). Its handlers' responses on a
  described path also get the HSR link.
* scrudful's early 400, 412 and 428 keep the Links a dependency set on the
  injected ``Response``, merged by relation, except those with the
  ``describedby`` or an RDF context relation (0.9 dropped them all).
* The callables ``add_options`` calls outside the endpoint are called as
  ``func()`` (were given the URL's path parameters and the request);
  ``add_error_links``' calls are unchanged.
* A router's resource added after its ``add_options`` call has no OPTIONS or
  HSR route, and its responses no HSR link, until it is called again.

New
---

* ``scrudful.utils.EXPOSE_HEADERS = ("ETag", "Link", "Location", "Allow")``,
  to give a CORS middleware that sets its own expose list.
* ``scrudful.problem.problem_from_errors(status, errors, detail=None)``.
* The ``SCRUDFUL`` Django setting: ``BASE_URL``, ``HSR_ROUTE`` (default
  ``True``), ``HSR_URL``, ``HSR_CACHE_CONTROL`` (default ``public,
  max-age=300``) and, for DRF, ``ERROR``.
* ``handler404(error)`` and ``handler500(error)``, in ``scrudful.ninja`` and
  ``scrudful.rest_framework``: Django error views answering JSON (problem
  details for a problem-details entity, else ``{"detail": ...}``) with the
  entity's Links unless ``Accept`` prefers ``text/html``, with ``Vary:
  Accept``.
* ninja: ``ScrudfulRouter(base_url=, hsr_route=, hsr_url=, hsr_cache_control=,
  allowed=)``; ``allowed=`` on ``scrudful``, ``scrudful_create`` and
  ``scrudful_list``; ``described(entity)`` and ``add_api_operation(...,
  scrudful_entity=)``; ``http_schema_resource(router, path)`` and
  ``http_schema_resource_view(router, path)``.
* DRF: ``hsr_urls()``, ``http_schema_resource(view_func)`` and
  ``http_schema_resource_view(view_func)``; ``entity=``, ``statuses=``,
  ``allowed_func=`` and ``base_url=`` on ``scrudful``, ``scrudful_api_view``
  and ``ScrudfulViewFunc``; ``Meta.entity``, ``list_entity``, ``statuses``,
  ``allowed_func``, ``base_url``, ``hsr_route``, ``hsr_url`` and
  ``hsr_cache_control`` on ``scrudful_viewset``.
* FastAPI: ``scrudful(entity=)``; ``http_schema_resource(app_or_router,
  path)`` and ``http_schema_resource_endpoint(app_or_router, path)``.

Removed
-------

* The OPTIONS body, and the HSR's schema and context Links on OPTIONS.
* ``scrudful.utils.EXPOSABLE_HEADERS``: use ``EXPOSE_HEADERS``, which adds
  ``Allow``.
* DRF: ``ScrudfulMetadata.determine_metadata_for_<method>``, ``_write`` and
  ``required_conditional_headers``. They took the request, and the HSR is
  built without one: a subclass that defines one, or that defines
  ``determine_metadata`` (whose output is now served nowhere), raises
  ``TypeError`` when created. Shape the HSR with ``statuses=``, ``entity=``
  and ``Meta``.
* Private internals of the ``bcc338e`` model: ``scrudful._adapter``'s
  ``options_links()``, ``describe_options()`` and ``allow_header(body)``
  (``scrudful._hsr`` has ``hsr_response_links()``, ``options_operation()`` and
  ``allow_value(methods)``).

Examples and tests
------------------

* The examples set their base URL; the Django ones install ``handler404`` and
  ``handler500``, the DRF one includes ``hsr_urls()``, the FastAPI one passes
  ``add_options(app, base_url=..., load=...)``. Each example's tests run the
  wire-conformance suite's checks (``tests/test_wire_<adapter>.py``, new, one
  per adapter), and the spec fixtures are re-vendored from ``37ec1ce`` with
  the problem-details schema and context.
* The suite's Django settings module is now ``tests.settings`` (the DRF
  example's settings without ``SCRUDFUL``), no longer
  ``scrudful_rest_framework.settings``.

0.9.0 (since 0.8.0)
===================

A create now evaluates its preconditions against the collection it creates
in (#9): RFC 9110 §13.1 says an origin server MUST evaluate If-Match,
If-None-Match and If-Unmodified-Since against the target resource, and a
POST to a collection targets the collection.

Behaviour change (DRF)
----------------------

* DRF: ``scrudful_viewset``'s ``create`` (with or without
  ``object_validators``) evaluates the request's preconditions against the
  list validators, ``Meta.list_etag_func`` and ``Meta.list_last_modified_func``,
  called as for ``list`` (``func(view, request, **url_kwargs)``), before the
  view runs, for a POST carrying If-Match, If-None-Match or
  If-Unmodified-Since (not otherwise). A viewset with either one now answers
  **412** to a create with
  an If-Match the list doesn't have (strong comparison; ``*`` passes), a
  past If-Unmodified-Since (without If-Match) or ``If-None-Match: *`` (the
  collection always has a representation; a weak comparison otherwise), and
  **400** to a malformed If-Match or If-None-Match, where 0.8.0 answered 201;
  nothing is created. If-Modified-Since is ignored, and a create without a
  precondition proceeds as before, without calling the list validators:
  never a 428. A viewset without list validators is unchanged. The 400 and
  412 carry the create's ``error`` entity (the item ``Meta.error``, as its
  serializer 400 does) and the body of the paired ``Meta.error_body_func``,
  which ``create`` now gets (it had no ``error_body_func``). OPTIONS
  describes the create's 400 and 412 (with the ``error`` content), still
  without precondition parameters.
* Unlike for ``list``, list validators returning ``None`` don't make the
  collection missing (``If-None-Match: *`` is still a 412): raise ``Http404``
  in the callback to answer a POST to a missing collection with 404.
* DRF: an explicit ``@scrudful`` on the ``create`` of a ``scrudful_viewset``
  whose ``Meta`` sets ``list_etag_func`` or ``list_last_modified_func`` now
  raises ``TypeError`` unless it is given list validators of its own: the
  explicit decorator replaces the ``Meta`` configuration, so its
  preconditions would never be evaluated.
* The check and the create are atomic only if the application serializes
  every write that changes the collection's validators with conditional
  creates: lock a row per collection (``select_for_update()``) in a list
  validator callback, for a POST only (the ``list`` action calls the same
  callbacks), with a transaction spanning the request (``ATOMIC_REQUESTS``)
  so the lock is held until the create commits, and take the same lock in
  the other writes (unconditional creates and item PUT, PATCH and DELETE).

New
---

* DRF: ``ScrudfulViewFunc`` and ``@scrudful`` take ``list_etag_func`` and
  ``list_last_modified_func`` (optional): the collection's validators,
  against which a POST's preconditions are evaluated as above, for an
  explicit create (``creates=True``), or a POST handler without validators
  of its own. With ``etag_func`` or ``last_modified_func`` they need
  ``creates=True``, and on a class-body handler named after a standard
  non-POST handler (``get``, ``put``, ``list``, ``retrieve`` ...) they raise
  ``TypeError`` when the class is created; other handlers (a function view's
  other methods, a GET ``@action``) ignore them.
* ninja: ``scrudful_create`` takes ``list_load``, ``list_etag_func``,
  ``list_last_modified_func``, ``error`` and ``error_body_func`` (optional).
  ``list_load(request, **params)`` returns the collection (what the list
  operation's handler returns for the same request; ``params`` as for
  ``load``), and ``list_etag_func(collection)`` /
  ``list_last_modified_func(collection)`` (the list operation's) its
  validators. A POST's preconditions are evaluated against them before the
  handler, with the DRF rules above (412, 400, never a 428; ``list_load``
  runs only when the POST carries one), the 400 and 412 carrying ``error``'s
  Links (callables get the collection) and ``error_body_func``'s body, and
  OPTIONS lists them. ``list_load`` returning ``None`` means no collection:
  no precondition can fail and the handler answers, as with ``load``
  (a malformed header is still a 400). A list validator without
  ``list_load``, or ``list_load`` without one, is a ``TypeError``, and list
  validators on an operation without POST a ``ConfigError`` when it is
  built. A ``scrudful_create`` without them is unchanged. Serialize the
  writes as for DRF, in a transaction around the operation
  (``ATOMIC_REQUESTS``, ``router.add_decorator(transaction.atomic,
  mode="operation")`` on a router of sync operations, or a per-operation
  wrapper of ``view_func`` for a sync create on a path with an async
  operation; see the ``scrudful.ninja`` documentation); in an async
  operation ``list_load`` and the handler can't share a transaction:
  re-check in the handler.

0.8.0 (since 0.7.0)
===================

Additive and backwards compatible: an operation without ``after_throttles=``
(and an item or create operation without ``before=``) behaves as in 0.7.0,
but for two messages. The ``RuntimeError`` raised when ``before`` can't get
the path parameters now names the hook as ``before=``. The ``ConfigError``
of any scrudful operation whose ``view_func`` something replaced now lists
``before=`` among the places for per-operation authorization ("in before=,
load or auth=", was "in load or auth=").

New
---

* ninja: ``scrudful_list(after_throttles=...)`` (optional):
  ``after_throttles(request, **path_params)`` is called once per request
  where DRF's ``list()`` starts: after ninja's authentication, after
  ``before`` (if given) and after ninja's throttles, and before ninja
  validates any parameter. So DRF's whole order can be reproduced
  (authentication, ``before``, throttles, ``after_throttles``, then the
  parameters): a throttled request (429) never calls it, so a client over
  its quota can't force an expensive check (e.g. a per-row authorization
  filter that may answer 400) on every request, and its error beats the 422
  of a bad ``page``, as DRF's ``list()`` filters before it paginates. A
  request it refuses has used its throttle quota, as in DRF. Its contract is
  ``before``'s: the URL's path parameters as they are (each hook gets its own
  copy), ``None`` to let the request go on (anything else, or an awaitable in
  a sync operation, whose coroutine is closed or future cancelled, raises
  ``TypeError``), an error raised to answer first (answered by the API's
  exception handler, with the operation's error Links), neither that error
  nor the 422 that follows reaching the ``add_decorator(mode="operation")``
  decorators, ``async def`` in an ``async def`` operation, and the same
  guards: ``ConfigError`` when the operation is built (an installed ninja's
  ``_run_checks`` or ``run`` differing from 1.7.0's and 1.7.1's, an
  ``Operation`` subclass overriding either, or a ``decorate_view`` below
  ``@scrudful_list`` without ``functools.wraps``), and ``RuntimeError`` for a
  request that reaches the function without the hooks having allowed it.
  Use it alone or with ``before``. When it returns, one 422 lists every bad
  parameter, and the OpenAPI description is unchanged. The ``ConfigError``
  and ``RuntimeError`` messages name the hooks the operation has
  (``before=``, ``after_throttles=`` or both).
* ninja: item and create operations take the same hooks:
  ``scrudful(before=..., after_throttles=...)`` and
  ``scrudful_create(before=..., after_throttles=...)`` (optional), with
  ``scrudful_list``'s contract, machinery and guards (the ``ConfigError``
  of a ``decorate_view`` hiding the operation names ``@scrudful`` or
  ``@scrudful_create``). The order is DRF's for these operations too:
  authentication, ``before`` (where DRF's ``has_permission`` runs, before
  its throttles), throttles, ``after_throttles``, then the parameters and
  body, ``load`` and the preconditions. So a client ``before`` refuses gets
  its 401 or 403 even when throttled, and a client either hook refuses never
  gets a 422 for a bad body, or a 304, 412 or 428 (nor a validator). A hook
  gets the raw request and may read the body as the operation's parameters
  do: ``request.body`` for a JSON body, ``request.POST`` and
  ``request.FILES`` for a form or an upload, never ``request.body`` of a
  multipart body (ninja's CSRF check or ``fix_request_files_middleware`` may
  have parsed it already, and it would hold the uploaded files in memory);
  the body is still validated after it. The OpenAPI description is
  unchanged. As for a list with ``before=``, the decorated function of an
  operation with a hook can't be called directly (e.g. in a unit test): it
  raises ``RuntimeError``, since the hooks didn't run. Test the operation
  through ninja's ``TestClient``.

0.7.0 (since 0.6.0)
===================

Behaviour change (DRF)
----------------------

* DRF: ``ScrudfulPagination`` parses ``page`` and ``page_size`` as ninja's
  ``scrudful_list`` does, with pydantic v2's lax ``int`` from a string, not
  Python's ``int()``, so both front ends accept the same values. ``?page=2.0``
  (and ``2.00``) is now page 2, where it was a 400; ``?page=٣`` (and other
  non-ASCII digits, e.g. ``３``) is now a 400 ("A valid integer is
  required."), where it was page 3. Only Unicode ``White_Space`` is stripped
  around the value (not U+001C..U+001F, which ``int()`` strips). pydantic is
  not a dependency of the DRF adapter: the rules are implemented in
  ``scrudful._adapter.parse_query_int`` and tested against pydantic. A value
  with more digits than the interpreter allows ``int()``
  (``sys.set_int_max_str_digits``) is a 400, as in 0.6.0. A
  subclass overriding ``_integer`` (or ``_parse_parameter``) decides for
  itself.

Behaviour change (ninja)
------------------------

* ninja: the automatic OPTIONS body and scrudful's own 400, 412 and 428 bodies
  are now written by the serving API's renderer (``NinjaAPI(renderer=...)``),
  as its other responses are, not by Django's ``JsonResponse``. With ninja's
  default ``JSONRenderer`` the bytes are unchanged; an API with another JSON
  renderer (compact separators, ORJSON, ``ensure_ascii=False``, JSON-LD ...)
  gets other bytes on those responses than with 0.6.0. A renderer that
  changes the data's shape (renames keys, e.g. to camelCase, or wraps each
  body) now changes these bodies too, as it does ninja's own 422s: the
  OPTIONS HTTP Schema Resource then no longer has the spec's shape, so use
  one that changes only how the data is written. Their ``Content-Type`` keeps its
  media type (``application/json``, or the ``error`` entity's) and now
  carries the renderer's ``charset`` parameter, as ninja's own responses do
  (``application/json; charset=utf-8`` by default). The body is first reduced
  to JSON values as ninja's ``NinjaJSONEncoder`` (a ``DjangoJSONEncoder``)
  writes them, so a datetime, ``Decimal``, ``UUID`` or lazy string from an
  ``error_body_func`` is still written as in 0.6.0 by a renderer of plain
  JSON (ORJSON, ``json.dumps``). Links, ``Allow`` and expose headers are
  unchanged, a 304 still has no body, and a renderer whose ``media_type`` is
  not JSON content keeps the ``JsonResponse`` bytes and ``Content-Type``.
  Written by the renderer, these responses are ``HttpResponse`` objects, no
  longer ``JsonResponse``. A ``JSONRenderer`` with a ``DjangoJSONEncoder``
  subclass writes Django's values (datetime, ``Decimal`` ...) its own way in
  them too; another renderer gets them reduced as ``NinjaJSONEncoder``
  writes them.
* ninja: an error the API's exception handler answers for a scrudful
  operation (or under ``add_error_links``), served as the ``error`` entity's
  media type, now also carries the charset of its bytes
  (``application/problem+json; charset=utf-8``), where 0.6.0 dropped it; a
  charset the entity's ``media_type`` names is replaced by the bytes' one,
  never repeated.

New
---

* ninja: ``scrudful_list(before=...)`` (optional):
  ``before(request, **path_params)`` is called once per request where DRF
  checks permissions: after ninja's authentication, before its throttles and
  before it validates any parameter, with the URL's path parameters as they
  are (strings, not the values the function gets: ``"07"`` is 7 to an
  ``int`` parameter, so compare the function's value or parse as its type
  does, never deny by testing a raw string). It returns ``None`` to let the
  request go on: anything else it returns (``False``, a response, or an
  awaitable in a sync operation, whose coroutine is closed or future
  cancelled, never awaited) raises ``TypeError``, as a refusal is raised,
  never returned. So a list can answer 401 or 403 before the 422 of a bad
  parameter, and a refused request uses none of a throttle's quota. When it
  returns, ninja throttles and validates every parameter as without it: one
  422 lists every bad one, and the OpenAPI description is unchanged. Neither
  its error nor that 422 reaches the decorators added with
  ``add_decorator(mode="operation")``. With ``before``, scrudful runs its own
  copy of ninja's per-operation checks (CSRF flag, authentication,
  throttles) with ``before`` between the last two, relying on
  ``Operation.run`` to answer their response before it reads any parameter.
  Both are verified against ninja 1.7.0 and 1.7.1. The ``ninja`` extra still
  allows any ninja from 1.7 below 2, so a later release scrudful hasn't
  verified may be installed: an app using ``before=`` should lock its ninja
  version. When the operation is built (at startup), ``before=`` raises
  ``ConfigError`` if the installed ninja's ``_run_checks`` or ``run`` differs
  from those releases' (upgrade scrudful, or keep ninja at a verified
  release), if an ``Operation`` subclass overrides either, or if a
  ``decorate_view`` below ``@scrudful_list`` hides the operation (no
  ``functools.wraps``). They are compared by a digest of their source,
  comments, docstrings and blank lines aside; where the source isn't
  installed (``.pyc`` only), only the names, local variables and arguments
  they use are compared. A request that reaches the function without
  ``before`` having allowed it (ninja served a copy of the operation without
  scrudful's checks) fails with ``RuntimeError`` instead of going on.
  ``request.auth`` exists only when an auth callback succeeded: where
  ``before`` may run without one, fail closed (``getattr(request, "auth",
  None)`` and a 401 when it is ``None``). Without ``before`` nothing changes.

Fixes
-----

* DRF: a create calling ``self.perform_create(serializer, ...)`` with extra
  arguments (``owner=request.user``, as DRF documents) no longer raises
  ``TypeError``: the wrapper ``scrudful_viewset`` puts on ``perform_create``
  passes them through and returns its result, and the 201 still gets the
  created object's ``Location`` and validators.
* DRF: ``ScrudfulPagination``'s bad ``page`` and ``page_size`` errors carry
  the codes of a DRF ``IntegerField``'s (``invalid``, ``min_value``,
  ``max_value``), with its messages (translated, as DRF's are), and one 400
  names both parameters
  when both are bad, as ninja's ``scrudful_list`` 422 already did. Each
  parameter still goes through ``_integer``, so a subclass calling or
  overriding it (to clamp ``page_size``, or accept ``page=last``) keeps its
  behaviour, and with an overridden ``_integer`` the 400 names the first bad
  parameter only, as in 0.6.0 (the override is not called for ``page_size``
  once ``page`` failed). ``_parse_parameter`` (returning the value and the
  error, parsed once) is the new hook below it, and keeps both errors.

0.6.0 (since 0.5.0)
===================

Everything in this release is opt-in: a DRF, ninja or FastAPI view that sets
none of the new options behaves exactly as in 0.5.0.

DRF parity (#2)
---------------

* DRF: ``object_validators`` (``Meta.object_validators`` on a viewset, or a
  keyword of ``@scrudful`` / ``ScrudfulViewFunc``) computes the
  validators from ``view.get_object()``, after DRF's queryset filtering and
  object permissions, so a missing or forbidden object is a 404 or 403 and
  never a validator, 412 or 428. ``etag_func(obj)`` and
  ``last_modified_func(obj)`` take the object, the handler's ``get_object()``
  returns the same instance, and a PUT or PATCH response gets validators from a
  refreshed object. A handler calling ``get_object()`` after saving gets the
  pre-save instance. ``@scrudful(object_validators=True)`` on a function view
  raises ``TypeError`` at request time.
* DRF: creates describe the new resource. With ``object_validators`` (which
  needs ``etag_func`` or ``last_modified_func``), ``creates=True`` (set by
  ``scrudful_viewset`` for ``create``; without ``object_validators`` it gets
  only links and the error entity) sends ``ETag`` and ``Last-Modified`` on
  the 201, and ``location_func(view, request, obj)``
  (``Meta.location_func``; needs ``creates=True`` and ``object_validators``) sends ``Location``. A custom
  ``create()`` that bypasses ``perform_create``, or a ``many=True`` create,
  gets none of them, and OPTIONS
  describes the 201 without the validator headers, as ninja does.
* DRF: ``ScrudfulPagination`` lists in the scrud envelope django-ninja's
  ``scrudful_list`` returns (``count``, ``page_count``, ``first``,
  ``previous``, ``next``, ``last``, ``content`` of entries with ``href``,
  ``etag``, ``last_modified``, ``link``, ``content``). It needs
  ``Meta.href_func(view, request, obj)``; ``page`` and ``page_size`` default to
  1 and 100 (maximum 1000); bad parameters are a 400, a page past the last one
  a 404 ("Invalid page.").
* ``scrudful.collection`` gains ``page_envelope``, ``envelope_entry`` and
  ``InvalidPage`` (standard library only), the code both front ends share.

Error bodies
------------

* ``error_body_func(request, result)`` replaces the ``{"detail": ...}`` body of
  scrudful's own 400, 412 and 428, on DRF (``@scrudful``, ``scrudful_viewset``
  with ``Meta.error_body_func`` and ``Meta.list_error_body_func``), ninja
  (``scrudful``, ``scrudful_list``) and FastAPI (``scrudful``). It must be a
  plain function, also in async ninja and FastAPI endpoints (DRF views are
  sync); ``scrudful_viewset`` does not apply it to ``create``. Status, headers,
  Links and Content-Type are unchanged and a 304 never calls it.
* New ``scrudful.problem`` module: ``problem_details`` returns RFC 9457 problem
  details for those responses and ``PROBLEM_JSON`` is the media type for the
  ``error`` entity.

0.5.0 (since 0.4.0)
===================

Semantic HTTP 0.1 (breaking)
----------------------------

All three adapters now follow `Semantic HTTP 0.1
<https://w3id.org/semantic-http/0.1/>`_ (#4). Responses and OPTIONS bodies
change, so clients and tests that compare them must be updated.

* The schema Link defaults changed: ``rel="describedby"`` (was ``describedBy``)
  and ``type="application/schema+json"`` (was ``application/json``). The
  context Link keeps ``rel="http://www.w3.org/ns/json-ld#context"`` and
  ``type="application/ld+json"``. ``DEFAULT_SCHEMA_REL`` and the other
  ``DEFAULT_*`` names remain. Overriding the schema ``rel`` makes the
  response's Link no longer a Schema Link Header, while the OPTIONS body still
  describes it as the schema: use the default to stay conformant.
* OPTIONS (DRF's ``ScrudfulMetadata``, ``ScrudfulRouter``, FastAPI's
  ``add_options``) answers the 0.1 HTTP Schema Resource, in place of the 2019
  draft shape. The ``content`` of a request or response is a Media Type Object
  (``schema`` ``$ref``, ``x-schema`` for a schema that is not JSON Schema, and
  ``x-rdf-context`` for a non-in-band media type). The body has an ``options``
  entry and a content-less ``head`` entry where HEAD is served. ``Allow`` lists
  exactly the body's methods, and the response is ``application/json`` with the
  two published HTTP Schema Resource Links (also for DRF's browsable API).
  The body's keys follow the order ``get``, ``put``, ``post``, ``delete``,
  ``options``, ``head``, ``patch``, ``trace``, and a write's ``requestBody`` is
  omitted when it has no content (it was an entry without ``content``).
* DRF: ``scrudful_viewset``, ``scrudful_api_view`` and now a ``@scrudful``
  method in a class body replace a class's stock ``APIView.options`` (an
  ``options`` the class defines is kept), as well as DRF's stock
  ``SimpleMetadata``; a ``@scrudful`` method of a mixin does so on each
  ``APIView`` subclassing the mixin. So an ``APIView`` with ``@scrudful``
  methods answers OPTIONS with the HTTP Schema Resource; a plain
  ``@api_view`` + ``@scrudful`` function view does not, and the errors it
  raises get no error Links through ``exception_handler`` (use
  ``scrudful_api_view``, which gives them both).
* DRF: a PATCH that DRF's stock ``partial_update`` serves through an explicit
  ``@scrudful`` ``update`` is described in OPTIONS as that ``update``
  (conditional, with its content and error responses), and
  ``exception_handler`` gives its errors that ``update``'s ``error`` Links.
* DRF: ``scrudful_api_view`` keeps a ``metadata_class`` set on the function
  (a custom class, or ``None``) on DRF 3.15.0 too, whose ``api_view`` doesn't
  copy it onto the view class.
* DRF: ``exception_handler`` gives an error that no scrudful handler answers
  (one raised while answering OPTIONS, a 405, one raised by a handler that
  isn't scrudful) the view-wide error entity: the ``error`` the view's
  scrudful handlers share; when they have different ones, the GET
  handler's, else the first in method order (a 405 got the first allowed
  handler's before). An error raised by a non-scrudful handler is served
  with that entity's Links and media type.
* DRF: scrudful's own 400, 412 and 428 with an ``error`` entity, and the
  ``exception_handler`` responses of views with one, bypass DRF's content
  negotiation, so the browsable API gets JSON for them. The view's first JSON
  renderer renders them (DRF's ``JSONRenderer`` if it has none). The
  browsable API's HTML responses are not conformant: drop
  ``BrowsableAPIRenderer`` for full conformance.
* Removed ``options_schema_link`` and ``options_context_link`` from
  ``ScrudfulRouter``: the OPTIONS Links are the fixed w3id.org 0.1 URLs.
* Every Link header target scrudful sends and every URI in the OPTIONS document
  is absolute: relative link values are resolved against the request. A
  ``Location`` (from ``location_func`` or ``ScrudfulResponse.location``, which
  RFC 9110 allows to be relative) and a list entry's ``href`` (from
  ``href_func``) are sent as given: return absolute ones from those callables
  if you want them. Links are de-duplicated: a Link that the view set itself
  with the relation of scrudful's schema link (``describedby`` by default, or
  an overridden ``schema_rel_or_func``'s) suppresses that link; one with either
  context relation (JSON-LD or ``rdf-context``, a single slot) suppresses
  scrudful's context link of either, and an overridden context relation is
  matched exactly. Relations are compared case-insensitively, and a Link of
  another relation suppresses nothing.
* ``Entity`` moved to ``scrudful.entity`` (still importable from the three
  adapters) and gained ``media_type``, which decides the context Link's
  defaults (none for in-band RDF media types, ``rdf-context`` for other
  non-JSON ones). For other non-JSON content a ``context_type_or_func``
  callable must return a type: one that returns nothing raises
  ``ValueError`` on the Links as in OPTIONS, rather than send a context Link
  without a type.
* DRF and FastAPI: the view's (endpoint's) schema and context Links go only
  on a 2xx response other than 204 that it returns, as in django-ninja. A
  204, a redirect, or an error the view returns gets no Links from scrudful
  (a Link it set itself is kept), instead of the success entity's: raise
  errors, rather than return them, to give them the error entity's Links. A
  FastAPI route whose ``status_code`` is not such a 2xx is described without
  content in OPTIONS.
* TRACE, OPTIONS and CONNECT ignore the ``If-*`` headers in every adapter
  (RFC 9110 §13.2.1): a decorated TRACE with validators no longer answers
  412 (or 304, 400, 428), its validator callbacks are not called, and its
  response gets no ``ETag`` or ``Last-Modified``, as its OPTIONS entry,
  without those statuses, says.
* No adapter gives a TRACE response the default (item) entity's Links, and
  django-ninja's OPTIONS no longer describes a TRACE with that content: a
  TRACE's content is the echoed request. An entity declared for the status
  (ninja's ``responses={200: Entity(...)}``) still applies.
* Error entities: ``error=`` for DRF (``Meta.error`` / ``Meta.list_error``)
  and FastAPI, as ninja already had. An ``error`` entity's ``media_type`` must
  be JSON content (``application/json`` or ``+json``, other than JSON-LD);
  every error response that carries an error entity's Links is served with
  ``Content-Type`` equal to its ``media_type``: scrudful's own 400, 412 and
  428 (all adapters), the responses of DRF's ``exception_handler`` and of both
  ``add_error_links`` helpers, and the errors raised inside a ninja scrudful
  operation (with the entity OPTIONS describes for their status). DRF's
  OPTIONS calls the ``error`` entity's callables only for the methods whose
  description has its content.
* New helpers that give the errors a framework answers itself the error
  entity's Links: ``add_error_links(api, error)`` for django-ninja and
  ``add_error_links(app, error)`` for FastAPI, and
  ``scrudful.rest_framework.exception_handler`` (named as DRF's
  ``EXCEPTION_HANDLER``; a project handler delegating to it carries its
  ``scrudful_error_links = True`` marker, e.g. through
  ``functools.wraps(exception_handler)``, for OPTIONS to describe the
  serializer's 400). Errors raised inside a ninja scrudful operation get
  the Links of the entity OPTIONS describes for their status. Calling an
  ``add_error_links`` again replaces the entity of the earlier call.
* FastAPI: ``add_options(app)`` answers OPTIONS on every URL a scrudful
  endpoint serves. A callable used outside the endpoint (by ``add_options``
  and ``add_error_links``) gets only the URL's path parameters that the
  endpoint takes (not those of an ``include_router`` prefix it doesn't
  declare) plus the request under the name of the endpoint's ``Request``
  parameter; the helpers check
  every such callable when called and raise ``TypeError`` for one that can't
  be called that way. ``add_options`` leaves mounted apps and middleware
  Mounts and Routers to themselves, and ``add_error_links`` covers the
  endpoints under middleware Mounts but not mounted apps: those need their own
  calls. The FastAPI adapter has no request entity: a request body is
  described with the endpoint's response links.
* The ninja example (``examples/ninja``) calls ``add_error_links`` and serves
  HEAD on the list; all three examples are tested against the 0.1 rules.
* The DRF example (``examples/scrudful_rest_framework``) renders and parses
  JSON only (no browsable API, no form bodies, which OPTIONS can't describe),
  its callbacks accept ``**kwargs`` (the router's format-suffix routes pass
  ``format``), and its link callables return ``django.urls.reverse`` paths,
  so ``?format=`` stays out of the link targets. The README's DRF snippet
  does the same.
* The documentation has a "Semantic HTTP conformance" section, builds without
  warnings, and documents ``Entity`` once.

0.4.0 (since 0.3.0)
===================

django-ninja adapter
--------------------

* Added ``scrudful.ninja``, which makes django-ninja operations SCRUDful. Its
  decorators wrap the operation's function, so everything runs after ninja's
  authentication, throttling and parameter validation, and every validator
  and precondition result is computed from what authorization approved:

  * ``@scrudful(load=..., ...)`` for single resources (GET, HEAD, PUT, PATCH,
    DELETE and POST actions): ``load(request, **params)`` is the
    fetch-if-allowed callable; the validator callbacks and the handler get the
    object it returns. It returns ``None`` for "no current representation"
    (RFC 9110 semantics) or raises, through ninja's exception handlers.
    ``load`` alone decides whether the resource exists: an object it returns
    exists even when ``etag_func`` / ``last_modified_func`` return ``None``
    for it, so an unconditional write gets 428, ``If-None-Match: *`` gets 412
    and ``If-Match: *`` passes (RFC 9110 §13.1.1-2). 304, 400, 412 and 428 as
    in the other adapters; fresh validators and Links after a 2xx PUT/PATCH
    (``load`` is called again when a validator callback is set or a value of
    the response's entity is callable; if that call raises, the client gets
    the error although the write is committed). The default entity's Links
    go on 2xx responses except a 204, which has no body; an explicit
    ``responses[204]`` or ``ScrudfulResponse.entity`` still applies.
  * ``@scrudful_create(...)``: 201 with ``Location``, the item's Links and the
    created item's ``ETag`` and ``Last-Modified``. A 201 without a created
    object (the handler returned ``None`` or an ``HttpResponse`` and set no
    ``ScrudfulResponse.created``) gets no ``Location`` or validators, and
    ``location_func`` and the validator callbacks are not called. If it has
    a body (e.g. ``JsonResponse(item, status=201)``) it keeps the item's
    Links when none of the link values is callable; with a link callable,
    which would get ``None`` in place of the created item, or without a
    body, it gets no item Links.
  * ``@scrudful_list(...)``: the handler returns the authorized queryset; the
    response is the paginated scrud list envelope (``page``, ``page_size``),
    with per-item validators and Links equal to the detail GET's, a typed
    envelope schema in OpenAPI, and optional collection validators. The
    collection response must be a list, set or tuple of the item schema
    (``ConfigError`` for ``Optional[List[...]]`` or a bare ``list``). In an
    async operation with sync per-item callables, a page takes one
    ``sync_to_async`` call.
  * ``Entity`` declares request and per-status response entities (schema and
    JSON-LD context Links) and the ``error`` entity carried as Links by
    scrudful's own 400, 412 and 428 bodies (an argument of ``@scrudful`` and
    ``@scrudful_list``; ``@scrudful_create`` has none, since a create
    produces none of these responses); ``ScrudfulResponse`` is the
    handler's place to set the status, ``Location``, validators, Links or
    created object of one response.
  * ``ScrudfulRouter`` answers OPTIONS on every path with a scrudful
    operation, without ``auth``, with the semantic-http "HTTP Schema
    Resource": a superset of DRF's ``ScrudfulMetadata`` output, except that
    the statuses are those the operation answers (those it declares, else
    200; 201 for a create), not DRF's fixed 201 for POST and 204 for DELETE.
  * Sync and async operations; ``CONDITIONAL_READ_RESPONSES`` and
    ``CONDITIONAL_WRITE_RESPONSES`` for OpenAPI. A function may be
    registered on several paths with different ``response=``; each operation
    keeps its own. Handlers in modules with ``from __future__ import
    annotations`` work even when the hidden parameter's annotation is
    imported only under ``TYPE_CHECKING``.
  * The decorators sit directly below the operation decorator. Something
    that replaces ``operation.view_func`` before scrudful binds the operation
    (a ``_ninja_contribute_to_operation`` callback contributed below the
    decorator, a decorator between it and the operation decorator, or a
    second scrudful decorator) raises ``ConfigError`` when the operation is
    built instead of being silently discarded. Authorization belongs in
    ``load`` or ``auth=``, never in a decorator below scrudful's (it would
    run after the preconditions); router- or API-wide decorators use
    ``add_decorator(mode="operation")``. See "Placement of other decorators"
    in the django-ninja documentation.
  * It imports without Django REST Framework.

  201 validators, entity declarations, error Links and load-based validators
  are not in the DRF and FastAPI adapters yet.
* ``scrudful.conditional.evaluate_preconditions`` takes a keyword-only
  ``exists``: ``True`` or ``False`` says whether the target resource exists,
  in place of inferring it from the validators (the default, ``None``, which
  the DRF and FastAPI adapters keep using). With ``exists=True`` and no
  validators, a 428 lists ``If-Match`` as the accepted precondition header.
* Added ``scrudful.collection``: ``collection_schema()`` and
  ``collection_context()`` build the JSON Schema (draft 2020-12) and the
  JSON-LD context of the scrud list envelope. It is framework-neutral.
* Added the example project ``examples/ninja``, a ``Role`` resource using the
  three decorators, entities, error Links and OPTIONS.

Internal
--------

* The OPTIONS description builder (``describe_read``, ``describe_write``,
  ``conditional_header_parameters``) and the endpoint-signature helpers moved
  to ``scrudful._adapter``, and the Django request/response helpers to
  ``scrudful._django``, to be shared with the new adapter. No change to the
  DRF or FastAPI adapters' behaviour or output.
* ``CONDITIONAL_READ_RESPONSES``, ``CONDITIONAL_WRITE_RESPONSES`` and the
  representation headers an early response drops are defined once, in
  ``scrudful._adapter``; ``scrudful.fastapi`` and ``scrudful.ninja`` publish
  the same objects under the same names. ``scrudful.rest_framework`` no longer
  has the private aliases ``_READ_RESPONSES`` and ``_WRITE_RESPONSES``.

Packaging
---------

* New extra ``scrudful[ninja]``: ``django>=4.2`` and
  ``django-ninja>=1.7,<2``. The upper bound is there because the adapter
  relies on private django-ninja internals (how operations are built and
  their response models), which a major release may change.
  The adapter's code also works with django-ninja 1.6, but the test suite
  verifies 1.7.0 and newer only: 1.6's ``TestAsyncClient`` methods are not
  ``async def``, so driving them with ``asgiref``'s ``async_to_sync`` warns,
  which the tests treat as an error.
* The package description names django-ninja (there is no django-ninja
  classifier; ``Framework :: Django`` covers it).

0.3.0 (since 0.2.0)
===================

Packaging and tooling
---------------------

* Migrated from Poetry to uv (``uv.lock``, ``[dependency-groups]`` for ``dev``
  and ``docs``). Python 3.10 to 3.14 are supported and tested.
* Added the MIT license. Django-derived code ships under BSD-3-Clause
  (``LICENSES/Django-BSD-3-Clause.txt``), so the license expression is
  ``MIT AND BSD-3-Clause`` (PEP 639).
* Framework dependencies are now optional extras: ``scrudful[drf]`` and
  ``scrudful[fastapi]``. The package itself has no required dependencies.
* Modernised the stack: current Django, Django REST Framework, FastAPI
  (pydantic v2) and Sphinx; pytest with pytest-randomly (order-independent
  tests); dropped pytest-freezegun and safetydance-django.
* ``scrudful.__version__`` is read from the installed package metadata, or is
  ``"0+unknown"`` when scrudful is not installed (a vendored source tree).
* The README is now the package's long description; the CI pipeline uses uv and
  publishes only after the tests pass.
* The CI pipeline runs for merge requests and the default branch; the docs
  (``pages``) and the release (``publish``) run only on the default branch, and
  the docs build treats warnings as errors. A merge request that changes the
  package sets the version to release (``uv version --bump minor``); the
  ``version-check`` job requires it to be higher than the target branch's and
  PyPI's. Merging publishes that version to PyPI and creates the ``vX.Y.Z`` tag
  and GitLab release; merges that don't change the version publish nothing.
* The package ships a ``py.typed`` marker and declares ``Source`` and
  ``Changelog`` project URLs; the sdist also contains the tests, the example
  projects and this changelog.

Breaking changes
----------------

Both adapters
~~~~~~~~~~~~~

* A missing precondition on PUT, PATCH or DELETE is answered with 428
  Precondition Required and
  ``{"detail": "This request must be conditional.", "accepted-precondition-headers": [...]}``
  (before: 400 ``{"missing-required-headers": [...]}``, PUT and DELETE only).
  Either ``If-Match`` or a valid ``If-Unmodified-Since`` is enough (before:
  both were required). ``If-None-Match: *`` ("create only", RFC 9110
  §13.1.2) also makes the request conditional; on an existing resource it is
  answered with 412.
* A malformed ``If-Match`` or ``If-None-Match`` is answered with 400 (before: it
  was ignored). Invalid ``If-Modified-Since`` / ``If-Unmodified-Since`` dates are
  ignored. 412 responses have a JSON body, ``{"detail": "Precondition failed."}``.
* Without ``etag_func`` or ``last_modified_func`` no preconditions are evaluated
  and the If-* headers are ignored.
* A validator callback returning ``None`` means "no current representation":
  preconditions are skipped and the view answers (typically 404), except for PUT,
  where ``If-None-Match: *`` passes and ``If-Match`` gives 412.
* ETags no longer carry a ``__version__`` suffix (``"x0.1.0"`` becomes ``"x"``),
  so previously issued ETags no longer match. ``str()`` of the callback's value
  is used; valid entity-tags are sent unchanged, other values are quoted, and
  values that cannot be an entity-tag raise ``ValueError``.
* Validators are set on 2xx GET/HEAD responses (2xx is judged by the status
  FastAPI will send, including the response class's default, e.g. 307 for
  ``RedirectResponse``), freshly recomputed on 2xx
  PUT/PATCH responses, and not set on POST, DELETE or non-2xx responses. 304
  responses now carry ``ETag`` and ``Last-Modified``. Validators set by the view
  are not overwritten.
* ``Last-Modified``: aware datetimes are converted to UTC (before: the offset was
  discarded) and sub-second precision is floored. Naive datetimes are UTC with
  FastAPI and are interpreted in ``settings.TIME_ZONE`` with DRF (before: UTC).
* ``Access-Control-Expose-Headers`` now includes ``ETag`` (and ``Location`` when
  present) and is merged with an existing value, also on 304/400/412/428
  responses. The ``Link`` header is appended to a ``Link`` value set by the view
  instead of overwriting it. Both are computed after the view runs.
* Decorator options are keyword-only and ``etag_func`` / ``last_modified_func``
  must be callable (``TypeError`` otherwise). ``http_schema_or_func`` is removed
  from the DRF decorators.
* ``scrudful.handlers`` (the vendored Django helpers, including
  ``scrudful.handlers.fastapi.get_conditional_response``) is removed; use
  ``scrudful.conditional``.
* The empty ``scrudful.flask`` module is removed.

``scrudful.utils``
~~~~~~~~~~~~~~~~~~

* ``link_content`` percent-encodes the URL outside the RFC 3986 allowed set and
  emits ``rel`` / ``type`` as escaped quoted-strings; control characters (and
  characters above U+00FF) in ``rel`` / ``type`` raise ``ValueError``.
  ``content_type`` is optional: when empty the ``type`` parameter is omitted.
* ``get_string_or_evaluate`` is removed (the adapters no longer use it).

``scrudful.fastapi``
~~~~~~~~~~~~~~~~~~~~

* Endpoints no longer have to declare ``request`` and ``response`` parameters and
  no longer receive them positionally; they get them only if they declare
  parameters annotated ``Request`` / ``Response`` (also ``Annotated[...]``).
* Callbacks receive the endpoint's keyword arguments only. Sync endpoints and
  callbacks run in the threadpool (the validator callbacks share one call and
  run in order), async callbacks are awaited.
* Headers are applied to a ``Response`` returned by the endpoint (before: dropped).
  Generator endpoints raise ``TypeError`` when decorated.
* Routes with a validator now apply preconditions to POST as well.
* The example application changed: ``/users``, ``/users/{user_id}``,
  ``/schemas/user.json`` and ``/contexts/user.jsonld`` with plain ``User``
  bodies (not ``{status, message, payload}``).

``scrudful.rest_framework``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``ScrudfulViewFunc`` no longer takes ``view_is_class_method``; the request is
  found by type.
* ``scrudful_api_view``: the first positional parameter is now
  ``http_method_names`` (default ``["GET"]``, as in DRF's ``api_view``); a string
  or use without parentheses raises ``TypeError``. DRF policy decorators
  (throttling, authentication, renderers, parsers, ...) are now honoured.
* ``scrudful_viewset`` no longer assigns ``cls.options`` (the module-level
  ``options()`` function is removed): OPTIONS is answered through
  ``metadata_class = ScrudfulMetadata`` unless the class's ``metadata_class`` is
  something other than DRF's default ``SimpleMetadata`` (a custom class, ``None``
  or a project-wide ``DEFAULT_METADATA_CLASS`` is kept; subclass or mix in
  ``ScrudfulMetadata`` to combine it with your own). It no longer adds methods
  the class lacks (read-only viewsets answer 405 instead of 500), wraps
  ``partial_update``, leaves explicit ``@scrudful`` methods alone and gives
  ``create`` the link configuration only. An explicit ``@scrudful`` on
  ``update`` also applies to PATCH when ``partial_update`` is DRF's stock one,
  which is then not wrapped. Re-decorating a subclass replaces inherited
  wrappers.
* ``ScrudfulMetadata``: ``determine_metadata_for_detail`` and
  ``determine_metadata_for_list`` are removed;
  ``determine_metadata_for_get/post/put/delete`` take ``(handler, args, kwargs)``
  and ``determine_metadata_for_patch`` is new. ``determine_metadata(request,
  view)`` describes the methods routed to the view (viewset action map or
  allowed methods) instead of guessing "detail" from URL arguments.
  ``get_method(view, name)`` returns the ``ScrudfulViewFunc`` or ``None`` (it
  used to assume a ``functools.partial``). ``required_conditional_headers``
  marks a header ``required`` only when it is the only one accepted and adds a
  description. The OPTIONS body now lists only routed methods, uses "Created" /
  204 "No Content", and documents 304/400/412/428 and the precondition headers.
  ``ScrudfulAPIViewMetadata`` is an alias.
* Decorating an ``async def`` view raises ``TypeError`` (DRF views are
  synchronous; before, the request failed).
* Callbacks receive ``(view, request, **url_kwargs)``; extra keyword arguments
  such as ``partial=True`` are no longer passed. ``*_or_func`` values are called
  when callable and used as they are otherwise (lazy strings and ``reverse``
  results now work).

Fixes
-----

* RFC 9110 evaluation order: ``If-Match`` / ``If-Unmodified-Since`` before
  ``If-None-Match`` / ``If-Modified-Since``, strong comparison for ``If-Match``,
  weak for ``If-None-Match``, ``*`` handling, 304 only for GET/HEAD, and
  ``Last-Modified`` epoch 0 treated as a valid timestamp.
* Entity-tag lists are tokenised (commas inside quoted tags no longer split
  them) and HTTP dates are parsed strictly (IMF-fixdate, RFC 850, asctime).
* ``Link`` header injection: URLs, ``rel`` and ``type`` are now escaped.
* The FastAPI adapter works with current FastAPI and pydantic v2, keeps the
  endpoint signature (no leaked internal parameters in OpenAPI), supports async
  and sync callbacks and endpoints, and no longer misaligns positional
  arguments.
* The DRF adapter no longer fails on PATCH, no longer passes ``partial`` to
  callbacks, handles ``@action`` methods and function views, and keeps
  ``partial_update`` / ``update`` from being evaluated twice.
* ``scrudful_api_view`` no longer replaces a project's
  ``DEFAULT_METADATA_CLASS`` (including ``None``, which disables OPTIONS
  metadata); only DRF's default ``SimpleMetadata`` is replaced.
* The OPTIONS description is correct for list, detail, nested and ``@action``
  routes, and mutating it no longer changes later responses (the response
  descriptions were shared module-level dictionaries).
* ``scrudful.fastapi`` evaluates string annotations (``from __future__ import
  annotations``) in the endpoint's module: FastAPI before 0.124 evaluated them
  in the wrapper's module, so body models became required query parameters
  (422).
* ``scrudful.fastapi``: the 304 response now carries the headers dependencies set
  on the injected ``Response`` (``Cache-Control``, ``Vary``, ``Set-Cookie``, ...,
  but not ``Content-*`` metadata), as RFC 9110 §15.4.5 requires; the 400/412/428
  responses carry its ``Set-Cookie`` lines only.
* The example projects: the DRF validator callbacks look items up in
  ``view.get_queryset()``, so items the view hides are a 404 whatever the
  preconditions; the FastAPI README example uses timezone-aware datetimes.
* Documentation: the README, usage and changelog pages were added, the API
  documentation covers all modules, and the Sphinx build is warning-free.

Added
-----

* ``scrudful.conditional``: the framework-agnostic RFC 9110 conditional-request
  core (``evaluate_preconditions``, ``quote_etag``, ``parse_etag_list``,
  ``http_timestamp``, ``parse_http_date``, ``format_http_date``, ...).
* ``scrudful.utils``: ``join_links``, ``merge_expose_headers``,
  ``EXPOSABLE_HEADERS``.
* ``scrudful.fastapi``: ``CONDITIONAL_READ_RESPONSES`` and
  ``CONDITIONAL_WRITE_RESPONSES`` for the route's OpenAPI ``responses=``;
  ``DEFAULT_SCHEMA_REL`` / ``DEFAULT_SCHEMA_TYPE`` / ``DEFAULT_CONTEXT_REL`` /
  ``DEFAULT_CONTEXT_TYPE``.
* ``scrudful.rest_framework``: PATCH support, ``ScrudfulViewFunc.has_validators``
  and ``link_header()``, the same ``DEFAULT_*`` constants.
* PATCH requires a precondition like PUT and DELETE.
* Example projects for FastAPI and Django REST Framework, with tests.
