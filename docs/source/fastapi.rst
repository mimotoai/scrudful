FastAPI
=======

``scrudful.fastapi`` adapts the shared conditional-request core to FastAPI
endpoints. Install it with ``pip install "scrudful[fastapi]"``. See
``examples/fastapi/api.py`` for a complete application, and :doc:`usage` for the
behaviour table, a quickstart, and the OPTIONS and HTTP Schema Resource model
the three adapters share.

.. code-block:: python

    @app.get("/users/{user_id}", responses=CONDITIONAL_READ_RESPONSES)
    @scrudful(etag_func=user_etag, last_modified_func=user_last_modified)
    async def get_user(user_id: int): ...

``@scrudful(...)`` must sit below the route decorator.

Semantic HTTP: error Links, OPTIONS and the HTTP Schema Resource
-----------------------------------------------------------------

Two helpers complete what the decorator does, so that every response follows
Semantic HTTP 0.1. Call both once the routes and exception handlers are
declared, and before the app serves requests:

.. code-block:: python

    from scrudful.fastapi import Entity, add_error_links, add_options, scrudful

    ERROR = Entity(
        schema_link_or_func="/schemas/error.json",
        context_link_or_func="/contexts/error.jsonld",
    )


    @app.get("/users/{user_id}", responses=CONDITIONAL_READ_RESPONSES)
    @app.head("/users/{user_id}", responses=CONDITIONAL_READ_RESPONSES)
    @scrudful(
        etag_func=user_etag,
        last_modified_func=user_last_modified,
        schema_link_or_func="/schemas/user.json",
        context_link_or_func="/contexts/user.jsonld",
        error=ERROR,
    )
    async def get_user(user_id: int): ...


    def load_user(request, user_id: str):
        return find_user(user_id)  # None, or raise HTTPException(404)


    add_error_links(app, ERROR)
    add_options(app, base_url="https://api.example.com/", load=load_user)

:func:`~scrudful.fastapi.add_options` gives every path a scrudful endpoint
serves an HTTP Schema Resource (HSR), serves it by GET, links it from every
response of the path, and answers the path's OPTIONS with a 204 whose
``Allow`` lists what the requester may use.
:func:`~scrudful.fastapi.add_error_links` gives the errors FastAPI and
Starlette answer the error entity's Links, and the HSR link on a described
path. Its docstring and :func:`~scrudful.fastapi.add_options`'s have the
details; this page summarizes them.

The options of ``add_options``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

All keyword-only; calling it again adds no route and replaces the options (so
give them all again):

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Option
     - Meaning
   * - ``base_url``
     - The absolute URL relative link URLs resolve against, in the HSR, on
       the Link headers and for the HSR link (see "The base URL" in
       :doc:`usage`). Without it, a relative link URL in the HSR is a
       ``ValueError`` when the document is built, and the responses resolve
       their Links against the request's URL.
   * - ``hsr_route``
     - Whether to serve each document by GET at ``/_hsr/<name>`` (default
       ``True``). ``False`` needs ``hsr_url``.
   * - ``hsr_url``
     - ``hsr_url(path)``: the absolute URL of the document of ``path`` (the
       resource's template, its first route's), served elsewhere (see
       :func:`~scrudful.fastapi.http_schema_resource_endpoint`). The
       responses link to it as it returns it.
   * - ``hsr_cache_control``
     - The ``Cache-Control`` of the HSR route (default ``public,
       max-age=300``).
   * - ``allowed``
     - ``allowed(request, method, obj) -> bool``: when given, it alone
       decides each method of ``Allow`` (see `OPTIONS and Allow`_).
   * - ``load``
     - ``load(request, **path_params)``: the object at an item URL, for
       ``allowed``; ``None`` for none, or an ``HTTPException`` (a 404) to
       answer.

It raises ``ValueError`` for a ``base_url`` that isn't absolute,
``hsr_route=False`` without ``hsr_url``, a scrudful path under ``/_hsr/``, two
resources with one HSR route name, and a link callable of a scrudful endpoint
(or of its ``error``) that can't be called as ``func()`` (0.9 raised
``TypeError`` for a callable that couldn't take the path parameters); and
``TypeError`` for an option of the wrong type.

The document
~~~~~~~~~~~~

The HSR (see :func:`~scrudful.fastapi.http_schema_resource`) is built the first
time it is needed, without a request, and kept until the routes of its path or
the app's ``add_error_links`` entity change: the same document for every
requester. For each method of the path, in the order ``get``, ``put``,
``post``, ``delete``, ``options``, ``head``, ``patch``, ``trace``:

