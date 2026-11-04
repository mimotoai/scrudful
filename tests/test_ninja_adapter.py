"""Single-resource operations of ``scrudful.ninja``, tested with small routers."""

import functools
import inspect
import itertools
import json
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, List

import pytest
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from ninja import NinjaAPI, Router, Schema, Status
from ninja.decorators import decorate_view
from ninja.errors import ConfigError, HttpError
from ninja.security import APIKeyHeader
from ninja.throttling import BaseThrottle
from ninja.testing import TestClient
from ninja.utils import contribute_operation_callback

from scrudful._adapter import READ_DESCRIPTIONS, WRITE_DESCRIPTIONS, resolved_signature
from scrudful.ninja import (
    CONDITIONAL_READ_RESPONSES,
    CONDITIONAL_WRITE_RESPONSES,
    ScrudfulRouter,
    scrudful,
    scrudful_create,
    scrudful_list,
)
from tests import ninja_future_annotations as future_endpoints

# Non-zero microseconds: the header is floored to whole seconds.
MODIFIED = datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
MODIFIED_HTTP = "Tue, 02 Jan 2024 03:04:05 GMT"
SCHEMA_LINK = (
    '<http://testlocation/schemas/item>; rel="describedby"; '
    'type="application/schema+json"'
)
CONTEXT_LINK = (
    '<http://testlocation/contexts/item>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
LINKS = dict(schema_link_or_func="/schemas/item", context_link_or_func="/contexts/item")
METHODS = ["GET", "PUT", "PATCH", "DELETE"]
IF_HEADERS = [{}, {"If-Match": "*"}, {"If-None-Match": "*"}, {"If-Match": '"stale"'}]

ALICE = {"X-API-Key": "alice"}
MISSING = 3  # no such row
HIDDEN = 2  # a row alice may not see


class ItemIn(Schema):
    name: str


class ItemOut(Schema):
    id: int
    name: str
    version: int


class Message(Schema):
    detail: str


class Store:
    """In-memory rows; ``calls`` counts load and handler calls."""

    def __init__(self) -> None:
        self.items: dict[int, dict[str, Any]] = {
            1: {
                "id": 1,
                "name": "a",
                "version": 1,
                "modified": MODIFIED,
                "owner": "alice",
            },
            HIDDEN: {
                "id": HIDDEN,
                "name": "b",
                "version": 7,
                "modified": MODIFIED,
                "owner": "bob",
            },
        }
        self.calls: Counter[str] = Counter()
        self.load_params: list[dict[str, Any]] = []

    def _row(self, pk: int, params: dict[str, Any]) -> dict[str, Any] | None:
        self.calls["load"] += 1
        self.load_params.append({"pk": pk, **params})
        return self.items.get(pk)

    # "No PUT-create": missing and hidden both raise 404.
    def load(self, request: Any, pk: int, **params: Any) -> dict[str, Any]:
        item = self._row(pk, params)
        if item is None or item["owner"] != "alice":
            raise HttpError(404, "Not Found")
        return item

    # "PUT-create with hidden rows" (and "nothing hidden" when no row is hidden).
    def load_or_none(self, request: Any, pk: int, **params: Any) -> Any:
        item = self._row(pk, params)
        if item is not None and item["owner"] != "alice":
            raise HttpError(404, "Not Found")
        return item

    # "PUT-create, hidden treated as missing".
    def load_hidden_as_none(self, request: Any, pk: int, **params: Any) -> Any:
        item = self._row(pk, params)
        return item if item is not None and item["owner"] == "alice" else None


def etag(item: dict[str, Any]) -> str:
    return f"v{item['version']}"


def modified(item: dict[str, Any]) -> datetime:
    return item["modified"]


def item_router(store: Store, load: Any = None) -> Router:
    """GET/PUT/PATCH/DELETE on ``/items/{pk}`` with both validators."""
    options = dict(
        load=load or store.load,
        etag_func=etag,
        last_modified_func=modified,
        **LINKS,
    )
    router = Router()

    @router.get(
        "/items/{pk}",
        response=ItemOut,
        openapi_extra={"responses": CONDITIONAL_READ_RESPONSES},
    )
    @scrudful(**options)
    def get_item(request, pk: int, obj):
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
    def put_item(request, pk: int, payload: ItemIn, obj):
        store.calls["put"] += 1
        if obj is None:
            if pk in store.items:  # a create colliding with a hidden row
                return Status(409, {"detail": "Conflict"})
            store.items[pk] = obj = {"id": pk, "version": 0, "owner": "alice"}
            update(obj, payload.name)
            return Status(201, obj)
        update(obj, payload.name)
        return obj

    @router.patch("/items/{pk}", response=ItemOut)
    @scrudful(**options)
    def patch_item(request, pk: int, payload: ItemIn, obj):
        store.calls["patch"] += 1
        if obj is None:
            raise HttpError(404, "Not Found")
        update(obj, payload.name)
        return obj

    @router.delete("/items/{pk}", response={204: None})
    @scrudful(**options)
    def delete_item(request, pk: int, obj):
        store.calls["delete"] += 1
        if obj is None:
            raise HttpError(404, "Not Found")
        del store.items[pk]

    return router


def update(item: dict[str, Any], name: str) -> None:
    item["name"] = name
    item["version"] += 1
    item["modified"] = MODIFIED + timedelta(hours=item["version"])


def call(client: TestClient, method: str, path: str, headers: dict[str, str]) -> Any:
    body = {"name": "new"} if method in ("PUT", "PATCH") else None
    return client.request(method, path, json=body, headers=headers)


class KeyAuth(APIKeyHeader):
    param_name = "X-API-Key"

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store

    def authenticate(self, request, key):
        self.store.calls["auth"] += 1
        return key if key == "alice" else None


class Deny(BaseThrottle):
    """A throttle refusing every request (ninja answers 429)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def allow_request(self, request):
        self.store.calls["throttle"] += 1
        return False

    def wait(self):
        return 30


def make_api(router: Router, store: Store, **options: Any) -> NinjaAPI:
    api = NinjaAPI(
        auth=KeyAuth(store), urls_namespace=f"api-{uuid.uuid4().hex}", **options
    )
    api.add_router("", router)
    return api


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(item_router(store))


def expose(response: Any) -> set[str]:
    value = response.headers.get("Access-Control-Expose-Headers", "")
    return {name.strip() for name in value.split(",") if name.strip()}


def test_get_sets_validators_and_links(client):
    response = client.get("/items/1")
    assert response.status_code == 200
    assert response.json() == {"id": 1, "name": "a", "version": 1}
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert {"ETag", "Link"} <= expose(response)


def test_get_if_none_match_304(client, store):
    response = client.get("/items/1", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.headers["ETag"] == '"v1"'
    assert response.content == b""
    assert store.calls["get"] == 0


def test_get_if_modified_since_304(client, store):
    response = client.get("/items/1", headers={"If-Modified-Since": MODIFIED_HTTP})
    assert response.status_code == 304
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert store.calls["get"] == 0


def test_put_without_precondition_428(client, store):
    response = client.put("/items/1", json={"name": "new"})
    assert response.status_code == 428
    assert response.json()["accepted-precondition-headers"] == [
        "If-Match",
        "If-Unmodified-Since",
    ]
    assert store.calls["put"] == 0
    assert store.items[1]["version"] == 1


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_put_stale_if_match_412(client, store, method):
    response = call(client, method, "/items/1", {"If-Match": '"v0"'})
    assert response.status_code == 412
    assert response.json() == {"detail": "Precondition failed."}
    assert "ETag" not in response.headers
    assert store.calls[method.lower()] == 0


@pytest.mark.parametrize(
    "headers", [{"If-Match": "v1"}, {"If-None-Match": '"v1'}], ids=["im", "inm"]
)
def test_malformed_if_match_400(client, store, headers):
    response = client.put("/items/1", json={"name": "new"}, headers=headers)
    assert response.status_code == 400
    assert response.json()["detail"].startswith("Malformed")
    assert store.calls["put"] == 0


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_put_success_fresh_validators(client, store, method):
    response = call(client, method, "/items/1", {"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.headers["ETag"] == '"v2"'
    assert response.headers["Last-Modified"] == "Tue, 02 Jan 2024 05:04:05 GMT"
    # Once for the preconditions, once more for the fresh validators.
    assert store.calls["load"] == 2


def test_delete_success_no_validators(client, store):
    response = client.delete("/items/1", headers={"If-Match": '"v1"'})
    assert response.status_code == 204
    assert "ETag" not in response.headers
    assert "Last-Modified" not in response.headers
    assert 1 not in store.items


def test_unauthenticated_get_401_before_load(store):
    client = TestClient(make_api(item_router(store), store))
    response = client.get("/items/1", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 401
    assert "ETag" not in response.headers
    assert store.calls["auth"] == 1
    assert store.calls["load"] == 0

    response = client.get("/items/1", headers={"If-None-Match": '"v1"', **ALICE})
    assert response.status_code == 304


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    "headers",
    [{"If-None-Match": '"v1"'}, {"If-Match": '"stale"'}, {}],
    ids=["match-not-304", "stale-not-412", "none-not-428"],
)
def test_throttle_429_before_load(store, method, headers):
    client = TestClient(make_api(item_router(store), store, throttle=Deny(store)))
    response = call(client, method, "/items/1", {**headers, **ALICE})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert "ETag" not in response.headers
    assert "Last-Modified" not in response.headers
    assert "Link" not in response.headers
    assert store.calls["throttle"] == 1
    assert store.calls["load"] == 0
    assert store.calls[method.lower()] == 0


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "headers", [{"If-Match": '"stale"'}, {}], ids=["stale-not-412", "none-not-428"]
)
def test_unauthenticated_put_401(store, method, headers):
    client = TestClient(make_api(item_router(store), store))
    response = call(client, method, "/items/1", headers)
    assert response.status_code == 401
    assert "ETag" not in response.headers
    assert store.calls["load"] == 0

    authorized = call(client, method, "/items/1", {**headers, **ALICE})
    assert authorized.status_code == (412 if headers else 428)


def test_load_http404_uses_ninja_handler(store):
    def load(request, pk: int):
        raise Http404("no such item")

    router = Router()

    @router.get("/items/{pk}")
    @scrudful(load=load, etag_func=etag)
    def get_item(request, pk: int, obj):
        raise AssertionError("not reached")

    response = TestClient(router).get("/items/1")
    assert response.status_code == 404
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "method, headers",
    list(itertools.product(METHODS, IF_HEADERS)),
    ids=[f"{m}-{h}" for m, h in itertools.product(METHODS, IF_HEADERS)],
)
def test_hidden_resource_same_for_all_if_headers(client, store, method, headers):
    hidden = call(client, method, f"/items/{HIDDEN}", headers)
    missing = call(client, method, f"/items/{MISSING}", headers)
    for response in (hidden, missing):
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        assert "ETag" not in response.headers
        assert "Last-Modified" not in response.headers
    assert dict(hidden.headers) == dict(missing.headers)
    assert store.items[HIDDEN]["version"] == 7


def test_load_none_follows_rfc(store):
    client = TestClient(item_router(store, load=store.load_hidden_as_none))

    response = client.put(
        f"/items/{MISSING}", json={"name": "new"}, headers={"If-None-Match": "*"}
    )
    assert response.status_code == 201
    assert store.calls["put"] == 1

    response = client.put("/items/4", json={"name": "new"}, headers={"If-Match": '"x"'})
    assert response.status_code == 412
    assert store.calls["put"] == 1

    response = client.get("/items/4")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert store.calls["get"] == 1
    assert "ETag" not in response.headers


def test_handler_error_has_no_validators(store):
    router = Router()

    @router.put("/items/{pk}")
    @scrudful(load=store.load, etag_func=etag, last_modified_func=modified, **LINKS)
    def put_item(request, pk: int, obj, response: HttpResponse):
        response["ETag"] = '"handler"'
        raise HttpError(403, "Forbidden")

    client = TestClient(router)
    for method_headers in ({"If-Match": '"v1"'}, {"If-Match": "*"}):
        response = client.put("/items/1", headers=method_headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}
        assert "ETag" not in response.headers
        assert "Last-Modified" not in response.headers


def no_put_create(store: Store) -> None:
    client = TestClient(item_router(store))
    for method, headers in itertools.product(METHODS, IF_HEADERS):
        hidden = call(client, method, f"/items/{HIDDEN}", headers)
        missing = call(client, method, f"/items/{MISSING}", headers)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.content == missing.content
        assert dict(hidden.headers) == dict(missing.headers)


def put_create_nothing_hidden(store: Store) -> None:
    del store.items[HIDDEN]
    client = TestClient(item_router(store, load=store.load_or_none))
    response = client.put(
        f"/items/{MISSING}", json={"name": "new"}, headers={"If-Match": '"v1"'}
    )
    assert response.status_code == 412
    response = client.put(
        f"/items/{MISSING}", json={"name": "new"}, headers={"If-None-Match": "*"}
    )
    assert response.status_code == 201
    # The fresh validators describe the representation just created.
    assert response.headers["ETag"] == '"v1"'
    # Now it exists: a second create-only PUT fails.
    response = client.put(
        f"/items/{MISSING}", json={"name": "new"}, headers={"If-None-Match": "*"}
    )
    assert response.status_code == 412


def put_create_with_hidden(store: Store) -> None:
    client = TestClient(item_router(store, load=store.load_or_none))
    headers = {"If-Match": '"v1"'}
    # The documented trade-off: If-Match tells missing (412) from hidden (404).
    missing = client.put(f"/items/{MISSING}", json={"name": "new"}, headers=headers)
    hidden = client.put(f"/items/{HIDDEN}", json={"name": "new"}, headers=headers)
    assert missing.status_code == 412
    assert hidden.status_code == 404


def hidden_as_missing(store: Store) -> None:
    client = TestClient(item_router(store, load=store.load_hidden_as_none))
    response = client.put(
        f"/items/{HIDDEN}", json={"name": "new"}, headers={"If-None-Match": "*"}
    )
    assert response.status_code == 409
    assert "ETag" not in response.headers
    assert store.items[HIDDEN] == {
        "id": HIDDEN,
        "name": "b",
        "version": 7,
        "modified": MODIFIED,
        "owner": "bob",
    }
    # Missing and hidden look the same to If-Match.
    for pk in (HIDDEN, MISSING):
        response = client.put(
            f"/items/{pk}", json={"name": "new"}, headers={"If-Match": '"v7"'}
        )
        assert response.status_code == 412


@pytest.mark.parametrize(
    "pattern",
    [
        no_put_create,
        put_create_nothing_hidden,
        put_create_with_hidden,
        hidden_as_missing,
    ],
    ids=lambda pattern: pattern.__name__,
)
def test_load_patterns(store, pattern):
    pattern(store)


def test_head_routed_with_get(store):
    router = Router()

    @router.api_operation(["GET", "HEAD"], "/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        store.calls["get"] += 1
        return obj

    client = TestClient(router)
    response = client.request("HEAD", "/items/1")
    assert response.status_code == 200
    assert response.headers["ETag"] == '"v1"'
    response = client.request("HEAD", "/items/1", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.headers["ETag"] == '"v1"'
    assert store.calls["get"] == 1


def test_params_are_validated_types(store):
    router = Router()

    @router.get("/items/{pk}")
    @scrudful(load=store.load, etag_func=etag)
    def get_item(request, pk: int, obj, q: int = 0):
        return {"q": q}

    response = TestClient(router).get("/items/1?q=5")
    assert response.status_code == 200
    assert store.load_params == [{"pk": 1, "q": 5}]
    assert type(store.load_params[0]["pk"]) is int


def test_hidden_params_not_in_openapi(store):
    router = Router()

    @router.get("/items/{pk}")
    @scrudful(load=store.load, etag_func=etag)
    def get_item(request, pk: int, obj: dict):
        return obj

    @router.get("/other/{pk}")
    @scrudful(load=store.load, load_as="item", etag_func=etag)
    def get_other(request, pk: int, response: HttpResponse, item: dict, **kwargs):
        return item

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", router)
    paths = api.get_openapi_schema(path_prefix="")["paths"]
    for path in ("/items/{pk}", "/other/{pk}"):
        parameters = paths[path]["get"]["parameters"]
        assert [parameter["name"] for parameter in parameters] == ["pk"]

    params = inspect.signature(get_item).parameters
    assert list(params) == ["request", "pk", "_scrudful_response"]
    assert params["_scrudful_response"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["_scrudful_response"].annotation is HttpResponse
    assert list(inspect.signature(get_other).parameters) == [
        "request",
        "pk",
        "response",
        "kwargs",
    ]


def test_openapi_constants(store):
    # Int keys, like ninja's own, so that they merge with declared codes.
    assert CONDITIONAL_READ_RESPONSES == {
        status: {"description": text} for status, text in READ_DESCRIPTIONS.items()
    }
    assert CONDITIONAL_WRITE_RESPONSES == {
        status: {"description": text} for status, text in WRITE_DESCRIPTIONS.items()
    }
    assert list(CONDITIONAL_READ_RESPONSES) == [304, 400, 412]
    assert list(CONDITIONAL_WRITE_RESPONSES) == [400, 412, 428]

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", item_router(store))
    schema = api.get_openapi_schema(path_prefix="")
    operations = schema["paths"]["/items/{pk}"]
    assert operations["put"]["responses"][428] == CONDITIONAL_WRITE_RESPONSES[428]
    assert operations["get"]["responses"][304] == CONDITIONAL_READ_RESPONSES[304]
    assert '"428": {"description": ' in json.dumps(schema, sort_keys=True)


def test_openapi_constants_merge_with_declared_codes(store):
    router = Router()

    @router.put(
        "/items/{pk}",
        response={200: ItemOut, 412: Message},
        openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES},
    )
    @scrudful(load=store.load, etag_func=etag)
    def put_item(request, pk: int, obj):
        return obj

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", router)
    schema = api.get_openapi_schema(path_prefix="")
    responses = schema["paths"]["/items/{pk}"]["put"]["responses"]
    assert sorted(responses, key=str) == [200, 400, 412, 428]
    assert responses[412]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Message"
    }
    assert responses[412]["description"] == "Precondition Failed."
    encoded = json.dumps(schema, sort_keys=True)
    assert encoded.count('"412":') == 1


def test_decoration_errors(store):
    def handler(request, pk: int, obj):
        pass

    with pytest.raises(TypeError, match="load"):
        scrudful(etag_func=etag)
    with pytest.raises(TypeError, match="load"):
        scrudful(last_modified_func=modified)
    with pytest.raises(TypeError, match="etag_func"):
        scrudful(load=store.load, etag_func="v1")
    with pytest.raises(TypeError, match="load"):
        scrudful(load="items")
    with pytest.raises(TypeError, match="'item'"):
        scrudful(load=store.load, load_as="item")(handler)

    def generator(request, pk: int, obj):
        yield obj

    async def async_generator(request, pk: int, obj):
        yield obj

    for func in (generator, async_generator):
        with pytest.raises(TypeError, match="generator"):
            scrudful(load=store.load)(func)


def test_future_annotations_module():
    client = TestClient(future_endpoints.make_router())
    response = client.get("/items/1")
    assert response.status_code == 200
    assert response.json() == {"pk": 1, "name": "a"}
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["X-Handler"] == "yes"

    response = client.put("/items/1", json={"name": "b"}, headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response.json() == {"pk": 1, "name": "b"}
    assert response.headers["ETag"] == '"v2"'
    response = client.put("/items/1", json={"name": 1}, headers={"If-Match": '"v2"'})
    assert response.status_code == 422


def test_future_annotations_with_an_unresolvable_hidden_annotation():
    """The hidden parameter's annotation is imported only under TYPE_CHECKING.

    Each annotation is evaluated on its own, so the visible ones (``Body``)
    still reach ninja as classes.
    """
    client = TestClient(future_endpoints.make_type_checking_router())
    response = client.put("/items/1", json={"name": "b"}, headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response.json() == {"pk": 1, "name": "b"}
    assert response.headers["ETag"] == '"v2"'
    response = client.put("/items/1", json={"name": 1}, headers={"If-Match": '"v2"'})
    assert response.status_code == 422
    response = client.put("/items/1", json={"name": "c"})
    assert response.status_code == 428


def test_resolved_signature_evaluates_each_annotation():
    signature = resolved_signature(future_endpoints.put_unchecked_item.__wrapped__)
    annotations = {
        name: param.annotation for name, param in signature.parameters.items()
    }
    assert annotations == {
        "request": HttpRequest,
        "pk": int,
        "body": future_endpoints.Body,
        "item": "TypeCheckingOnly",
    }
    assert signature.return_annotation == dict[str, object]


def test_conditional_responses_defined_once():
    from scrudful import _adapter, fastapi, ninja

    assert ninja.CONDITIONAL_READ_RESPONSES is _adapter.CONDITIONAL_READ_RESPONSES
    assert fastapi.CONDITIONAL_READ_RESPONSES is _adapter.CONDITIONAL_READ_RESPONSES
    assert ninja.CONDITIONAL_WRITE_RESPONSES is _adapter.CONDITIONAL_WRITE_RESPONSES
    assert fastapi.CONDITIONAL_WRITE_RESPONSES is _adapter.CONDITIONAL_WRITE_RESPONSES
    assert CONDITIONAL_READ_RESPONSES == {
        status: {"description": text} for status, text in READ_DESCRIPTIONS.items()
    }


def test_config_attribute(store):
    @scrudful(load=store.load, load_as="item", etag_func=etag, **LINKS)
    def get_item(request, pk: int, item):
        pass

    config = get_item._scrudful
    assert config.kind == "item"
    assert config.load == store.load
    assert config.load_as == "item"
    assert config.has_etag and not config.has_last_modified
    assert config.schema_link_or_func == "/schemas/item"
    assert config.context_link_or_func == "/contexts/item"
    assert config.schema_rel_or_func is None
    with pytest.raises(AttributeError):
        config.kind = "list"


def test_handler_headers_kept_and_link_joined(store):
    router = Router()

    @router.get("/items/{pk}")
    @scrudful(load=store.load, etag_func=etag, last_modified_func=modified, **LINKS)
    def get_item(request, pk: int, obj, response: HttpResponse):
        response["ETag"] = '"own"'
        response["Link"] = '</items/2>; rel="next"'
        response["Access-Control-Expose-Headers"] = "X-Total"
        response.set_cookie("seen", "1")
        return {"id": obj["id"]}

    response = TestClient(router).get("/items/1")
    assert response.status_code == 200
    assert response.headers["ETag"] == '"own"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert response.headers["Link"] == (
        f'</items/2>; rel="next", {SCHEMA_LINK}, {CONTEXT_LINK}'
    )
    assert response.headers["Access-Control-Expose-Headers"] == "X-Total, ETag, Link"
    assert response.cookies["seen"].value == "1"


def test_returned_http_response_gets_the_headers(store):
    router = Router()

    @router.get("/items/{pk}")
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return JsonResponse({"id": obj["id"]}, status=203)

    response = TestClient(router).get("/items/1")
    assert response.status_code == 203
    assert response.json() == {"id": 1}
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert {"ETag", "Link"} <= expose(response)


def test_status_decides_the_validators(store):
    router = Router()

    @router.get("/status/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(load=store.load, etag_func=etag)
    def returned_status(request, pk: int, obj):
        return Status(404, {"detail": "gone"})

    @router.get("/declared/{pk}", response={410: Message})
    @scrudful(load=store.load, etag_func=etag)
    def declared_code(request, pk: int, obj):
        return {"detail": "gone"}

    @router.get("/default/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(load=store.load, etag_func=etag)
    def default_code(request, pk: int, obj):
        return obj

    @router.get("/tuple/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(load=store.load, etag_func=etag)
    def returned_tuple(request, pk: int, obj):
        return 404, {"detail": "gone"}

    client = TestClient(router)
    assert client.get("/status/1").status_code == 404
    assert "ETag" not in client.get("/status/1").headers
    assert client.get("/declared/1").status_code == 410
    assert "ETag" not in client.get("/declared/1").headers
    assert client.get("/default/1").headers["ETag"] == '"v1"'
    with pytest.warns(DeprecationWarning):
        response = client.get("/tuple/1")
    assert response.status_code == 404
    assert "ETag" not in response.headers


def test_link_callables_get_request_and_object(store):
    seen = []

    def schema(request, obj):
        seen.append((request.method, obj and obj["version"]))
        return f"/schemas/item/{obj['version']}" if obj else "/schemas/new"

    router = Router()

    @router.api_operation(["GET", "PUT"], "/items/{pk}", response={200: ItemOut})
    @scrudful(
        load=store.load_or_none,
        etag_func=etag,
        schema_link_or_func=schema,
        schema_rel_or_func=lambda request, obj: "profile",
        context_link_or_func="/contexts/item",
        context_type_or_func=lambda request, obj: "application/json",
    )
    def item(request, pk: int, obj):
        if request.method == "PUT":
            update(obj, "new")
        return obj

    client = TestClient(router)
    response = client.get("/items/1")
    assert response.headers["Link"] == (
        '<http://testlocation/schemas/item/1>; rel="profile"; '
        'type="application/schema+json", '
        "<http://testlocation/contexts/item>; "
        'rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/json"'
    )
    response = client.put("/items/1", headers={"If-Match": '"v1"'})
    # After a PUT the links describe the object loaded again.
    assert response.headers["Link"].startswith("<http://testlocation/schemas/item/2>")
    assert seen == [("GET", 1), ("PUT", 2)]


def test_links_without_load(store):
    router = Router()

    @router.get("/items/{pk}")
    @scrudful(
        schema_link_or_func=lambda request, obj: f"/schemas/{obj}",
        context_link_or_func="/contexts/item",
    )
    def get_item(request, pk: int, obj: str = "q"):
        return {"obj": obj}

    response = TestClient(router).get("/items/1?obj=x", headers={"If-Match": '"x"'})
    assert response.status_code == 200
    assert response.json() == {"obj": "x"}
    assert "ETag" not in response.headers
    assert response.headers["Link"].startswith("<http://testlocation/schemas/None>")


def test_load_without_validators_ignores_preconditions(store):
    router = Router()

    @router.put("/items/{pk}")
    @scrudful(load=store.load, **LINKS)
    def put_item(request, pk: int, obj):
        return {"id": obj["id"]}

    response = TestClient(router).put("/items/1", headers={"If-Match": '"stale"'})
    assert response.status_code == 200
    assert "ETag" not in response.headers
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert store.calls["load"] == 1


@pytest.mark.parametrize("failing", ["etag_func", "schema_link_or_func"])
def test_callback_errors_use_ninja_handlers(store, failing):
    def fail(*args):
        raise HttpError(403, "Forbidden")

    options = {
        "etag_func": etag,
        "schema_link_or_func": "/schemas/item",
        "context_link_or_func": "/contexts/item",
        failing: fail,
    }
    router = Router()

    @router.get("/items/{pk}")
    @scrudful(load=store.load, **options)
    def get_item(request, pk: int, obj):
        return {"id": obj["id"]}

    response = TestClient(router).get("/items/1")
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert "ETag" not in response.headers


def test_naive_last_modified_uses_time_zone(store, settings):
    settings.TIME_ZONE = "Europe/Paris"
    store.items[1]["modified"] = datetime(2024, 1, 2, 4, 4, 5)
    client = TestClient(item_router(store))
    assert client.get("/items/1").headers["Last-Modified"] == MODIFIED_HTTP


def test_load_not_called_when_validation_fails(store):
    client = TestClient(item_router(store))
    response = client.put("/items/1", json={"name": 1}, headers={"If-Match": '"v1"'})
    assert response.status_code == 422
    response = client.get("/items/one", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 422
    assert store.calls["load"] == 0


def test_handler_contribute_list_not_shared(store):
    def callback(operation):
        pass

    def handler(request, pk: int, obj):
        pass

    handler._ninja_contribute_to_operation = [callback]
    wrapper = scrudful(load=store.load)(handler)
    assert handler._ninja_contribute_to_operation == [callback]
    assert wrapper._ninja_contribute_to_operation[0] is callback
    assert len(wrapper._ninja_contribute_to_operation) == 2


def none_etag_router(store: Store) -> Router:
    """``/items/{pk}`` whose ``etag_func`` returns None (no last_modified_func)."""
    router = Router()
    options = dict(load=store.load, etag_func=lambda item: None)

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(**options)
    def get_item(request, pk: int, obj):
        store.calls["get"] += 1
        return obj

    @router.put("/items/{pk}", response=ItemOut)
    @scrudful(**options)
    def put_item(request, pk: int, payload: ItemIn, obj):
        store.calls["put"] += 1
        update(obj, payload.name)
        return obj

    @router.patch("/items/{pk}", response=ItemOut)
    @scrudful(**options)
    def patch_item(request, pk: int, payload: ItemIn, obj):
        store.calls["patch"] += 1
        update(obj, payload.name)
        return obj

    @router.delete("/items/{pk}", response={204: None})
    @scrudful(**options)
    def delete_item(request, pk: int, obj):
        store.calls["delete"] += 1
        del store.items[pk]
        return Status(204, None)

    return router


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_loaded_object_without_validators_exists(store, method):
    """``load`` returned an object: it exists although its validators are
    None (RFC 9110 §13.1.1-2), so writes must be conditional, ``If-None-Match:
    *`` fails and ``If-Match: *`` passes."""
    client = TestClient(none_etag_router(store))

    response = call(client, method, "/items/1", {})
    assert response.status_code == 428
    assert response.json()["accepted-precondition-headers"] == ["If-Match"]

    for headers in ({"If-None-Match": "*"}, {"If-Match": '"v1"'}):
        response = call(client, method, "/items/1", headers)
        assert response.status_code == 412
    assert store.calls[method.lower()] == 0
    assert store.items[1]["version"] == 1

    response = call(client, method, "/items/1", {"If-Match": "*"})
    assert response.status_code == (204 if method == "DELETE" else 200)
    assert "ETag" not in response.headers
    assert store.calls[method.lower()] == 1


def test_loaded_object_without_validators_get(store):
    client = TestClient(none_etag_router(store))
    response = client.get("/items/1", headers={"If-None-Match": "*"})
    assert response.status_code == 304
    assert "ETag" not in response.headers
    assert client.get("/items/1", headers={"If-None-Match": '"v1"'}).status_code == 200
    assert store.calls["get"] == 1


@pytest.mark.parametrize(
    "decorator, name",
    [
        (lambda store: scrudful(load=store.load), "@scrudful"),
        (
            lambda store: scrudful_create(location_func=lambda r, o: "/x"),
            "@scrudful_create",
        ),
        (lambda store: scrudful_list(href_func=lambda r, o: "/x"), "@scrudful_list"),
    ],
)
def test_decorator_above_the_operation_decorator_raises(store, decorator, name):
    """Above the router decorator, ninja has already registered the
    undecorated function: the scrudful decorator would silently do nothing."""
    router = Router()
    with pytest.raises(TypeError, match=f"{name}.*below"):

        @decorator(store)
        @router.get("/items/{pk}")
        def get_item(request, pk: int, obj=None):
            return {}


def wrap_view_func(seen: list[str], label: str) -> Any:
    """A third-party decorator wrapping ``operation.view_func`` when ninja
    builds the operation (with a ``_ninja_contribute_to_operation``
    callback)."""

    def callback(operation: Any) -> None:
        inner = operation.view_func

        @functools.wraps(inner)
        def outer(request: Any, **values: Any) -> Any:
            seen.append(label)
            return inner(request, **values)

        operation.view_func = outer

    def decorator(func: Any) -> Any:
        contribute_operation_callback(func, callback)
        return func

    return decorator


def recording(seen: list[str], label: str) -> Any:
    """A plain decorator: ``func`` wrapped with ``functools.wraps``."""

    def decorator(func: Any) -> Any:
        @functools.wraps(func)
        def inner(request: Any, *args: Any, **values: Any) -> Any:
            seen.append(label)
            return func(request, *args, **values)

        return inner

    return decorator


SCRUDFUL_DECORATORS = [
    pytest.param(
        lambda store: scrudful(load=store.load, etag_func=etag),
        "@scrudful",
        id="scrudful",
    ),
    pytest.param(
        lambda store: scrudful_create(location_func=lambda r, o: "/x"),
        "@scrudful_create",
        id="scrudful_create",
    ),
    pytest.param(
        lambda store: scrudful_list(href_func=lambda r, o: "/x"),
        "@scrudful_list",
        id="scrudful_list",
    ),
]


@pytest.mark.parametrize("decorator, name", SCRUDFUL_DECORATORS)
def test_view_func_wrapper_below_the_decorator_raises(store, decorator, name):
    """A callback contributed below the scrudful decorator runs before it
    binds the operation: the binding would silently discard its wrapper."""
    seen: list[str] = []
    router = Router()
    with pytest.raises(ConfigError) as raised:

        @router.get("/items/{pk}", response={200: List[ItemOut]})
        @decorator(store)
        @wrap_view_func(seen, "below")
        def get_item(request, pk: int, obj=None):
            return []

    message = str(raised.value)
    assert f"{name}: operation GET '/items/{{pk}}' (" in message
    assert "get_item())" in message
    assert "directly below the operation decorator" in message
    assert seen == []


def test_view_func_error_steers_authorization_away_from_below(store):
    """Following the message must not move an auth check below ``@scrudful``,
    where it runs after ``load`` and the preconditions (a 304 leak)."""
    router = Router()
    with pytest.raises(ConfigError) as raised:

        @router.get("/items/{pk}", response=ItemOut)
        @scrudful(load=store.load, etag_func=etag)
        @wrap_view_func([], "below")
        def get_item(request, pk: int, obj):
            return obj

    message = str(raised.value)
    assert "never move an authorization or caching decorator below @scrudful" in (
        message
    )
    assert "runs after load and the preconditions" in message
    assert "authorization in before=, load or auth=" in message
    assert "add_decorator(mode='operation')" in message
    assert "Stacking two scrudful decorators is not supported" in message
    assert '"Placement of other decorators"' in message


def test_stacked_scrudful_decorators_raise(store):
    """Not supported: the inner decorator's binding would replace the outer
    one's wrapper. (With ``load``, the outer one already raises TypeError:
    the inner one hides the ``obj`` parameter.)"""
    router = Router()
    with pytest.raises(ConfigError, match="Stacking two scrudful decorators"):

        @router.get("/items/{pk}", response=ItemOut)
        @scrudful(**LINKS)
        @scrudful(**LINKS)
        def get_item(request, pk: int):
            return store.items[pk]


def test_decorator_between_operation_and_scrudful_raises(store):
    """A plain decorator between the operation decorator and ``@scrudful``
    would be discarded the same way."""
    seen: list[str] = []
    router = Router()
    with pytest.raises(ConfigError, match=r"@scrudful: operation GET .*get_item"):

        @router.get("/items/{pk}", response=ItemOut)
        @recording(seen, "between")
        @scrudful(load=store.load, etag_func=etag)
        def get_item(request, pk: int, obj):
            return obj

    assert seen == []


def test_view_func_wrapper_above_the_decorator_wraps_the_bound_operation(store):
    """Above ``@scrudful``, the callback runs after the binding: it wraps the
    wrapper bound to the operation's own declarations (here, 202)."""
    seen: list[str] = []
    router = Router()

    @router.get("/items/{pk}", response={202: ItemOut})
    @wrap_view_func(seen, "above")
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        seen.append("handler")
        return obj

    response = TestClient(router).get("/items/1")
    assert response.status_code == 202
    assert response.headers["ETag"] == '"v1"'
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert seen == ["above", "handler"]


