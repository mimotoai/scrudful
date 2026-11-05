"""OPTIONS, the 204 that ``ScrudfulRouter`` adds to every path, and the HTTP
Schema Resource its link targets (served by GET on the HSR route)."""

import json
import uuid
from typing import Any, List

import pytest
from asgiref.sync import async_to_sync
from django.http import HttpResponse
from django.test import override_settings
from django.urls import path
from ninja import NinjaAPI, Router, Status
from ninja.errors import ConfigError, HttpError
from ninja.testing import TestAsyncClient, TestClient
from rest_framework import viewsets
from rest_framework.response import Response

from scrudful._adapter import SCHEMA_REL
from scrudful._hsr import hsr_link, hsr_route_name, options_operation
from scrudful.ninja import (
    CONDITIONAL_READ_RESPONSES,
    CONDITIONAL_WRITE_RESPONSES,
    Entity,
    ScrudfulRouter,
    described,
    scrudful,
    scrudful_create,
    scrudful_list,
)
from scrudful.rest_framework import http_schema_resource as drf_http_schema_resource
from scrudful.rest_framework import scrudful_viewset
from tests.test_ninja_adapter import (
    ALICE,
    HIDDEN,
    LINKS,
    TEST_BASE_URL,
    ItemIn,
    ItemOut,
    KeyAuth,
    Message,
    Store,
    etag,
    expose,
    modified,
)
from tests.semantic_http import (
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    validate_http_schema_resource,
)

# ninja's TestClient builds absolute URIs against this base.
BASE = "http://testlocation"


def content(schema: str, context: str | None = None) -> dict[str, Any]:
    """The ``content`` of a JSON entity with these links, resolved."""
    entry: dict[str, Any] = {"schema": {"$ref": f"{BASE}{schema}"}}
    if context is not None:
        entry["x-rdf-context"] = {
            "href": f"{BASE}{context}",
            "type": "application/ld+json",
        }
    return {"application/json": entry}


ITEM_CONTENT = content("/schemas/item", "/contexts/item")
LIST_LINKS = dict(
    list_schema_link_or_func="/schemas/items",
    list_context_link_or_func="/contexts/items",
)
LIST_CONTENT = content("/schemas/items", "/contexts/items")
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
#: The ``default`` entry of an operation without an error entity.
DEFAULT_ERROR = {"description": "Error."}
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


def hsr_get(client: Any, template: str, prefix: str = "") -> dict[str, Any]:
    """The HTTP Schema Resource of the path ``template`` (as given to the
    operation decorator) of the router mounted at ``prefix``: a GET of its
    route, ``/_hsr/<name>``, which runs no auth, load or handler."""
    response = client.get(f"{prefix}/_hsr/{hsr_route_name(template)}")
    headers = dict(response.headers)
    assert hsr_get_problems(response.status_code, headers, response.content) == []
    return json.loads(response.content)


def hsr_target(template: str, prefix: str = "", base: str = BASE) -> str:
    """The HSR link target of the path ``template``."""
    return f"{base}{prefix}/_hsr/{hsr_route_name(template)}"


def options_204(
    client: Any, url: str, allow: str, target: str | None, **kwargs: Any
) -> Any:
    """The OPTIONS response of ``url``: a 204 with ``Allow: <allow>`` (exactly,
    in its order) and the HSR link (to ``target``, unless ``None``)."""
    response = options(client, url, **kwargs)
    headers = dict(response.headers)
    expected = {method.strip() for method in allow.split(",")}
    assert (
        options_204_problems(response.status_code, headers, response.content, expected)
        == []
    ), url
    assert response.headers["Allow"] == allow, url
    assert hsr_link_problems(headers, target) == [], url
    return response


