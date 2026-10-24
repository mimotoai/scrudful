"""List operations: ``scrudful_list`` (scrud envelope, pagination, Links)."""

import inspect
import uuid
from typing import Any, List, Optional, Union

import pytest
from jsonschema import Draft202012Validator
from asgiref.sync import async_to_sync
from django.core.paginator import UnorderedObjectListWarning
from django.http import HttpResponse
from django.urls import path
from items.models import Item
from ninja import NinjaAPI, Router, Status
from ninja.errors import ConfigError, HttpError
from ninja.testing import TestAsyncClient, TestClient
from referencing import Registry, Resource

from scrudful.collection import ENVELOPE_FIELDS, PAGE_FIELDS, collection_schema
from scrudful.ninja import Entity, scrudful, scrudful_list
from tests.test_ninja_adapter import (
    ALICE,
    CONTEXT_LINK,
    LINKS,
    SCHEMA_LINK,
    ItemOut,
    Message,
    Store,
    expose,
    make_api,
)
from tests.test_ninja_entities import ERROR, ERROR_LINK

ITEM_LINK = f"{SCHEMA_LINK}, {CONTEXT_LINK}"
LIST_LINKS = dict(
    list_schema_link_or_func="/schemas/items",
    list_context_link_or_func="/contexts/items",
)
LIST_LINK = (
    '</schemas/items>; rel="describedby"; type="application/schema+json", '
    '</contexts/items>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
# ninja's TestClient builds absolute URIs without the path.
BASE = "http://testlocation/"


def href(request: Any, item: Any) -> str:
    return request.build_absolute_uri(f"/items/{item.pk}")


def item_etag(item: Item) -> int:
    return item.version


def item_modified(item: Item) -> Any:
    return item.updated_at


ITEM_OPTIONS = dict(
    etag_func=item_etag,
    last_modified_func=item_modified,
    **LINKS,
)


@pytest.fixture
def items(db) -> list[Item]:
    return [Item.objects.create(name=name) for name in ("a", "b", "c")]


def list_router(router: Router | None = None, **options: Any) -> Router:
    """``GET /items`` listing every item, and the detail ``GET /items/{pk}``."""
    router = router or Router()
    options = {"href_func": href, **ITEM_OPTIONS, **LIST_LINKS, **options}

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(**options)
    def list_items(request):
        return Item.objects.all()

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=lambda request, pk: Item.objects.get(pk=pk), **ITEM_OPTIONS)
    def get_item(request, pk: int, obj: Item):
        return obj

    return router


def test_list_envelope_shape(items):
    client = TestClient(list_router())
    response = client.get("/items?page_size=2")
    assert response.status_code == 200
    body = response.json()
    assert list(body) == list(PAGE_FIELDS)
    assert body["count"] == 3
    assert body["page_count"] == 2
    assert body["first"] == f"{BASE}?page=1&page_size=2"
    assert body["previous"] is None
    assert body["next"].endswith("?page=2&page_size=2")
    assert body["next"] == f"{BASE}?page=2&page_size=2"
    assert body["last"] == f"{BASE}?page=2&page_size=2"
    assert len(body["content"]) == 2
    for entry, item in zip(body["content"], items):
        assert list(entry) == list(ENVELOPE_FIELDS)
        assert entry["href"] == f"http://testlocation/items/{item.pk}"
        assert entry["content"] == {"id": item.pk, "name": item.name, "version": 1}
        detail = client.get(f"/items/{item.pk}")
        assert detail.status_code == 200
        assert entry["etag"] == detail.headers["ETag"]
        assert entry["last_modified"] == detail.headers["Last-Modified"]
        assert entry["link"] == detail.headers["Link"] == ITEM_LINK

    response = client.get("/items?page_size=2&page=2")
    body = response.json()
    assert body["previous"] == f"{BASE}?page=1&page_size=2"
    assert body["next"] is None
    assert [entry["content"]["id"] for entry in body["content"]] == [items[2].pk]


