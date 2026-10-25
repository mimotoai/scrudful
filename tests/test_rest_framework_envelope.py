"""``ScrudfulPagination``: the scrud list envelope for DRF."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import UnorderedObjectListWarning
from django.urls import include, path
from items.models import Item
from ninja import NinjaAPI, Router, Schema
from rest_framework import serializers, viewsets
from rest_framework.routers import SimpleRouter
from rest_framework.test import APIClient

from scrudful.collection import ENVELOPE_FIELDS, PAGE_FIELDS
from scrudful.ninja import scrudful_list
from scrudful.rest_framework import ScrudfulPagination, scrudful_viewset

pytestmark = [pytest.mark.urls(__name__), pytest.mark.django_db]

SCHEMA_URL = "/schemas/item"
CONTEXT_URL = "/contexts/item"


class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ["id", "name", "version"]


class ItemOut(Schema):
    id: int
    name: str
    version: int


def drf_href(view, request, obj):
    return request.build_absolute_uri(f"/items/{obj.pk}")


def ninja_href(request, obj):
    return request.build_absolute_uri(f"/items/{obj.pk}")


def item_etag(obj):
    return obj.version


def item_modified(obj):
    return obj.updated_at


@scrudful_viewset
class ItemViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Item.objects.order_by("pk")
    serializer_class = ItemSerializer
    pagination_class = ScrudfulPagination

    class Meta:
        href_func = drf_href
        object_validators = True
        etag_func = item_etag
        last_modified_func = item_modified
        schema_link_or_func = SCHEMA_URL
        context_link_or_func = CONTEXT_URL


@scrudful_viewset
class BareViewSet(viewsets.ReadOnlyModelViewSet):
    """No validators: etag and last_modified are null."""

    queryset = Item.objects.order_by("pk")
    serializer_class = ItemSerializer
    pagination_class = ScrudfulPagination

    class Meta:
        href_func = drf_href


class NoHrefViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Item.objects.order_by("pk")
    serializer_class = ItemSerializer
    pagination_class = ScrudfulPagination


class UnorderedViewSet(BareViewSet):
    queryset = Item.objects.order_by()


class SmallPagination(ScrudfulPagination):
    page_size = 1
    max_page_size = 2


class SmallViewSet(BareViewSet):
    pagination_class = SmallPagination


router = SimpleRouter()
router.register("items", ItemViewSet, basename="item")
router.register("bare", BareViewSet, basename="bare")
router.register("nohref", NoHrefViewSet, basename="nohref")
router.register("unordered", UnorderedViewSet, basename="unordered")
router.register("small", SmallViewSet, basename="small")

ninja_router = Router()


@ninja_router.get("/items", response=list[ItemOut])
@scrudful_list(
    href_func=ninja_href,
    etag_func=item_etag,
    last_modified_func=item_modified,
    schema_link_or_func=SCHEMA_URL,
    context_link_or_func=CONTEXT_URL,
)
def ninja_items(request):
    return Item.objects.order_by("pk")


api = NinjaAPI(urls_namespace="test-drf-envelope")
api.add_router("", ninja_router)

urlpatterns = [
    path("drf/", include(router.urls)),
    path("ninja/", api.urls),
]


@pytest.fixture
def items():
    return [Item.objects.create(name=name) for name in ("a", "b", "c")]


@pytest.fixture
def client():
    return APIClient()


def test_envelope_matches_scrudful_list(items, client):
    query = {"page": 2, "page_size": 2, "q": "x"}
    drf = client.get("/drf/items/", query)
    ninja = client.get("/ninja/items", query)
    assert drf.status_code == ninja.status_code == 200
    body = drf.json()
    assert list(body) == list(PAGE_FIELDS)
    assert body["content"] and list(body["content"][0]) == list(ENVELOPE_FIELDS)
    expected = ninja.json()

    def normalize(value):
        if isinstance(value, str):
            return value.replace("/drf/items/", "/P").replace("/ninja/items", "/P")
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        return value

    assert normalize(body) == normalize(expected)
    assert body["content"][0]["etag"] == '"1"'


def test_entries_carry_item_links(items, client):
    body = client.get("/drf/items/").json()
    link = (
        '<http://testserver/schemas/item>; rel="describedby"; '
        'type="application/schema+json", '
        "<http://testserver/contexts/item>; "
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )
    assert [entry["link"] for entry in body["content"]] == [link] * 3
    assert body["content"][0]["href"] == f"http://testserver/items/{items[0].pk}"


def test_entries_without_validators_or_links(items, client):
    body = client.get("/drf/bare/").json()
    assert body["count"] == 3
    for entry in body["content"]:
        assert entry["etag"] is None
        assert entry["last_modified"] is None
        assert entry["link"] is None


def test_pagination_invalid_page_is_404(items, client):
    response = client.get("/drf/bare/", {"page": 2})
    assert response.status_code == 404
    assert response.json() == {"detail": "Invalid page."}


def test_pagination_page_one_exists_when_empty(client):
    body = client.get("/drf/bare/").json()
    assert body["count"] == 0
    assert body["page_count"] == 0
    assert body["content"] == []


@pytest.mark.parametrize("name", ["page", "page_size"])
@pytest.mark.parametrize("value", ["x", "0", "-1", "1.5", ""])
def test_pagination_bad_values_are_400(items, client, name, value):
    response = client.get("/drf/bare/", {name: value})
    assert response.status_code == 400
    assert list(response.json()) == [name]


def test_pagination_bad_page_size_is_400(items, client):
    response = client.get("/drf/small/", {"page_size": 3})
    assert response.status_code == 400
    assert list(response.json()) == ["page_size"]


def test_pagination_max_page_size(items, client):
    body = client.get("/drf/small/", {"page_size": 2}).json()
    assert len(body["content"]) == 2
    assert body["page_count"] == 2
    default = client.get("/drf/small/").json()
    assert len(default["content"]) == 1
    assert default["page_count"] == 3


def test_pagination_default_page_size(items, client):
    assert ScrudfulPagination.page_size == 100
    assert ScrudfulPagination.max_page_size == 1000
    assert client.get("/drf/bare/", {"page_size": 1000}).status_code == 200
    assert client.get("/drf/bare/", {"page_size": 1001}).status_code == 400


def test_pagination_warns_on_unordered_queryset(items, client):
    with pytest.warns(UnorderedObjectListWarning, match="Item") as record:
        response = client.get("/drf/unordered/")
    assert response.status_code == 200
    assert record[0].filename == __import__("rest_framework.generics").generics.__file__


def test_href_func_missing_is_improperly_configured(items, client):
    with pytest.raises(ImproperlyConfigured, match="href_func"):
        client.get("/drf/nohref/")


def test_schema_describes_the_envelope():
    schema = ScrudfulPagination().get_paginated_response_schema(
        {"type": "array", "items": {"type": "object"}}
    )
    assert list(schema["properties"]) == list(PAGE_FIELDS)
    entry = schema["properties"]["content"]["items"]
    assert list(entry["properties"]) == list(ENVELOPE_FIELDS)
    assert entry["properties"]["content"] == {"type": "object"}
