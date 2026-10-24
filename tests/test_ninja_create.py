"""Create operations: ``scrudful_create`` (201, Location, item validators)."""

import inspect
from collections import Counter
from datetime import datetime
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from ninja import Router, Status
from ninja.errors import ConfigError
from ninja.testing import TestAsyncClient, TestClient

from scrudful.ninja import Entity, ScrudfulResponse, scrudful_create
from tests.test_ninja_adapter import (
    ALICE,
    CONTEXT_LINK,
    LINKS,
    MISSING,
    MODIFIED,
    MODIFIED_HTTP,
    SCHEMA_LINK,
    ItemIn,
    ItemOut,
    Message,
    Store,
    etag,
    expose,
    item_router,
    make_api,
    modified,
)
from tests.test_ninja_entities import ERROR, ERROR_LINK, QUERY

ITEM_LINK = f"{SCHEMA_LINK}, {CONTEXT_LINK}"


class Counted:
    """Call counters for the create decorator's callables."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.objects: list[Any] = []

    def location(self, request: Any, item: Any) -> str:
        self.calls["location"] += 1
        self.objects.append(item)
        return f"/items/{item['id']}"

    def etag(self, item: Any) -> str:
        self.calls["etag"] += 1
        return etag(item)

    def modified(self, item: Any) -> datetime:
        self.calls["modified"] += 1
        return modified(item)


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def counted() -> Counted:
    return Counted()


def new_item(store: Store, name: str) -> dict[str, Any]:
    item = {
        "id": MISSING,
        "name": name,
        "version": 1,
        "modified": MODIFIED,
        "owner": "alice",
    }
    store.items[MISSING] = item
    return item


def create_router(store: Store, counted: Counted, router: Router | None = None):
    """``POST /items`` creating item 3, added to ``router``."""
    router = router or Router()

    @router.post("/items", response={201: ItemOut, 400: Message})
    @scrudful_create(
        location_func=counted.location,
        etag_func=counted.etag,
        last_modified_func=counted.modified,
        **LINKS,
    )
    def create_item(request, payload: ItemIn):
        store.calls["create"] += 1
        if payload.name == "bad":
            return Status(400, {"detail": "bad name"})
        return new_item(store, payload.name)

    return router


def test_create_201_location_links_validators(store, counted):
    response = TestClient(create_router(store, counted)).post(
        "/items", json={"name": "c"}
    )
    assert response.status_code == 201
    assert response.json() == {"id": 3, "name": "c", "version": 1}
    assert response.headers["Location"] == "/items/3"
    assert response.headers["Link"] == ITEM_LINK
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert expose(response) == {"ETag", "Link", "Location"}
    assert counted.calls == {"location": 1, "etag": 1, "modified": 1}
    assert counted.objects == [store.items[3]]


def test_create_validators_match_detail_get(store, counted):
    client = TestClient(create_router(store, counted, item_router(store)))
    created = client.post("/items", json={"name": "c"})
    assert created.status_code == 201
    detail = client.get(created.headers["Location"])
    assert detail.status_code == 200
    assert detail.headers["ETag"] == created.headers["ETag"]
    assert detail.headers["Last-Modified"] == created.headers["Last-Modified"]
    assert detail.headers["Link"] == created.headers["Link"]
    # So the client can edit right away.
    response = client.put(
        "/items/3", json={"name": "d"}, headers={"If-Match": created.headers["ETag"]}
    )
    assert response.status_code == 200


def test_create_uses_meta_created(store, counted):
    router = Router()

    @router.post("/items", response={201: dict[str, Any]})
    @scrudful_create(
        location_func=counted.location,
        etag_func=counted.etag,
        last_modified_func=counted.modified,
        **LINKS,
    )
    def create_item(request, payload: ItemIn, meta: ScrudfulResponse):
        item = new_item(store, payload.name)
        item["version"] = 5
        meta.created = item
        return {"created": True}

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.json() == {"created": True}
    assert response.headers["Location"] == "/items/3"
    assert response.headers["ETag"] == '"v5"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert counted.objects == [store.items[3]]


def test_create_created_object_from_returned_status(store, counted):
    router = Router()

    @router.post("/items", response={200: ItemOut, 201: ItemOut})
    @scrudful_create(location_func=counted.location, etag_func=counted.etag)
    def create_item(request, payload: ItemIn):
        return Status(201, new_item(store, payload.name))

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Location"] == "/items/3"
    assert response.headers["ETag"] == '"v1"'


@pytest.mark.parametrize(
    "headers",
    [{}, {"If-Match": '"stale"'}, {"If-None-Match": "*"}, {"If-Match": "v1"}],
    ids=["none", "stale", "inm-star", "malformed"],
)
def test_create_never_428(store, counted, headers):
    response = TestClient(create_router(store, counted)).post(
        "/items", json={"name": "c"}, headers=headers
    )
    assert response.status_code == 201
    assert response.headers["ETag"] == '"v1"'
    assert store.calls["create"] == 1
    # No precondition evaluation: the validators are computed once, after.
    assert counted.calls["etag"] == 1


def test_create_non_201_no_location(store, counted):
    response = TestClient(create_router(store, counted)).post(
        "/items", json={"name": "bad"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "bad name"}
    for name in ("Location", "ETag", "Last-Modified", "Link"):
        assert name not in response.headers
    assert "Access-Control-Expose-Headers" not in response.headers
    assert counted.calls == {}


def test_create_unauthenticated_401(store, counted):
    client = TestClient(make_api(create_router(store, counted), store))
    response = client.post("/items", json={"name": "c"})
    assert response.status_code == 401
    assert "Location" not in response.headers
    assert "ETag" not in response.headers
    assert store.calls["create"] == 0
    assert counted.calls == {}
    assert MISSING not in store.items

    response = client.post("/items", json={"name": "c"}, headers=ALICE)
    assert response.status_code == 201
    assert response.headers["Location"] == "/items/3"


def test_create_runtime_values_win(store, counted):
    router = Router()

    @router.post("/items", response={201: ItemOut, 202: Message})
    @scrudful_create(
        location_func=counted.location,
        etag_func=counted.etag,
        last_modified_func=counted.modified,
        **LINKS,
    )
    def create_item(request, payload: ItemIn, meta: ScrudfulResponse):
        if payload.name == "later":
            meta.status = 202
            return {"detail": "queued"}
        meta.location = "/elsewhere/3"
        meta.etag = "x1"
        meta.entity = ERROR
        return new_item(store, payload.name)

    client = TestClient(router)
    response = client.post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Location"] == "/elsewhere/3"
    assert response.headers["ETag"] == '"x1"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert response.headers["Link"] == ERROR_LINK
    assert counted.calls["location"] == 0

    # Another status: not a created item, so none of the item's headers.
    response = client.post("/items", json={"name": "later"})
    assert response.status_code == 202
    for name in ("Location", "ETag", "Last-Modified", "Link"):
        assert name not in response.headers


def test_create_handler_headers_win(store, counted):
    router = Router()

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(location_func=counted.location, etag_func=counted.etag, **LINKS)
    def create_item(request, payload: ItemIn, response: HttpResponse):
        response["Location"] = "/mine/3"
        response["ETag"] = '"mine"'
        response["Link"] = "</other>; rel=related"
        return new_item(store, payload.name)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Location"] == "/mine/3"
    assert response.headers["ETag"] == '"mine"'
    assert response.headers["Link"] == f"</other>; rel=related, {ITEM_LINK}"
    # The handler set Location: location_func isn't needed.
    assert counted.calls["location"] == 0


def test_create_link_callables_get_created_object(store, counted):
    router = Router()

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(
        location_func=counted.location,
        schema_link_or_func=lambda request, item: f"/schemas/item/{item['id']}",
    )
    def create_item(request, payload: ItemIn):
        return new_item(store, payload.name)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.headers["Link"] == (
        '</schemas/item/3>; rel="describedBy"; type="application/json"'
    )


def test_create_http_response_needs_meta_created(store, counted):
    router = Router()

    @router.post("/items")
    @scrudful_create(location_func=counted.location, etag_func=counted.etag, **LINKS)
    def create_item(request, payload: ItemIn, meta: ScrudfulResponse):
        item = new_item(store, payload.name)
        if request.GET.get("meta"):
            meta.created = item
        return JsonResponse({"id": item["id"]}, status=201)

    client = TestClient(router)
    # A returned HttpResponse is not the created object: without one, the
    # 201 gets no Location or validators. Its body still gets the item
    # entity's Links, which are all static here.
    response = client.post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert "Location" not in response.headers
    assert "ETag" not in response.headers
    assert response.headers["Link"] == ITEM_LINK
    assert counted.calls == {}

    response = client.post("/items?meta=1", json={"name": "c"})
    assert response.headers["Location"] == "/items/3"
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Link"] == ITEM_LINK


@pytest.mark.parametrize(
    "body",
    [
        lambda item: JsonResponse({"id": item["id"]}, status=201),
        lambda item: HttpResponse(b"created", status=201),
        lambda item: StreamingHttpResponse(iter([b"created"]), status=201),
    ],
    ids=["json", "http", "streaming"],
)
def test_create_http_response_body_gets_static_item_links(store, counted, body):
    """No created object, but a body: the item entity's static Links."""
    router = Router()

    @router.post("/items")
    @scrudful_create(location_func=counted.location, etag_func=counted.etag, **LINKS)
    def create_item(request, payload: ItemIn):
        return body(new_item(store, payload.name))

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Link"] == ITEM_LINK
    assert expose(response) == {"Link"}
    for name in ("Location", "ETag", "Last-Modified"):
        assert name not in response.headers
    assert counted.calls == {}