* a scrudful endpoint's success status, its route's ``status_code=`` (else the
  response class's default, 200 for JSON), with the endpoint's content: its
  ``entity=`` (an ``Entity``, for another media type than JSON), else
  ``application/json`` with its link options;
* the statuses its route declares in ``responses=`` (codes, or ranges such as
  ``"4XX"``), with their ``description``: a success status with the
  endpoint's content, under its entity's media type only (another media type
  the declared ``content`` lists isn't described, since scrudful's Links
  describe the entity's: give the endpoint ``entity=`` for it), an error
  status with the error entity's content. A status set at run time
  (``response.status_code = 201``) can't be known: declare it;
* scrudful's 304, 400, 412 and 428 when the endpoint is conditional, with the
  ``error`` content and the precondition headers;
* a ``default`` entry, with the content of the entity the errors raised for
  the endpoint get: its ``error``, else ``add_error_links``'s (no content
  without ``add_error_links``);
* ``head`` like ``get`` without content (where a route serves HEAD), a TRACE
  without content, ``options`` as a 204, and a method served by a route that
  isn't scrudful with its success status, without content.

FastAPI endpoints have no request entity: an endpoint's request body is
described with the content of its responses.

Link callables are called in *describe* mode, ``func()``, with no arguments:
they must return the target they return on the responses, or the document
disagrees with the Links. ``add_options`` checks that each can be called so
when it is called (``ValueError`` otherwise); one that raises when the document
is built is a ``ValueError`` naming it, and so are a relative URL without
``base_url`` and a schema link without an RDF context link (for a non-in-band
media type). Async callables are awaited. Give the parameters a callable takes
inside the endpoint defaults, or accept only ``**kwargs``, and return a path
(``app.url_path_for("user_schema")``) that ``base_url`` resolves.

**Resources and their templates.** Given the app, ``add_options`` keys the
resources by the full path templates of the routes (those of included routers
with their prefix, and those under a ``Mount(routes=...)``), with the
parameters' names left out and their convertors kept (``str`` being none):
``/items/{id}`` and ``/items/{item_id}``, on the app or on different routers,
are one resource, whose document describes, for each method, the first route
that serves it, as Starlette routes it (a route a ``Mount`` before it shadows
is left out). Templates that overlap otherwise stay separate resources, with
their own document and OPTIONS: ``/items/new`` beside ``/items/{id}``, or
``/items/{id:int}`` beside ``/items/{name}``, although a URL both match
reaches whichever route comes first. A method a literal URL gets only through
the parameter route isn't listed for the literal: avoid such overlaps, or give
them the same methods.

The HSR route
~~~~~~~~~~~~~

With ``hsr_route`` (the default), each resource's document is served by GET and
HEAD at ``/_hsr/<name>``: the resource's template (its first route's), its
segments joined with ``.``, each convertor but ``str`` joined to its
parameter's name with ``-``. ``/users/{user_id}`` gives
``/_hsr/users.user_id``, ``/users/{id:int}`` gives ``/_hsr/users.id-int``,
``/users`` gives ``/_hsr/users`` and ``/`` gives ``/_hsr/_root``. It answers
``application/json``, the compact JSON of the document, the HSR's own schema
and context Links, a strong ``ETag`` (a 304 for a matching ``If-None-Match``)
and ``Cache-Control``; any other method with a 405 (``Allow: GET, HEAD``, no
content). It is a Starlette ``Route``, not an ``APIRoute``: no app, router or
``include_router`` dependency applies to it, so it needs no credentials, and
it is not in the OpenAPI document.

* Given the app, one route, ``/_hsr/{name}``, put before every other route,
  resolves the name on each request (paths added later are served too); it
  matches only the names of the app's resources, so another ``/_hsr/<x>`` goes
  on to your routes.
* Given an ``APIRouter``, each of its resources gets its own route on the
  router, so the prefix it is included under applies (``/v1/_hsr/items.id``):
  call ``add_options`` before including the router, and again after adding a
  resource to it. A resource added since has no HSR route, no OPTIONS route
  and no HSR link.
* **Paths under** ``/_hsr/`` **are reserved**, and two resources can't share a
  name (``/a.b`` beside ``/a/b``): ``add_options`` raises ``ValueError`` for
  the paths there are, and the app's HSR route raises it on a request for
  paths added since.