def django_api() -> NinjaAPI:
    api = NinjaAPI(urls_namespace="test-ninja-list")
    api.add_router("", list_router())
    return api


# For Django's test client: real absolute URIs.
urlpatterns = [path("api/", django_api().urls)]


@pytest.mark.urls(__name__)
def test_list_page_urls_with_django_request(items, client):
    response = client.get("/api/items", {"page_size": 1, "page": 2, "q": "é"})
    assert response.status_code == 200
    body = response.json()
    base = "http://testserver/api/items"
    assert body["first"] == f"{base}?page=1&page_size=1&q=%C3%A9"
    assert body["previous"] == f"{base}?page=1&page_size=1&q=%C3%A9"
    assert body["next"] == f"{base}?page=3&page_size=1&q=%C3%A9"
    assert body["last"] == f"{base}?page=3&page_size=1&q=%C3%A9"
    assert body["content"][0]["href"] == f"http://testserver/items/{items[1].pk}"


def test_list_default_page_size_and_other_query_parameters(items):
    response = TestClient(list_router(page_size=1)).get("/items?q=x&page=2&b=1&b=2")
    body = response.json()
    assert body["page_count"] == 3
    # Sorted by name, like DRF's replace_query_param; the others are kept.
    assert body["first"] == f"{BASE}?b=1&b=2&page=1&q=x"
    assert body["previous"] == f"{BASE}?b=1&b=2&page=1&q=x"
    assert body["next"] == f"{BASE}?b=1&b=2&page=3&q=x"
    assert body["last"] == f"{BASE}?b=1&b=2&page=3&q=x"
    assert [entry["content"]["id"] for entry in body["content"]] == [items[1].pk]


def test_list_scoped_by_handler_queryset(items):
    hidden = items[1]
    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=href, **ITEM_OPTIONS)
    def list_items(request):
        return Item.objects.exclude(pk=hidden.pk)

    body = TestClient(router).get("/items").json()
    assert body["count"] == 2
    assert body["page_count"] == 1
    assert all(f"/{hidden.pk}" not in entry["href"] for entry in body["content"])
    assert [entry["content"]["id"] for entry in body["content"]] == [
        items[0].pk,
        items[2].pk,
    ]


def test_list_sequence():
    rows = [{"id": n, "name": f"n{n}", "version": n} for n in range(1, 6)]
    router = Router()

    @router.get("/rows", response=list[ItemOut])
    @scrudful_list(
        href_func=lambda request, row: f"/rows/{row['id']}",
        etag_func=lambda row: f"v{row['version']}",
    )
    def list_rows(request):
        return rows

    body = TestClient(router).get("/rows?page=2&page_size=2").json()
    assert body["count"] == 5
    assert body["page_count"] == 3
    assert body["content"] == [
        {
            "href": f"/rows/{n}",
            "etag": f'"v{n}"',
            "last_modified": None,
            "link": None,
            "content": rows[n - 1],
        }
        for n in (3, 4)
    ]


def test_list_empty():
    router = Router()

    @router.get("/rows", response=list[ItemOut])
    @scrudful_list(href_func=lambda request, row: "/x")
    def list_rows(request):
        return []

    client = TestClient(router)
    body = client.get("/rows").json()
    assert body == {
        "count": 0,
        "page_count": 0,
        "first": f"{BASE}?page=1",
        "previous": None,
        "next": None,
        "last": f"{BASE}?page=1",
        "content": [],
    }
    assert client.get("/rows?page=2").status_code == 404


def test_list_headers(items):
    response = TestClient(list_router()).get("/items")
    assert response.status_code == 200
    assert response.headers["Link"] == LIST_LINK
    assert "ETag" not in response.headers
    assert "Last-Modified" not in response.headers
    assert expose(response) == {"Link"}

    response = TestClient(
        list_router(
            list_etag_func=lambda qs: f"n{qs.count()}",
            list_last_modified_func=lambda qs: max(i.updated_at for i in qs),
        )
    ).get("/items")
    assert response.status_code == 200
    assert response.headers["Link"] == LIST_LINK
    assert response.headers["ETag"] == '"n3"'
    assert "Last-Modified" in response.headers
    assert expose(response) == {"ETag", "Link"}