@pytest.mark.parametrize(
    "field",
    [
        "schema_link_or_func",
        "schema_rel_or_func",
        "schema_type_or_func",
        "context_link_or_func",
        "context_rel_or_func",
        "context_type_or_func",
    ],
)
def test_create_http_response_body_with_link_callables_gets_no_links(
    store, counted, field
):
    """A callable would get ``None`` for the created item: it isn't called,
    and the 201 gets none of the item entity's Links."""
    calls = []

    def value(request, item):
        calls.append(item)
        return "x"

    router = Router()

    @router.post("/items")
    @scrudful_create(
        location_func=counted.location,
        etag_func=counted.etag,
        **{**LINKS, field: value},
    )
    def create_item(request, payload: ItemIn):
        item = new_item(store, payload.name)
        return JsonResponse({"id": item["id"]}, status=201)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    for name in ("Location", "ETag", "Link"):
        assert name not in response.headers
    assert calls == []
    assert counted.calls == {}


@pytest.mark.parametrize(
    "result",
    [
        lambda item: None,
        lambda item: Status(201, None),
        lambda item: HttpResponse(status=201),
    ],
    ids=["none", "status-none", "empty-http-response"],
)
def test_create_without_a_body_gets_no_static_item_links(store, counted, result):
    """No created object and no body: nothing for the item entity to describe."""
    router = Router()

    @router.post("/items", response={201: None})
    @scrudful_create(location_func=counted.location, etag_func=counted.etag, **LINKS)
    def create_item(request, payload: ItemIn):
        return result(new_item(store, payload.name))

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    for name in ("Location", "ETag", "Link"):
        assert name not in response.headers
    assert counted.calls == {}


