=========
Changelog
=========

Unreleased (0.3.0, since 0.2.0)
===============================

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
* ``scrudful.__version__`` is read from the installed package metadata.
* The README is now the package's long description; the CI pipeline uses uv and
  publishes only after the tests pass.
* The CI pipeline runs for merge requests and the default branch; the docs
  (``pages``) and the release (``publish``) run only on the default branch, and
  the docs build treats warnings as errors. ``publish`` releases the version in
  ``pyproject.toml`` and then bumps the patch version on the default branch, so
  the branch always holds the next unreleased version.
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
* Validators are set on 2xx GET/HEAD responses, freshly recomputed on 2xx
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
  callbacks run in the threadpool, async callbacks are awaited.
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
  ``ScrudfulMetadata`` to combine it with your own). It no
  longer adds methods the class lacks (read-only viewsets answer 405 instead of
  500), wraps ``partial_update``, leaves explicit ``@scrudful`` methods alone and
  gives ``create`` the link configuration only. Re-decorating a subclass replaces
  inherited wrappers.
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
* ``scrudful_viewset`` / ``scrudful_api_view`` no longer override a project's
  ``DEFAULT_METADATA_CLASS`` (including ``None``, which disables OPTIONS
  metadata); only DRF's default ``SimpleMetadata`` is replaced.
* An explicit ``@scrudful`` on ``update`` in a ``scrudful_viewset`` now applies to
  PATCH when ``partial_update`` is DRF's stock one (before it was ignored).
* ``import scrudful`` works from an uninstalled source tree
  (``__version__`` is ``"0+unknown"`` there).
* The OPTIONS description is correct for list, detail, nested and ``@action``
  routes, and mutating it no longer changes later responses (the response
  descriptions were shared module-level dictionaries).
* ``scrudful.fastapi`` evaluates string annotations (``from __future__ import
  annotations``) in the endpoint's module: FastAPI before 0.124 evaluated them
  in the wrapper's module, so body models became required query parameters
  (422).
* ``scrudful.fastapi``: endpoints with generic-alias parameters (``list[int]``,
  ``dict[str, int]``) no longer fail at decoration on Python 3.10.
* ``scrudful.fastapi``: the 304 response now carries the headers dependencies set
  on the injected ``Response`` (``Cache-Control``, ``Vary``, ``Set-Cookie``, ...,
  but not ``Content-*`` metadata), as RFC 9110 §15.4.5 requires; the 400/412/428
  responses carry its ``Set-Cookie`` lines only.
* ``scrudful.fastapi``: a route without ``status_code`` uses its response class's
  default status (e.g. 307 for ``RedirectResponse``) to decide whether validators
  are added.
* ``scrudful.fastapi``: sync validator callbacks run in a single threadpool call
  (still sequentially, ETag first) instead of one per callback.
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
