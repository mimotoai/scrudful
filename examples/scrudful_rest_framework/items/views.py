"""SCRUDful views of :class:`items.models.Item`.

Validator callbacks are called as ``func(view, request, **url_kwargs)`` and
return ``None`` when the item does not exist, so that the view itself answers
404. They run before the view's own lookup and permission checks, so they look
the item up in ``view.get_queryset()``: an item the view would not show must
not reveal its validators (304) or its existence (412/428 instead of 404).

Settings enable ``ATOMIC_REQUESTS``: the callbacks and the write run in one
transaction, and on PUT/PATCH/DELETE the callbacks lock the row with
``select_for_update()`` so a concurrent write cannot slip in between the
precondition check and the save (SQLite ignores the lock; use a database that
supports it in production).
"""

import hashlib

from rest_framework import viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.reverse import reverse

from items.models import Item
from items.serializers import ItemSerializer
from scrudful.rest_framework import scrudful_api_view, scrudful_viewset

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


def _item_value(view, request, pk, field):
    # The view's queryset, not Item.objects: apply the view's own scoping.
    items = view.get_queryset().filter(pk=pk)
    if request.method not in ("GET", "HEAD"):
        items = items.select_for_update()
    return items.values_list(field, flat=True).first()


def item_etag(view, request, pk):
    version = _item_value(view, request, pk, "version")
    return None if version is None else f"v{version}"


def item_last_modified(view, request, pk):
    return _item_value(view, request, pk, "updated_at")


def collection_etag(view, request):
    """An ETag that changes whenever an item is created, changed or deleted."""
    # Viewsets list their own queryset; the stats function view covers all items.
    items = view.get_queryset() if hasattr(view, "get_queryset") else Item.objects
    rows = list(items.order_by("pk").values_list("pk", "version"))
    return hashlib.sha256(repr(rows).encode()).hexdigest()[:32]


def item_schema_url(view, request, **kwargs):
    return reverse("item-schema", request=request)


def item_context_url(view, request, **kwargs):
    return reverse("item-context", request=request)


@scrudful_viewset
class ItemViewSet(viewsets.ModelViewSet):
    """Search, create, read, update and delete items.

    PUT, PATCH and DELETE must carry ``If-Match`` (or ``If-Unmodified-Since``).
    """

    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    lookup_value_regex = "[0-9]+"

    class Meta:
        etag_func = item_etag
        last_modified_func = item_last_modified
        schema_link_or_func = item_schema_url
        schema_type_or_func = "application/schema+json"
        context_link_or_func = item_context_url
        list_etag_func = collection_etag


@scrudful_viewset
class ReadOnlyItemViewSet(viewsets.ReadOnlyModelViewSet):
    """Search and read items."""

    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    lookup_value_regex = "[0-9]+"

    class Meta:
        etag_func = item_etag
        last_modified_func = item_last_modified
        schema_link_or_func = item_schema_url
        schema_type_or_func = "application/schema+json"
        list_etag_func = collection_etag


@scrudful_api_view(["GET"], etag_func=collection_etag)
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