def test_create_meta_created_none_with_a_body_gets_static_item_links(store, counted):
    """``meta.created = None`` names no created object; the body still gets
    the item entity's static Links."""
    router = Router()

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(location_func=counted.location, etag_func=counted.etag, **LINKS)
    def create_item(request, payload: ItemIn, meta: ScrudfulResponse):
        meta.created = None
        return new_item(store, payload.name)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Link"] == ITEM_LINK
    assert "Location" not in response.headers
    assert "ETag" not in response.headers
    assert counted.calls == {}


@pytest.mark.parametrize("item_schema", ["/schemas/item", lambda request, obj: "/x"])
def test_create_http_response_body_explicit_entities_win(store, counted, item_schema):
    """``meta.entity`` and ``responses[201]`` replace the item entity, static
    or not; their callables get ``None`` (no created object)."""
    seen = []

    def accepted(request, obj):
        seen.append(obj)
        return "/schemas/accepted"

    router = Router()

    @router.post("/items")
    @scrudful_create(
        location_func=counted.location,
        responses={201: Entity(schema_link_or_func=accepted)},
        schema_link_or_func=item_schema,
        context_link_or_func="/contexts/item",
    )
    def create_item(request, payload: ItemIn, meta: ScrudfulResponse):
        item = new_item(store, payload.name)
        if request.GET.get("meta"):
            meta.entity = ERROR
        return JsonResponse({"id": item["id"]}, status=201)

    client = TestClient(router)
    response = client.post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Link"] == (
        '</schemas/accepted>; rel="describedBy"; type="application/json"'
    )
    assert seen == [None]

    response = client.post("/items?meta=1", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Link"] == ERROR_LINK
    assert seen == [None]
    assert "Location" not in response.headers
    assert counted.calls == {}


def test_create_without_a_created_object(store):
    """A 201 whose body is None: no Location, validators or item Links.

    The callables would get ``None`` for the item: they aren't called. The
    handler names the created object with ``meta.created`` (or sets
    ``meta.location``).
    """
    calls = []

    def location(request, item):
        calls.append("location")
        return f"/items/{item['id']}"

    def schema(request, item):
        calls.append("schema")
        return f"/schemas/item/{item['id']}"

    def counted_etag(item):
        calls.append("etag")
        return etag(item)

    router = Router()

    @router.post("/items", response={201: None})
    @scrudful_create(
        location_func=location,
        etag_func=counted_etag,
        schema_link_or_func=schema,
        context_link_or_func="/contexts/item",
    )
    def create_item(request, payload: ItemIn, meta: ScrudfulResponse):
        item = new_item(store, payload.name)
        if request.GET.get("meta") == "created":
            meta.created = item
        elif request.GET.get("meta") == "location":
            meta.location = f"/items/{item['id']}"

    client = TestClient(router)
    response = client.post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert store.items[MISSING]["name"] == "c"
    for name in ("Location", "ETag", "Last-Modified", "Link"):
        assert name not in response.headers
    assert calls == []

    response = client.post("/items?meta=location", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Location"] == "/items/3"
    assert "Link" not in response.headers
    assert calls == []

    response = client.post("/items?meta=created", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Location"] == "/items/3"
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Link"] == (
        '</schemas/item/3>; rel="describedBy"; type="application/json", ' + CONTEXT_LINK
    )
    assert calls == ["location", "etag", "schema"]


def test_create_without_a_created_object_gets_responses_201(store):
    """An explicit ``responses[201]`` still applies; its callables get None."""
    seen = []

    def schema(request, obj):
        seen.append(obj)
        return "/schemas/accepted"

    router = Router()

    @router.post("/items", response={201: None})
    @scrudful_create(
        location_func=lambda request, item: f"/items/{item['id']}",
        responses={201: Entity(schema_link_or_func=schema)},
        **LINKS,
    )
    def create_item(request, payload: ItemIn):
        new_item(store, payload.name)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert "Location" not in response.headers
    assert response.headers["Link"] == (
        '</schemas/accepted>; rel="describedBy"; type="application/json"'
    )
    assert seen == [None]


def test_create_needs_declared_201(store, counted):
    router = Router()

    @router.post("/items", response=ItemOut)
    @scrudful_create(location_func=counted.location)
    def create_item(request, payload: ItemIn):
        return new_item(store, payload.name)

    # The wrapper returns ``Status(201, ...)``: ninja needs a 201 schema.
    with pytest.raises(ConfigError, match="201"):
        TestClient(router).post("/items", json={"name": "c"})


def test_create_with_wildcard_response(store, counted):
    router = Router()

    @router.post("/items", response={...: Any})
    @scrudful_create(location_func=counted.location)
    def create_item(request, payload: ItemIn):
        return new_item(store, payload.name)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Location"] == "/items/3"


def test_create_location_iri_is_encoded(store):
    router = Router()

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(location_func=lambda request, item: "/items/é")
    def create_item(request, payload: ItemIn):
        return new_item(store, payload.name)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.headers["Location"] == "/items/%C3%A9"


def test_create_location_func_none_sends_no_location(store):
    router = Router()

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(location_func=lambda request, item: None, etag_func=etag)
    def create_item(request, payload: ItemIn):
        return new_item(store, payload.name)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.status_code == 201
    assert "Location" not in response.headers
    assert response.headers["ETag"] == '"v1"'


def test_create_responses_201_replaces_item_entity(store, counted):
    router = Router()

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(location_func=counted.location, responses={201: ERROR}, **LINKS)
    def create_item(request, payload: ItemIn):
        return new_item(store, payload.name)

    response = TestClient(router).post("/items", json={"name": "c"})
    assert response.headers["Link"] == ERROR_LINK
    assert response.headers["Location"] == "/items/3"


def test_create_async(store, counted):
    async def location(request, item):
        return f"/items/{item['id']}"

    router = Router()

    @router.post("/items", response={201: ItemOut, 400: Message})
    @scrudful_create(
        location_func=location,
        etag_func=counted.etag,
        last_modified_func=counted.modified,
        **LINKS,
    )
    async def create_item(request, payload: ItemIn):
        if payload.name == "bad":
            return Status(400, {"detail": "bad name"})
        return new_item(store, payload.name)

    assert inspect.iscoroutinefunction(create_item)
    client = TestAsyncClient(router)
    response = async_to_sync(client.post)("/items", json={"name": "c"})
    assert response.status_code == 201
    assert response.headers["Location"] == "/items/3"
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert response.headers["Link"] == ITEM_LINK
    response = async_to_sync(client.post)("/items", json={"name": "bad"})
    assert response.status_code == 400
    assert "Location" not in response.headers


async def async_value(*args: Any) -> str:
    return "/x"


@pytest.mark.parametrize(
    "options, name",
    [
        ({"location_func": async_value}, "location_func"),
        ({"etag_func": async_value}, "etag_func"),
        ({"schema_link_or_func": async_value}, "schema_link_or_func"),
        ({"request": Entity(async_value)}, "request.schema_link_or_func"),
        ({"responses": {400: Entity(async_value)}}, r"responses\[400\]"),
    ],
)
def test_create_sync_handler_rejects_async_callables(options, name):
    decorator = scrudful_create(**{"location_func": lambda r, o: "/x", **options})

    def handler(request): ...

    with pytest.raises(TypeError, match=name):
        decorator(handler)

    async def async_handler(request): ...

    assert inspect.iscoroutinefunction(decorator(async_handler))


def test_create_config():
    @scrudful_create(
        location_func=lambda request, item: "/x",
        etag_func=etag,
        responses={400: ERROR},
        **LINKS,
    )
    def handler(request): ...

    config = handler._scrudful
    assert config.kind == "create"
    assert config.load is None
    assert config.entity == Entity(
        "/schemas/item", context_link_or_func="/contexts/item"
    )
    # The request body is an item, as for DRF's create.
    assert config.request == config.entity
    assert dict(config.responses) == {400: ERROR}
    # A create produces no 400, 412 or 428 of its own: no error entity.
    assert config.error is None
    assert "error" not in inspect.signature(scrudful_create).parameters
    assert config.has_etag
    assert not config.has_last_modified

    @scrudful_create(location_func=lambda request, item: "/x", request=QUERY)
    def other(request): ...

    assert other._scrudful.request == QUERY
    assert not other._scrudful.has_validators


@pytest.mark.parametrize(
    "options, match",
    [
        ({"location_func": "/items/1"}, "location_func"),
        ({"location_func": None}, "location_func"),
        ({"location_func": lambda r, o: "/x", "etag_func": "v1"}, "etag_func"),
        ({"location_func": lambda r, o: "/x", "request": "/s"}, "request"),
        ({"location_func": lambda r, o: "/x", "responses": {"201": ERROR}}, "resp"),
    ],
)
def test_create_argument_errors(options, match):
    with pytest.raises(TypeError, match=match):
        scrudful_create(**options)


def test_create_rejects_generator_handler():
    decorator = scrudful_create(location_func=lambda request, item: "/x")

    def handler(request):
        yield

    with pytest.raises(TypeError, match="generator"):
        decorator(handler)
