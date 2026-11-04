"""``scrudful_create`` evaluates the preconditions of the collection."""

from datetime import datetime, timezone
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from ninja import Router
from ninja.errors import ConfigError
from ninja.testing import TestAsyncClient, TestClient

from scrudful.ninja import Entity, ScrudfulRouter, scrudful_create
from scrudful.problem import PROBLEM_JSON, problem_details
from tests.test_ninja_adapter import TEST_BASE_URL, ItemIn, ItemOut, Store, etag
from tests.test_ninja_entities import ERROR, ERROR_LINK
from tests.test_ninja_options import hsr_get

LIST_MODIFIED = datetime(2026, 1, 1, tzinfo=timezone.utc)
BEFORE = "Wed, 31 Dec 2025 00:00:00 GMT"
AFTER = "Fri, 02 Jan 2026 00:00:00 GMT"


@pytest.fixture
def store() -> Store:
    return Store()


def collection_etag(items: list[Any]) -> str:
    return f"n{len(items)}"


def collection_modified(items: list[Any]) -> datetime:
    return LIST_MODIFIED


def create_router(store: Store, router: Router | None = None, **options: Any) -> Router:
    """``POST /items`` whose collection is ``store.items`` (two rows: ``"n2"``)."""
    router = router or ScrudfulRouter(base_url=TEST_BASE_URL)

    def items(request: Any, **params: Any) -> list[Any]:
        store.calls["list_load"] += 1
        store.load_params.append(params)
        return list(store.items.values())

    config: dict[str, Any] = {
        "list_load": items,
        "list_etag_func": collection_etag,
        "list_last_modified_func": collection_modified,
        **options,
    }

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(
        location_func=lambda request, item: f"/items/{item['id']}",
        etag_func=etag,
        **config,
    )
    def create_item(request, payload: ItemIn):
        store.calls["create"] += 1
        item = {"id": 3, "name": payload.name, "version": 1}
        store.items[3] = item
        return item

    return router


def post(store: Store, headers: dict[str, str] | None = None, **options: Any) -> Any:
    client = TestClient(create_router(store, **options))
    return client.post("/items", json={"name": "c"}, headers=headers or {})


def test_unknown_if_match_is_412_and_creates_nothing(store):
    response = post(store, {"If-Match": '"nope"'})
    assert response.status_code == 412
    assert store.calls["create"] == 0
    assert 3 not in store.items


def test_current_if_match_creates(store):
    response = post(store, {"If-Match": '"n2"'})
    assert response.status_code == 201
    assert store.calls["create"] == 1
    # The 201 describes the created item, not the collection.
    assert response.headers["ETag"] == '"v1"'


def test_if_match_is_a_strong_comparison(store):
    assert post(store, {"If-Match": 'W/"n2"'}).status_code == 412


def test_if_match_star_creates(store):
    assert post(store, {"If-Match": "*"}).status_code == 201


def test_if_none_match_star_is_412(store):
    response = post(store, {"If-None-Match": "*"})
    assert response.status_code == 412
    assert store.calls["create"] == 0


def test_if_none_match_is_a_weak_comparison(store):
    assert post(store, {"If-None-Match": 'W/"n2"'}).status_code == 412
    assert post(Store(), {"If-None-Match": '"n9"'}).status_code == 201


def test_past_if_unmodified_since_is_412(store):
    response = post(store, {"If-Unmodified-Since": BEFORE})
    assert response.status_code == 412
    assert store.calls["create"] == 0


def test_later_if_unmodified_since_creates(store):
    assert post(store, {"If-Unmodified-Since": AFTER}).status_code == 201


def test_if_unmodified_since_is_ignored_with_if_match(store):
    headers = {"If-Match": '"n2"', "If-Unmodified-Since": BEFORE}
    assert post(store, headers).status_code == 201


def test_if_modified_since_is_ignored(store):
    assert post(store, {"If-Modified-Since": AFTER}).status_code == 201


@pytest.mark.parametrize(
    "headers", [{}, {"If-Modified-Since": AFTER}], ids=["none", "ims-only"]
)
def test_unconditional_create_is_not_428_and_loads_nothing(store, headers):
    response = post(store, headers)
    assert response.status_code == 201
    assert store.calls["list_load"] == 0


def test_a_missing_collection_lets_the_handler_answer(store):
    """As for load: None is no current representation, so even
    ``If-None-Match: *`` passes and the handler answers (e.g. 404)."""
    response = post(
        store, {"If-None-Match": "*"}, list_load=lambda request, **params: None
    )
    assert response.status_code == 201


def test_malformed_if_match_is_400(store):
    response = post(store, {"If-Match": "nope"})
    assert response.status_code == 400
    assert store.calls["create"] == 0


def test_list_load_gets_the_validated_parameters(store):
    post(store, {"If-Match": '"n2"'})
    assert store.load_params == [{"payload": ItemIn(name="c")}]