* A mounted Starlette or FastAPI app, and a ``Mount`` or ``Router`` with
  ``middleware``, are left alone, so that their middleware still sees their
  requests: call ``add_options`` on a mounted app itself; the paths under a
  middleware-wrapped ``Mount`` or ``Router`` get no OPTIONS and no HSR.
* **Serving it elsewhere.** With ``hsr_route=False`` and an ``hsr_url``, add
  :func:`~scrudful.fastapi.http_schema_resource_endpoint` with ``add_route``
  (a Starlette route, without dependencies):

  .. code-block:: python

      HSR_URLS = {"/items/{id}": "https://api.example.com/schemas/item"}
      add_options(
          app,
          base_url="https://api.example.com/",
          hsr_route=False,
          hsr_url=HSR_URLS.__getitem__,
      )
      app.add_route(
          "/schemas/item", http_schema_resource_endpoint(app, "/items/{id}")
      )

OPTIONS and Allow
~~~~~~~~~~~~~~~~~

An OPTIONS to a URL of a described resource, which no route of yours serves
OPTIONS for, is answered without any dependency (OPTIONS itself needs no
credentials: an anonymous requester of a locked resource gets ``Allow:
OPTIONS``, not a 401), in this order:

1. **A CORS preflight** (``Origin`` and ``Access-Control-Request-Method``) is a
   204 with no content, no Links and no ``Allow``, before any dependency,
   ``load`` or ``allowed`` runs. A ``CORSMiddleware`` in front usually answers
   it first.
2. **Each method is checked without the object.** With ``allowed``, every
   method passes this step. Otherwise only the route's *security
   dependencies* run and decide: those declared with ``Security(...)``, and
   any ``Depends`` with a security scheme below it (a ``fastapi.security``
   class such as ``HTTPBearer``, or a ``Security(...)``, at any depth, as a
   ``get_current_user`` reading a token), wherever declared (an endpoint
   parameter, or ``dependencies=`` of the route, its routers, ``include_router``
   or the app). They are resolved by FastAPI on a copy of the request whose
   method is the method checked, without its body. An ``HTTPException`` with
   401, 403 or 404, or a parameter of theirs failing validation, means not
   allowed.
3. **When a method passed and the URL has path parameters**, the object is
   loaded: ``load(request, **path_params)``, with all the matched URL's path
   parameters (a ``Mount``'s and a router prefix's too), as Starlette converts
   them (a ``str`` without a convertor), named as in the resource's first
   route. A URL without any (a top-level collection) has no object, and
   ``load`` isn't called; a nested collection's (``/orgs/{org}/items``) gets
   its parent's parameters: return ``None`` for it. A requester the security
   dependencies refuse everything gets ``Allow: OPTIONS`` for a missing and an
   existing object alike.
4. **With** ``allowed``, ``allowed(request, method, obj)`` alone decides each
   method (``request`` a copy with that method; a ``bool``, else
   ``TypeError``). An ``HTTPException`` 401 or 403 it raises refuses the
   method, as DRF's ``allowed_func`` and ninja's ``allowed`` do.
5. **The answer** is a 204 without content or ``Content-Type``, with ``Allow``
   (the allowed methods and ``OPTIONS``; ``HEAD`` only when a route serves it),
   one HSR link and ``Access-Control-Expose-Headers``.

An ``HTTPException`` raised by ``load`` or ``allowed`` (the 404 of a missing
object; for ``allowed``, another status than 401 and 403), or by a check with
another status than 401, 403 and 404 (a 429), is
answered by the app's exception handlers, with the HSR link; anything else
raised propagates (a 500). A 405 is Starlette's, with its ``Allow``: the
methods of the first route whose path matches, which may be fewer than the
path serves (``HEAD`` alone for a POST to ``/users/1`` in the example, whose
``@app.head`` route comes first).

Things to know:

* **Only security dependencies run.** A dependency with no security scheme
  below it is not run, even in ``dependencies=``: a throttle declared beside
  the security ones consumes no quota, but authorization written as a plain
  ``Depends`` (a hand-rolled header check) doesn't show in ``Allow``. Declare
  it with ``Security(...)``, or give ``allowed``. A security dependency runs
  whole, with its sub-dependencies, plain ones included: a database session or
  a throttle a ``get_current_user`` depends on runs for each method checked.
  One that needs the request body can't be resolved: it is skipped, and does
  not refuse its method.