def test_list_link_callables_get_queryset_and_item(items):
    seen: list[tuple[str, Any]] = []

    def list_link(request, qs):
        seen.append(("list", qs))
        return "/schemas/items"

    def item_link(request, item):
        seen.append(("item", item))
        return "/schemas/item"

    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(
        href_func=href,
        schema_link_or_func=item_link,
        list_schema_link_or_func=list_link,
    )
    def list_items(request):
        return Item.objects.all()

    response = TestClient(router).get("/items")
    assert response.headers["Link"] == (
        '</schemas/items>; rel="describedby"; type="application/schema+json"'
    )
    assert [kind for kind, _ in seen] == ["item", "item", "item", "list"]
    assert [obj for kind, obj in seen if kind == "item"] == items
    assert list(seen[-1][1]) == items


def test_list_validators_304(items):
    calls = []

    def list_etag(qs):
        calls.append("list_etag")
        return f"n{qs.count()}"

    def item_href(request, item):
        calls.append("href")
        return href(request, item)

    router = list_router(href_func=item_href, list_etag_func=list_etag)

    client = TestClient(router)
    response = client.get("/items", headers={"If-None-Match": '"n3"'})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["ETag"] == '"n3"'
    assert "Link" not in response.headers
    # Evaluated after the handler, with its queryset; nothing else is built.
    assert calls == ["list_etag"]

    response = client.get("/items", headers={"If-None-Match": '"n2"'})
    assert response.status_code == 200
    assert response.headers["ETag"] == '"n3"'

    response = client.get("/items", headers={"If-Match": '"n2"'})
    assert response.status_code == 412
    assert "Link" not in response.headers


def test_list_early_responses_keep_handler_headers(items):
    """R12: the 304 keeps the handler's headers but Content-*; the 400 and 412
    keep only its cookies. The error entity's Links go on the error bodies."""
    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=href, list_etag_func=lambda qs: "n3", error=ERROR)
    def list_items(request, response: HttpResponse):
        response.set_cookie("seen", "1")
        response["Cache-Control"] = "public, max-age=60"
        response["Vary"] = "X-API-Key"
        response["Content-Language"] = "en"
        return Item.objects.all()

    client = TestClient(router)
    response = client.get("/items", headers={"If-None-Match": '"n3"'})
    assert response.status_code == 304
    assert response.headers["Cache-Control"] == "public, max-age=60"
    assert response.headers["Vary"] == "X-API-Key"
    assert response.cookies["seen"].value == "1"
    assert response.headers["ETag"] == '"n3"'
    assert "Content-Type" not in response.headers
    assert "Content-Language" not in response.headers
    assert expose(response) == {"ETag"}

    for headers, status in (({"If-Match": '"n2"'}, 412), ({"If-Match": "bad"}, 400)):
        response = client.get("/items", headers=headers)
        assert response.status_code == status
        assert response.cookies["seen"].value == "1"
        assert "Cache-Control" not in response.headers
        assert "Vary" not in response.headers
        assert "Content-Language" not in response.headers
        assert response.headers["Content-Type"] == "application/json"
        assert response.headers["Link"] == ERROR_LINK


def test_list_tuple_of_rows():
    rows = tuple({"id": n, "name": f"n{n}", "version": n} for n in range(1, 4))
    router = Router()

    @router.get("/rows", response=list[ItemOut])
    @scrudful_list(href_func=lambda request, row: f"/rows/{row['id']}")
    def list_rows(request):
        return rows

    response = TestClient(router).get("/rows?page_size=2")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert [entry["content"] for entry in body["content"]] == list(rows[:2])


def test_list_href_func_none_raises(items):
    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=lambda request, item: None)
    def list_items(request):
        return Item.objects.all()

    with pytest.raises(TypeError, match="href_func returned None"):
        TestClient(router).get("/items")