def item_router(
    store: Store,
    router: Router | None = None,
    methods: tuple[str, ...] = ("GET", "PUT", "DELETE"),
) -> Router:
    """Scrudful ``methods`` on ``/items/{pk}``, with both validators."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL) if router is None else router
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
    router = ScrudfulRouter(base_url=TEST_BASE_URL) if router is None else router

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
        # The list's: a create's preconditions are the collection's.
        list_load=lambda request, **params: list(store.items.values()),
        list_etag_func=lambda items: f"n{len(items)}",
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


# --- Semantic HTTP 0.1 ---------------------------------------------------------

#: An error entity with both links, so that its JSON bodies conform.
ERROR = Entity(
    schema_link_or_func="/schemas/error", context_link_or_func="/contexts/error"
)


def described_router(store: Store) -> Router:
    """One path of each kind: item, list and create, search, ``error``, no links."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    # Before ``/items/{pk}``, which would match it too.
    @router.post("/items/search", response=List[ItemOut])
    @scrudful(
        request=Entity(
            schema_link_or_func="/schemas/search",
            context_link_or_func="/contexts/search",
        ),
        **LINKS,
    )
    def search(request, payload: ItemIn):
        return []

    item_router(store, router, methods=("GET", "PUT", "PATCH", "DELETE"))
    list_router(store, router)

    @router.put("/jobs/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(load=store.load, etag_func=etag, error=ERROR, **LINKS)
    def put_job(request, pk: int, payload: ItemIn, obj):
        return obj

    @router.get("/bare/{pk}")
    @scrudful(request=Entity(), responses={200: Entity()})
    def get_bare(request, pk: int):
        return {}

    return router


#: A URL of each path of ``described_router``: its template, and the
#: ``Allow`` of its OPTIONS (no auth, no hook: every method).
DESCRIBED_PATHS = {
    "/items/1": ("/items/{pk}", "GET, PUT, DELETE, OPTIONS, PATCH"),
    "/items": ("/items", "GET, POST, OPTIONS"),
    "/items/search": ("/items/search", "POST, OPTIONS"),
    "/jobs/1": ("/jobs/{pk}", "PUT, OPTIONS"),
    "/bare/1": ("/bare/{pk}", "GET, OPTIONS"),
}


def conformance_router(store: Store) -> Router:
    """The shape of the example project: an item GET/HEAD, PUT and PATCH with
    ``error``, a collection create and an item with a relative schema URL."""
    # Django's test client serves it: its absolute URIs are testserver's.
    router = ScrudfulRouter(base_url="http://testserver/")
    item = dict(
        load=store.load,
        etag_func=etag,
        last_modified_func=modified,
        error=ERROR,
        **LINKS,
    )

    @router.api_operation(["GET", "HEAD"], "/items/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    @router.put("/items/{pk}", response=ItemOut)
    @scrudful(**item)
    def put_item(request, pk: int, payload: ItemIn, obj):
        return obj

    @router.patch("/items/{pk}", response=ItemOut)
    @scrudful(**item)
    def patch_item(request, pk: int, payload: ItemIn, obj):
        return obj

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(
        location_func=lambda request, item: f"/items/{item['id']}",
        etag_func=etag,
        **LINKS,
    )
    def create_item(request, payload: ItemIn):
        return {"id": 3, "name": payload.name, "version": 1}

    @router.get("/roles/{pk}")
    @scrudful(
        schema_link_or_func="/schemas/role", context_link_or_func="/contexts/role"
    )
    def get_role(request, pk: int):
        return {}

    return router


def conformance_api() -> NinjaAPI:
    api = NinjaAPI(urls_namespace="test-ninja-options")
    api.add_router("", conformance_router(Store()))
    return api


# For Django's test client: real absolute URIs, and HEAD without content.
urlpatterns = [path("api/", conformance_api().urls)]


def test_options_body_is_valid_0_1(store):
    client = TestClient(described_router(store))
    bodies = {
        url: hsr_get(client, template) for url, (template, _) in DESCRIBED_PATHS.items()
    }
    for body in bodies.values():
        validate_http_schema_resource(body)
    assert {url: list(body) for url, body in bodies.items()} == {
        "/items/1": ["get", "put", "delete", "options", "patch"],
        "/items": ["get", "post", "options"],
        "/items/search": ["post", "options"],
        "/jobs/1": ["put", "options"],
        "/bare/1": ["get", "options"],
    }
    assert bodies["/items/search"]["post"]["requestBody"]["content"] == content(
        "/schemas/search", "/contexts/search"
    )
    assert "content" not in bodies["/bare/1"]["get"]["responses"]["200"]
    assert store.calls == {}


def test_options_headers(store):
    client = TestClient(described_router(store))
    for url, (template, allow) in DESCRIBED_PATHS.items():
        response = options_204(client, url, allow, hsr_target(template))
        assert "Content-Type" not in response.headers, url
        assert response.content == b"", url
        assert expose(response) == {"Link", "Allow"}
        assert hsr_get(client, template)["options"] == options_operation()


def test_options_lists_options_and_head(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.api_operation(["GET", "HEAD"], "/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, error=ERROR, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    client = TestClient(router)
    options_204(client, "/items/1", "GET, OPTIONS, HEAD", hsr_target("/items/{pk}"))
    body = hsr_get(client, "/items/{pk}")
    assert list(body) == ["get", "options", "head"]
    assert body["options"] == options_operation()
    assert body["get"]["responses"]["200"]["content"]
    assert set(body["head"]["responses"]) == set(body["get"]["responses"])
    for response_ in body["head"]["responses"].values():
        assert "content" not in response_
    validate_http_schema_resource(body)


@pytest.mark.urls(__name__)
def test_relative_links_resolved(client, db):
    body = hsr_get(client, "/roles/{pk}", prefix="/api")
    content = body["get"]["responses"]["200"]["content"]["application/json"]
    assert content == {
        "schema": {"$ref": "http://testserver/schemas/role"},
        "x-rdf-context": {
            "href": "http://testserver/contexts/role",
            "type": "application/ld+json",
        },
    }
    response = client.get("/api/roles/1")
    target = hsr_target("/roles/{pk}", prefix="/api", base="http://testserver")
    # The entity's Links, then the HTTP Schema Resource link.
    assert [link for link, _ in parse_links(response.headers["Link"])] == [
        "http://testserver/schemas/role",
        "http://testserver/contexts/role",
        target,
    ]
    assert link_problems("GET", 200, response.headers, True, body) == []
    assert hsr_link_problems(dict(client.options("/api/roles/1").items()), target) == []


def test_mixed_path_agrees(store):
    """A scrudful GET and a plain ninja PUT on one path."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
    item_router(store, router, methods=("GET",))

    @router.put("/items/{pk}", response={200: ItemOut, 404: Message})
    @described(Entity())
    def plain_put(request, pk: int, payload: ItemIn):
        raise HttpError(404, "Not Found")

    client = TestClient(router)
    target = hsr_target("/items/{pk}")
    options_204(client, "/items/1", "GET, PUT, OPTIONS", target)
    body = hsr_get(client, "/items/{pk}")
    assert list(body) == ["get", "put", "options"]
    assert "requestBody" not in body["put"]
    for response_ in body["put"]["responses"].values():
        assert "content" not in response_
    validate_http_schema_resource(body)


@pytest.mark.urls(__name__)
def test_agreement_on_responses(client, db):
    item, collection = "/api/items/1", "/api/items"
    hsr = hsr_get(client, "/items/{pk}", prefix="/api")
    get = client.get(item)
    etag_ = get.headers["ETag"]
    payload = {"name": "a"}
    write = dict(content_type="application/json")
    responses = [
        ("GET", get, hsr),
        ("HEAD", client.head(item), hsr),
        ("PUT", client.put(item, payload, **write, headers={"If-Match": etag_}), hsr),
        (
            "PATCH",
            client.patch(item, payload, **write, headers={"If-Match": etag_}),
            hsr,
        ),
        ("POST", client.post(collection, payload, **write), None),
        ("PUT", client.put(item, payload, **write, headers={"If-Match": '"x"'}), hsr),
        ("PUT", client.put(item, payload, **write), hsr),
    ]
    statuses = [response.status_code for _, response, _ in responses]
    assert statuses == [200, 200, 200, 200, 201, 412, 428]
    collection_hsr = hsr_get(client, "/items", prefix="/api")
    for method, response, body in responses:
        body = collection_hsr if body is None else body
        has_content = bool(response.content)
        assert has_content == (method != "HEAD")
        problems = link_problems(
            method, response.status_code, response.headers, has_content, hsr=body
        )
        assert problems == [], (method, response.status_code)


def test_view_link_not_duplicated(store):
    router = Router()

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj, response: HttpResponse):
        response["Link"] = '<https://o/s>; rel="describedby"'
        return obj

    response = TestClient(router).get("/items/1")
    links = parse_links(response.headers["Link"])
    described = [target for target, params in links if params["rel"] == SCHEMA_REL]
    assert described == ["https://o/s"]
    # The context link is still added.
    assert [target for target, _ in links] == [
        "https://o/s",
        "http://testlocation/contexts/item",
    ]


def test_options_media_type_objects():
    """Non-JSON entities: an ``x-schema``, and an ``x-rdf-context`` unless the
    media type is in-band RDF; the Links agree with them."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
    csv = Entity(
        "/schemas/rows",
        schema_type_or_func="application/csvm+json",
        context_link_or_func=lambda request, obj: "/contexts/rows",
        context_type_or_func="application/csvm+json",
        media_type="Text/CSV; header=present",
    )
    turtle = Entity(
        "/shapes/graph", schema_type_or_func="text/turtle", media_type="text/turtle"
    )

    @router.get("/rows/{pk}")
    @scrudful(responses={200: csv})
    def get_rows(request, pk: int):
        return HttpResponse("a,b\n", content_type="text/csv; header=present")

    @router.get("/graphs/{pk}")
    @scrudful(responses={200: turtle})
    def get_graph(request, pk: int):
        return HttpResponse("<a> <b> <c> .", content_type="text/turtle")

    client = TestClient(router)
    rows = hsr_get(client, "/rows/{pk}")
    graph = hsr_get(client, "/graphs/{pk}")
    for body in (rows, graph):
        validate_http_schema_resource(body)
    assert rows["get"]["responses"]["200"]["content"] == {
        "text/csv; header=present": {
            "x-schema": {
                "href": f"{BASE}/schemas/rows",
                "type": "application/csvm+json",
            },
            "x-rdf-context": {
                "href": f"{BASE}/contexts/rows",
                "type": "application/csvm+json",
            },
        }
    }
    assert graph["get"]["responses"]["200"]["content"] == {
        "text/turtle": {
            "x-schema": {"href": f"{BASE}/shapes/graph", "type": "text/turtle"}
        }
    }
    for url, body in (("/rows/1", rows), ("/graphs/1", graph)):
        response = client.get(url)
        assert link_problems("GET", 200, response.headers, True, body) == [], url


@pytest.mark.parametrize("context_type", [None, "", "application/csvm+json"])
def test_a_context_type_callable_of_non_json_content(context_type):
    """It must give a type: without one, GET fails, and so does the HTTP
    Schema Resource, as a ``ConfigError`` naming the entry."""
    csv = Entity(
        "/schemas/rows",
        schema_type_or_func="application/csvm+json",
        context_link_or_func="/contexts/rows",
        context_type_or_func=lambda request, obj: context_type,
        media_type="text/csv",
    )
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.get("/rows/{pk}")
    @scrudful(responses={200: csv})
    def get_rows(request, pk: int):
        return HttpResponse("a,b\n", content_type="text/csv")

    client = TestClient(router)
    if not context_type:
        with pytest.raises(ValueError, match="the context of 'text/csv' needs a type"):
            client.get("/rows/1")
        with pytest.raises(
            ConfigError, match=r"get_rows\(\)'s 200 response: .*needs a type"
        ):
            client.get("/_hsr/rows.pk")
        return
    body = hsr_get(client, "/rows/{pk}")
    response = client.get("/rows/1")
    # The context link, then the HTTP Schema Resource link.
    assert response.headers["Link"].endswith(
        "<http://testlocation/contexts/rows>; "
        'rel="https://w3id.org/semantic-http/ns#rdf-context"; '
        f'type="application/csvm+json", {hsr_link(hsr_target("/rows/{pk}"))}'
    )
    assert link_problems("GET", 200, response.headers, True, body) == []


def test_options_trace_when_routed(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
    item_router(store, router, methods=("GET",))

    @router.api_operation(["TRACE"], "/items/{pk}", response={200: Message})
    @described(Entity())
    def trace(request, pk: int):
        return {"detail": "trace"}

    client = TestClient(router)
    target = hsr_target("/items/{pk}")
    options_204(client, "/items/1", "GET, OPTIONS, TRACE", target)
    body = hsr_get(client, "/items/{pk}")
    assert list(body) == ["get", "options", "trace"]
    assert body["trace"] == {
        "responses": {"200": {"description": "OK"}, "default": DEFAULT_ERROR}
    }
    validate_http_schema_resource(body)


def test_trace_gets_no_default_entity():
    """A TRACE's content is the echoed request, not the item."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.api_operation(["GET", "TRACE"], "/items/{pk}", response={200: Message})
    @scrudful(**LINKS)
    def get_item(request, pk: int):
        return {"detail": "item"}

    # An entity declared for the status still applies to TRACE.
    @router.api_operation(["GET", "TRACE"], "/echo/{pk}", response={200: Message})
    @scrudful(
        **LINKS,
        responses={200: Entity("/schemas/echo", context_link_or_func="/contexts/echo")},
    )
    def echo(request, pk: int):
        return {"detail": "echo"}

    client = TestClient(router)
    body = hsr_get(client, "/items/{pk}")
    validate_http_schema_resource(body)
    assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT
    assert body["trace"] == {
        "responses": {"200": {"description": "OK"}, "default": DEFAULT_ERROR}
    }
    response = client.request("TRACE", "/items/1")
    assert response.status_code == 200
    # No entity's Links: the HTTP Schema Resource link alone.
    assert response.headers["Link"] == hsr_link(hsr_target("/items/{pk}"))

    body = hsr_get(client, "/echo/{pk}")
    echo_content = content("/schemas/echo", "/contexts/echo")
    assert body["trace"]["responses"]["200"]["content"] == echo_content
    response = client.request("TRACE", "/echo/1")
    assert response.headers["Link"] == (
        '<http://testlocation/schemas/echo>; rel="describedby"; '
        'type="application/schema+json", '
        "<http://testlocation/contexts/echo>; "
        'rel="http://www.w3.org/ns/json-ld#context"; '
        f'type="application/ld+json", {hsr_link(hsr_target("/echo/{pk}"))}'
    )


def test_trace_ignores_preconditions(store):
    """A TRACE's content is no representation: no If-*, no validators."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.api_operation(["GET", "TRACE"], "/items/{pk}", response={200: Message})
    @scrudful(load=store.load, etag_func=etag, last_modified_func=modified)
    def get_item(request, pk: int, obj):
        return {"detail": obj["name"]}

    client = TestClient(router)
    for headers in ({"If-Match": '"nope"'}, {"If-None-Match": '"v1"'}, {}):
        response = client.request("TRACE", "/items/1", headers=headers)
        assert response.status_code == 200, headers
        assert response.json() == {"detail": "a"}
        assert "ETag" not in response.headers
        assert "Last-Modified" not in response.headers
    assert client.get("/items/1", headers={"If-Match": '"nope"'}).status_code == 412
    body = hsr_get(client, "/items/{pk}")
    validate_http_schema_resource(body)
    assert body["trace"] == {
        "responses": {"200": {"description": "OK"}, "default": DEFAULT_ERROR}
    }
    assert "412" in body["get"]["responses"]


def test_router_rejects_options_links():
    with pytest.raises(TypeError):
        ScrudfulRouter(options_schema_link="x")
    with pytest.raises(TypeError):
        ScrudfulRouter(options_context_link="x")
    assert not hasattr(ScrudfulRouter(base_url=TEST_BASE_URL), "options_schema_link")


# --- the automatic operation -----------------------------------------------


def test_options_auto_added(store):
    client = TestClient(item_router(store))
    target = hsr_target("/items/{pk}")
    options_204(client, "/items/1", "GET, PUT, DELETE, OPTIONS", target)
    # The object is loaded (for an exact Allow); no handler runs.
    assert store.calls == {"load": 1}
    body = hsr_get(client, "/items/{pk}")
    assert list(body) == ["get", "put", "delete", "options"]
    assert "If-Match" in [parameter["name"] for parameter in body["put"]["parameters"]]
    assert body["put"]["requestBody"]["content"] == ITEM_CONTENT
    assert store.calls == {"load": 1}


def test_options_body(store):
    body = hsr_get(TestClient(item_router(store)), "/items/{pk}")
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "delete", "options"]
    assert body == {
        "get": {
            "responses": {
                "200": {"description": "OK", "content": ITEM_CONTENT},
                "304": NOT_MODIFIED,
                "400": MALFORMED,
                "412": FAILED,
                "default": DEFAULT_ERROR,
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
                "default": DEFAULT_ERROR,
            },
            "parameters": BOTH_PARAMETERS,
        },
        "delete": {
            "responses": {
                "204": {"description": "No Content"},
                "400": MALFORMED,
                "412": FAILED,
                "428": REQUIRED,
                "default": DEFAULT_ERROR,
            },
            "parameters": BOTH_PARAMETERS,
        },
        "options": options_operation(),
    }


def test_options_not_in_openapi(store):
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", item_router(store))
    paths = api.get_openapi_schema(path_prefix="")["paths"]
    assert {path: set(item) for path, item in paths.items()} == {
        "/items/{pk}": {"get", "put", "delete"}
    }


def test_options_needs_no_credentials(store):
    """OPTIONS isn't gated: without credentials it is a 204 offering only
    OPTIONS, and loads nothing; the HTTP Schema Resource needs none either."""
    client = TestClient(api_with_auth(store, item_router(store)))
    target = hsr_target("/items/{pk}")
    options_204(client, "/items/1", "OPTIONS", target)
    assert store.calls["load"] == 0
    assert set(hsr_get(client, "/items/{pk}")) == {"get", "put", "delete", "options"}
    assert store.calls["load"] == 0
    # The other operations still need credentials.
    assert client.get("/items/1").status_code == 401
    assert client.get("/items/1", headers=ALICE).status_code == 200
    assert store.calls["load"] == 1
    # With them, OPTIONS offers every method, on the object loaded.
    allow = "GET, PUT, DELETE, OPTIONS"
    options_204(client, "/items/1", allow, target, headers=ALICE)
    assert store.calls["load"] == 2


def test_options_same_for_missing_and_hidden(store):
    """OPTIONS loads the object: its loader answers a missing and a hidden
    item alike (a 404), so OPTIONS doesn't tell them apart; the HTTP Schema
    Resource is the same for every URL of the path."""
    client = TestClient(api_with_auth(store, item_router(store)))
    target = hsr_target("/items/{pk}")
    allow = "GET, PUT, DELETE, OPTIONS"
    options_204(client, "/items/1", allow, target, headers=ALICE)
    missing = options(client, "/items/999", headers=ALICE)
    hidden = options(client, f"/items/{HIDDEN}", headers=ALICE)
    assert missing.status_code == hidden.status_code == 404
    assert missing.content == hidden.content
    assert dict(missing.headers) == dict(hidden.headers)
    assert hsr_link_problems(dict(missing.headers), target) == []
    assert "Allow" not in missing.headers
    # A path parameter the operations would refuse (422): no object, and no
    # method its request would refuse is listed.
    options_204(client, "/items/not-a-number", "OPTIONS", target, headers=ALICE)
    # No handler ran.
    assert set(store.calls) == {"auth", "load"}


def test_cors_preflight_not_broken(store):
    """A CORS preflight is a 204 without content or Links, before anything
    runs: no auth, no load (the browser sends it without credentials)."""
    client = TestClient(api_with_auth(store, item_router(store)))
    preflight = options(
        client,
        "/items/1",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "if-match, x-api-key",
        },
    )
    assert preflight.status_code == 204
    assert preflight.content == b""
    assert "Link" not in preflight.headers
    assert "Content-Type" not in preflight.headers
    assert store.calls == {}


def test_options_with_path_converters(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

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
    for url, template in (
        ("/ints/1", "/ints/{int:pk}"),
        (f"/uuids/{uuid.uuid4()}", "/uuids/{uuid:key}"),
        ("/files/a/b.txt", "/files/{path:name}"),
    ):
        options_204(client, url, "GET, OPTIONS", hsr_target(template))
        body = hsr_get(client, template)
        assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT


def test_options_default_router(store):
    api = NinjaAPI(
        default_router=ScrudfulRouter(base_url=TEST_BASE_URL),
        urls_namespace=f"api-{uuid.uuid4().hex}",
    )

    @api.get("/things/{pk}")
    @scrudful(**LINKS)
    def get_thing(request, pk: int):
        return {"pk": pk}

    client = TestClient(api)
    options_204(client, "/things/1", "GET, OPTIONS", hsr_target("/things/{pk}"))
    assert list(hsr_get(client, "/things/{pk}")) == ["get", "options"]


def test_options_head_when_routed(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.api_operation(["GET", "HEAD"], "/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    body = hsr_get(TestClient(router), "/items/{pk}")
    assert list(body) == ["get", "options", "head"]
    # Described like GET, but a HEAD response has no content.
    assert body["head"] == {
        "responses": {
            "200": {"description": "OK"},
            "304": NOT_MODIFIED,
            "400": MALFORMED,
            "412": FAILED,
            "default": DEFAULT_ERROR,
        }
    }
    assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT


def test_options_list_path(store):
    client = TestClient(list_router(store))
    # A collection: nothing to load.
    options_204(client, "/items", "GET, POST, OPTIONS", hsr_target("/items"))
    assert store.calls == {}
    body = hsr_get(client, "/items")
    assert body == {
        "get": {
            "responses": {
                "200": {"description": "OK", "content": LIST_CONTENT},
                "304": NOT_MODIFIED,
                "400": MALFORMED,
                "412": FAILED,
                "default": DEFAULT_ERROR,
            }
        },
        "post": {
            "requestBody": {
                "description": "The content for the resource to be created.",
                "required": True,
                "content": ITEM_CONTENT,
            },
            # Evaluated against the list's ETag, never required: no 428 and
            # no parameters.
            "responses": {
                "201": {"description": "Created", "content": ITEM_CONTENT},
                "400": MALFORMED,
                "412": FAILED,
                "default": DEFAULT_ERROR,
            },
        },
        "options": options_operation(),
    }
    validate_http_schema_resource(body)
    assert store.calls == {}


def test_options_entities():
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
    error = Entity(
        schema_link_or_func="/schemas/error", context_link_or_func="/contexts/error"
    )
    conflict = Entity(
        schema_link_or_func="/schemas/conflict",
        context_link_or_func="/contexts/conflict",
    )

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
            202: Entity(
                schema_link_or_func="/schemas/job",
                context_link_or_func="/contexts/job",
            ),
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
        responses={
            409: Entity(
                schema_link_or_func="/schemas/conflict",
                context_link_or_func="/contexts/conflict",
            )
        },
    )
    def create_job(request, payload: ItemIn):
        return {"id": 1}

    client = TestClient(router)
    body = hsr_get(client, "/jobs/{pk}")
    error_content = content("/schemas/error", "/contexts/error")
    conflict_content = content("/schemas/conflict", "/contexts/conflict")
    assert body["put"]["requestBody"]["content"] == content(
        "/schemas/job-in", "/contexts/job-in"
    )
    assert body["put"]["responses"] == {
        "200": {"description": "OK"},
        # scrudful's own early responses carry the error entity's Links.
        "400": {**MALFORMED, "content": error_content},
        "412": {**FAILED, "content": error_content},
        "428": {**REQUIRED, "content": error_content},
        "202": {
            "description": "Accepted",
            "content": content("/schemas/job", "/contexts/job"),
        },
        # A Media Type Object needs a schema: a context alone is not described.
        "404": {"description": "Not Found"},
        # Without an API-wide entity, ninja's own 401, 403, 422 and 429
        # carry the operation's error entity too: under default.
        "default": {**DEFAULT_ERROR, "content": error_content},
    }
    assert body["delete"]["responses"] == {
        "204": {"description": "No Content"},
        "400": MALFORMED,
        "412": {**FAILED, "content": conflict_content},
        "428": REQUIRED,
        "default": DEFAULT_ERROR,
    }
    post = hsr_get(client, "/jobs")["post"]
    assert "requestBody" not in post
    assert post["responses"] == {
        "201": {"description": "Created", "content": ITEM_CONTENT},
        "400": {"description": "Bad Request"},
        "409": {"description": "Conflict", "content": conflict_content},
        "default": DEFAULT_ERROR,
    }


def test_options_statuses_are_those_the_operation_answers(store):
    """A create answers 201, other operations their declared codes, else 200."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

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
    @described(Entity())
    def plain_post(request, pk: int, payload: ItemIn):
        return {}

    client = TestClient(router)
    search_responses = hsr_get(client, "/items/search")["post"]["responses"]
    assert search_responses == {
        "200": {"description": "OK", "content": ITEM_CONTENT},
        "default": DEFAULT_ERROR,
    }
    archive_responses = hsr_get(client, "/items/{pk}/archive")["post"]["responses"]
    assert archive_responses == {
        "202": {"description": "Accepted"},
        "404": {"description": "Not Found"},
        "400": MALFORMED,
        "412": FAILED,
        "default": DEFAULT_ERROR,
    }
    body = hsr_get(client, "/items/{pk}")
    # The DELETE declares a 200 (with a body: the default entity), not a 204.
    assert body["delete"]["responses"] == {
        "200": {"description": "OK", "content": ITEM_CONTENT},
        "400": MALFORMED,
        "412": FAILED,
        "428": REQUIRED,
        "default": DEFAULT_ERROR,
    }
    # No ``response=``: ninja answers 200.
    assert body["get"]["responses"] == {
        "200": {"description": "OK", "content": ITEM_CONTENT},
        "default": DEFAULT_ERROR,
    }
    # Not scrudful: the declared statuses, without content (an entity
    # without links).
    assert body["post"]["responses"] == {
        "200": {"description": "OK"},
        "400": {"description": "Bad Request"},
        "default": DEFAULT_ERROR,
    }


def test_options_link_callables_are_called_in_describe_mode():
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
    calls = []

    def schema(request, obj):
        calls.append((request, obj))
        return "/schemas/options"

    @router.get("/items/{pk}")
    @scrudful(schema_link_or_func=schema, context_link_or_func="/contexts/get")
    def get_item(request, pk: int):
        return {}

    client = TestClient(router)
    body = hsr_get(client, "/items/{pk}")
    assert body["get"]["responses"]["200"]["content"] == content(
        "/schemas/options", "/contexts/get"
    )
    # Without a request or an object (func(None, None)), once: the HTTP
    # Schema Resource is the same for every request.
    assert calls == [(None, None)]
    hsr_get(TestClient(router), "/items/{pk}")
    assert calls == [(None, None)]
    # OPTIONS calls none: it has no content to describe.
    options_204(client, "/items/6", "GET, OPTIONS", hsr_target("/items/{pk}"))
    assert calls == [(None, None)]


def test_options_async_operation_with_async_request_entity(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
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
        context_link_or_func="/contexts/item",
        request=Entity(
            schema_link_or_func=request_schema,
            context_link_or_func="/contexts/item-in",
        ),
    )
    async def put_item(request, pk: int, payload: ItemIn, obj):
        return obj

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    # The HSR route is a sync view, which ninja's async test client runs on
    # its event loop (an ASGI server runs it in a thread): GET it with the
    # sync client, where the async link callable is awaited.
    body = hsr_get(TestClient(router), "/items/{pk}")
    client = TestAsyncClient(router)
    assert body["put"]["requestBody"]["content"] == content(
        "/schemas/item-in", "/contexts/item-in"
    )
    assert body["put"]["responses"]["200"]["content"] == content(
        "/schemas/item", "/contexts/item"
    )
    assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT
    assert sorted(calls) == [("async", None), ("sync", None)]
    assert store.calls == {}
    # OPTIONS of the async path: the GET's (sync) load, run off the loop,
    # then the PUT's own (async) load, awaited, for PUT's Allow.
    response = async_to_sync(client.request)("OPTIONS", "/items/1")
    allow = {"GET", "PUT", "OPTIONS"}
    assert (
        options_204_problems(
            response.status_code, dict(response.headers), response.content, allow
        )
        == []
    )
    assert store.calls == {"load": 2}


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
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.api_operation(["GET", "OPTIONS"], "/items/{pk}")
    @described(Entity())
    def own(request, pk: int):
        return {"own": request.method}

    item_router(store, router, methods=("PUT", "DELETE"))
    client = TestClient(router)
    assert options(client, "/items/1").json() == {"own": "OPTIONS"}
    path_view = router.path_operations["/items/{pk}"]
    assert len([op for op in path_view.operations if "OPTIONS" in op.methods]) == 1


def test_options_on_a_path_of_described_operations_only():
    """Semantic HTTP requirement 3: a path its HTTP Schema Resource describes
    answers OPTIONS, also without a scrudful operation."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.get("/plain/{pk}")
    @described(Entity())
    def plain(request, pk: int):
        return {}

    options_204(
        TestClient(router), "/plain/1", "GET, OPTIONS", hsr_target("/plain/{pk}")
    )


def test_options_mixed_operations(store):
    """Only some operations of the path are scrudful."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.put("/items/{pk}", response={200: ItemOut, 404: Message})
    @described(Entity())
    def plain_put(request, pk: int, payload: ItemIn):
        raise HttpError(404, "Not Found")

    item_router(store, router, methods=("GET",))

    @router.delete("/items/{pk}")
    @described(Entity())
    def plain_delete(request, pk: int):
        return None

    @router.patch("/items/{pk}", response={200: ItemOut})
    @scrudful(**LINKS)
    def links_only_patch(request, pk: int, payload: ItemIn):
        return {}

    client = TestClient(router)
    body = hsr_get(client, "/items/{pk}")
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "delete", "options", "patch"]
    assert store.calls == {}
    allow = "GET, PUT, DELETE, OPTIONS, PATCH"
    options_204(client, "/items/1", allow, hsr_target("/items/{pk}"))
    # The GET's load (an exact Allow); no handler.
    assert store.calls == {"load": 1}
    assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT
    # Not scrudful (an entity without links): no links and no precondition
    # parameters, as in DRF, but the declared statuses.
    assert body["put"] == {
        "responses": {
            "200": {"description": "OK"},
            "404": {"description": "Not Found"},
            "default": DEFAULT_ERROR,
        },
    }
    # No ``response=``: ninja answers 200 (with the handler's ``None``).
    assert body["delete"] == {
        "responses": {"200": {"description": "OK"}, "default": DEFAULT_ERROR}
    }
    # Scrudful without validators: links, no preconditions.
    assert body["patch"] == {
        "requestBody": {
            "description": "The changes to apply to the resource.",
            "required": True,
            "content": ITEM_CONTENT,
        },
        "responses": {
            "200": {"description": "OK", "content": ITEM_CONTENT},
            "default": DEFAULT_ERROR,
        },
    }


def test_options_nested_router_api_auth(store):
    """Nested in a router with a prefix, under API auth."""
    parent = Router()
    child = item_router(store)
    parent.add_router("/v1", child)
    api = api_with_auth(store, parent, prefix="/shop")
    client = TestClient(api)
    target = hsr_target("/items/{pk}", prefix="/shop/v1")
    options_204(client, "/shop/v1/items/1", "OPTIONS", target)
    body = hsr_get(client, "/items/{pk}", prefix="/shop/v1")
    assert set(body) == {"get", "put", "delete", "options"}
    assert client.get("/shop/v1/items/1").status_code == 401
    assert client.put("/shop/v1/items/1", json={"name": "x"}).status_code == 401
    assert store.calls["load"] == 0
    assert client.get("/shop/v1/items/1", headers=ALICE).status_code == 200
    allow = "GET, PUT, DELETE, OPTIONS"
    options_204(client, "/shop/v1/items/1", allow, target, headers=ALICE)


def test_options_is_not_gated_by_router_and_operation_auth(store):
    """The router's and the operation's ``auth`` decide the GET's place in
    ``Allow``, not whether OPTIONS answers."""
    router = ScrudfulRouter(auth=KeyAuth(store), base_url=TEST_BASE_URL)

    @router.get("/items/{pk}", response=ItemOut, auth=KeyAuth(store))
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    client = TestClient(router)
    target = hsr_target("/items/{pk}")
    options_204(client, "/items/1", "OPTIONS", target)
    assert client.get("/items/1").status_code == 401
    assert store.calls["load"] == 0
    options_204(client, "/items/1", "GET, OPTIONS", target, headers=ALICE)


def test_options_router_mounted_twice(store):
    router = item_router(store)
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/a", router, url_name_prefix="a")
    api.add_router("/b", router, auth=KeyAuth(store), url_name_prefix="b")
    client = TestClient(api)
    # One document for both mounts.
    assert hsr_get(client, "/items/{pk}", "/a") == hsr_get(client, "/items/{pk}", "/b")
    # Each mount's Allow, with its auth, and the HSR link to its own route.
    for prefix, allow in (
        ("/a", "GET, PUT, DELETE, OPTIONS"),
        ("/b", "OPTIONS"),
    ):
        target = hsr_target("/items/{pk}", prefix)
        options_204(client, f"{prefix}/items/1", allow, target)
    assert client.get("/b/items/1").status_code == 401
    response = client.get("/a/items/1")
    assert (
        hsr_link_problems(dict(response.headers), hsr_target("/items/{pk}", "/a")) == []
    )


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


def drf_hsr(actions: dict[str, str]) -> Any:
    """DRF's HTTP Schema Resource of ``ItemViewSet.as_view(actions)``, with
    its links resolved against ``BASE``, as DRF's builder
    (``http_schema_resource``, which its ``hsr_urls()`` route serves) builds
    it. The comparisons are of the documents, ninja's served by its HSR
    route."""
    view = ItemViewSet.as_view(actions)
    # DRF's is built without a request: against its base URL.
    with override_settings(SCRUDFUL={"BASE_URL": TEST_BASE_URL}):
        body = json.loads(json.dumps(drf_http_schema_resource(view)))
    validate_http_schema_resource(body)
    return body


def assert_superset(drf: Any, ninja: Any, path: tuple[str, ...] = ()) -> None:
    """Every key path of ``drf`` exists in ``ninja``, with an equal value."""
    if isinstance(drf, dict):
        assert isinstance(ninja, dict), path
        for key, value in drf.items():
            assert key in ninja, path + (key,)
            assert_superset(value, ninja[key], path + (key,))
    else:
        assert ninja == drf, path


def without_head(drf: dict[str, Any]) -> dict[str, Any]:
    """``drf`` without its ``head``: a DRF viewset answers HEAD on every GET
    route (``ViewSetMixin`` maps it to the GET action), while ninja answers
    it only where an operation declares it, which ``item_router`` doesn't."""
    assert drf["head"] == {
        "responses": {
            status: {"description": response["description"]}
            for status, response in drf["get"]["responses"].items()
        }
    }
    return {key: value for key, value in drf.items() if key != "head"}


def test_options_superset_of_drf(store):
    router = item_router(store, methods=("GET", "PUT", "PATCH", "DELETE"))
    list_router(store, router)
    client = TestClient(router)

    detail = drf_hsr(
        {
            "get": "retrieve",
            "put": "update",
            "patch": "partial_update",
            "delete": "destroy",
        }
    )
    ninja_detail = hsr_get(client, "/items/{pk}")
    # Both describe OPTIONS as the 204 they answer.
    assert detail["options"] == ninja_detail["options"] == options_operation()
    detail = without_head(detail)
    assert list(ninja_detail) == list(detail)
    assert_superset(detail, ninja_detail)
    assert detail["put"]["responses"]["200"]["content"] == ITEM_CONTENT
    # The superset: the statuses ninja's operations declare.
    assert "409" in ninja_detail["put"]["responses"]
    assert "409" not in detail["put"]["responses"]

    listing = drf_hsr({"get": "list", "post": "create"})
    ninja_listing = hsr_get(client, "/items")
    assert listing["options"] == ninja_listing["options"] == options_operation()
    listing = without_head(listing)
    assert list(ninja_listing) == list(listing)
    assert_superset(listing, ninja_listing)
    assert listing["get"]["responses"]["200"]["content"] == LIST_CONTENT
    assert listing["post"]["responses"]["201"]["content"] == ITEM_CONTENT
    assert store.calls == {}
