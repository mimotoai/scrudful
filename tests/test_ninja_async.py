"""Async operations of ``scrudful.ninja``: the same behaviour as sync ones.

The async test client is driven with ``asgiref.sync.async_to_sync``, so no
async pytest plugin is needed.
"""

import asyncio
import functools
import inspect
import itertools
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.http import Http404
from items.models import Item
from ninja import Router, Status
from ninja.errors import HttpError
from ninja.operation import AsyncOperation, Operation
from ninja.testing import TestAsyncClient, TestClient

from scrudful.ninja import (
    CONDITIONAL_READ_RESPONSES,
    CONDITIONAL_WRITE_RESPONSES,
    scrudful,
)
from tests.test_ninja_adapter import (
    ALICE,
    CONTEXT_LINK,
    HIDDEN,
    IF_HEADERS,
    LINKS,
    METHODS,
    MISSING,
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
    update,
)


def async_item_router(store: Store, load: Any) -> Router:
    """``item_router`` with ``async def`` handlers and the given ``load``."""
    options = dict(load=load, etag_func=etag, last_modified_func=modified, **LINKS)
    router = Router()

    @router.get(
        "/items/{pk}",
        response=ItemOut,
        openapi_extra={"responses": CONDITIONAL_READ_RESPONSES},
    )
    @scrudful(**options)
    async def get_item(request, pk: int, obj):
        store.calls["get"] += 1
        if obj is None:
            raise HttpError(404, "Not Found")
        return obj

    @router.put(
        "/items/{pk}",
        response={200: ItemOut, 201: ItemOut, 409: Message},
        openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES},
    )
    @scrudful(**options)
    async def put_item(request, pk: int, payload: ItemIn, obj):
        store.calls["put"] += 1
        if obj is None:
            if pk in store.items:
                return Status(409, {"detail": "Conflict"})
            store.items[pk] = obj = {"id": pk, "version": 0, "owner": "alice"}
            update(obj, payload.name)
            return Status(201, obj)
        update(obj, payload.name)
        return obj

    @router.patch("/items/{pk}", response=ItemOut)
    @scrudful(**options)
    async def patch_item(request, pk: int, payload: ItemIn, obj):
        store.calls["patch"] += 1
        if obj is None:
            raise HttpError(404, "Not Found")
        update(obj, payload.name)
        return obj

    @router.delete("/items/{pk}", response={204: None})
    @scrudful(**options)
    async def delete_item(request, pk: int, obj):
        store.calls["delete"] += 1
        if obj is None:
            raise HttpError(404, "Not Found")
        del store.items[pk]

    return router


@pytest.fixture
def store() -> Store:
    return Store()


def sync_load(store: Store) -> Any:
    return store.load


def async_load(store: Store) -> Any:
    async def load(request, pk: int, **params):
        return store.load(request, pk, **params)

    return load


def awaitable_load(store: Store) -> Any:
    """A sync callable returning an awaitable, which is awaited too."""

    def load(request, pk: int, **params):
        async def result():
            return store.load(request, pk, **params)

        return result()

    return load


LOADS = {"sync-load": sync_load, "async-load": async_load}


def send(client: Any, method: str, path: str, headers: dict[str, str]) -> Any:
    body = {"name": "new"} if method in ("PUT", "PATCH") else None
    request = client.request
    if isinstance(client, TestAsyncClient):
        request = async_to_sync(request)
    return request(method, path, json=body, headers=headers)


# Each case: the requests to send, as (method, path, headers, authenticated),
# and a check of the async responses.


def check_200(responses, store):
    (response,) = responses
    assert response.status_code == 200
    assert response.json() == {"id": 1, "name": "a", "version": 1}
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert {"ETag", "Link"} <= expose(response)


def check_304(responses, store):
    for response in responses:
        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["ETag"] == '"v1"'
        assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert store.calls["get"] == 0


def check_428(responses, store):
    for response in responses:
        assert response.status_code == 428
    assert responses[0].json()["accepted-precondition-headers"] == [
        "If-Match",
        "If-Unmodified-Since",
    ]
    assert store.calls["put"] == store.calls["patch"] == store.calls["delete"] == 0
    assert store.items[1]["version"] == 1


def check_412(responses, store):
    for response in responses:
        assert response.status_code == 412
        assert response.json() == {"detail": "Precondition failed."}
        assert "ETag" not in response.headers
    assert store.calls["put"] == store.calls["patch"] == store.calls["delete"] == 0


def check_fresh(responses, store):
    put, patch = responses
    assert put.status_code == patch.status_code == 200
    assert put.headers["ETag"] == '"v2"'
    assert put.headers["Last-Modified"] == "Tue, 02 Jan 2024 05:04:05 GMT"
    assert patch.headers["ETag"] == '"v3"'
    # Once for the preconditions, once more for the fresh validators.
    assert store.calls["load"] == 4


def check_401(responses, store):
    for response in responses[:-1]:
        assert response.status_code == 401
        assert "ETag" not in response.headers
    assert store.calls["load"] == 1  # only the authenticated request's
    assert responses[-1].status_code == 304