def test_list_without_list_validators_ignores_if_headers(items):
    response = TestClient(list_router()).get("/items", headers={"If-None-Match": "*"})
    assert response.status_code == 200
    response = TestClient(list_router()).get("/items", headers={"If-Match": "bad"})
    assert response.status_code == 200


def test_list_status_and_other_responses(items):
    router = Router()

    @router.get("/items", response={200: list[ItemOut], 403: Message})
    @scrudful_list(href_func=href, **LIST_LINKS)
    def list_items(request, deny: bool = False):
        if deny:
            return Status(403, {"detail": "no"})
        return Status(200, Item.objects.all())

    client = TestClient(router)
    response = client.get("/items")
    assert response.status_code == 200
    assert response.json()["count"] == 3
    assert response.headers["Link"] == LIST_LINK
    response = client.get("/items?deny=true")
    assert response.status_code == 403
    assert response.json() == {"detail": "no"}
    # Not the collection: no Link (and no pagination).
    assert "Link" not in response.headers


def test_list_openapi_is_envelope(items):
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", list_router())
    schema = api.get_openapi_schema(path_prefix="")
    operation = schema["paths"]["/items"]["get"]
    assert operation["responses"][200]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ScrudfulItemOutPage"
    }
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert list(parameters) == ["page", "page_size"]
    assert parameters["page"]["in"] == "query"
    assert parameters["page"]["schema"]["minimum"] == 1
    assert parameters["page_size"]["schema"]["default"] == 100
    assert parameters["page_size"]["schema"]["maximum"] == 1000
    components = schema["components"]["schemas"]
    page = components["ScrudfulItemOutPage"]
    assert list(page["properties"]) == list(PAGE_FIELDS)
    assert sorted(page["required"]) == sorted(PAGE_FIELDS)
    assert page["properties"]["content"]["items"] == {
        "$ref": "#/components/schemas/ScrudfulItemOutEnvelope"
    }
    envelope = components["ScrudfulItemOutEnvelope"]
    assert list(envelope["properties"]) == list(ENVELOPE_FIELDS)
    assert sorted(envelope["required"]) == sorted(ENVELOPE_FIELDS)
    assert envelope["properties"]["content"] == {"$ref": "#/components/schemas/ItemOut"}


def test_list_page_models_shared_per_item_schema():
    router = Router()
    for route in ("/a", "/b"):

        @router.get(route, response=list[ItemOut])
        @scrudful_list(href_func=href)
        def list_items(request):
            return []

    first, second = (
        operation.response_models[200]
        for path_view in router.path_operations.values()
        for operation in path_view.operations
    )
    assert first.__annotations__["response"].__name__ == "ScrudfulItemOutPage"
    assert first.__annotations__["response"] is second.__annotations__["response"]


@pytest.mark.parametrize(
    "response",
    [ItemOut, {200: ItemOut}, None],
    ids=["schema", "dict", "none"],
)
def test_list_requires_collection_response(response):
    router = Router()
    options = {} if response is None else {"response": response}
    decorator = router.get("/items", **options)
    wrapped = scrudful_list(href_func=href)(lambda request: [])
    with pytest.raises(ConfigError, match="collection response"):
        decorator(wrapped)


@pytest.mark.parametrize(
    "response, match",
    [
        # Python 3.14 writes Optional[X] as X | None.
        (
            Optional[List[ItemOut]],
            r"(Optional\[List\[ItemOut\]\]|List\[ItemOut\] \| None) must be a list",
        ),
        (list[ItemOut] | None, r"list\[ItemOut\] \| None must be a list"),
        ({200: Union[list[ItemOut], Message]}, r"\[ItemOut\], Message\]|\| Message"),
        (list, "response list names no item type"),
        (List, "response List names no item type"),
        ({200: tuple}, "response tuple names no item type"),
    ],
    ids=["Optional", "or-None", "Union", "list", "List", "tuple"],
)
def test_list_collection_response_must_name_one_item_type(response, match):
    """The envelope is always sent, and its entries need an item schema."""
    router = Router()
    decorator = router.get("/items", response=response)
    wrapped = scrudful_list(href_func=href)(lambda request: [])
    with pytest.raises(ConfigError, match=match):
        decorator(wrapped)


