"""``ScrudfulPagination``: the scrud list envelope for DRF."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import UnorderedObjectListWarning
from django.urls import include, path
from items.models import Item
from ninja import NinjaAPI, Router, Schema
from rest_framework import serializers, viewsets
from rest_framework.routers import SimpleRouter
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.pagination import PageNumberPagination
from rest_framework.test import APIClient, APIRequestFactory

from scrudful._hsr import hsr_link
from scrudful.collection import ENVELOPE_FIELDS, PAGE_FIELDS
from scrudful.ninja import scrudful_list
from scrudful.rest_framework import ScrudfulPagination, hsr_urls, scrudful_viewset

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


class ClampingPagination(SmallPagination):
    """A 0.6.0 subclass clamping page_size, as DRF's PageNumberPagination does."""

    def _integer(self, request, name, default, maximum):
        if name == self.page_size_query_param:
            raw = request.query_params.get(name)
            if raw is not None and raw.isdigit() and int(raw) > maximum:
                return maximum
        return super()._integer(request, name, default, maximum)


class ClampingViewSet(BareViewSet):
    pagination_class = ClampingPagination


class LastPagePagination(SmallPagination):
    """A 0.6.0 subclass accepting ``page=last``."""

    def _integer(self, request, name, default, maximum):
        if name == self.page_query_param and request.query_params.get(name) == "last":
            return Item.objects.count()
        return super()._integer(request, name, default, maximum)


class LastPageViewSet(BareViewSet):
    pagination_class = LastPagePagination


class LastPageParsePagination(SmallPagination):
    """``page=last`` through the parsing hook, not ``_integer``."""

    def _parse_parameter(self, request, name, default, maximum):
        if name == self.page_query_param and request.query_params.get(name) == "last":
            return Item.objects.count(), None
        return super()._parse_parameter(request, name, default, maximum)


class LastPageParseViewSet(BareViewSet):
    pagination_class = LastPageParsePagination


def none_modified(obj):
    return None


@scrudful_viewset
class NoneModifiedViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Item.objects.order_by("pk")
    serializer_class = ItemSerializer
    pagination_class = ScrudfulPagination

    class Meta:
        href_func = drf_href
        object_validators = True
        etag_func = item_etag
        last_modified_func = none_modified


def nested_schema(view, request, parent, pk=None):
    return f"/schemas/{parent}/{pk}"


@scrudful_viewset
class NestedViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Item.objects.order_by("pk")
    serializer_class = ItemSerializer
    pagination_class = ScrudfulPagination

    class Meta:
        href_func = drf_href
        schema_link_or_func = nested_schema


def listed_etag(view, request):
    return f"n{Item.objects.count()}"


@scrudful_viewset
class ListValidatedViewSet(viewsets.ReadOnlyModelViewSet):
    """A list with validators, one item a page."""

    queryset = Item.objects.order_by("pk")
    serializer_class = ItemSerializer
    pagination_class = SmallPagination

    class Meta:
        href_func = drf_href
        list_etag_func = listed_etag


class FirstOnly:
    """A filter backend keeping the first item: the page checked is the
    filtered list's."""

    def filter_queryset(self, request, queryset, view):
        return queryset.filter(pk=queryset.order_by("pk").values("pk")[:1])


class FilteredListViewSet(ListValidatedViewSet):
    filter_backends = [FirstOnly]


class OnePerPageNumber(PageNumberPagination):
    page_size = 1


class OtherPaginatorViewSet(ListValidatedViewSet):
    """A paginator without check_page: its page errors lose to a precondition,
    as before 0.10.1."""

    pagination_class = OnePerPageNumber


class OwnPagesPagination(SmallPagination):
    """Overrides paginate_queryset (its own idea of a page, say one past the
    last clamped to the last), not check_page."""

    def paginate_queryset(self, queryset, request, view=None):
        return super().paginate_queryset(queryset, request, view)


class OwnPagesViewSet(ListValidatedViewSet):
    pagination_class = OwnPagesPagination