def test_the_412_has_the_error_links_and_body(store):
    response = post(
        store,
        {"If-Match": '"nope"'},
        error=Entity(
            "/schemas/error",
            context_link_or_func="/contexts/error",
            media_type=PROBLEM_JSON,
        ),
        error_body_func=problem_details,
    )
    assert response.status_code == 412
    assert response.headers["Link"] == ERROR_LINK
    assert response.headers["Content-Type"].startswith(PROBLEM_JSON)
    assert response.json()["status"] == 412


def test_error_link_callables_get_the_collection(store):
    seen = []

    def schema(request, obj):
        seen.append(obj)
        return "/schemas/error"

    response = post(
        store,
        {"If-Match": '"nope"'},
        error=Entity(schema, context_link_or_func="/contexts/error"),
    )
    assert response.status_code == 412
    assert seen == [list(store.items.values())]


def test_without_list_validators_preconditions_are_ignored(store):
    response = post(
        store,
        {"If-Match": '"nope"'},
        list_load=None,
        list_etag_func=None,
        list_last_modified_func=None,
    )
    assert response.status_code == 201
    assert store.calls["list_load"] == 0


def test_options_describes_the_412(store):
    client = TestClient(create_router(store, error=ERROR))
    described = hsr_get(client, "/items")["post"]
    assert {"201", "400", "412"} <= set(described["responses"])
    assert "428" not in described["responses"]
    assert "parameters" not in described
    assert described["responses"]["412"]["content"] == {
        "application/json": {
            "schema": {"$ref": "http://testlocation/schemas/error"},
            "x-rdf-context": {
                "href": "http://testlocation/contexts/error",
                "type": "application/ld+json",
            },
        }
    }
    assert store.calls == {}


def test_async_create_with_async_list_load(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    async def items(request: Any, **params: Any) -> list[Any]:
        return list(store.items.values())

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(
        location_func=lambda request, item: f"/items/{item['id']}",
        list_load=items,
        list_etag_func=collection_etag,
    )
    async def create_item(request, payload: ItemIn):
        store.calls["create"] += 1
        return {"id": 3, "name": payload.name, "version": 1}

    client = TestAsyncClient(router)
    stale = async_to_sync(client.post)(
        "/items", json={"name": "c"}, headers={"If-Match": '"nope"'}
    )
    assert stale.status_code == 412
    current = async_to_sync(client.post)(
        "/items", json={"name": "c"}, headers={"If-Match": '"n2"'}
    )
    assert current.status_code == 201
    assert store.calls["create"] == 1


@pytest.mark.parametrize(
    "options, match",
    [
        ({"list_etag_func": collection_etag}, "list_load"),
        ({"list_last_modified_func": collection_modified}, "list_load"),
        ({"list_load": lambda request: []}, "list_etag_func"),
        (
            {"list_load": "items", "list_etag_func": collection_etag},
            "list_load must be callable",
        ),
        (
            {"list_load": lambda request: [], "list_etag_func": "n1"},
            "list_etag_func must be callable",
        ),
    ],
)
def test_bad_arguments(options, match):
    with pytest.raises(TypeError, match=match):
        scrudful_create(location_func=lambda request, item: "/x", **options)


def test_sync_handler_rejects_async_list_load():
    async def items(request):
        return []

    decorate = scrudful_create(
        location_func=lambda request, item: "/x",
        list_load=items,
        list_etag_func=collection_etag,
    )
    with pytest.raises(TypeError, match="list_load is async"):

        @decorate
        def create_item(request): ...


@pytest.mark.parametrize("method", ["put", "patch"])
def test_list_validators_on_an_operation_without_post_are_rejected(method):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
    decorate = scrudful_create(
        location_func=lambda request, item: "/x",
        list_load=lambda request, **params: [],
        list_etag_func=collection_etag,
    )
    with pytest.raises(ConfigError, match="POST"):

        @getattr(router, method)("/items", response={201: ItemOut})
        @decorate
        def create_item(request, payload: ItemIn): ...


def test_list_validators_on_an_operation_with_post_are_accepted():
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.api_operation(["POST", "PUT"], "/items", response={201: ItemOut})
    @scrudful_create(
        location_func=lambda request, item: "/x",
        list_load=lambda request, **params: [],
        list_etag_func=collection_etag,
    )
    def create_item(request, payload: ItemIn):
        return {"id": 3, "name": payload.name, "version": 1}

    client = TestClient(router)
    response = client.post("/items", json={"name": "c"}, headers={"If-Match": '"n9"'})
    assert response.status_code == 412


def test_list_last_modified_func_alone(store):
    assert (
        post(store, {"If-Unmodified-Since": BEFORE}, list_etag_func=None).status_code
        == 412
    )
    assert (
        post(Store(), {"If-Unmodified-Since": AFTER}, list_etag_func=None).status_code
        == 201
    )
    assert post(Store(), {"If-Match": '"n2"'}, list_etag_func=None).status_code == 412
