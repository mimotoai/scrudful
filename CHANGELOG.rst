=========
Changelog
=========

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
* Removed ``options_schema_link`` and ``options_context_link`` from
  ``ScrudfulRouter``: the OPTIONS Links are the fixed w3id.org 0.1 URLs.
* Every URI scrudful sends or describes is absolute: relative link values are
  resolved against the request. Links are de-duplicated: a ``describedby`` or
  context Link that the view set itself suppresses scrudful's of that kind,
  and a Link of another relation suppresses nothing.
* ``Entity`` moved to ``scrudful.entity`` (still importable from the three
  adapters) and gained ``media_type``, which decides the context Link's
  defaults (none for in-band RDF media types, ``rdf-context`` for other
  non-JSON ones).
* Error entities: ``error=`` for DRF (``Meta.error`` / ``Meta.list_error``)
  and FastAPI, as ninja already had. An ``error`` entity's ``media_type`` must
  be JSON content (``application/json`` or ``+json``, other than JSON-LD);
  scrudful's own 400, 412 and 428, and the responses of the helpers below, are
  served with ``Content-Type`` equal to ``error.media_type``.
* New helpers that give the errors a framework answers itself the error
  entity's Links: ``add_error_links(api, error)`` for django-ninja and
  ``add_error_links(app, error)`` for FastAPI, and
  ``scrudful.rest_framework.exception_handler`` (named as DRF's
  ``EXCEPTION_HANDLER``). Errors raised inside a ninja scrudful operation get
  the Links of the entity OPTIONS describes for their status.
* FastAPI: ``add_options(app)`` answers OPTIONS on every URL a scrudful
  endpoint serves. A callable used outside the endpoint (by ``add_options``
  and ``add_error_links``) gets only the URL's path parameters plus the request
  under the name of the endpoint's ``Request`` parameter; the helpers check
  every such callable when called and raise ``TypeError`` for one that can't
  be called that way. Mounted apps need their own calls. The FastAPI adapter
  has no request entity: a request body is described with the endpoint's
  response links.
* The ninja example (``examples/ninja``) calls ``add_error_links`` and serves
  HEAD on the list; all three examples are tested against the 0.1 rules.
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