class LastPageListViewSet(ListValidatedViewSet):
    """Parsing of its own (``page=last``), scrudful's idea of a page."""

    pagination_class = LastPagePagination


class LastPageParseListViewSet(ListValidatedViewSet):
    pagination_class = LastPageParsePagination


def listed_modified(view, request):
    return max(item.updated_at for item in Item.objects.all())


@scrudful_viewset
class LastModifiedOnlyViewSet(ListValidatedViewSet):
    # Decorated itself: scrudful_viewset reads the Meta of the class it
    # decorates.
    class Meta:
        href_func = drf_href
        list_last_modified_func = listed_modified


router = SimpleRouter()
router.register("nonemod", NoneModifiedViewSet, basename="nonemod")
router.register("listed", ListValidatedViewSet, basename="listed")
router.register("filtered", FilteredListViewSet, basename="filtered")
router.register("other-pages", OtherPaginatorViewSet, basename="other-pages")
router.register("own-pages", OwnPagesViewSet, basename="own-pages")
router.register("last-list", LastPageListViewSet, basename="last-list")
router.register("last-parse-list", LastPageParseListViewSet, basename="last-parse-list")
router.register("modified-only", LastModifiedOnlyViewSet, basename="modified-only")
router.register("items", ItemViewSet, basename="item")
router.register("bare", BareViewSet, basename="bare")
router.register("nohref", NoHrefViewSet, basename="nohref")
router.register("unordered", UnorderedViewSet, basename="unordered")
router.register("small", SmallViewSet, basename="small")
router.register("clamping", ClampingViewSet, basename="clamping")
router.register("lastpage", LastPageViewSet, basename="lastpage")
router.register("lastparse", LastPageParseViewSet, basename="lastparse")

ninja_router = Router()


@ninja_router.get("/nonemod", response=list[ItemOut])
@scrudful_list(
    href_func=ninja_href, etag_func=item_etag, last_modified_func=none_modified
)
def ninja_nonemod(request):
    return Item.objects.order_by("pk")


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
    path(
        "drf/p/<int:parent>/items/",
        NestedViewSet.as_view({"get": "list"}),
        name="nested-list",
    ),
    path(
        "drf/p/<int:parent>/items/<int:pk>/",
        NestedViewSet.as_view({"get": "retrieve"}),
        name="nested-detail",
    ),
    path("drf/", include(router.urls)),
    path("ninja/", api.urls),
    # Every response of a scrudful view links its HTTP Schema Resource.
    path("", include(hsr_urls())),
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


@pytest.mark.parametrize(
    "params, name, message, code",
    [
        ({"page": "x"}, "page", "A valid integer is required.", "invalid"),
        ({"page": "1.5"}, "page", "A valid integer is required.", "invalid"),
        ({"page": ""}, "page", "A valid integer is required.", "invalid"),
        (
            {"page": "0"},
            "page",
            "Ensure this value is greater than or equal to 1.",
            "min_value",
        ),
        ({"page_size": "y"}, "page_size", "A valid integer is required.", "invalid"),
        (
            {"page_size": "-1"},
            "page_size",
            "Ensure this value is greater than or equal to 1.",
            "min_value",
        ),
        (
            {"page_size": "3"},
            "page_size",
            "Ensure this value is less than or equal to 2.",
            "max_value",
        ),
    ],
)
def test_pagination_errors_carry_codes(items, params, name, message, code):
    request = Request(APIRequestFactory().get("/drf/small/", params))
    with pytest.raises(ValidationError) as caught:
        SmallPagination().paginate_queryset(Item.objects.all(), request, SmallViewSet())
    assert caught.value.detail == {name: [message]}
    assert caught.value.get_codes() == {name: [code]}


def test_pagination_reports_both_bad_parameters(items, client):
    response = client.get("/drf/small/", {"page": "x", "page_size": "3"})
    assert response.status_code == 400
    assert response.json() == {
        "page": ["A valid integer is required."],
        "page_size": ["Ensure this value is less than or equal to 2."],
    }


