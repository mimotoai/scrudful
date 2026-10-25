"""SCRUDful views of :class:`items.models.Item`.

Validator callbacks are called as ``func(view, request, **url_kwargs)``, with
every URL kwarg (DRF's ``format`` too, on the format-suffix routes the router
adds: hence the ``**kwargs``), and return ``None`` when the item does not
exist, so that the view itself answers 404. They run before the view's own
lookup and permission checks, so they look the item up in
``view.get_queryset()``: an item the view would not show must not reveal its
validators (304) or its existence (412/428 instead of 404).

Settings enable ``ATOMIC_REQUESTS``: the callbacks and the write run in one
transaction, and on PUT/PATCH/DELETE the callbacks lock the row with
``select_for_update()`` so a concurrent write cannot slip in between the
precondition check and the save (SQLite ignores the lock; use a database that
supports it in production).
"""

import hashlib

from django.urls import reverse
from rest_framework import viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response

from items.models import Item
from items.serializers import ItemSerializer
from scrudful.rest_framework import Entity, scrudful_api_view, scrudful_viewset

ITEM_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Item",
    "type": "object",
    "properties": {
        "id": {"type": "integer", "readOnly": True},
        "url": {"type": "string", "format": "uri", "readOnly": True},
        "name": {"type": "string", "maxLength": 100},
        "description": {"type": "string"},
        "version": {"type": "integer", "readOnly": True},
        "created_at": {"type": "string", "format": "date-time", "readOnly": True},
        "updated_at": {"type": "string", "format": "date-time", "readOnly": True},
    },
    "required": ["name"],
}

ITEM_CONTEXT = {
    "@context": {
        "@vocab": "https://schema.org/",
        "url": "@id",
        "created_at": "dateCreated",
        "updated_at": "dateModified",
    }
}


ITEMS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Items",
    "type": "array",
    "items": {"$ref": "item.json"},
}

STATS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Item statistics",
    "type": "object",
    "properties": {
        "count": {"type": "integer", "minimum": 0},
        "latest_update": {"type": ["string", "null"], "format": "date-time"},
    },
    "required": ["count", "latest_update"],
}

STATS_CONTEXT = {
    "@context": {
        "@vocab": "https://schema.org/",
        "count": "numberOfItems",
        "latest_update": "dateModified",
    }
}

# What DRF answers on an error: ``{"detail": "..."}``, or the serializer's
# ``{"<field>": ["..."]}``.
ERROR_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Error",
    "type": "object",
    "properties": {"detail": {"type": "string"}},
    "additionalProperties": {"type": "array", "items": {"type": "string"}},
}

ERROR_CONTEXT = {"@context": {"@vocab": "https://schema.org/", "detail": "description"}}


def _item_value(view, request, pk, field):
    # The view's queryset, not Item.objects: apply the view's own scoping.
    items = view.get_queryset().filter(pk=pk)
    if request.method not in ("GET", "HEAD"):
        items = items.select_for_update()
    return items.values_list(field, flat=True).first()


def item_etag(view, request, pk, **kwargs):
    version = _item_value(view, request, pk, "version")
    return None if version is None else f"v{version}"


def item_last_modified(view, request, pk, **kwargs):
    return _item_value(view, request, pk, "updated_at")


def collection_etag(view, request, **kwargs):
    """An ETag that changes whenever an item is created, changed or deleted."""
    # Viewsets list their own queryset; the stats function view covers all items.
    items = view.get_queryset() if hasattr(view, "get_queryset") else Item.objects
    rows = list(items.order_by("pk").values_list("pk", "version"))
    return hashlib.sha256(repr(rows).encode()).hexdigest()[:32]


# Django's ``reverse`` gives a path, which scrudful resolves against the
# request. DRF's ``reverse(..., request=request)`` would keep the request's
# ``?format=`` in the link targets, which then answer 404.
def item_schema_url(view, request, **kwargs):
    return reverse("item-schema")


def item_context_url(view, request, **kwargs):
    return reverse("item-context")


def items_schema_url(view, request, **kwargs):
    return reverse("items-schema")


def stats_schema_url(view, request, **kwargs):
    return reverse("stats-schema")


def stats_context_url(view, request, **kwargs):
    return reverse("stats-context")


# The body of every error: scrudful's own 400/412/428 and, through
# ``scrudful.rest_framework.exception_handler`` (see the settings), DRF's.
ERROR = Entity(
    schema_link_or_func=lambda view, request, **kwargs: reverse("error-schema"),
    context_link_or_func=lambda view, request, **kwargs: reverse("error-context"),
)


class ItemMeta:
    """The configuration of both viewsets: items, and the list of items."""

    etag_func = item_etag
    last_modified_func = item_last_modified
    schema_link_or_func = item_schema_url
    context_link_or_func = item_context_url
    error = ERROR
    list_etag_func = collection_etag
    list_schema_link_or_func = items_schema_url
    list_context_link_or_func = item_context_url
    list_error = ERROR


@scrudful_viewset
class ItemViewSet(viewsets.ModelViewSet):
    """Search, create, read, update and delete items.

    PUT, PATCH and DELETE must carry ``If-Match`` (or ``If-Unmodified-Since``).
    """

    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    lookup_value_regex = "[0-9]+"

    Meta = ItemMeta


@scrudful_viewset
class ReadOnlyItemViewSet(viewsets.ReadOnlyModelViewSet):
    """Search and read items."""

    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    lookup_value_regex = "[0-9]+"

    Meta = ItemMeta


@scrudful_api_view(
    ["GET", "HEAD"],
    etag_func=collection_etag,
    schema_link_or_func=stats_schema_url,
    context_link_or_func=stats_context_url,
    error=ERROR,
)
def item_stats(request):
    """Number of items and the most recent modification."""
    latest = Item.objects.order_by("-updated_at").values_list("updated_at").first()
    return Response(
        {
            "count": Item.objects.count(),
            "latest_update": latest[0] if latest else None,
        }
    )


@api_view(["GET"])
def item_schema(request):
    """JSON Schema of an item."""
    return Response(ITEM_SCHEMA, content_type="application/schema+json")


@api_view(["GET"])
def item_context(request):
    """JSON-LD context of an item."""
    return Response(ITEM_CONTEXT, content_type="application/ld+json")


@api_view(["GET"])
def items_schema(request):
    """JSON Schema of the list of items."""
    return Response(ITEMS_SCHEMA, content_type="application/schema+json")


@api_view(["GET"])
def stats_schema(request):
    """JSON Schema of the item statistics."""
    return Response(STATS_SCHEMA, content_type="application/schema+json")


@api_view(["GET"])
def stats_context(request):
    """JSON-LD context of the item statistics."""
    return Response(STATS_CONTEXT, content_type="application/ld+json")


@api_view(["GET"])
def error_schema(request):
    """JSON Schema of an error."""
    return Response(ERROR_SCHEMA, content_type="application/schema+json")


@api_view(["GET"])
def error_context(request):
    """JSON-LD context of an error."""
    return Response(ERROR_CONTEXT, content_type="application/ld+json")
