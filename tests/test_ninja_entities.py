"""Entities, the runtime ``ScrudfulResponse``, general POST and error Links."""

import inspect
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.http import HttpResponse
from ninja import NinjaAPI, Router, Schema, Status
from ninja.errors import ConfigError
from ninja.testing import TestAsyncClient, TestClient

from scrudful.ninja import Entity, ScrudfulResponse, scrudful
from tests.test_ninja_adapter import (
    CONTEXT_LINK,
    LINKS,
    MODIFIED_HTTP,
    SCHEMA_LINK,
    ItemOut,
    Message,
    Store,
    etag,
    expose,
    modified,
)

RESULTS = Entity("/schemas/results", context_link_or_func="/contexts/results")
RESULTS_LINK = (
    '</schemas/results>; rel="describedby"; type="application/schema+json", '
    '</contexts/results>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
QUERY = Entity("/schemas/query", context_link_or_func="/contexts/query")
ERROR = Entity("/schemas/error")
ERROR_LINK = '</schemas/error>; rel="describedby"; type="application/schema+json"'
PROBLEM = Entity("/schemas/problem", context_link_or_func="/contexts/problem")
PROBLEM_LINK = (
    '</schemas/problem>; rel="describedby"; type="application/schema+json", '
    '</contexts/problem>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
JOB_LINK = '</schemas/job>; rel="describedby"; type="application/schema+json"'


class Query(Schema):
    q: str


class Job(Schema):
    id: int


@pytest.fixture
def store() -> Store:
    return Store()


def test_entity_fields():
    entity = Entity("/s", context_link_or_func="/c")
    assert entity.schema_link_or_func == "/s"
    assert entity.context_link_or_func == "/c"
    assert entity.schema_rel_or_func is None
    assert entity.schema_type_or_func is None
    assert entity.context_rel_or_func is None
    assert entity.context_type_or_func is None
    assert entity == Entity(schema_link_or_func="/s", context_link_or_func="/c")
    with pytest.raises(AttributeError):
        entity.schema_link_or_func = "/t"


def test_scrudful_response_defaults():
    meta = ScrudfulResponse()
    assert meta.status is None
    assert meta.location is None
    assert meta.etag is None
    assert meta.last_modified is None
    assert meta.entity is None
    # ``created`` is unset (not None: None may be what a handler created).
    assert meta.created is not None
    assert "unset" in repr(meta.created)
    assert ScrudfulResponse().created is meta.created
    with pytest.raises(AttributeError):
        meta.stauts = 202  # a typo is an error, not a silently ignored attribute


def test_general_post_200_result_links(store):
    router = Router()

    @router.post("/items/search", response=list[ItemOut])
    @scrudful(request=QUERY, responses={200: RESULTS})
    def search(request, query: Query):
        return [item for item in store.items.values() if query.q in item["name"]]

    response = TestClient(router).post("/items/search", json={"q": "a"})
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "a", "version": 1}]
    assert response.headers["Link"] == RESULTS_LINK
    assert "ETag" not in response.headers
    assert "Last-Modified" not in response.headers
    assert "Location" not in response.headers
    assert expose(response) == {"Link"}
    config = search._scrudful
    assert config.request == QUERY
    assert dict(config.responses) == {200: RESULTS}
    assert config.error is None


def test_request_entity_puts_no_header(store):
    router = Router()

    @router.post("/items/search")
    @scrudful(request=QUERY)
    def search(request, query: Query):
        return {"q": query.q}

    response = TestClient(router).post("/items/search", json={"q": "a"})
    assert response.status_code == 200
    assert "Link" not in response.headers
    assert "Access-Control-Expose-Headers" not in response.headers