def test_pagination_integer_helper_still_raises_with_code(items):
    request = Request(APIRequestFactory().get("/drf/small/", {"page_size": "0"}))
    with pytest.raises(ValidationError) as caught:
        SmallPagination()._integer(request, "page_size", 1, 2)
    assert caught.value.get_codes() == {"page_size": ["min_value"]}
    request = Request(APIRequestFactory().get("/drf/small/", {"page_size": "2"}))
    assert SmallPagination()._integer(request, "page_size", 1, 2) == 2
    assert SmallPagination()._integer(request, "page", 7, None) == 7


def test_pagination_subclass_integer_override_clamps(items, client):
    response = client.get("/drf/clamping/", {"page_size": 5000})
    assert response.status_code == 200
    assert len(response.json()["content"]) == 2
    # An overridden _integer: the first error only, as in 0.6.0.
    first = client.get("/drf/clamping/", {"page": "x", "page_size": "0"})
    assert first.status_code == 400
    assert first.json() == {"page": ["A valid integer is required."]}


def test_pagination_subclass_integer_override_accepts_last(items, client):
    response = client.get("/drf/lastpage/", {"page": "last"})
    assert response.status_code == 200
    body = response.json()
    assert [entry["content"]["name"] for entry in body["content"]] == ["c"]
    bad = client.get("/drf/lastpage/", {"page": "last", "page_size": "3"})
    assert bad.status_code == 400
    assert bad.json() == {
        "page_size": ["Ensure this value is less than or equal to 2."]
    }


@pytest.mark.parametrize("url", ["/drf/lastpage/", "/drf/lastparse/"])
def test_pagination_page_last_through_either_hook(items, client, url):
    response = client.get(url, {"page": "last"})
    assert response.status_code == 200
    assert [e["content"]["name"] for e in response.json()["content"]] == ["c"]
    bad = client.get(url, {"page": "x", "page_size": "3"})
    assert bad.status_code == 400
    page_error = {"page": ["A valid integer is required."]}
    if url == "/drf/lastparse/":
        # The default _integer: both parameters' errors.
        page_error["page_size"] = ["Ensure this value is less than or equal to 2."]
    assert bad.json() == page_error


def test_pagination_overridden_integer_stops_at_the_first_error(items):
    """0.6.0's order: an override is not called for page_size once page failed."""
    calls = []

    class Recording(ClampingPagination):
        def _integer(self, request, name, default, maximum):
            calls.append(name)
            return super()._integer(request, name, default, maximum)

    request = Request(APIRequestFactory().get("/", {"page": "x", "page_size": "y"}))
    with pytest.raises(ValidationError) as caught:
        Recording().paginate_queryset(Item.objects.all(), request, SmallViewSet())
    assert caught.value.get_codes() == {"page": ["invalid"]}
    assert calls == ["page"]


def test_pagination_parse_parameter_returns_value_and_error(items):
    def parse(params, name, maximum=None):
        request = Request(APIRequestFactory().get("/drf/small/", params))
        return SmallPagination()._parse_parameter(request, name, 7, maximum)

    assert parse({}, "page") == (7, None)
    assert parse({"page": " 2.0 "}, "page") == (2, None)
    value, error = parse({"page": "٣"}, "page")
    assert value is None and error.code == "invalid"
    value, error = parse({"page_size": "3"}, "page_size", 2)
    assert value is None and error.code == "max_value"


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


def test_none_last_modified_is_null_like_ninja(items, client):
    drf = client.get("/drf/nonemod/").json()
    ninja = client.get("/ninja/nonemod").json()
    assert [e["last_modified"] for e in drf["content"]] == [None] * 3
    assert [e["last_modified"] for e in ninja["content"]] == [None] * 3
    assert [e["etag"] for e in drf["content"]] == [e["etag"] for e in ninja["content"]]