def check_hidden(responses, store):
    for hidden, missing in zip(responses[::2], responses[1::2]):
        for response in (hidden, missing):
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}
            assert "ETag" not in response.headers
            assert "Last-Modified" not in response.headers
        assert dict(hidden.headers) == dict(missing.headers)
    assert store.items[HIDDEN]["version"] == 7


CASES = {
    "200-validators": ([("GET", "/items/1", {}, True)], check_200),
    "304": (
        [
            ("GET", "/items/1", {"If-None-Match": '"v1"'}, True),
            ("GET", "/items/1", {"If-Modified-Since": MODIFIED_HTTP}, True),
        ],
        check_304,
    ),
    "428": (
        [(method, "/items/1", {}, True) for method in ("PUT", "PATCH", "DELETE")],
        check_428,
    ),
    "412": (
        [
            (method, "/items/1", {"If-Match": '"v0"'}, True)
            for method in ("PUT", "PATCH", "DELETE")
        ],
        check_412,
    ),
    "fresh-validators": (
        [
            ("PUT", "/items/1", {"If-Match": '"v1"'}, True),
            ("PATCH", "/items/1", {"If-Match": '"v2"'}, True),
        ],
        check_fresh,
    ),
    "401-before-load": (
        [
            ("GET", "/items/1", {"If-None-Match": '"v1"'}, False),
            ("PUT", "/items/1", {"If-Match": '"stale"'}, False),
            ("DELETE", "/items/1", {}, False),
            ("GET", "/items/1", {"If-None-Match": '"v1"'}, True),
        ],
        check_401,
    ),
    "hidden-404": (
        [
            (method, path, headers, True)
            for method, headers in itertools.product(METHODS, IF_HEADERS)
            for path in (f"/items/{HIDDEN}", f"/items/{MISSING}")
        ],
        check_hidden,
    ),
}


def summary(response: Any) -> tuple[int, dict[str, str], bytes]:
    return response.status_code, dict(response.headers), response.content


@pytest.mark.parametrize("load_kind", list(LOADS))
@pytest.mark.parametrize("case", list(CASES))
def test_async_matches_sync(case, load_kind):
    requests, check = CASES[case]
    sync_store, async_store = Store(), Store()
    sync_client = TestClient(make_api(item_router(sync_store), sync_store))
    load = LOADS[load_kind](async_store)
    async_client = TestAsyncClient(
        make_api(async_item_router(async_store, load), async_store)
    )

    def run(client):
        return [
            send(client, method, path, {**headers, **(ALICE if authed else {})})
            for method, path, headers, authed in requests
        ]

    expected, responses = run(sync_client), run(async_client)
    check(responses, async_store)
    assert [summary(r) for r in responses] == [summary(r) for r in expected]
    assert async_store.calls == sync_store.calls
    assert async_store.items == sync_store.items


def test_wrapper_kind_follows_the_handler(store):
    router = Router()

    @scrudful(load=store.load, etag_func=etag)
    async def async_handler(request, pk: int, obj): ...

    @scrudful(load=store.load, etag_func=etag)
    def sync_handler(request, pk: int, obj): ...

    assert inspect.iscoroutinefunction(async_handler)
    assert not inspect.iscoroutinefunction(sync_handler)
    router.get("/a/{pk}")(async_handler)
    router.get("/s/{pk}")(sync_handler)
    operations = {
        path: view.operations[0] for path, view in router.path_operations.items()
    }
    assert type(operations["/a/{pk}"]) is AsyncOperation
    assert type(operations["/s/{pk}"]) is Operation


