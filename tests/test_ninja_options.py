"""OPTIONS: the HTTP Schema Resource that ``ScrudfulRouter`` adds to every path."""

import json
import uuid
from typing import Any, List

import pytest
from asgiref.sync import async_to_sync
from ninja import NinjaAPI, Router, Status
from ninja.errors import HttpError
from ninja.testing import TestAsyncClient, TestClient
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from scrudful.ninja import (
    CONDITIONAL_READ_RESPONSES,
    CONDITIONAL_WRITE_RESPONSES,
    Entity,
    ScrudfulRouter,
    scrudful,
    scrudful_create,
    scrudful_list,
)
from scrudful.rest_framework import scrudful_viewset
from tests.test_ninja_adapter import (
    ALICE,
    HIDDEN,
    LINKS,
    ItemIn,
    ItemOut,
    KeyAuth,
    Message,
    Store,
    etag,
    expose,
    modified,
)

ITEM_CONTENT = {
    "application/json": {"schema": "/schemas/item", "context": "/contexts/item"}
}
LIST_LINKS = dict(
    list_schema_link_or_func="/schemas/items",
    list_context_link_or_func="/contexts/items",
)
LIST_CONTENT = {
    "application/json": {"schema": "/schemas/items", "context": "/contexts/items"}
}
SCHEMA_RESOURCE = "https://example.com/schemas/http-schema-resource.json"
CONTEXT_RESOURCE = "https://example.com/contexts/http-schema-resource.jsonld"
NOT_MODIFIED = {
    "description": "Not Modified: the representation matches the request's "
    "If-None-Match or If-Modified-Since validators."
}
MALFORMED = {"description": "Malformed If-Match or If-None-Match header."}
FAILED = {"description": "Precondition Failed."}
REQUIRED = {
    "description": "Precondition Required: this request must be conditional "
    "(send If-Match or If-Unmodified-Since)."
}
EITHER = " Either If-Match or If-Unmodified-Since is required."
BOTH_PARAMETERS = [
    {
        "in": "header",
        "name": "If-Match",
        "schema": {"type": "string"},
        "required": False,
        "description": "The entity-tag of the current representation." + EITHER,
    },
    {
        "in": "header",
        "name": "If-Unmodified-Since",
        "schema": {"type": "string"},
        "required": False,
        "description": "The Last-Modified date of the current representation." + EITHER,
    },
]


def options(client: TestClient, path: str, **kwargs: Any) -> Any:
    return client.request("OPTIONS", path, **kwargs)


def item_router(
    store: Store,
    router: Router | None = None,
    methods: tuple[str, ...] = ("GET", "PUT", "DELETE"),
) -> Router:
    """Scrudful ``methods`` on ``/items/{pk}``, with both validators."""
    router = ScrudfulRouter() if router is None else router
    item = dict(load=store.load, etag_func=etag, last_modified_func=modified, **LINKS)
    if "GET" in methods:

        @router.get(
            "/items/{pk}",
            response=ItemOut,
            openapi_extra={"responses": CONDITIONAL_READ_RESPONSES},
        )
        @scrudful(**item)
        def get_item(request, pk: int, obj):
            store.calls["get"] += 1
            return obj

    if "PUT" in methods:

        @router.put(
            "/items/{pk}",
            response={200: ItemOut, 201: ItemOut, 409: Message},
            openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES},
        )
        @scrudful(**item)
        def put_item(request, pk: int, payload: ItemIn, obj):
            store.calls["put"] += 1
            return obj

    if "PATCH" in methods:

        @router.patch("/items/{pk}", response=ItemOut)
        @scrudful(**item)
        def patch_item(request, pk: int, payload: ItemIn, obj):
            store.calls["patch"] += 1
            return obj

    if "DELETE" in methods:

        @router.delete("/items/{pk}", response={204: None})
        @scrudful(**item)
        def delete_item(request, pk: int, obj):
            store.calls["delete"] += 1
            return Status(204, None)

    return router


