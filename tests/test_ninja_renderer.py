"""The bodies scrudful's ninja adapter writes itself (OPTIONS and the early 400,
412 and 428) go through the serving API's renderer, as every other response."""

import functools
import json
import uuid
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from ninja import NinjaAPI, Router
from ninja.decorators import decorate_view
from ninja.renderers import BaseRenderer, JSONRenderer
from ninja.testing import TestAsyncClient, TestClient
from scrudful.ninja import Entity, ScrudfulRouter, scrudful
from scrudful.problem import PROBLEM_JSON, problem_details

from tests.test_ninja_adapter import (
    LINKS,
    ItemIn,
    ItemOut,
    Store,
    etag,
    modified,
    update,
)

PROBLEM = Entity(schema_link_or_func="/schemas/problem", media_type=PROBLEM_JSON)


class CompactRenderer(JSONRenderer):
    """Compact separators and UTF-8 non-ASCII: other bytes than JsonResponse's."""

    json_dumps_params = {"separators": (",", ":"), "ensure_ascii": False}


class TextRenderer(BaseRenderer):
    """A renderer that does not write JSON."""

    media_type = "text/plain"

    def render(self, request, data, *, response_status):
        return f"status {response_status}"


def compact(body: Any) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def accented_problem(request, result):
    return {**problem_details(request, result), "title": "Précondition échouée"}


def router(store: Store, asynchronous: bool = False, **extra: Any) -> Router:
    """GET and PUT on ``/items/{pk}`` with validators, served by a ScrudfulRouter."""
    options = dict(
        load=store.load, etag_func=etag, last_modified_func=modified, **LINKS, **extra
    )
    items = ScrudfulRouter()

    if asynchronous:

        @items.get("/items/{pk}", response=ItemOut)
        @scrudful(**options)
        async def get_item(request, pk: int, obj):
            return obj

        @items.put("/items/{pk}", response=ItemOut)
        @scrudful(**options)
        async def put_item(request, pk: int, payload: ItemIn, obj):
            update(obj, payload.name)
            return obj

    else:

        @items.get("/items/{pk}", response=ItemOut)
        @scrudful(**options)
        def get_item(request, pk: int, obj):
            return obj

        @items.put("/items/{pk}", response=ItemOut)
        @scrudful(**options)
        def put_item(request, pk: int, payload: ItemIn, obj):
            update(obj, payload.name)
            return obj

    return items


def api_for(items: Router, renderer: BaseRenderer | None = None) -> NinjaAPI:
    api = NinjaAPI(renderer=renderer, urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", items)
    return api


def requests(asynchronous: bool, api: NinjaAPI):
    """``request(method, path, **kwargs)`` through the (async) test client."""
    if asynchronous:
        client = TestAsyncClient(api)
        return lambda *args, **kwargs: async_to_sync(client.request)(*args, **kwargs)
    client = TestClient(api)
    return client.request


#: (method, headers, status) of each response scrudful writes itself.
EARLY = [
    ("OPTIONS", {}, 200),
    ("PUT", {"If-Match": '"v0"'}, 412),
    ("PUT", {}, 428),
    ("PUT", {"If-Match": "v1"}, 400),
]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method, headers, status", EARLY)
def test_custom_renderer_writes_scrudful_bodies(asynchronous, method, headers, status):
    responses = [
        requests(asynchronous, api_for(router(Store(), asynchronous), renderer))(
            method, "/items/1", json={"name": "x"}, headers=headers
        )
        for renderer in (CompactRenderer(), None)
    ]
    response, default = responses
    assert response.status_code == default.status_code == status
    assert response.content == compact(response.json())
    assert response.json() == default.json()
    assert dict(response.headers) == dict(default.headers)
    assert response.headers["Content-Type"] == "application/json"
    if method == "OPTIONS":
        assert response.headers["Allow"]
        assert 'rel="describedby"' in response.headers["Link"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method, headers, status", EARLY[1:])
def test_custom_renderer_error_entity_media_type(asynchronous, method, headers, status):
    items = router(
        Store(), asynchronous, error=PROBLEM, error_body_func=accented_problem
    )
    request = requests(asynchronous, api_for(items, CompactRenderer()))
    response = request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.json()["title"] == "Précondition échouée"
    assert response.content == compact(response.json())
    assert response.headers["Content-Type"] == PROBLEM_JSON
    assert "/schemas/problem" in response.headers["Link"]
    assert "Link" in response.headers["Access-Control-Expose-Headers"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method, headers, status", EARLY)
def test_default_renderer_bodies_unchanged(asynchronous, method, headers, status):
    request = requests(asynchronous, api_for(router(Store(), asynchronous)))
    response = request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.content == json.dumps(response.json()).encode()
    assert response.headers["Content-Type"] == "application/json"


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_not_modified_stays_bodiless(asynchronous):
    request = requests(
        asynchronous, api_for(router(Store(), asynchronous), CompactRenderer())
    )
    response = request("GET", "/items/1", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.content == b""


def test_each_mount_uses_its_own_api_renderer():
    """ninja clones the operations of a router mounted in several APIs."""
    items = router(Store())
    compact_api = api_for(items, CompactRenderer())
    default_api = api_for(items)
    for api, dumps in (
        (compact_api, compact),
        (default_api, lambda body: json.dumps(body).encode()),
    ):
        client = TestClient(api)
        for method, headers, status in EARLY:
            response = client.request(
                method, "/items/1", json={"name": "x"}, headers=headers
            )
            assert response.status_code == status
            assert response.content == dumps(response.json())


@pytest.mark.parametrize("method, headers, status", EARLY)
def test_non_json_renderer_falls_back_to_json(method, headers, status):
    client = TestClient(api_for(router(Store()), TextRenderer()))
    response = client.request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.content == json.dumps(response.json()).encode()
    assert response.headers["Content-Type"] == "application/json"


def passthrough(run):
    @functools.wraps(run)
    def wrapped(request, *args, **kwargs):
        return run(request, *args, **kwargs)

    return wrapped


def test_renderer_found_through_earlier_run_decorators():
    """A ``decorate_view`` below scrudful wraps ``Operation.run`` first."""
    store = Store()
    items = Router()

    @items.put("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag)
    @decorate_view(passthrough)
    def put_item(request, pk: int, payload: ItemIn, obj):
        update(obj, payload.name)
        return obj

    client = TestClient(api_for(items, CompactRenderer()))
    response = client.put("/items/1", json={"name": "x"})
    assert response.status_code == 428
    assert response.content == compact(response.json())