def test_awaitable_result_of_sync_callable_is_awaited(store):
    client = TestAsyncClient(async_item_router(store, awaitable_load(store)))
    response = send(client, "GET", "/items/1", {})
    assert response.status_code == 200
    assert response.headers["ETag"] == '"v1"'
    response = send(client, "PUT", "/items/1", {"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response.headers["ETag"] == '"v2"'


def coroutine_function(func):
    async def wrapped(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapped


def awaitable_result(func):
    def wrapped(*args, **kwargs):
        async def result():
            return func(*args, **kwargs)

        return result()

    return wrapped


def async_callable_object(func):
    class AsyncCallable:
        async def __call__(self, *args, **kwargs):
            return func(*args, **kwargs)

    return AsyncCallable()


@pytest.mark.parametrize(
    "kind",
    [lambda func: func, coroutine_function, awaitable_result, async_callable_object],
    ids=["sync", "async", "awaitable", "async-callable-object"],
)
def test_callbacks_sync_or_async(store, kind):
    seen = []

    def link(request, obj):
        seen.append((request.method, None if obj is None else obj["version"]))
        return "/schemas/item"

    router = Router()

    @router.api_operation(["GET", "PUT"], "/items/{pk}")
    @scrudful(
        load=kind(store.load),
        etag_func=kind(etag),
        last_modified_func=kind(modified),
        schema_link_or_func=kind(link),
        schema_rel_or_func=kind(lambda request, obj: "describedBy"),
        schema_type_or_func=kind(lambda request, obj: "application/json"),
        context_link_or_func=kind(lambda request, obj: "/contexts/item"),
    )
    async def item(request, pk: int, obj):
        if request.method == "PUT":
            update(obj, "new")
        return {"version": obj["version"]}

    client = TestAsyncClient(router)
    response = send(client, "GET", "/items/1", {})
    assert response.status_code == 200
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    response = send(client, "GET", "/items/1", {"If-None-Match": '"v1"'})
    assert response.status_code == 304
    response = send(client, "PUT", "/items/1", {"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response.headers["ETag"] == '"v2"'
    assert response.headers["Last-Modified"] == "Tue, 02 Jan 2024 05:04:05 GMT"
    # The link callables get the object loaded again after the PUT.
    assert seen == [("GET", 1), ("PUT", 2)]


def test_sync_callables_run_off_the_event_loop(store):
    loops = []

    def record(func):
        def wrapped(*args, **kwargs):
            try:
                loops.append(asyncio.get_running_loop())
            except RuntimeError:  # no event loop runs in this thread
                loops.append(None)
            return func(*args, **kwargs)

        return wrapped

    router = Router()

    @router.get("/items/{pk}")
    @scrudful(
        load=record(store.load),
        etag_func=record(etag),
        last_modified_func=record(modified),
        schema_link_or_func=record(lambda request, obj: "/schemas/item"),
    )
    async def get_item(request, pk: int, obj):
        return {"version": obj["version"]}

    response = send(TestAsyncClient(router), "GET", "/items/1", {})
    assert response.status_code == 200
    assert loops == [None] * 4


@pytest.mark.parametrize("load_kind", list(LOADS))
@pytest.mark.parametrize(
    "error, status", [(Http404("gone"), 404), (HttpError(403, "Forbidden"), 403)]
)
def test_load_errors_use_ninja_handlers(store, load_kind, error, status):
    def failing(request, pk: int):
        raise error

    load = {"sync-load": failing, "async-load": coroutine_function(failing)}
    router = Router()

    @router.get("/items/{pk}")
    @scrudful(load=load[load_kind], etag_func=etag)
    async def get_item(request, pk: int, obj):
        raise AssertionError("not reached")

    response = send(TestAsyncClient(router), "GET", "/items/1", {})
    assert response.status_code == status
    assert response.headers["Content-Type"].startswith("application/json")
    assert "ETag" not in response.headers


def test_sync_load_runs_in_thread(db):
    item = Item.objects.create(name="a")
    counts = []

    def load(request, pk: int):
        counts.append(Item.objects.count())
        return Item.objects.get(pk=pk)

    router = Router()

    @router.api_operation(["GET", "PUT"], "/items/{pk}")
    @scrudful(
        load=load,
        etag_func=lambda item: item.version,
        last_modified_func=lambda item: item.updated_at,
    )
    async def item_view(request, pk: int, obj: Item):
        if request.method == "PUT":
            obj.name = "b"
            await obj.asave()
        return {"name": obj.name}

    client = TestAsyncClient(router)
    response = send(client, "GET", f"/items/{item.pk}", {})
    assert response.status_code == 200
    assert response.headers["ETag"] == '"1"'
    response = send(client, "PUT", f"/items/{item.pk}", {"If-Match": '"1"'})
    assert response.status_code == 200
    assert response.json() == {"name": "b"}
    assert response.headers["ETag"] == '"2"'
    assert counts == [1, 1, 1]


async def async_value(*args, **kwargs):
    return "/schemas/item"


ASYNC_CALLABLES = {
    "load": async_value,
    "etag_func": async_value,
    "last_modified_func": functools.partial(async_value),
    "schema_link_or_func": async_value,
    "context_type_or_func": async_callable_object(lambda *args: "x"),
}


@pytest.mark.parametrize("name", list(ASYNC_CALLABLES))
def test_sync_handler_rejects_async_callables(store, name):
    options = {"load": store.load, name: ASYNC_CALLABLES[name]}
    decorator = scrudful(**options)

    def handler(request, pk: int, obj): ...

    with pytest.raises(
        TypeError, match="async callables need an async def handler"
    ) as info:
        decorator(handler)
    assert str(info.value).startswith(f"{name} is async")


def test_sync_handler_accepts_sync_callables_and_values(store):
    @scrudful(
        load=store.load,
        etag_func=etag,
        last_modified_func=functools.partial(modified),
        schema_link_or_func=lambda request, obj: "/schemas/item",
        schema_rel_or_func=None,
        context_link_or_func="/contexts/item",
    )
    def handler(request, pk: int, obj): ...

    assert not inspect.iscoroutinefunction(handler)


@pytest.mark.parametrize("name", list(ASYNC_CALLABLES))
def test_async_handler_accepts_async_callables(store, name):
    options = {"load": store.load, name: ASYNC_CALLABLES[name]}

    @scrudful(**options)
    async def handler(request, pk: int, obj): ...

    assert inspect.iscoroutinefunction(handler)
