========
scrudful
========

Helpers for building *SCRUDful* REST APIs (Search, Create, Read, Update,
Delete) that get HTTP conditional requests right, for Django REST Framework
and FastAPI.

A decorated view or endpoint:

* evaluates ``If-Match``, ``If-None-Match``, ``If-Modified-Since`` and
  ``If-Unmodified-Since`` as specified by RFC 9110 section 13, answering 304,
  400, 412 or 428 without calling your code when appropriate;
* sets ``ETag`` and ``Last-Modified`` on the responses that should carry them;
* adds ``Link`` headers pointing at the JSON schema and the JSON-LD context of
  the response body (RFC 8288);
* exposes ``ETag``, ``Link`` and ``Location`` to browsers through
  ``Access-Control-Expose-Headers``.

The HTTP logic is framework-agnostic and lives in ``scrudful.conditional`` and
``scrudful.utils``; ``scrudful.rest_framework`` and ``scrudful.fastapi`` are
thin adapters. The package has no required dependencies.

Installation
============

.. code-block:: console

    $ pip install "scrudful[drf]"      # Django >= 4.2, djangorestframework >= 3.15
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
view.

Rules worth knowing:

* **Validators are opt-in.** Preconditions, and the 428 requirement, apply only
  when ``etag_func`` or ``last_modified_func`` is configured. Without them a
  decorated view behaves like an undecorated one, apart from the ``Link`` and
  expose headers.
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
  (304) and its existence (412 or 428 instead of 404).
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
  to be in ``settings.TIME_ZONE`` by the DRF adapter (the same thing when
  ``TIME_ZONE`` is ``"UTC"``). Prefer aware datetimes.
* **Link headers.** The schema and context links are added to every response
  your view returns and are appended to any ``Link`` value it set. Each ``*_or_func``
  argument is a value or a callable.

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

Callbacks are called as ``func(view, request, **url_kwargs)``. ``@scrudful_viewset``
reads its configuration from an inner ``Meta`` class (``list_*`` options
configure the list response):

.. code-block:: python

    from rest_framework import viewsets
    from rest_framework.reverse import reverse

    from scrudful.rest_framework import scrudful_viewset

    from items.models import Item
    from items.serializers import ItemSerializer


    # The callbacks run before the view's lookup: use its queryset.
    def item_etag(view, request, pk):
        items = view.get_queryset().filter(pk=pk)
        version = items.values_list("version", flat=True).first()
        return None if version is None else f"v{version}"


    def item_last_modified(view, request, pk):
        items = view.get_queryset().filter(pk=pk)
        return items.values_list("updated_at", flat=True).first()


    def item_schema_url(view, request, **kwargs):
        return reverse("item-schema", request=request)


    @scrudful_viewset
    class ItemViewSet(viewsets.ModelViewSet):
        queryset = Item.objects.all()
        serializer_class = ItemSerializer

        class Meta:
            etag_func = item_etag
            last_modified_func = item_last_modified
            schema_link_or_func = item_schema_url
            schema_type_or_func = "application/schema+json"

For single methods use ``@scrudful(...)`` on a view method or function view
(below ``@api_view``), and ``@scrudful_api_view(["GET", "PUT"], ...)`` as a
replacement for ``@api_view``. OPTIONS requests are answered with an
OpenAPI-like description of the methods the route serves
(``ScrudfulMetadata``). See ``examples/scrudful_rest_framework`` for a complete
project.

Caveats of ``scrudful_viewset``:

* The item link callbacks also run for ``create`` and for OPTIONS and POST on the
  list route, where there is no ``pk``. Accept ``**kwargs``, as
  ``item_schema_url`` does above.
* An explicit ``@scrudful`` on ``update`` applies to PATCH when
  ``partial_update`` is DRF's stock one (decorate a subclass of a decorated
  viewset with ``scrudful_viewset`` too, or it inherits the parent's wrapper). With a user-defined ``partial_update``
  that calls ``update``, the ``Meta``-configured wrapper of ``partial_update``
  wins: decorate ``partial_update`` explicitly to override it.
* OPTIONS uses ``ScrudfulMetadata`` only when the viewset's ``metadata_class``
  is DRF's default ``SimpleMetadata``. A custom ``metadata_class`` (or ``None``,
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
* FastAPI answers 405 to HEAD on a route declared with ``@app.get``. Use
  ``@app.api_route(path, methods=["GET", "HEAD"])`` to serve HEAD.
* To have a link callback build absolute URLs, declare a ``request: Request``
  parameter on the endpoint (``request.url_for(...)``).

See ``examples/fastapi/api.py`` for a complete application (run it with
``python examples/fastapi/api.py``).

Development
===========

.. code-block:: console

    $ uv sync
    $ uv run pytest
    $ uv run --group docs make -C docs html

License
=======

MIT. The conditional-request core adapts code from Django (BSD-3-Clause); its
license is in ``LICENSES/Django-BSD-3-Clause.txt``.