def test_list_registered_on_several_paths(items):
    """Each registration keeps its own declared statuses and envelope code."""

    @scrudful_list(href_func=href, list_etag_func=lambda qs: "n", **LIST_LINKS)
    def list_items(request):
        if request.path.endswith("/b"):
            return Status(206, Item.objects.all())
        return Item.objects.all()

    for order in (("a", "b"), ("b", "a")):
        router = Router()
        for name in order:
            if name == "a":
                router.get("/a", response=list[ItemOut])(list_items)
            else:
                router.get("/b", response={200: Message, 206: list[ItemOut]})(
                    list_items
                )
        api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
        # Mounted twice: ninja clones the operations.
        api.add_router("/one", router, url_name_prefix="one")
        api.add_router("/two", router, url_name_prefix="two")
        client = TestClient(api)
        for prefix in ("/one", "/two"):
            for name, status in (("a", 200), ("b", 206)):
                response = client.get(f"{prefix}/{name}")
                assert response.status_code == status, (order, prefix, name)
                assert response.json()["count"] == 3
                assert response.headers["ETag"] == '"n"'
                assert response.headers["Link"] == LIST_LINK


@pytest.mark.parametrize(
    "query, status",
    [
        ("page=0", 422),
        ("page=x", 422),
        ("page_size=0", 422),
        ("page_size=5000", 422),
        ("page_size=y", 422),
        ("page=9", 404),
        ("page=2", 404),
        ("page=1", 200),
        ("page_size=1000", 200),
    ],
)
def test_pagination_bad_input(items, query, status):
    response = TestClient(list_router()).get(f"/items?{query}")
    assert response.status_code == status


def test_pagination_parameter_not_passed_to_handler(items):
    received = []
    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=href)
    def list_items(request, q: str = "", **kwargs):
        received.append(kwargs)
        return Item.objects.filter(name__contains=q)

    assert "scrudful_pagination" not in inspect.signature(list_items).parameters
    body = TestClient(router).get("/items?q=a&page_size=5").json()
    assert body["count"] == 1
    assert received == [{}]


def test_list_unordered_queryset_warns(items):
    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=href)
    def list_items(request):
        return Item.objects.order_by()

    with pytest.warns(UnorderedObjectListWarning, match="Item"):
        response = TestClient(router).get("/items")
    assert response.status_code == 200


def test_list_async_handler(items):
    async def async_href(request, item):
        return href(request, item)

    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(
        href_func=async_href,
        list_etag_func=lambda qs: f"n{qs.count()}",
        **ITEM_OPTIONS,
        **LIST_LINKS,
    )
    async def list_items(request):
        # Not evaluated here: the adapter does it off the event loop.
        return Item.objects.all()

    assert inspect.iscoroutinefunction(list_items)
    client = TestAsyncClient(router)
    response = async_to_sync(client.get)("/items?page_size=2&page=2")
    assert response.status_code == 200
    assert response.headers["ETag"] == '"n3"'
    assert response.headers["Link"] == LIST_LINK
    body = response.json()
    assert body["count"] == 3
    assert body["content"] == [
        {
            "href": f"http://testlocation/items/{items[2].pk}",
            "etag": '"1"',
            "last_modified": TestClient(list_router())
            .get(f"/items/{items[2].pk}")
            .headers["Last-Modified"],
            "link": ITEM_LINK,
            "content": {"id": items[2].pk, "name": "c", "version": 1},
        }
    ]
    response = async_to_sync(client.get)("/items", headers={"If-None-Match": '"n3"'})
    assert response.status_code == 304
    response = async_to_sync(client.get)("/items?page=3")
    assert response.status_code == 404