def test_entry_link_callbacks_get_the_list_route_kwargs(items, client):
    response = client.get("/drf/p/7/items/")
    assert response.status_code == 200
    for item, entry in zip(items, response.json()["content"]):
        detail = client.get(f"/drf/p/7/items/{item.pk}/")
        assert detail.status_code == 200
        # The item's Links, but the link to the detail route's HTTP Schema
        # Resource, which an item's response carries besides.
        hsr = hsr_link("http://testserver/_hsr/nested-detail")
        assert detail["Link"] == f"{entry['link']}, {hsr}"
        assert f"/schemas/7/{item.pk}" in entry["link"]


# A list's page comes before its preconditions (RFC 9110 13.2.1): a request
# whose page is a 400 or 404 is answered so, its preconditions ignored.


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize(
    "query, status", [("?page=99", 404), ("?page=x", 400), ("?page_size=0", 400)]
)
@pytest.mark.parametrize("headers", [{"If-None-Match": "*"}, {"If-Match": '"no"'}])
def test_a_list_page_that_is_an_error_ignores_the_preconditions(
    method, query, status, headers
):
    for name in "abc":
        Item.objects.create(name=name)

    response = getattr(APIClient(), method)("/drf/listed/" + query, headers=headers)

    assert response.status_code == status


def test_a_list_page_that_exists_answers_its_preconditions():
    for name in "abc":
        Item.objects.create(name=name)
    client = APIClient()

    page_1 = client.get("/drf/listed/", headers={"If-None-Match": '"n3"'})
    page_3 = client.get("/drf/listed/?page=3", headers={"If-None-Match": '"n3"'})
    stale = client.get("/drf/listed/?page=2", headers={"If-Match": '"n2"'})

    assert page_1.status_code == page_3.status_code == 304
    assert stale.status_code == 412


def test_the_page_checked_is_the_filtered_lists():
    # Three items, one after the filter: page 2 doesn't exist.
    for name in "abc":
        Item.objects.create(name=name)

    response = APIClient().get(
        "/drf/filtered/?page=2", headers={"If-None-Match": '"n3"'}
    )

    assert response.status_code == 404


@pytest.mark.parametrize("query, status", [("", 304), ("?page=2", 404)])
def test_an_empty_list_has_its_first_page_only(query, status):
    response = APIClient().get("/drf/listed/" + query, headers={"If-None-Match": "*"})

    assert response.status_code == status


@pytest.mark.parametrize("prefix", ["other-pages", "own-pages"])
def test_a_paginator_of_its_own_still_loses_to_the_preconditions(prefix):
    # Without check_page, or overriding paginate_queryset without it: as
    # before 0.10.1, never asked.
    for name in "abc":
        Item.objects.create(name=name)

    response = APIClient().get(
        f"/drf/{prefix}/?page=99", headers={"If-None-Match": '"n3"'}
    )

    assert response.status_code == 304


def test_a_list_with_last_modified_only_checks_its_page():
    for name in "abc":
        Item.objects.create(name=name)
    client = APIClient()
    since = {"If-Modified-Since": client.get("/drf/modified-only/")["Last-Modified"]}

    assert client.get("/drf/modified-only/?page=99", headers=since).status_code == 404
    assert client.get("/drf/modified-only/?page=3", headers=since).status_code == 304


def test_an_unconditional_list_checks_no_page():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for name in "abc":
        Item.objects.create(name=name)
    client = APIClient()

    def queries(url):
        with CaptureQueriesContext(connection) as captured:
            assert client.get(url).status_code == 200
        return len(captured.captured_queries)

    assert queries("/drf/listed/?page=2") == queries("/drf/listed/")


@pytest.mark.parametrize("prefix", ["last-list", "last-parse-list"])
@pytest.mark.parametrize("query, status", [("?page=last", 304), ("?page=99", 404)])
def test_a_paginator_parsing_its_own_way_still_checks_its_page(prefix, query, status):
    # Overriding _integer or _parse_parameter (not paginate_queryset): asked,
    # with its own parsing.
    for name in "abc":
        Item.objects.create(name=name)

    response = APIClient().get(
        f"/drf/{prefix}/{query}", headers={"If-None-Match": '"n3"'}
    )

    assert response.status_code == status