def list_router(store: Store, router: Router | None = None) -> Router:
    """A scrudful list GET and a create POST on ``/items``."""
    router = ScrudfulRouter() if router is None else router

    @router.get("/items", response=List[ItemOut])
    @scrudful_list(
        href_func=lambda request, item: f"/items/{item['id']}",
        etag_func=etag,
        last_modified_func=modified,
        list_etag_func=lambda items: f"n{len(items)}",
        **LINKS,
        **LIST_LINKS,
    )
    def list_items(request):
        store.calls["list"] += 1
        return list(store.items.values())

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(
        location_func=lambda request, item: f"/items/{item['id']}",
        etag_func=etag,
        last_modified_func=modified,
        **LINKS,
    )
    def create_item(request, payload: ItemIn):
        store.calls["create"] += 1
        return {"id": 3, "name": payload.name, "version": 1}

    return router


def api_with_auth(store: Store, router: Router, prefix: str = "") -> NinjaAPI:
    api = NinjaAPI(auth=KeyAuth(store), urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router(prefix, router)
    return api


@pytest.fixture
def store() -> Store:
    return Store()


# --- the automatic operation -----------------------------------------------


def test_options_auto_added(store):
    response = options(TestClient(item_router(store)), "/items/1")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    body = response.json()
    assert set(body) == {"get", "put", "delete"}
    assert "If-Match" in [parameter["name"] for parameter in body["put"]["parameters"]]
    assert body["put"]["requestBody"]["content"]["application/json"] == {
        "schema": "/schemas/item",
        "context": "/contexts/item",
    }
    assert store.calls == {}


def test_options_body(store):
    body = options(TestClient(item_router(store)), "/items/1").json()
    assert body == {
        "get": {
            "responses": {
                "200": {"description": "OK", "content": ITEM_CONTENT},
                "304": NOT_MODIFIED,
                "400": MALFORMED,
                "412": FAILED,
            }
        },
        "put": {
            "requestBody": {
                "description": "The new content of the resource.",
                "required": True,
                "content": ITEM_CONTENT,
            },
            "responses": {
                "200": {"description": "OK", "content": ITEM_CONTENT},
                "400": MALFORMED,
                "412": FAILED,
                "428": REQUIRED,
                # Declared by the operation: the 201 is a 2xx (the default
                # entity), the 409 has no entity.
                "201": {"description": "Created", "content": ITEM_CONTENT},
                "409": {"description": "Conflict"},
            },
            "parameters": BOTH_PARAMETERS,
        },
        "delete": {
            "responses": {
                "204": {"description": "No Content"},
                "400": MALFORMED,
                "412": FAILED,
                "428": REQUIRED,
            },
            "parameters": BOTH_PARAMETERS,
        },
    }


def test_options_not_in_openapi(store):
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", item_router(store))
    paths = api.get_openapi_schema(path_prefix="")["paths"]
    assert {path: set(item) for path, item in paths.items()} == {
        "/items/{pk}": {"get", "put", "delete"}
    }


def test_options_no_auth_no_load(store):
    client = TestClient(api_with_auth(store, item_router(store)))
    response = options(client, "/items/1")
    assert response.status_code == 200
    assert set(response.json()) == {"get", "put", "delete"}
    assert store.calls == {}
    # The other operations still need credentials.
    assert client.get("/items/1").status_code == 401
    assert client.get("/items/1", headers=ALICE).status_code == 200
    assert store.calls["load"] == 1


def test_options_same_for_missing_and_hidden(store):
    client = TestClient(api_with_auth(store, item_router(store)))
    existing = options(client, "/items/1")
    for pk in (999, HIDDEN, "not-a-number"):
        response = options(client, f"/items/{pk}", headers=ALICE)
        assert response.status_code == 200
        assert response.json() == existing.json()
        assert dict(response.headers) == dict(existing.headers)
    assert store.calls == {}


def test_options_links_configured_and_omitted(store):
    response = options(
        TestClient(
            item_router(
                store,
                ScrudfulRouter(
                    options_schema_link=SCHEMA_RESOURCE,
                    options_context_link=CONTEXT_RESOURCE,
                ),
            )
        ),
        "/items/1",
    )
    assert response.headers["Link"] == (
        f'<{SCHEMA_RESOURCE}>; rel="describedBy"; type="application/json", '
        f'<{CONTEXT_RESOURCE}>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"'
    )
    assert expose(response) == {"Link"}

    schema_only = options(
        TestClient(
            item_router(store, ScrudfulRouter(options_schema_link=SCHEMA_RESOURCE))
        ),
        "/items/1",
    )
    assert schema_only.headers["Link"] == (
        f'<{SCHEMA_RESOURCE}>; rel="describedBy"; type="application/json"'
    )

    omitted = options(TestClient(item_router(store)), "/items/1")
    assert omitted.status_code == 200
    assert "Link" not in omitted.headers
    assert "Access-Control-Expose-Headers" not in omitted.headers


def test_cors_preflight_not_broken(store):
    client = TestClient(api_with_auth(store, item_router(store)))
    plain = options(client, "/items/1")
    preflight = options(
        client,
        "/items/1",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "if-match, x-api-key",
        },
    )
    assert preflight.status_code == 200
    assert preflight.json() == plain.json()


