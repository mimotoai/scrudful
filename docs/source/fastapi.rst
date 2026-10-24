FastAPI
=======

``scrudful.fastapi`` adapts the shared conditional-request core to FastAPI
endpoints. Install it with ``pip install "scrudful[fastapi]"``. See
``examples/fastapi/api.py`` for a complete application, and :doc:`usage` for the
behaviour table and a quickstart.

.. code-block:: python

    @app.get("/users/{user_id}", responses=CONDITIONAL_READ_RESPONSES)
    @scrudful(etag_func=user_etag, last_modified_func=user_last_modified)
    async def get_user(user_id: int): ...

``@scrudful(...)`` must sit below the route decorator.

Semantic HTTP: error Links and OPTIONS
--------------------------------------

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


    add_error_links(app, ERROR)
    add_options(app)

:func:`~scrudful.fastapi.add_error_links` wraps the app's exception handlers,
FastAPI's defaults and your own, so that the errors FastAPI and Starlette
answer (an ``HTTPException`` an endpoint raises, the 422 of request
validation, the 404 of an unknown path and the 405 of an unrouted method) carry
the Links of an error entity. The entity is the one passed to it, unless the
request was routed to a scrudful endpoint with an ``error`` of its own: then
the errors raised for that endpoint get the endpoint's ``error``, the entity
its OPTIONS body describes. It raises ``RuntimeError`` once the app has served
a request, since the exception handlers are fixed by then. A mounted FastAPI
app has its own exception handlers: call it on that app too.

:func:`~scrudful.fastapi.add_options` answers OPTIONS on every URL a scrudful
endpoint serves (unless a route of yours serves OPTIONS there) with the URL's
HTTP Schema Resource: an OpenAPI Path Item describing each method, the
content of its requests and responses, scrudful's own 304, 400, 412 and 428
and the ``error`` content of the last three, with the ``Link`` headers of the
HTTP Schema Resource's published schema and context and an ``Allow`` listing
its methods. Given the app, it finds the endpoint of each method the way
Starlette routes the request, through included routers, ``Mount``\ s and
mounted FastAPI apps, so a path split across several routers is described as
a whole. The answer has no dependencies, so it needs no credentials. Given an
``APIRouter``, it describes each of the router's own paths; call it before
including the router. Prefer calling it on the app.

Callables inside and outside the endpoint
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Inside the endpoint, every callable of ``@scrudful(...)`` and of its ``error``
gets the keyword arguments FastAPI resolved for the endpoint: path, query,
body and dependency values, plus its ``Request`` and ``Response`` parameters
if it declares them.

Outside the endpoint, the helpers call some of them with much less:

* :func:`~scrudful.fastapi.add_options` calls the endpoints' link callables
  (schema and context URL and type), and their ``error``'s when the endpoint
  is conditional, to build the OPTIONS body;
* :func:`~scrudful.fastapi.add_error_links` calls the endpoint's ``error``
  callables for the errors raised for it.

There they get only the URL's path parameters as Starlette matched them
(strings, unless the path uses a convertor such as ``{id:int}``), plus the
request under the name of the endpoint's ``Request`` parameter when it
declares one. The entity passed to ``add_error_links`` gets ``request=``
only. Both helpers check every such callable when they are called and raise
``TypeError`` for one that can't be called that way, naming the endpoint and
the argument. To use a callable in both places, accept ``**kwargs`` or give
the parameters only the endpoint gets defaults:

.. code-block:: python

    def item_schema(item_id, request, **kwargs):  # inside and outside
        return request.url_for("item_schema")

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

HEAD
~~~~

FastAPI routes HEAD only to a route that lists it. Stack ``@app.head(...)``
with ``@app.get(...)`` above ``@scrudful(...)``, as above, or use
``@app.api_route(path, methods=["GET", "HEAD"])``; FastAPI gives both methods
of one ``api_route`` the same OpenAPI operation id, and warns about it.

.. automodule:: scrudful.fastapi
   :members:
   :member-order: bysource