@pytest.fixture
def hops(monkeypatch) -> list[Any]:
    """The functions ``scrudful.ninja`` runs through ``sync_to_async``."""
    import scrudful.ninja

    calls: list[Any] = []
    original = scrudful.ninja.sync_to_async

    def counting(func, *args, **kwargs):
        calls.append(func)
        return original(func, *args, **kwargs)

    monkeypatch.setattr(scrudful.ninja, "sync_to_async", counting)
    return calls


def async_list_router(**options: Any) -> Router:
    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(**{"href_func": href, **options})
    async def list_items(request):
        return Item.objects.all()

    return router


def item_link(request, item):
    return f"/schemas/item/{item.pk}"


@pytest.mark.parametrize("size", [1, 2, 3])
def test_async_list_page_in_one_thread_hop(db, hops, size):
    """With sync per-item callables, the whole page takes one thread hop."""
    for name in "abcdef":
        Item.objects.create(name=name)
    client = TestAsyncClient(
        async_list_router(**ITEM_OPTIONS, context_rel_or_func=lambda r, i: "context")
    )
    response = async_to_sync(client.get)(f"/items?page_size={size}")
    assert response.status_code == 200
    assert len(response.json()["content"]) == size
    assert len(hops) == 1


def test_async_list_async_per_item_callable(db, hops):
    """An async per-item callable is awaited: the others take a hop each."""
    items = [Item.objects.create(name=name) for name in "abc"]

    async def async_etag(item):
        return f"a{item.version}"

    client = TestAsyncClient(
        async_list_router(etag_func=async_etag, schema_link_or_func=item_link)
    )
    response = async_to_sync(client.get)("/items")
    assert response.status_code == 200
    assert [entry["etag"] for entry in response.json()["content"]] == ['"a1"'] * 3
    assert [entry["link"] for entry in response.json()["content"]] == [
        f'</schemas/item/{item.pk}>; rel="describedby"; type="application/schema+json"'
        for item in items
    ]
    # The slice, then href_func and the link callable of each item.
    assert len(hops) == 1 + 2 * 3


def test_async_list_sync_callable_returning_an_awaitable(db, hops):
    """In the page's thread hop too, an awaitable a sync callable returns is
    awaited."""
    items = [Item.objects.create(name=name) for name in "ab"]

    def awaitable_href(request, item):
        async def value():
            return href(request, item)

        return value()

    client = TestAsyncClient(async_list_router(href_func=awaitable_href))
    response = async_to_sync(client.get)("/items")
    assert response.status_code == 200
    assert [entry["href"] for entry in response.json()["content"]] == [
        f"http://testlocation/items/{item.pk}" for item in items
    ]
    assert len(hops) == 1


def test_list_unauthenticated_401(items):
    store = Store()
    calls = []

    def counted_href(request, item):
        calls.append("href")
        return href(request, item)

    def list_etag(qs):
        calls.append("list_etag")
        return "n"

    client = TestClient(
        make_api(list_router(href_func=counted_href, list_etag_func=list_etag), store)
    )
    response = client.get("/items")
    assert response.status_code == 401
    assert "ETag" not in response.headers
    assert "Link" not in response.headers
    assert calls == []

    response = client.get("/items", headers=ALICE)
    assert response.status_code == 200
    assert response.json()["count"] == 3