def test_options_with_path_converters(store):
    router = ScrudfulRouter()

    @router.get("/ints/{int:pk}", response=ItemOut)
    @scrudful(**LINKS)
    def get_int(request, pk: int):
        return store.items[pk]

    @router.get("/uuids/{uuid:key}")
    @scrudful(**LINKS)
    def get_uuid(request, key: uuid.UUID):
        return {"key": str(key)}

    @router.get("/files/{path:name}")
    @scrudful(**LINKS)
    def get_file(request, name: str):
        return {"name": name}

    client = TestClient(router)
    for path in ("/ints/1", f"/uuids/{uuid.uuid4()}", "/files/a/b.txt"):
        response = options(client, path)
        assert response.status_code == 200, path
        assert response.json()["get"]["responses"]["200"]["content"] == ITEM_CONTENT


def test_options_default_router(store):
    api = NinjaAPI(
        default_router=ScrudfulRouter(options_schema_link=SCHEMA_RESOURCE),
        urls_namespace=f"api-{uuid.uuid4().hex}",
    )

    @api.get("/things/{pk}")
    @scrudful(**LINKS)
    def get_thing(request, pk: int):
        return {"pk": pk}

    response = options(TestClient(api), "/things/1")
    assert response.status_code == 200
    assert set(response.json()) == {"get"}
    assert response.headers["Link"].startswith(f"<{SCHEMA_RESOURCE}>")