def test_handler_callbacks_that_keep_view_func_still_run(store):
    """Callbacks below ``@scrudful`` that don't replace ``view_func`` (here
    ``decorate_view``, which wraps ``operation.run``) keep working."""
    seen: list[str] = []
    router = Router()

    @router.get("/items/{pk}", response={203: ItemOut})
    @scrudful(load=store.load, etag_func=etag)
    @decorate_view(recording(seen, "run"))
    def get_item(request, pk: int, obj):
        return obj

    response = TestClient(router).get("/items/1")
    assert response.status_code == 203
    assert response.headers["ETag"] == '"v1"'
    assert seen == ["run"]


def test_decorate_view_between_operation_and_scrudful(store):
    seen: list[str] = []
    router = Router()

    @router.get("/items/{pk}", response={203: ItemOut})
    @decorate_view(recording(seen, "run"))
    @scrudful(load=store.load, etag_func=etag)
    def get_item(request, pk: int, obj):
        return obj

    response = TestClient(router).get("/items/1")
    assert response.status_code == 203
    assert response.headers["ETag"] == '"v1"'
    assert seen == ["run"]


@pytest.mark.parametrize("mode", ["operation", "view"])
def test_router_add_decorator_wraps_the_bound_operations(store, mode):
    """``add_decorator`` applies after the binding, on every mount: each
    operation keeps its declared status and its validators."""
    seen: list[str] = []

    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    router = ScrudfulRouter()
    router.get("/x/{pk}", response=ItemOut)(get_item)
    router.get("/y/{pk}", response={202: ItemOut})(get_item)
    router.add_decorator(recording(seen, mode), mode=mode)
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/one", router, url_name_prefix="one")
    api.add_router("/two", router, url_name_prefix="two")
    client = TestClient(api)
    for prefix in ("/one", "/two"):
        response = client.get(f"{prefix}/x/1")
        assert response.status_code == 200
        assert response.headers["ETag"] == '"v1"'
        assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
        response = client.get(f"{prefix}/y/1", headers={"If-None-Match": '"v1"'})
        assert response.status_code == 304
        response = client.get(f"{prefix}/y/1")
        assert response.status_code == 202
        assert response.headers["ETag"] == '"v1"'
    assert seen == [mode] * 6