def test_envelope_validates_against_collection_schema(items):
    registry = Registry().with_resource(
        "https://x/item.json",
        Resource.from_contents(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
            }
        ),
    )
    validator = Draft202012Validator(
        collection_schema("https://x/item.json"),
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    validator.validate(TestClient(list_router()).get("/items?page_size=2").json())

    # Also nullable item fields, and an empty page.
    router = Router()

    @router.get("/rows", response=list[ItemOut])
    @scrudful_list(href_func=lambda request, row: "https://x/rows/1")
    def list_rows(request, empty: bool = False):
        return [] if empty else [{"id": 1, "name": "a", "version": 1}]

    client = TestClient(router)
    validator.validate(client.get("/rows").json())
    validator.validate(client.get("/rows?empty=true").json())


def test_list_config():
    @scrudful_list(
        href_func=href,
        page_size=10,
        max_page_size=20,
        list_etag_func=lambda qs: "n",
        error=ERROR,
        **ITEM_OPTIONS,
        **LIST_LINKS,
    )
    def handler(request): ...

    config = handler._scrudful
    assert config.kind == "list"
    assert config.load is None
    assert config.href_func is href
    assert config.entity == Entity(
        "/schemas/item", context_link_or_func="/contexts/item"
    )
    assert config.collection == Entity(
        "/schemas/items", context_link_or_func="/contexts/items"
    )
    assert config.has_etag and config.has_last_modified
    assert config.has_list_etag and not config.has_list_last_modified
    assert config.has_list_validators
    assert (config.page_size, config.max_page_size) == (10, 20)
    assert config.error == ERROR
    assert not config.evaluates_preconditions

    @scrudful_list(href_func=href)
    def other(request): ...

    assert other._scrudful.collection == Entity()
    assert not other._scrudful.has_validators
    assert not other._scrudful.has_list_validators
    assert (other._scrudful.page_size, other._scrudful.max_page_size) == (100, 1000)


@pytest.mark.parametrize(
    "options, error, match",
    [
        ({"href_func": "/items/1"}, TypeError, "href_func"),
        ({"href_func": None}, TypeError, "href_func"),
        ({"etag_func": "v1"}, TypeError, "etag_func"),
        ({"list_etag_func": "v1"}, TypeError, "list_etag_func"),
        ({"list_last_modified_func": 1}, TypeError, "list_last_modified_func"),
        ({"error": "/s"}, TypeError, "error"),
        ({"page_size": "10"}, TypeError, "page_size"),
        ({"page_size": True}, TypeError, "page_size"),
        ({"max_page_size": 1.5}, TypeError, "max_page_size"),
        ({"page_size": 0}, ValueError, "page_size"),
        ({"page_size": 11, "max_page_size": 10}, ValueError, "max_page_size"),
    ],
)
def test_list_argument_errors(options, error, match):
    with pytest.raises(error, match=match):
        scrudful_list(**{"href_func": href, **options})


async def async_value(*args: Any) -> str:
    return "/x"


@pytest.mark.parametrize(
    "options, name",
    [
        ({"href_func": async_value}, "href_func"),
        ({"etag_func": async_value}, "etag_func"),
        ({"list_etag_func": async_value}, "list_etag_func"),
        ({"list_last_modified_func": async_value}, "list_last_modified_func"),
        ({"schema_link_or_func": async_value}, "schema_link_or_func"),
        ({"list_schema_link_or_func": async_value}, "list_schema_link_or_func"),
        ({"list_context_type_or_func": async_value}, "list_context_type_or_func"),
        ({"error": Entity(async_value)}, "error.schema_link_or_func"),
    ],
)
def test_list_sync_handler_rejects_async_callables(options, name):
    decorator = scrudful_list(**{"href_func": href, **options})

    def handler(request): ...

    with pytest.raises(TypeError, match=name):
        decorator(handler)

    async def async_handler(request): ...

    assert inspect.iscoroutinefunction(decorator(async_handler))


def test_list_rejects_generator_handler():
    def handler(request):
        yield

    with pytest.raises(TypeError, match="generator"):
        scrudful_list(href_func=href)(handler)


def test_list_handler_errors_use_ninja_handlers(items):
    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=href)
    def list_items(request):
        raise HttpError(403, "Forbidden")

    response = TestClient(router).get("/items")
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_list_without_validator_values_exists(db):
    """The queryset the handler returned is the collection: it exists even
    when the list validators return None (e.g. the latest modification of an
    empty collection)."""
    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=href, list_last_modified_func=lambda qs: None)
    def list_items(request):
        return Item.objects.order_by("pk")

    client = TestClient(router)
    response = client.get("/items", headers={"If-None-Match": "*"})
    assert response.status_code == 304
    assert "Last-Modified" not in response.headers
    assert client.get("/items", headers={"If-Match": "*"}).status_code == 200
    assert client.get("/items", headers={"If-Match": '"n1"'}).status_code == 412
    assert client.get("/items").status_code == 200