def test_per_status_entity(store):
    router = Router()

    @router.get("/items/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(load=store.load, etag_func=etag, responses={404: ERROR}, **LINKS)
    def get_item(request, pk: int, obj):
        return Status(404, {"detail": "gone"})

    response = TestClient(router).get("/items/1")
    assert response.status_code == 404
    assert response.headers["Link"] == ERROR_LINK
    assert "ETag" not in response.headers
    assert "Link" in expose(response)


def test_default_entity_only_on_2xx(store):
    router = Router()

    @router.get("/items/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        if pk == 1:
            return Status(404, {"detail": "gone"})
        return obj

    response = TestClient(router).get("/items/1")
    assert response.status_code == 404
    assert "Link" not in response.headers
    assert "Access-Control-Expose-Headers" not in response.headers


def test_per_status_entity_overrides_default_on_2xx(store):
    router = Router()

    @router.get("/items/{pk}", response={200: ItemOut, 203: ItemOut})
    @scrudful(load=store.load, responses={203: ERROR}, **LINKS)
    def get_item(request, pk: int, obj):
        return Status(203, obj) if request.GET.get("partial") else obj

    client = TestClient(router)
    assert client.get("/items/1").headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert client.get("/items/1?partial=1").headers["Link"] == ERROR_LINK


def test_runtime_overrides(store):
    router = Router()

    @router.post("/jobs", response={202: Job})
    @scrudful(**LINKS)
    def start_job(request, meta: ScrudfulResponse):
        meta.status = 202
        meta.location = "/jobs/1"
        meta.etag = "j1"
        meta.entity = Entity("/schemas/job")
        return {"id": 1}

    response = TestClient(router).post("/jobs")
    assert response.status_code == 202
    assert response.json() == {"id": 1}
    assert response.headers["Location"] == "/jobs/1"
    assert response.headers["ETag"] == '"j1"'
    assert response.headers["Link"] == JOB_LINK
    assert {"ETag", "Link", "Location"} <= expose(response)
    assert "Location" in response.headers["Access-Control-Expose-Headers"]


def test_runtime_status_needs_declared_response(store):
    router = Router()

    @router.post("/jobs")
    @scrudful()
    def start_job(request, meta: ScrudfulResponse):
        meta.status = 202
        return {"id": 1}

    # ninja validates a Status against the declared responses.
    with pytest.raises(ConfigError, match="202"):
        TestClient(router).post("/jobs")


def test_runtime_status_with_wildcard_response(store):
    router = Router()

    @router.post("/jobs", response={...: Any})
    @scrudful(**LINKS)
    def start_job(request, meta: ScrudfulResponse):
        meta.status = 201
        return {"id": 1}

    response = TestClient(router).post("/jobs")
    assert response.status_code == 201
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"


def test_returned_status_wins_over_runtime_status(store):
    router = Router()

    @router.post("/jobs", response={200: Job, 202: Job, 409: Message})
    @scrudful(responses={409: ERROR}, **LINKS)
    def start_job(request, meta: ScrudfulResponse):
        meta.status = 202
        if request.GET.get("conflict"):
            return Status(409, {"detail": "busy"})
        if request.GET.get("raw"):
            return HttpResponse(status=200)
        return {"id": 1}

    client = TestClient(router)
    response = client.post("/jobs?conflict=1")
    assert response.status_code == 409
    assert response.headers["Link"] == ERROR_LINK
    response = client.post("/jobs?raw=1")
    assert response.status_code == 200
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert client.post("/jobs").status_code == 202


def test_runtime_validators_override_computed(store):
    router = Router()

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, last_modified_func=modified)
    def get_item(request, pk: int, obj, meta: ScrudfulResponse):
        meta.etag = 'W/"weak"'
        meta.last_modified = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
        return obj

    @router.get("/other/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, last_modified_func=modified)
    def get_other(request, pk: int, obj, meta: ScrudfulResponse):
        meta.last_modified = datetime(2025, 1, 1, 0, 0, 0, 999)
        return obj

    client = TestClient(router)
    response = client.get("/items/1")
    assert response.headers["ETag"] == 'W/"weak"'
    assert response.headers["Last-Modified"] == "Wed, 01 Jan 2025 00:00:00 GMT"
    response = client.get("/other/1")
    assert response.headers["ETag"] == '"v1"'  # computed, not overridden
    assert response.headers["Last-Modified"] == "Wed, 01 Jan 2025 00:00:00 GMT"
    # The preconditions use the computed validators.
    assert client.get("/items/1", headers={"If-None-Match": '"v1"'}).status_code == 304


def test_runtime_values_sent_on_any_status_and_method(store):
    router = Router()

    @router.delete("/items/{pk}", response={404: Message})
    @scrudful(load=store.load, etag_func=etag, last_modified_func=modified)
    def delete_item(request, pk: int, obj, meta: ScrudfulResponse):
        meta.etag = "x"
        meta.last_modified = obj["modified"]
        meta.location = "/items/elsewhere"
        return {"detail": "moved"}

    response = TestClient(router).delete("/items/1", headers={"If-Match": '"v1"'})
    assert response.status_code == 404
    assert response.headers["ETag"] == '"x"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert response.headers["Location"] == "/items/elsewhere"
    assert "Link" not in response.headers


def test_handler_headers_win_over_runtime_values(store):
    router = Router()

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj, response: HttpResponse, meta: ScrudfulResponse):
        response["ETag"] = '"own"'
        response["Location"] = "/own"
        response["Link"] = '</items/2>; rel="next"'
        meta.etag = "meta"
        meta.location = "/meta"
        meta.entity = ERROR
        return obj

    response = TestClient(router).get("/items/1")
    assert response.headers["ETag"] == '"own"'
    assert response.headers["Location"] == "/own"
    assert response.headers["Link"] == f'</items/2>; rel="next", {ERROR_LINK}'


def test_location_iri_is_encoded(store):
    router = Router()

    @router.post("/jobs", response={201: Job})
    @scrudful()
    def start_job(request, meta: ScrudfulResponse):
        meta.location = "/jobs/é"
        return {"id": 1}

    response = TestClient(router).post("/jobs")
    assert response.headers["Location"] == "/jobs/%C3%A9"


def test_runtime_entity_wins_and_gets_the_object(store):
    seen = []

    def schema(request, obj):
        seen.append(obj["id"])
        return f"/schemas/item/{obj['id']}"

    router = Router()

    @router.get("/items/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(load=store.load, responses={404: ERROR}, **LINKS)
    def get_item(request, pk: int, obj, meta: ScrudfulResponse):
        meta.entity = Entity(schema)
        return Status(404, {"detail": "gone"})

    response = TestClient(router).get("/items/1")
    assert response.headers["Link"] == (
        '</schemas/item/1>; rel="describedby"; type="application/schema+json"'
    )
    assert seen == [1]


def test_runtime_entity_must_be_an_entity(store):
    router = Router()

    @router.get("/items")
    @scrudful()
    def get_items(request, meta: ScrudfulResponse):
        meta.entity = "/schemas/items"
        return {}

    with pytest.raises(TypeError, match="Entity"):
        TestClient(router).get("/items")


def test_scrudful_response_hidden_from_openapi(store):
    instances = []
    router = Router()

    @router.post("/jobs", response={202: Job})
    @scrudful()
    def start_job(request, meta: ScrudfulResponse, response: HttpResponse):
        instances.append(meta)
        meta.status = 202
        return {"id": 1}

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag)
    def get_item(request, pk: int, obj, meta: "ScrudfulResponse"):
        instances.append(meta)
        return obj

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", router)
    paths = api.get_openapi_schema(path_prefix="")["paths"]
    assert paths["/jobs"]["post"]["parameters"] == []
    assert "requestBody" not in paths["/jobs"]["post"]
    parameters = paths["/items/{pk}"]["get"]["parameters"]
    assert [parameter["name"] for parameter in parameters] == ["pk"]
    assert list(inspect.signature(start_job).parameters) == ["request", "response"]
    assert list(inspect.signature(get_item).parameters) == [
        "request",
        "pk",
        "_scrudful_response",
    ]

    client = TestClient(router)
    assert client.post("/jobs").status_code == 202
    assert client.post("/jobs").status_code == 202
    assert client.get("/items/1").status_code == 200
    # One fresh instance per request.
    assert len({id(meta) for meta in instances}) == 3
    assert all(isinstance(meta, ScrudfulResponse) for meta in instances)
    assert instances[2].status is None


def test_load_does_not_receive_scrudful_response(store):
    router = Router()

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag)
    def get_item(request, pk: int, obj, meta: ScrudfulResponse):
        return obj

    assert TestClient(router).get("/items/1").status_code == 200
    assert store.load_params == [{"pk": 1}]