* **Object-level rules need** ``allowed`` (and ``load``): the security
  dependencies have no object. With ``allowed``, every requester passes step 2,
  so the object is loaded for each: ``load`` should then hide (``None``, or a
  404) the objects a requester may not see, or OPTIONS tells anyone which
  exist.
* OPTIONS relies on ``fastapi.dependencies.utils.solve_dependencies`` (and the
  ``Dependant`` and exit stack it takes): an installed FastAPI whose differ
  from the releases scrudful supports raises ``RuntimeError`` on OPTIONS.
* Given an ``APIRouter``, ``add_options`` adds an OPTIONS route to each of its
  resources (at its first route's path), answered with the router's options.
  Prefer calling it on the app.

Error Links and the JSON 500
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:func:`~scrudful.fastapi.add_error_links` wraps the app's exception handlers,
FastAPI's defaults and your own for ``HTTPException`` and request validation,
so that the errors FastAPI and Starlette answer (an ``HTTPException`` an
endpoint or a dependency raises, the 422 of request validation, the 404 of an
unknown path and the 405 of an unrouted method) carry the Links of an error
entity, and are served as its ``media_type``. The entity is the one passed to
it, unless the request was routed to a scrudful endpoint with an ``error`` of
its own: then the errors raised for that endpoint get the endpoint's ``error``,
the entity its HSR's ``default`` entry describes. It raises ``RuntimeError``
once the app has served a request, since the exception handlers are fixed by
then. A mounted FastAPI (or Starlette) app has its own exception handlers:
call it on that app too.

* **The 500.** It also wraps the app's handler for ``500`` or ``Exception``,
  or, without one, installs one answering a JSON 500, ``{"detail": "Internal
  Server Error"}``, with the Links, in place of Starlette's ``text/plain``
  one. Starlette's ``ServerErrorMiddleware`` sends it only with ``debug``
  off (with ``debug=True``, its traceback page), and raises the exception
  again once it is sent (so a ``TestClient`` raises it unless created with
  ``raise_server_exceptions=False``). That middleware is outside every
  middleware added with ``add_middleware``, so a cross-origin 500 gets no
  ``Access-Control-Allow-Origin`` from a ``CORSMiddleware`` added that way:
  serve the app wrapped in it instead, ``CORSMiddleware(app, ...)``.
* **Problem details.** With an ``application/problem+json`` entity, FastAPI's
  default validation 422 is RFC 9457 problem details, FastAPI's errors as the
  ``errors`` member (``problem_from_errors(422, exc.errors(), "Request
  validation failed")``, :func:`scrudful.problem.problem_from_errors`), and
  the JSON 500 ``problem_from_errors(500, None)``. A handler of your own keeps
  its body. The w3id problem-details context doesn't map ``errors``: publish
  a context that imports it (``@import``) and maps ``errors``, and link that
  one.
* **The HSR link.** On a described path, every response of a wrapped handler
  (the 401, 403, 404, 405, 422 and the 500) also gets the HSR link, whatever
  its content. An unknown path's 404, and a path without a scrudful endpoint,
  get none.
* **Not covered:** an exception class with a handler of your own (a domain
  exception) is not wrapped, so its responses get no Links, nor the HSR link.
  A plain FastAPI route on a described path gets the HSR link on the errors
  raised for it, but not on its success responses, which scrudful doesn't
  decorate.

The endpoint's schema and context links describe its success content: they
go on a 2xx response other than 204 that the endpoint returns, and on no
other (nor on a TRACE's, whose content is the echoed request). A 204, a
redirect, or an error the endpoint *returns* (``return JSONResponse({...},
status_code=404)``, or ``response.status_code = 400``) gets no schema or
context Links from scrudful, only the HSR link; a ``Link`` the endpoint set on
it is kept as is. To give an error the error entity's Links, *raise* it
(``HTTPException``): :func:`~scrudful.fastapi.add_error_links` then adds them.
The HSR agrees: a route whose ``status_code`` is not such a 2xx is described
without content.

scrudful's own 400, 412 and 428 are returned by the endpoint, not raised, so
no exception handler sees them: they carry Links only from the decorator's
``error``. Give every conditional ``@scrudful(...)`` an ``error=``, also with
:func:`~scrudful.fastapi.add_error_links` installed. A Link a dependency set on
the injected ``Response`` stays on them, merged by relation, except one with
the ``describedby`` relation or an RDF context one: that describes the success
content, and the body is the error's.

Callables inside and outside the endpoint
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Inside the endpoint, every callable of ``@scrudful(...)`` and of its ``error``
gets the keyword arguments FastAPI resolved for the endpoint: path, query,
body and dependency values, plus its ``Request`` and ``Response`` parameters
if it declares them.

Outside the endpoint, the helpers call some of them with much less:

* :func:`~scrudful.fastapi.add_options` calls the endpoints' link callables
  (schema and context URL and type), and their ``error``'s, as ``func()``
  with no arguments, to build the HSR (see `The document`_);
* :func:`~scrudful.fastapi.add_error_links` calls the endpoint's ``error``
  callables for the errors raised for it.

For ``add_error_links``, they get only the URL's path parameters that the
endpoint takes, as Starlette matched them (strings, unless the path uses a
convertor such as ``{id:int}``): a parameter of an ``include_router`` prefix
that the endpoint doesn't declare reaches neither the endpoint nor its
callables. An aliased one (``item: int = Path(alias="item_id")``) is passed
under the endpoint's name for it (``item``), and one that only a
``Depends(...)`` takes is not passed, as in the endpoint. They also get the
request under the name of the endpoint's ``Request`` parameter when it
declares one. The entity passed to ``add_error_links`` gets ``request=``
only. Each helper checks every such callable when it is called: a callable
that can't be called with no arguments is a ``ValueError`` from
``add_options``, one that can't take what ``add_error_links`` gives a
``TypeError`` from it. To use a callable everywhere, accept ``**kwargs`` and
return the same path whatever it gets:

.. code-block:: python

    def item_schema(**kwargs):  # inside, outside and in describe mode
        return app.url_path_for("item_schema")

The error media type
~~~~~~~~~~~~~~~~~~~~

An error entity describes JSON bodies: its ``media_type`` must be
``application/json`` or another ``+json`` type (e.g.
``application/problem+json``), other than JSON-LD; anything else raises
``ValueError``. scrudful's own 400, 412 and 428 are served with the endpoint's
``error.media_type`` as their ``Content-Type`` (``application/json`` without
an ``error``). The responses :func:`~scrudful.fastapi.add_error_links` touches
are those with JSON content only, and each is served with the entity's
``media_type``, whatever the handler chose: a custom handler's
``application/json`` response is relabelled, say, ``application/problem+json``,
so its body must be content the entity describes. Responses without content
(e.g. a 304) or with other content are left as they are.

Error bodies
~~~~~~~~~~~~

``error_body_func(request, result)`` on ``@scrudful`` returns the dict that
replaces the ``{"detail": ...}`` body of scrudful's own 400, 412 and 428. It
must be a plain function, also for an ``async def`` endpoint.
:func:`scrudful.problem.problem_details` gives RFC 9457 problem details; pair
it with an ``error`` entity whose ``media_type`` is ``PROBLEM_JSON``.

HEAD
~~~~

FastAPI routes HEAD only to a route that lists it, and ``Allow`` lists HEAD only
where a route serves it. Stack ``@app.head(...)`` with ``@app.get(...)`` above
``@scrudful(...)``, as above, or use ``@app.api_route(path, methods=["GET",
"HEAD"])``. The stacked decorators are the form that works on every FastAPI
version: newer versions warn about the duplicate OpenAPI operation id that both
methods of one ``api_route`` get, and the tests run with warnings as errors.

Overriding the schema ``rel`` (e.g. ``schema_rel_or_func="profile"``) makes the
response's Link no longer a Schema Link Header, while the HSR still describes
it as the schema, so the API is not conformant. Use the default.

CORS
~~~~

Configure Starlette's ``CORSMiddleware`` as "CORS" in :doc:`usage` says:

.. code-block:: python

    from starlette.middleware.cors import CORSMiddleware

    from scrudful.utils import EXPOSE_HEADERS

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://app.example.org"],
        allow_methods=["*"],  # its default, GET only, refuses OPTIONS and PUT
        allow_credentials=True,  # Allow depends on the credentials
        expose_headers=list(EXPOSE_HEADERS),  # it replaces scrudful's
    )

An app-wide middleware covers the ``/_hsr/`` route, which sets no CORS headers
of its own. For the 500 to carry them too, wrap the app instead (see `Error
Links and the JSON 500`_). Under a ``Mount`` or ``Router`` with
``middleware``, scrudful answers no OPTIONS: its middleware must answer the
preflights.

.. automodule:: scrudful.fastapi
   :members:
   :member-order: bysource
   :exclude-members: Entity