def test_options_head_when_routed(store):
    router = ScrudfulRouter()

    @router.api_operation(["GET", "HEAD"], "/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    body = options(TestClient(router), "/items/1").json()
    assert set(body) == {"get", "head"}
    assert body["head"] == body["get"]
    assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT


def test_options_list_path(store):
    body = options(TestClient(list_router(store)), "/items").json()
    assert body == {
        "get": {
            "responses": {
                "200": {"description": "OK", "content": LIST_CONTENT},
                "304": NOT_MODIFIED,
                "400": MALFORMED,
                "412": FAILED,
            }
        },
        "post": {
            "requestBody": {
                "description": "The content for the resource to be created.",
                "required": True,
                "content": ITEM_CONTENT,
            },
            # A create is never evaluated: no 400/412, no parameters.
            "responses": {"201": {"description": "Created", "content": ITEM_CONTENT}},
        },
    }
    assert store.calls == {}


def test_options_entities():
    router = ScrudfulRouter()
    error = Entity(schema_link_or_func="/schemas/error")
    conflict = Entity(schema_link_or_func="/schemas/conflict")

    @router.put(
        "/jobs/{pk}",
        response={200: ItemOut, 202: Message, 404: Message, 412: Message},
    )
    @scrudful(
        load=lambda request, pk: {"id": pk},
        etag_func=etag,
        **LINKS,
        request=Entity(
            schema_link_or_func="/schemas/job-in",
            context_link_or_func="/contexts/job-in",
        ),
        responses={
            200: Entity(),  # no entity: no content
            202: Entity(schema_link_or_func="/schemas/job"),
            404: Entity(context_link_or_func="/contexts/missing"),
            412: conflict,
        },
        error=error,
    )
    def put_job(request, pk: int, payload: ItemIn, obj):
        return obj

    # Without ``error``, the declared entity of an early status is described.
    @router.delete("/jobs/{pk}", response={204: None})
    @scrudful(
        load=lambda request, pk: {"id": pk}, etag_func=etag, responses={412: conflict}
    )
    def delete_job(request, pk: int, obj):
        return Status(204, None)

    @router.post("/jobs", response={201: ItemOut, 400: Message, 409: Message})
    @scrudful_create(
        location_func=lambda request, job: f"/jobs/{job['id']}",
        **LINKS,
        request=Entity(),  # no request entity
        responses={409: Entity(schema_link_or_func="/schemas/conflict")},
    )
    def create_job(request, payload: ItemIn):
        return {"id": 1}

    client = TestClient(router)
    body = options(client, "/jobs/1").json()
    error_content = {"application/json": {"schema": "/schemas/error"}}
    conflict_content = {"application/json": {"schema": "/schemas/conflict"}}
    assert body["put"]["requestBody"]["content"] == {
        "application/json": {
            "schema": "/schemas/job-in",
            "context": "/contexts/job-in",
        }
    }
    assert body["put"]["responses"] == {
        "200": {"description": "OK"},
        # scrudful's own early responses carry the error entity's Links.
        "400": {**MALFORMED, "content": error_content},
        "412": {**FAILED, "content": error_content},
        "428": {**REQUIRED, "content": error_content},
        "202": {
            "description": "Accepted",
            "content": {"application/json": {"schema": "/schemas/job"}},
        },
        "404": {
            "description": "Not Found",
            "content": {"application/json": {"context": "/contexts/missing"}},
        },
    }
    assert body["delete"]["responses"] == {
        "204": {"description": "No Content"},
        "400": MALFORMED,
        "412": {**FAILED, "content": conflict_content},
        "428": REQUIRED,
    }
    post = options(client, "/jobs").json()["post"]
    assert "content" not in post["requestBody"]
    assert post["responses"] == {
        "201": {"description": "Created", "content": ITEM_CONTENT},
        "400": {"description": "Bad Request"},
        "409": {
            "description": "Conflict",
            "content": {"application/json": {"schema": "/schemas/conflict"}},
        },
    }


def test_options_statuses_are_those_the_operation_answers(store):
    """A create answers 201, other operations their declared codes, else 200."""
    router = ScrudfulRouter()

    @router.post("/items/search", response=List[ItemOut])
    @scrudful(**LINKS)
    def search(request, payload: ItemIn):
        return []

    @router.post("/items/{pk}/archive", response={202: Message, 404: Message})
    @scrudful(load=store.load, etag_func=etag)
    def archive(request, pk: int, obj):
        return Status(202, {"detail": "archiving"})

    @router.delete("/items/{pk}", response=Message)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def delete_item(request, pk: int, obj):
        return {"detail": "deleted"}

    @router.get("/items/{pk}")
    @scrudful(**LINKS)
    def get_item(request, pk: int):
        return {}

    @router.post("/items/{pk}", response={200: ItemOut, 400: Message})
    def plain_post(request, pk: int, payload: ItemIn):
        return {}

    client = TestClient(router)
    search_responses = options(client, "/items/search").json()["post"]["responses"]
    assert search_responses == {"200": {"description": "OK", "content": ITEM_CONTENT}}
    archive_responses = options(client, "/items/1/archive").json()["post"]["responses"]
    assert archive_responses == {
        "202": {"description": "Accepted"},
        "404": {"description": "Not Found"},
        "400": MALFORMED,
        "412": FAILED,
    }
    body = options(client, "/items/1").json()
    # The DELETE declares a 200 (with a body: the default entity), not a 204.
    assert body["delete"]["responses"] == {
        "200": {"description": "OK", "content": ITEM_CONTENT},
        "400": MALFORMED,
        "412": FAILED,
        "428": REQUIRED,
    }
    # No ``response=``: ninja answers 200.
    assert body["get"]["responses"] == {
        "200": {"description": "OK", "content": ITEM_CONTENT}
    }
    # Not scrudful: the declared statuses, without content.
    assert body["post"]["responses"] == {
        "200": {"description": "OK"},
        "400": {"description": "Bad Request"},
    }


def test_options_link_callables_get_the_request_and_none():
    router = ScrudfulRouter()
    calls = []

    def schema(request, obj):
        calls.append((request.method, request.path, obj))
        return f"/schemas/{request.method.lower()}"

    @router.get("/items/{pk}")
    @scrudful(schema_link_or_func=schema)
    def get_item(request, pk: int):
        return {}

    body = options(TestClient(router), "/items/5").json()
    assert body["get"]["responses"]["200"]["content"] == {
        "application/json": {"schema": "/schemas/options"}
    }
    assert calls == [("OPTIONS", "/items/5", None)]


def test_options_async_operation_with_async_request_entity(store):
    router = ScrudfulRouter()
    calls = []

    async def request_schema(request, obj):
        calls.append(("async", obj))
        return "/schemas/item-in"

    def response_schema(request, obj):
        calls.append(("sync", obj))
        return "/schemas/item"

    async def load(request, pk):
        return store.load(request, pk)

    @router.put("/items/{pk}", response=ItemOut)
    @scrudful(
        load=load,
        etag_func=etag,
        schema_link_or_func=response_schema,
        request=Entity(schema_link_or_func=request_schema),
    )
    async def put_item(request, pk: int, payload: ItemIn, obj):
        return obj

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    response = async_to_sync(TestAsyncClient(router).request)("OPTIONS", "/items/1")
    assert response.status_code == 200
    body = response.json()
    assert body["put"]["requestBody"]["content"] == {
        "application/json": {"schema": "/schemas/item-in"}
    }
    assert body["put"]["responses"]["200"]["content"] == {
        "application/json": {"schema": "/schemas/item"}
    }
    assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT
    assert sorted(calls) == [("async", None), ("sync", None)]
    assert store.calls == {}


# --- the implementer's own OPTIONS -------------------------------------------


def test_user_options_wins(store):
    router = item_router(store)

    @router.api_operation(["OPTIONS"], "/items/{pk}")
    def own_options(request, pk: int):
        return {"own": True}

    assert options(TestClient(router), "/items/1").json() == {"own": True}
    path_view = router.path_operations["/items/{pk}"]
    assert [op.view_func for op in path_view.operations if "OPTIONS" in op.methods] == [
        own_options
    ]


def test_user_options_declared_first_is_kept(store):
    router = ScrudfulRouter()

    @router.api_operation(["GET", "OPTIONS"], "/items/{pk}")
    def own(request, pk: int):
        return {"own": request.method}

    item_router(store, router, methods=("PUT", "DELETE"))
    client = TestClient(router)
    assert options(client, "/items/1").json() == {"own": "OPTIONS"}
    path_view = router.path_operations["/items/{pk}"]
    assert len([op for op in path_view.operations if "OPTIONS" in op.methods]) == 1


def test_no_options_without_a_scrudful_operation():
    router = ScrudfulRouter()

    @router.get("/plain/{pk}")
    def plain(request, pk: int):
        return {}

    assert options(TestClient(router), "/plain/1").status_code == 405


def test_options_mixed_operations(store):
    """Review Focus 5: only some operations of the path are scrudful."""
    router = ScrudfulRouter()

    @router.put("/items/{pk}", response={200: ItemOut, 404: Message})
    def plain_put(request, pk: int, payload: ItemIn):
        raise HttpError(404, "Not Found")

    item_router(store, router, methods=("GET",))

    @router.delete("/items/{pk}")
    def plain_delete(request, pk: int):
        return None

    @router.patch("/items/{pk}", response={200: ItemOut})
    @scrudful(**LINKS)
    def links_only_patch(request, pk: int, payload: ItemIn):
        return {}

    body = options(TestClient(router), "/items/1").json()
    assert set(body) == {"get", "put", "patch", "delete"}
    assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT
    # Not scrudful: no links and no precondition parameters, as in DRF, but
    # the declared statuses.
    assert body["put"] == {
        "requestBody": {
            "description": "The new content of the resource.",
            "required": True,
        },
        "responses": {
            "200": {"description": "OK"},
            "404": {"description": "Not Found"},
        },
    }
    # No ``response=``: ninja answers 200 (with the handler's ``None``).
    assert body["delete"] == {"responses": {"200": {"description": "OK"}}}
    # Scrudful without validators: links, no preconditions.
    assert body["patch"] == {
        "requestBody": {
            "description": "The changes to apply to the resource.",
            "required": True,
            "content": ITEM_CONTENT,
        },
        "responses": {"200": {"description": "OK", "content": ITEM_CONTENT}},
    }
    assert store.calls == {}


def test_options_nested_router_api_auth(store):
    """Review Focus 3: nested in a router with a prefix, under API auth."""
    parent = Router()
    child = item_router(store)
    parent.add_router("/v1", child)
    api = api_with_auth(store, parent, prefix="/shop")
    client = TestClient(api)
    response = options(client, "/shop/v1/items/1")
    assert response.status_code == 200
    assert set(response.json()) == {"get", "put", "delete"}
    assert client.get("/shop/v1/items/1").status_code == 401
    assert client.put("/shop/v1/items/1", json={"name": "x"}).status_code == 401
    assert store.calls["load"] == 0
    assert client.get("/shop/v1/items/1", headers=ALICE).status_code == 200


def test_options_ignores_router_and_operation_auth(store):
    router = ScrudfulRouter(auth=KeyAuth(store))

    @router.get("/items/{pk}", response=ItemOut, auth=KeyAuth(store))
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    client = TestClient(router)
    assert options(client, "/items/1").status_code == 200
    assert client.get("/items/1").status_code == 401
    assert store.calls["load"] == 0


def test_options_router_mounted_twice(store):
    router = item_router(store)
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/a", router, url_name_prefix="a")
    api.add_router("/b", router, auth=KeyAuth(store), url_name_prefix="b")
    client = TestClient(api)
    first = options(client, "/a/items/1")
    second = options(client, "/b/items/1")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert client.get("/b/items/1").status_code == 401


# --- parity with DRF -----------------------------------------------------------


@scrudful_viewset
class ItemViewSet(viewsets.ViewSet):
    """The DRF equivalent of ``item_router`` + ``list_router``."""

    class Meta:
        etag_func = staticmethod(lambda view, request, pk: "v1")
        last_modified_func = staticmethod(lambda view, request, pk: 0)
        schema_link_or_func = "/schemas/item"
        context_link_or_func = "/contexts/item"
        list_etag_func = staticmethod(lambda view, request: "n2")
        list_schema_link_or_func = "/schemas/items"
        list_context_link_or_func = "/contexts/items"

    def list(self, request):
        return Response([])

    def create(self, request):
        return Response({}, status=201)

    def retrieve(self, request, pk):
        return Response({})

    def update(self, request, pk):
        return Response({})

    def partial_update(self, request, pk):
        return Response({})

    def destroy(self, request, pk):
        return Response(status=204)


def drf_options(actions: dict[str, str], **kwargs: Any) -> Any:
    view = ItemViewSet.as_view(actions)
    response = view(APIRequestFactory().options("/"), **kwargs)
    assert response.status_code == 200
    return json.loads(json.dumps(response.data))


def assert_superset(drf: Any, ninja: Any, path: tuple[str, ...] = ()) -> None:
    """Every key path of ``drf`` exists in ``ninja``, with an equal value."""
    if isinstance(drf, dict):
        assert isinstance(ninja, dict), path
        for key, value in drf.items():
            assert key in ninja, path + (key,)
            assert_superset(value, ninja[key], path + (key,))
    else:
        assert ninja == drf, path


def test_options_superset_of_drf(store):
    router = item_router(store, methods=("GET", "PUT", "PATCH", "DELETE"))
    list_router(store, router)
    client = TestClient(router)

    detail = drf_options(
        {
            "get": "retrieve",
            "put": "update",
            "patch": "partial_update",
            "delete": "destroy",
        },
        pk="1",
    )
    ninja_detail = options(client, "/items/1").json()
    assert set(ninja_detail) == set(detail)
    assert_superset(detail, ninja_detail)
    # The superset: response content on writes, declared statuses.
    assert "content" not in detail["put"]["responses"]["200"]
    assert ninja_detail["put"]["responses"]["200"]["content"] == ITEM_CONTENT
    assert "409" in ninja_detail["put"]["responses"]

    listing = drf_options({"get": "list", "post": "create"})
    ninja_listing = options(client, "/items").json()
    assert set(ninja_listing) == set(listing)
    assert_superset(listing, ninja_listing)
    assert ninja_listing["post"]["responses"]["201"]["content"] == ITEM_CONTENT
    assert store.calls == {}