def error_router(store: Store, **options: Any) -> Router:
    router = Router()

    @router.api_operation(["GET", "PUT"], "/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, last_modified_func=modified, **options)
    def item(request, pk: int, obj):
        return obj

    return router


EARLY = [
    ("PUT", {}, 428),
    ("PUT", {"If-Match": '"stale"'}, 412),
    ("PUT", {"If-Match": "bad"}, 400),
    ("GET", {"If-None-Match": "bad"}, 400),
]


@pytest.mark.parametrize("method, headers, status", EARLY)
def test_error_links_on_412_428_400(store, method, headers, status):
    client = TestClient(error_router(store, error=PROBLEM, **LINKS))
    response = client.request(method, "/items/1", headers=headers)
    assert response.status_code == status
    assert response.headers["Link"] == PROBLEM_LINK
    assert "Link" in expose(response)

    client = TestClient(error_router(store, **LINKS))
    response = client.request(method, "/items/1", headers=headers)
    assert response.status_code == status
    assert "Link" not in response.headers
    assert "Link" not in expose(response)


def test_no_error_links_on_304(store):
    client = TestClient(error_router(store, error=PROBLEM, **LINKS))
    response = client.get("/items/1", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert "Link" not in response.headers
    assert response.headers["ETag"] == '"v1"'


def test_error_link_callables_get_the_loaded_object(store):
    seen = []

    def schema(request, obj):
        seen.append((request.method, obj and obj["id"]))
        return "/schemas/problem"

    client = TestClient(
        error_router(store, error=Entity(schema, schema_rel_or_func="profile"))
    )
    response = client.put("/items/1")
    assert response.status_code == 428
    assert response.headers["Link"] == (
        '</schemas/problem>; rel="profile"; type="application/schema+json"'
    )
    assert seen == [("PUT", 1)]


def test_post_action_on_item_preconditions_not_required(store):
    router = Router()

    @router.post("/items/{pk}/approve", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def approve(request, pk: int, obj):
        store.calls["approve"] += 1
        return obj

    client = TestClient(router)
    response = client.post("/items/1/approve")
    assert response.status_code == 200
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    # Validators are not sent after a POST.
    assert "ETag" not in response.headers
    response = client.post("/items/1/approve", headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    response = client.post("/items/1/approve", headers={"If-Match": '"stale"'})
    assert response.status_code == 412
    assert store.calls["approve"] == 2


def test_config_entities(store):
    responses = {404: ERROR}

    @scrudful(request=QUERY, responses=responses, error=PROBLEM, **LINKS)
    def handler(request): ...

    config = handler._scrudful
    assert config.entity == Entity(
        "/schemas/item", context_link_or_func="/contexts/item"
    )
    assert config.request == QUERY
    assert config.error == PROBLEM
    assert dict(config.responses) == {404: ERROR}
    # A copy: changing the argument later changes nothing.
    responses[200] = RESULTS
    assert dict(config.responses) == {404: ERROR}
    with pytest.raises(TypeError):
        config.responses[200] = RESULTS

    @scrudful()
    def bare(request): ...

    assert bare._scrudful.entity == Entity()
    assert bare._scrudful.request is None
    assert bare._scrudful.error is None
    assert dict(bare._scrudful.responses) == {}


@pytest.mark.parametrize(
    "options, match",
    [
        ({"request": "/schemas/query"}, "request"),
        ({"error": {"schema_link_or_func": "/s"}}, "error"),
        ({"responses": {200: "/schemas/results"}}, "responses"),
        ({"responses": {"200": RESULTS}}, "responses"),
        ({"responses": {True: RESULTS}}, "responses"),
        ({"responses": {99: RESULTS}}, "responses"),
        ({"responses": [RESULTS]}, "responses"),
    ],
)
def test_entity_argument_errors(options, match):
    with pytest.raises(TypeError, match=match):
        scrudful(**options)


async def async_value(*args: Any) -> str:
    return "/schemas/x"


@pytest.mark.parametrize(
    "options, name",
    [
        ({"request": Entity(async_value)}, "request.schema_link_or_func"),
        ({"error": Entity(context_rel_or_func=async_value)}, "error.context_rel"),
        ({"responses": {404: Entity(async_value)}}, "responses[404].schema_link"),
    ],
)
def test_sync_handler_rejects_async_entity_callables(options, name):
    decorator = scrudful(**options)

    def handler(request): ...

    with pytest.raises(TypeError, match="async callables need an async def handler"):
        decorator(handler)
    with pytest.raises(TypeError, match=name.replace("[", r"\[")):
        decorator(handler)

    async def async_handler(request): ...

    assert inspect.iscoroutinefunction(decorator(async_handler))


def test_sync_operation_rejects_async_runtime_entity():
    router = Router()

    @router.get("/items")
    @scrudful()
    def get_items(request, meta: ScrudfulResponse):
        meta.entity = Entity(async_value)
        return {}

    with pytest.raises(TypeError, match="async callables need an async def handler"):
        TestClient(router).get("/items")


def test_async_operation_entities_and_runtime_values(store):
    async def schema(request, obj):
        return "/schemas/job"

    router = Router()

    @router.post("/jobs", response={202: Job, 409: Message})
    @scrudful(responses={409: Entity(schema)}, **LINKS)
    async def start_job(request, meta: ScrudfulResponse):
        if request.GET.get("conflict"):
            return Status(409, {"detail": "busy"})
        meta.status = 202
        meta.location = "/jobs/1"
        meta.etag = "j1"
        meta.entity = Entity(lambda request, obj: "/schemas/job")
        return {"id": 1}

    client = TestAsyncClient(router)
    response = async_to_sync(client.post)("/jobs")
    assert response.status_code == 202
    assert response.headers["Location"] == "/jobs/1"
    assert response.headers["ETag"] == '"j1"'
    assert response.headers["Link"] == JOB_LINK
    response = async_to_sync(client.post)("/jobs?conflict=1")
    assert response.status_code == 409
    assert response.headers["Link"] == JOB_LINK


def test_async_operation_error_links(store):
    async def schema(request, obj):
        return "/schemas/problem"

    router = Router()

    @router.put("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, error=Entity(schema))
    async def put_item(request, pk: int, obj):
        return obj

    response = async_to_sync(TestAsyncClient(router).put)("/items/1")
    assert response.status_code == 428
    assert response.headers["Link"] == (
        '</schemas/problem>; rel="describedby"; type="application/schema+json"'
    )


def test_no_default_entity_links_on_204(store):
    """A 204 has no body, so it gets none of the default (2xx) entity's
    Links; an explicit ``responses[204]`` or ``ScrudfulResponse.entity``
    still applies."""
    router = Router()
    options = dict(load=store.load, etag_func=etag, **LINKS)

    @router.delete("/items/{pk}", response={204: None})
    @scrudful(**options)
    def delete_item(request, pk: int, obj):
        return Status(204, None)

    @router.delete("/declared/{pk}", response={204: None})
    @scrudful(**options, responses={204: ERROR})
    def delete_declared(request, pk: int, obj):
        return Status(204, None)

    @router.delete("/runtime/{pk}", response={204: None})
    @scrudful(**options)
    def delete_runtime(request, pk: int, obj, meta: ScrudfulResponse):
        meta.entity = ERROR
        return Status(204, None)

    @router.post("/items/{pk}/touch", response={204: None})
    @scrudful(**LINKS)
    def touch(request, pk: int):
        return Status(204, None)

    client = TestClient(router)
    for path in ("/items/1", "/declared/1", "/runtime/1"):
        response = client.delete(path, headers={"If-Match": '"v1"'})
        assert response.status_code == 204
        if path == "/items/1":
            assert "Link" not in response.headers
            assert "Link" not in expose(response)
        else:
            assert response.headers["Link"] == ERROR_LINK
    response = client.post("/items/1/touch")
    assert response.status_code == 204
    assert "Link" not in response.headers


def test_link_callables_get_the_reloaded_object_without_validators(store):
    """After a 2xx PUT/PATCH, link callables get what ``load`` returns again.

    No validators are configured: the reload is for the Link callables. The
    handler replaces the row, so the first loaded object is stale.
    """
    seen = []

    def schema(request, obj):
        seen.append(obj["version"])
        return f"/schemas/item/v{obj['version']}"

    router = Router()

    @router.api_operation(["PUT", "PATCH"], "/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, schema_link_or_func=schema)
    def put_item(request, pk: int, obj):
        store.items[pk] = {**obj, "version": obj["version"] + 1}
        return store.items[pk]

    client = TestClient(router)
    for method, version in (("PUT", 2), ("PATCH", 3)):
        response = client.request(method, "/items/1")
        assert response.status_code == 200
        assert response.headers["Link"] == (
            f'</schemas/item/v{version}>; rel="describedby"; '
            'type="application/schema+json"'
        )
    assert seen == [2, 3]
    assert store.calls["load"] == 4


def test_no_reload_without_validators_or_link_callables(store):
    router = Router()

    @router.put("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, **LINKS)
    def put_item(request, pk: int, obj):
        return obj

    response = TestClient(router).put("/items/1")
    assert response.status_code == 200
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert store.calls["load"] == 1


def test_operation_registered_on_several_paths(store):
    """Each registration keeps its own declared status (and so its entity)."""

    @scrudful(load=store.load, responses={202: RESULTS}, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    for order in (("x", "y"), ("y", "x")):
        router = Router()
        for name in order:
            if name == "x":
                router.get("/x/{pk}", response=ItemOut)(get_item)
            else:
                router.get("/y/{pk}", response={202: ItemOut})(get_item)
        client = TestClient(router)
        response = client.get("/x/1")
        assert response.status_code == 200
        assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}", order
        response = client.get("/y/1")
        assert response.status_code == 202
        assert response.headers["Link"] == RESULTS_LINK, order
