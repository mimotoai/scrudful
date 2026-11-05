"""The FastAPI HTTP Schema Resource: built per path template, without a request."""

from __future__ import annotations

import copy
import dataclasses
import functools
import hashlib
import re
import typing
import warnings
from typing import Any

import pytest
from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Security,
)
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.convertors import Convertor, register_url_convertor
from starlette.routing import Host, Mount, Route, Router

from scrudful._hsr import (
    HsrConfigError,
    hsr_etag,
    hsr_response_links,
    options_operation,
    serialize_hsr,
)
from scrudful.fastapi import (
    CONDITIONAL_READ_RESPONSES,
    Entity,
    _hsr_target,
    add_error_links,
    add_options,
    http_schema_resource,
    http_schema_resource_endpoint,
    scrudful,
)
from tests import annotation_globals_base, annotation_globals_uses
from tests import fastapi_security_annotations as security_annotations
from tests.semantic_http import (
    HSR_DOCUMENT_REL,
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    validate_http_schema_resource,
)

BASE = "https://api.example/"
OPTIONS_ENTRY = {"responses": {"204": {"description": "No Content"}}}
USER_CONTENT = {
    "application/json": {
        "schema": {"$ref": "https://api.example/schemas/user.json"},
        "x-rdf-context": {
            "href": "https://api.example/contexts/user.jsonld",
            "type": "application/ld+json",
        },
    }
}
APP_ERROR = Entity(
    media_type="application/problem+json",
    schema_link_or_func="/schemas/problem.json",
    context_link_or_func="/contexts/problem.jsonld",
)
APP_ERROR_CONTENT = {
    "application/problem+json": {
        "schema": {"$ref": "https://api.example/schemas/problem.json"},
        "x-rdf-context": {
            "href": "https://api.example/contexts/problem.jsonld",
            "type": "application/ld+json",
        },
    }
}
USER_ERROR = Entity(
    schema_link_or_func="https://errors.example/user-error.json",
    context_link_or_func="https://errors.example/user-error.jsonld",
)
USER_ERROR_CONTENT = {
    "application/json": {
        "schema": {"$ref": "https://errors.example/user-error.json"},
        "x-rdf-context": {
            "href": "https://errors.example/user-error.jsonld",
            "type": "application/ld+json",
        },
    }
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such user"}}
USER_LINKS = dict(
    schema_link_or_func="/schemas/user.json",
    context_link_or_func=lambda **kwargs: "/contexts/user.jsonld",
)


def current_user(authorization: str | None = Header(None)) -> str | None:
    """The requester: ``Bearer <name>``, or anonymous."""
    if authorization is None:
        return None
    scheme, _, name = authorization.partition(" ")
    if scheme != "Bearer" or not name:
        raise HTTPException(status_code=401, detail="Bad credentials")
    return name


def make_app(*, error: Entity | None = APP_ERROR, **options: Any) -> FastAPI:
    """A user API: ``/users/{id}`` (GET, HEAD, PUT) and ``/users`` (POST)."""
    options.setdefault("base_url", BASE)
    app = FastAPI()

    def etag(**kwargs: Any) -> str:
        return '"v1"'

    @app.get("/users/{id}", responses=NOT_FOUND)
    @app.head("/users/{id}", responses=NOT_FOUND)
    @scrudful(etag_func=etag, **USER_LINKS)
    async def get_user(id: int, user: str | None = Depends(current_user)):
        if id == 404:
            raise HTTPException(status_code=404, detail="No such user")
        return {"id": id, "for": user}

    @app.put("/users/{id}")
    @scrudful(etag_func=etag, **USER_LINKS, error=USER_ERROR)
    async def put_user(id: int, user: str | None = Depends(current_user)):
        return {"id": id}

    @app.post("/users", status_code=201)
    @scrudful(**USER_LINKS)
    async def create_user():
        return {"id": 1}

    if error is not None:
        add_error_links(app, error)
    add_options(app, **options)
    return app


@pytest.fixture
def make_client():
    clients = []

    def make(app: FastAPI, **kwargs: Any) -> TestClient:
        client = TestClient(app, **kwargs)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


# --- requester independence ------------------------------------------------------


def test_hsr_identical_across_hosts(make_client):
    """Two apps (so each builds its own document), two Hosts, an anonymous and
    an authenticated requester: one document, equal to the built one."""
    anonymous = options_hsr(
        make_client(make_app()), "/users/7", headers={"Host": "a.example"}
    )
    authenticated = options_hsr(
        make_client(make_app(), base_url="http://b.example"),
        "/users/7",
        headers={"Authorization": "Bearer alice"},
    )
    built = http_schema_resource(make_app(), "/users/{id}")
    assert anonymous == authenticated == built
    assert built["get"]["responses"]["200"]["content"] == USER_CONTENT
    validate_http_schema_resource(built)


def test_options_entry_is_204():
    hsr = http_schema_resource(make_app(), "/users/{id}")
    assert hsr["options"] == OPTIONS_ENTRY == options_operation()
    assert list(hsr) == ["get", "put", "options", "head"]


def test_validates():
    app = make_app()
    for path in ("/users/{id}", "/users"):
        validate_http_schema_resource(http_schema_resource(app, path))


def test_hsr_is_built_once_and_copied(make_client):
    calls = []

    def schema():
        calls.append("schema")
        return "/schemas/item.json"

    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(schema_link_or_func=schema, context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        return {}

    add_options(app, base_url=BASE)
    first = http_schema_resource(app, "/items/{id}")
    first["get"]["responses"].clear()
    second = http_schema_resource(app, "/items/{id}")
    assert second["get"]["responses"]["200"]["content"]["application/json"] == {
        "schema": {"$ref": "https://api.example/schemas/item.json"},
        "x-rdf-context": {
            "href": "https://api.example/c.jsonld",
            "type": "application/ld+json",
        },
    }
    assert options_hsr(make_client(app), "/items/1") == second
    assert calls == ["schema"]


def test_routes_added_later_are_described():
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        return {}

    add_options(app, base_url=BASE)
    assert "delete" not in http_schema_resource(app, "/items/{id}")

    @app.delete("/items/{id}", status_code=204)
    @scrudful()
    async def delete_item(id: int):
        return None

    assert "delete" in http_schema_resource(app, "/items/{id}")


def test_requests_dont_walk_the_routes_again(make_client, monkeypatch):
    """The resources are indexed once per change of the routes: a request
    doesn't walk every route of the app (each walk costs O(routes))."""
    import scrudful.fastapi as module

    walks = []
    collect = module._collect_paths

    def counting(routes, prefix, paths, mounts):
        if not prefix and not mounts:
            walks.append(routes)
        return collect(routes, prefix, paths, mounts)

    monkeypatch.setattr(module, "_collect_paths", counting)
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    app = FastAPI()
    for i in range(20):

        @app.get(f"/r{i}/{{id}}")
        @scrudful(**links)
        async def get_item(id: int):
            return {}

    router = APIRouter()

    @router.get("/things/{id}")
    @scrudful(**links)
    async def get_thing(id: int):
        return {}

    app.include_router(router, prefix="/v1")
    add_options(app, base_url=BASE)
    client = make_client(app)
    for url in ("/r3/1", "/v1/things/1", "/_hsr/r3.id"):
        assert client.get(url).status_code == 200
    assert client.options("/r3/1").status_code == 204
    before = len(walks)
    for _ in range(5):
        response = client.get("/r19/1")
        assert hsr_link_problems(response.headers, f"{BASE}_hsr/r19.id") == []
        assert client.get("/v1/things/1").status_code == 200
        assert client.get("/_hsr/v1.things.id").status_code == 200
        assert client.options("/r19/1").status_code == 204
    assert len(walks) == before
    # A route added later is seen: the routes changed.

    @app.get("/later/{id}")
    @scrudful(**links)
    async def get_later(id: int):
        return {}

    response = client.get("/later/1")
    assert hsr_link_problems(response.headers, f"{BASE}_hsr/later.id") == []
    assert client.get("/_hsr/later.id").status_code == 200
    # So is one added to an included router, included lazily or copied.

    @router.get("/more/{id}")
    @scrudful(**links)
    async def get_more(id: int):
        return {}

    if module._iter_route_contexts is None:
        # A copying FastAPI serves them only from a new include.
        app.include_router(router, prefix="/v2")
        url, name = "/v2/more/1", "v2.more.id"
    else:
        url, name = "/v1/more/1", "v1.more.id"
    response = client.get(url)
    assert hsr_link_problems(response.headers, f"{BASE}_hsr/{name}") == []
    assert client.get(f"/_hsr/{name}").status_code == 200


def test_app_routes_added_later_to_an_included_router_are_described(make_client):
    """With ``add_options(app)``, a route added later to a router the app
    includes lazily (a new method of a resource, and a new path) is seen
    without any change to the app's own routes: ``Allow`` and the HSR list
    the new method, and the new path has its OPTIONS, its HSR and the HSR
    link."""
    import scrudful.fastapi as module

    if module._iter_route_contexts is None:
        pytest.skip("this FastAPI copies an included router's routes")
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful(**NOTE_LINKS)
    async def get_item(id: int):
        return {}

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    add_options(app, base_url=BASE)
    client = make_client(app)
    items = f"{BASE}_hsr/v1.items.id"
    options_204(client, "/v1/items/1", "GET, OPTIONS", items)
    app_routes = list(app.router.routes)

    @router.put("/items/{id}")
    @scrudful(**NOTE_LINKS)
    async def put_item(id: int):
        return {}

    @router.get("/late/{id}")
    @scrudful(**NOTE_LINKS)
    async def get_late(id: int):
        return {}

    assert app.router.routes == app_routes
    options_204(client, "/v1/items/1", "GET, PUT, OPTIONS", items)
    assert "put" in client.get(items).json()
    late = f"{BASE}_hsr/v1.late.id"
    assert hsr_link_problems(client.get("/v1/late/1").headers, late) == []
    options_204(client, "/v1/late/1", "GET, OPTIONS", late)
    assert client.get(late).status_code == 200


def test_a_route_replaced_in_place_is_seen(make_client):
    """The index is kept while the routes are the same objects: one replaced
    in place, keeping its list's length, rebuilds it too."""
    from fastapi.routing import APIRoute

    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    assert (
        hsr_link_problems(client.get("/items/1").headers, f"{BASE}_hsr/items.id") == []
    )

    async def get_thing(id: int):
        return {}

    routes = app.router.routes
    index = next(
        i for i, r in enumerate(routes) if getattr(r, "path", "") == "/items/{id}"
    )
    routes[index] = APIRoute(
        "/things/{id}", scrudful(**links)(get_thing), methods=["GET"]
    )
    response = client.get("/things/1")
    assert hsr_link_problems(response.headers, f"{BASE}_hsr/things.id") == []
    assert client.get("/_hsr/things.id").status_code == 200


def test_routes_reordered_in_place_are_seen(make_client):
    """So are two routes swapped: the document describes the one that now
    comes first, as Starlette now routes to it."""
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(schema_link_or_func="/a.json", context_link_or_func="/c.jsonld")
    async def get_a(id: int):
        return {}

    @app.get("/items/{item_id}")
    @scrudful(schema_link_or_func="/b.json", context_link_or_func="/c.jsonld")
    async def get_b(item_id: int):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)

    def schema(name: str) -> str:
        hsr = client.get(f"/_hsr/{name}").json()
        content = hsr["get"]["responses"]["200"]["content"]["application/json"]
        return content["schema"]["$ref"]

    assert schema("items.id") == "https://api.example/a.json"
    routes = app.router.routes
    first = next(
        i for i, r in enumerate(routes) if getattr(r, "path", "") == "/items/{id}"
    )
    routes[first], routes[first + 1] = routes[first + 1], routes[first]
    # Named after its first route's template, now get_b's.
    assert schema("items.item_id") == "https://api.example/b.json"
    response = client.get("/items/1")
    hsr = client.get("/_hsr/items.item_id").json()
    assert link_problems("GET", 200, response.headers, True, hsr) == []


def test_routes_it_cant_fingerprint_are_walked_each_time(make_client, monkeypatch):
    """Without a fingerprint (an included router it can't see into), nothing
    is kept: each request walks the routes, as before the index."""
    import scrudful.fastapi as module

    monkeypatch.setattr(module, "_routes_fingerprint", lambda routes: None)
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    assert client.get("/items/1").status_code == 200
    assert module._hsr_options(app).index is None

    @app.get("/later/{id}")
    @scrudful(**links)
    async def get_later(id: int):
        return {}

    response = client.get("/later/1")
    assert hsr_link_problems(response.headers, f"{BASE}_hsr/later.id") == []


class WalkedRoutes(list):
    """A list of routes counting how often it is iterated."""

    walks = 0

    def __iter__(self) -> Any:
        self.walks += 1
        return super().__iter__()


@pytest.mark.parametrize("mode", ["app", "router"])
def test_a_request_walks_the_routes_once(make_client, mode):
    """A GET, an OPTIONS and an HSR GET each walk the lists of routes once
    to see whether they changed (not once per lookup), in app and router
    mode: a ``Mount(routes=...)`` beside the resource, which no request here
    routes into, has its routes walked once per request, and only for
    that."""
    app = FastAPI()
    owner = app if mode == "app" else APIRouter()

    @owner.get("/items/{id}")
    @scrudful(**NOTE_LINKS)
    async def get_item(id: int):
        return {}

    elsewhere = Mount("/elsewhere", routes=[Route("/x", lambda request: Response())])
    walked = WalkedRoutes(elsewhere.routes)
    elsewhere.app.routes = walked  # type: ignore[attr-defined]
    owner.routes.append(elsewhere)
    add_options(owner, base_url=BASE)
    prefix = ""
    if mode == "router":
        app.include_router(owner, prefix="/v1")
        prefix = "/v1"
    client = make_client(app)
    requests = [
        ("GET", f"{prefix}/items/1", 200),
        ("OPTIONS", f"{prefix}/items/1", 204),
        ("GET", f"{prefix}/_hsr/items.id", 200),
    ]
    for method, url, status in requests:  # indexed, once
        assert client.request(method, url).status_code == status
    for method, url, status in requests:
        walked.walks = 0
        assert client.request(method, url).status_code == status
        assert walked.walks == 1, (method, url)


def test_unknown_path_raises():
    app = make_app()
    with pytest.raises(ValueError, match=r"'/people/\{id\}'.*'/users/\{id\}'"):
        http_schema_resource(app, "/people/{id}")
    with pytest.raises(TypeError, match="FastAPI application or an APIRouter"):
        http_schema_resource(object(), "/users/{id}")  # type: ignore[arg-type]


def test_a_path_without_a_scrudful_endpoint_has_no_hsr():
    app = FastAPI()

    @app.get("/plain")
    async def plain():
        return {}

    add_options(app)
    with pytest.raises(ValueError, match="'/plain'"):
        http_schema_resource(app, "/plain")


def test_parameter_names_are_one_resource_convertors_are_distinct(make_client):
    """Templates that match the same URLs are one resource whatever their
    parameters' names (``str`` is no convertor); another convertor matches
    other URLs, so it is another resource."""
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    reads, writes = APIRouter(), APIRouter()

    @reads.get("/items/{id}")
    @scrudful(**links)
    async def read(id: int):
        return {}

    @writes.put("/items/{item_id}")
    @scrudful(**links)
    async def write(item_id: int):
        return {}

    @writes.patch("/items/{x:str}")
    @scrudful(**links)
    async def patch(x: str):
        return {}

    @writes.delete("/items/{id:int}", status_code=204)
    @scrudful()
    async def remove(id: int):
        return None

    app = FastAPI()
    app.include_router(reads)
    app.include_router(writes)
    add_options(app, base_url=BASE)
    hsr = http_schema_resource(app, "/items/{id}")
    assert list(hsr) == ["get", "put", "options", "patch"]
    assert http_schema_resource(app, "/items/{item_id}") == hsr
    assert http_schema_resource(app, "/items/{y:str}") == hsr
    assert list(http_schema_resource(app, "/items/{n:int}")) == ["delete", "options"]
    client = make_client(app)
    assert options_hsr(client, "/items/abc") == hsr
    options_204(client, "/items/abc", "GET, PUT, OPTIONS, PATCH")
    assert client.delete("/items/abc").status_code == 405  # not this resource's
    # Each resource has its own HSR route.
    assert client.get("/_hsr/items.id").json() == hsr
    assert client.get("/_hsr/items.id-int").json() == http_schema_resource(
        app, "/items/{id:int}"
    )


def test_a_convertor_beside_a_str_parameter_is_a_separate_resource(make_client):
    """A limitation, like a literal beside a template: ``/items/5`` matches
    both, and reaches whichever route comes first."""
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    app = FastAPI()

    @app.put("/items/{id:int}")
    @scrudful(**links)
    async def write(id: int):
        return {}

    @app.get("/items/{name}")
    @scrudful(**links)
    async def read(name: str):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(
        client, "/items/abc", "GET, OPTIONS", "https://api.example/_hsr/items.name"
    )
    assert client.put("/items/abc").status_code == 405
    options_204(
        client, "/items/5", "PUT, OPTIONS", "https://api.example/_hsr/items.id-int"
    )


def test_a_literal_beside_a_template_is_a_separate_resource(make_client):
    """A limitation: ``/items/new`` and ``/items/{id}`` are separate resources."""
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    app = FastAPI()

    @app.get("/items/new")
    @scrudful(**links)
    async def new_item():
        return {}

    @app.put("/items/{id}")
    @scrudful(**links)
    async def write(id: int):
        return {}

    add_options(app, base_url=BASE)
    assert list(http_schema_resource(app, "/items/new")) == ["get", "options"]
    assert list(http_schema_resource(app, "/items/{id}")) == ["put", "options"]
    client = make_client(app)
    assert options_hsr(client, "/items/new") == http_schema_resource(app, "/items/new")
    assert options_hsr(client, "/items/5") == http_schema_resource(app, "/items/{id}")


def test_routes_a_mount_shadows_are_left_out(make_client):
    """Starlette routes every URL under a Mount into it: a later route whose
    URLs are all under it is never reached, so it isn't described."""
    from fastapi.routing import APIRoute

    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    async def item(id: int):
        return {}

    get_item = scrudful(**links)(item)
    put_item = scrudful(**links)(item)
    app = FastAPI()
    app.router.routes.append(
        Mount("/n", routes=[APIRoute("/items/{id}", get_item, methods=["GET"])])
    )
    app.router.routes.append(APIRoute("/n/items/{id}", put_item, methods=["PUT"]))
    app.router.routes.append(APIRoute("/{tenant}/x/{id}", put_item, methods=["PUT"]))
    app.router.routes.append(
        Mount("/{tenant}", routes=[APIRoute("/y", get_item, methods=["GET"])])
    )
    app.router.routes.append(APIRoute("/acme/z", get_item, methods=["GET"]))
    add_options(app, base_url=BASE)
    client = make_client(app)
    assert client.put("/n/items/1").status_code == 405  # the Mount's answer
    assert list(http_schema_resource(app, "/n/items/{id}")) == ["get", "options"]
    # Before the Mount: reached, and described.
    assert list(http_schema_resource(app, "/{t}/x/{id}")) == ["put", "options"]
    assert list(http_schema_resource(app, "/{t}/y")) == ["get", "options"]
    # After it, and under it: never reached.
    assert client.get("/acme/z").status_code == 404
    with pytest.raises(ValueError, match="'/acme/z'"):
        http_schema_resource(app, "/acme/z")


def test_a_convertor_mount_shadows_only_what_it_matches(make_client):
    """``Mount("/{x:int}")`` takes only digit segments: ``/about/us`` after it
    is still reached, described and answered."""
    from fastapi.routing import APIRoute

    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    async def page():
        return {}

    endpoint = scrudful(**links)(page)
    app = FastAPI()
    app.router.routes.append(
        Mount("/{x:int}", routes=[APIRoute("/y", endpoint, methods=["GET"])])
    )
    app.router.routes.append(APIRoute("/about/us", endpoint, methods=["GET"]))
    app.router.routes.append(APIRoute("/{n:int}/z", endpoint, methods=["GET"]))
    add_options(app, base_url=BASE)
    client = make_client(app)
    assert client.get("/about/us").status_code == 200
    hsr = http_schema_resource(app, "/about/us")
    assert list(hsr) == ["get", "options"]
    assert options_hsr(client, "/about/us") == hsr
    # Its like is shadowed: /5/z goes into the Mount.
    assert client.get("/5/z").status_code == 404
    with pytest.raises(ValueError, match=r"'/\{n:int\}/z'"):
        http_schema_resource(app, "/{n:int}/z")


def test_a_mount_under_a_host_shadows_only_within_it(make_client):
    """A Host routes only its host's requests: its Mount doesn't shadow the
    app's later routes."""
    from fastapi.routing import APIRoute
    from starlette.routing import Host

    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    async def page():
        return {}

    endpoint = scrudful(**links)(page)
    app = FastAPI()
    app.router.routes.append(
        Host(
            "api.example",
            app=Router(
                [Mount("/n", routes=[APIRoute("/y", endpoint, methods=["GET"])])]
            ),
        )
    )
    app.router.routes.append(APIRoute("/n/z", endpoint, methods=["GET"]))
    add_options(app, base_url=BASE)
    client = make_client(app)
    assert client.get("/n/z").status_code == 200
    hsr = http_schema_resource(app, "/n/z")
    assert list(hsr) == ["get", "options"]
    assert options_hsr(client, "/n/z") == hsr
    assert list(http_schema_resource(app, "/n/y")) == ["get", "options"]


def test_full_path_keys_under_a_prefix_and_a_mount(make_client):
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int):
        return {}

    async def item(id: int):
        return {}

    from fastapi.routing import APIRoute

    endpoint = scrudful(**links)(item)
    app = FastAPI()
    app.include_router(router, prefix="/v1/{tenant}")
    app.mount("/m", Router(routes=[APIRoute("/items/{id}", endpoint, methods=["PUT"])]))
    app.router.routes.append(
        Mount("/n", routes=[APIRoute("/items/{id}", endpoint, methods=["GET"])])
    )
    add_options(app, base_url=BASE)
    client = make_client(app)
    for url, path, methods in [
        ("/v1/acme/items/1", "/v1/{tenant}/items/{id}", ["get", "options"]),
        ("/m/items/1", "/m/items/{id}", ["put", "options"]),
        ("/n/items/1", "/n/items/{id}", ["get", "options"]),
    ]:
        hsr = http_schema_resource(app, path)
        assert list(hsr) == methods
        assert options_hsr(client, url) == hsr


def test_router_mode(make_client):
    """``add_options`` on an ``APIRouter``: keyed by the router's own paths,
    with its base URL, and the error entity of the app including it."""
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        raise HTTPException(status_code=404)

    add_options(router, base_url="https://router.example/")
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    add_error_links(app, APP_ERROR)
    hsr = http_schema_resource(router, "/items/{id}")
    schema = hsr["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "https://router.example/s.json"}
    default = hsr["get"]["responses"]["default"]["content"]
    assert default["application/problem+json"]["schema"] == {
        "$ref": "https://router.example/schemas/problem.json"
    }
    client = make_client(app)
    assert options_hsr(client, "/v1/items/1") == hsr
    # The live Links resolve against the router's base URL too.
    response = client.get("/v1/items/1")
    assert response.status_code == 404
    assert parse_links(response.headers["link"])[0][0] == (
        "https://router.example/schemas/problem.json"
    )
    assert link_problems("GET", 404, response.headers, True, hsr) == []


def test_router_included_in_apps_with_different_error_entities_raises():
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful()
    async def get_item(id: int):
        return {}

    add_options(router, base_url=BASE)
    apps = [FastAPI(), FastAPI()]
    for app, error in zip(apps, (APP_ERROR, Entity())):
        app.include_router(router)
        add_error_links(app, error)
    with pytest.raises(ValueError, match="different error entities"):
        http_schema_resource(router, "/items/{id}")


def error_entity(name: str) -> Entity:
    return Entity(
        f"https://api.example/schemas/{name}.json",
        context_link_or_func=f"https://api.example/contexts/{name}.jsonld",
    )


def router_in_three_apps() -> tuple[APIRouter, dict[str, FastAPI]]:
    """A conditional router (no ``error=``) included in apps ``a`` and ``b``,
    given different ``add_error_links`` entities, and ``c``, given none."""
    router = APIRouter()

    @router.get("/items/{iid}")
    @router.put("/items/{iid}")
    @scrudful(
        etag_func=lambda iid: '"1"',
        schema_link_or_func="https://api.example/s.json",
        context_link_or_func="https://api.example/c.jsonld",
    )
    def item(iid: int):
        if iid == 99:
            raise HTTPException(404)
        return {"id": iid}

    add_options(router, base_url=BASE)
    apps = {name: FastAPI() for name in "abc"}
    for name, app in apps.items():
        app.include_router(router, prefix=f"/{name}")
    add_error_links(apps["a"], error_entity("err-a"))
    add_error_links(apps["b"], error_entity("err-b"))
    return router, apps


#: ``(method, path, headers, status)``: scrudful's early 400 and 412, and a
#: raised 404, of :func:`router_in_three_apps`'s item.
ERRORS = [
    ("GET", "/items/1", {"If-None-Match": "garbage"}, 400),
    ("PUT", "/items/1", {"If-Match": '"9"'}, 412),
    ("PUT", "/items/1", {}, 428),
    ("GET", "/items/99", {}, 404),
]


@pytest.mark.parametrize("order", ["abc", "cba", "bac"])
def test_router_in_several_apps_serves_each_apps_hsr(make_client, order):
    """Each app's HSR describes its own ``add_error_links`` entity (none for
    ``c``), whichever app builds first, and agrees with its responses."""
    router, apps = router_in_three_apps()
    documents = {}
    for name in order:
        client = make_client(apps[name], raise_server_exceptions=False)
        hsr = options_hsr(client, f"/{name}/items/1")
        documents[name] = hsr
        for method, path, headers, status in ERRORS:
            response = client.request(method, f"/{name}{path}", headers=headers)
            assert response.status_code == status
            schemas = [
                target
                for target, params in parse_links(response.headers.get("link"))
                if params.get("rel") == "describedby"
            ]
            if name == "c":
                entries = hsr[method.lower()]["responses"]
                assert schemas == []
                assert "content" not in entries.get(str(status), entries["default"])
                continue
            assert schemas == [f"https://api.example/schemas/err-{name}.json"]
            problems = link_problems(method, status, response.headers, True, hsr)
            assert problems == [], (name, method, status)
    assert len({repr(document) for document in documents.values()}) == 3
    # Without a request, the app to describe is named.
    for name, app in apps.items():
        document = http_schema_resource(router, "/items/{iid}", for_app=app)
        assert document == documents[name]
    with pytest.raises(ValueError, match="different error entities"):
        http_schema_resource(router, "/items/{iid}")


def test_a_router_hsr_served_from_an_unrelated_app_describes_the_including_app(
    make_client,
):
    """A router's HSR served by :func:`http_schema_resource_endpoint` from a
    separate docs app (``hsr_route=False``) describes the entity of the app
    that includes the router, whose handlers answer its errors: the docs app
    includes none of its endpoints, so its own entity (here none) is not the
    one. The wire 422 agrees with the served ``default``."""
    router = APIRouter()

    @router.get("/items/{iid}")
    @scrudful(
        schema_link_or_func="https://api.example/s.json",
        context_link_or_func="https://api.example/c.jsonld",
    )
    def item(iid: int):
        return {"id": iid}

    add_options(
        router,
        base_url=BASE,
        hsr_route=False,
        hsr_url=lambda template: "https://api.example/docs/hsr/items",
    )
    api = FastAPI()
    api.include_router(router)
    docs = FastAPI()
    docs.add_route("/hsr/items", http_schema_resource_endpoint(router, "/items/{iid}"))
    api.mount("/docs", docs)
    add_error_links(api, error_entity("problem"))
    client = make_client(api)
    hsr = options_hsr(client, "/items/1")
    assert hsr == http_schema_resource(router, "/items/{iid}", for_app=api)
    assert hsr == http_schema_resource(router, "/items/{iid}", for_app=docs)
    assert "content" in hsr["get"]["responses"]["default"]
    response = client.get("/items/abc")
    assert response.status_code == 422
    assert link_problems("GET", 422, response.headers, True, hsr) == []


@dataclasses.dataclass
class LinkBuilder:
    """A link callable object: a dataclass with ``eq`` is unhashable, and so
    is an ``Entity`` holding it."""

    path: str

    def __call__(self, **kwargs: Any) -> str:
        return "https://api.example" + self.path


@pytest.mark.parametrize("mode", ["app", "router"])
def test_an_unhashable_error_entity_is_described_and_served(make_client, mode):
    """The HSRs are kept by the entity they describe, by identity: an
    unhashable one works end to end (GET, ``http_schema_resource`` and
    OPTIONS, whose HSR link is served), and agrees with the wire error."""
    error = Entity(
        media_type="application/problem+json",
        schema_link_or_func=LinkBuilder("/schemas/problem.json"),
        context_link_or_func="https://api.example/contexts/problem.jsonld",
    )
    with pytest.raises(TypeError):
        hash(error)
    owner: FastAPI | APIRouter = FastAPI() if mode == "app" else APIRouter()

    @owner.get("/items/{iid}")
    @scrudful(etag_func=lambda **kwargs: '"1"', **USER_LINKS)
    def item(iid: int):
        return {"id": iid}

    add_options(owner, base_url=BASE)
    if isinstance(owner, FastAPI):
        app = owner
    else:
        app = FastAPI()
        app.include_router(owner)
    add_error_links(app, error)
    client = make_client(app)
    assert client.get("/items/1").status_code == 200
    hsr = options_hsr(client, "/items/1")
    assert hsr == http_schema_resource(owner, "/items/{iid}")
    assert hsr["get"]["responses"]["default"]["content"] == APP_ERROR_CONTENT
    response = client.get("/items/1", headers={"If-None-Match": "garbage"})
    assert response.status_code == 400
    assert link_problems("GET", 400, response.headers, True, hsr) == []
    # Kept: the same document again.
    assert client.get("/_hsr/items.iid").json() == hsr


def test_an_app_whose_hsr_route_was_removed_warns_once(make_client):
    """An app's HSR route removed from its routes (a project filtering them)
    leaves every resource undescribed: no HSR link on a response or on
    OPTIONS. It isn't silent: a ``RuntimeWarning`` says so, once, and
    calling ``add_options`` again restores the route."""
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {"id": id}

    add_options(app, base_url=BASE)
    client = make_client(app)
    assert hsr_link_problems(dict(client.get("/notes/1").headers), NOTE_TARGET) == []
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not getattr(route, "path", "").startswith("/_hsr/")
    ]
    with pytest.warns(RuntimeWarning) as caught:
        response = client.get("/notes/1")
    assert [str(warning.message) for warning in caught] == [
        "the HTTP Schema Resource route of this FastAPI app ('/_hsr/{name}') "
        "was removed from its routes, or replaced, after add_options(app), so "
        "none of its resources is described: their responses get no HSR link, "
        "and OPTIONS answers without one; call add_options on the app again"
    ]
    assert HSR_DOCUMENT_REL not in (response.headers.get("link") or "")
    # Once (the suite runs with warnings as errors).
    options = client.options("/notes/1")
    assert (options.status_code, options.headers.get("link")) == (204, None)
    assert client.get(NOTE_TARGET).status_code == 404
    add_options(app, base_url=BASE)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET)
    assert client.get(NOTE_TARGET).status_code == 200


def test_router_hsr_per_app_has_its_own_etag(make_client):
    """The served bytes, and so the ``ETag``, are each app's."""
    _, apps = router_in_three_apps()
    etags = set()
    for name, app in apps.items():
        client = make_client(app)
        first = client.get(f"/{name}/_hsr/items.iid")
        assert (
            hsr_get_problems(first.status_code, dict(first.headers), first.content)
            == []
        )
        again = client.get(
            f"/{name}/_hsr/items.iid", headers={"If-None-Match": first.headers["etag"]}
        )
        assert again.status_code == 304
        etags.add(first.headers["etag"])
    assert len(etags) == 3


# --- base URL ----------------------------------------------------------------------


def test_relative_link_without_base_url_raises(make_client):
    app = make_app(base_url=None)
    with pytest.raises(ValueError, match="relative URL '/schemas/user.json'") as raised:
        http_schema_resource(app, "/users/{id}")
    # The shared HSR configuration error, a ValueError, not re-wrapped.
    assert type(raised.value) is HsrConfigError
    assert "get_user()'s 200 response schema link" in str(raised.value)
    # Its route builds it on a request: the same error (OPTIONS builds none).
    with pytest.raises(ValueError, match="set a base URL"):
        make_client(app).get("/_hsr/users.id")
    options_204(make_client(app), "/users/1", "GET, PUT, OPTIONS, HEAD")


def test_relative_link_with_base_url_on_the_hsr_and_the_link_header(make_client):
    """Base set: the base URL on both the HTTP Schema Resource and the live
    Link header (success, early 412 and raised errors), whatever the Host."""
    app = make_app()
    hsr = http_schema_resource(app, "/users/{id}")
    client = make_client(app, base_url="http://elsewhere.example")
    response = client.get("/users/1")
    assert parse_links(response.headers["link"])[0][0] == (
        "https://api.example/schemas/user.json"
    )
    assert link_problems("GET", 200, response.headers, True, hsr) == []
    response = client.get("/users/404")
    assert response.status_code == 404
    assert parse_links(response.headers["link"])[0][0] == (
        "https://api.example/schemas/problem.json"
    )
    assert link_problems("GET", 404, response.headers, True, hsr) == []


def test_without_base_url_live_links_resolve_against_the_request(make_client):
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(
        schema_link_or_func="https://schemas.example/item.json",
        context_link_or_func="https://schemas.example/item.jsonld",
    )
    async def get_item(id: int):
        raise HTTPException(status_code=404)

    error = Entity(schema_link_or_func="/e.json", context_link_or_func="/e.jsonld")
    add_error_links(app, error)
    add_options(app)
    response = make_client(app, base_url="http://here.example").get("/items/1")
    assert parse_links(response.headers["link"])[0][0] == "http://here.example/e.json"


def test_base_url_must_be_absolute():
    with pytest.raises(ValueError, match="base_url must be an absolute URL"):
        add_options(FastAPI(), base_url="/api/")


@pytest.mark.parametrize("base_url", [b"https://api.example/", 42])
def test_a_base_url_that_isnt_a_str_is_the_config_error(base_url):
    with pytest.raises(HsrConfigError, match="base_url must be an absolute URL"):
        add_options(FastAPI(), base_url=base_url)


def test_a_schema_link_without_a_context_is_refused_at_decoration():
    """The link options make an ``application/json`` entity, which needs an
    RDF context: refused by the decorator, as ``Entity`` refuses it (the HSR
    route would 500 while OPTIONS and every GET link to it)."""
    with pytest.raises(HsrConfigError, match="context"):
        scrudful(schema_link_or_func="https://e.example/s.json")
    # A callable can't be checked before it is called: its HSR build is.
    scrudful(schema_link_or_func=lambda **kwargs: "https://e.example/s.json")
    scrudful(
        schema_link_or_func="https://e.example/s.json",
        context_link_or_func="https://e.example/c.jsonld",
    )


def test_a_callable_schema_link_without_a_context_is_the_config_error():
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(schema_link_or_func=lambda **kwargs: "https://e.example/s.json")
    async def get_item(id: int):
        return {}

    add_options(app, base_url=BASE)
    with pytest.raises(HsrConfigError, match="get_item\\(\\)'s 200 response"):
        http_schema_resource(app, "/items/{id}")


def test_add_options_stores_its_options():
    def allowed(request, method, obj):
        return True

    def load(request, **path_params):
        return None

    def hsr_url(path):
        return f"https://docs.example/{path}"

    app = FastAPI()
    add_options(
        app,
        base_url=BASE,
        hsr_route=False,
        hsr_url=hsr_url,
        hsr_cache_control="no-cache",
        allowed=allowed,
        load=load,
    )
    from scrudful.fastapi import _hsr_options

    options = _hsr_options(app)
    assert options.base_url == BASE
    assert options.hsr_route is False
    assert options.hsr_url is hsr_url
    assert options.hsr_cache_control == "no-cache"
    assert options.allowed is allowed
    assert options.load is load
    assert _hsr_options(FastAPI()).hsr_cache_control == "public, max-age=300"
    # Fixed once given: the caches kept with them are valid only for them.
    with pytest.raises(AttributeError):
        options.hsr_route = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        options.config.base_url = None  # type: ignore[misc, union-attr]
    # Nor can the configuration be swapped for another one, unchecked.
    with pytest.raises(AttributeError):
        options.config = None  # type: ignore[misc]
    assert options.config is not None and options.config.load is load
    assert options.explicit and not _hsr_options(FastAPI()).explicit


@pytest.mark.parametrize(
    "fields, error, match",
    [
        ({"hsr_route": False}, ValueError, "hsr_route=False needs hsr_url"),
        ({"base_url": "/v1/"}, ValueError, "base_url"),
        ({"hsr_cache_control": ""}, ValueError, "hsr_cache_control"),
        ({"hsr_cache_control": "a\r\nb"}, ValueError, "hsr_cache_control"),
        ({"hsr_cache_control": 300}, TypeError, "hsr_cache_control"),
        ({"hsr_route": "yes"}, TypeError, "hsr_route"),
        ({"hsr_url": "https://x/"}, TypeError, "hsr_url"),
        ({"allowed": True}, TypeError, "allowed"),
        ({"load": "x"}, TypeError, "load"),
    ],
)
def test_the_options_configuration_checks_itself(fields, error, match):
    """The rules of the options are the configuration's own: one made
    anywhere, not only by add_options, is checked."""
    from scrudful.fastapi import _HsrConfig

    with pytest.raises(error, match=match):
        _HsrConfig(**fields)


@pytest.mark.parametrize(
    "name, value",
    [
        ("base_url", 1),
        ("hsr_route", "yes"),
        ("hsr_url", "https://docs.example/"),
        ("hsr_cache_control", 300),
        ("allowed", True),
        ("load", "x"),
    ],
)
def test_add_options_checks_its_options(name, value):
    # A non-str base_url is refused like a relative one (a ValueError).
    error = ValueError if name == "base_url" else TypeError
    with pytest.raises(error, match=name):
        add_options(FastAPI(), **{name: value})


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  ",
        "public\r\nX-Injected: 1",
        "no-cache\n",
        "public\x00",
        "max-age=300 \u2713",
    ],
)
def test_add_options_rejects_an_unusable_hsr_cache_control(value):
    with pytest.raises(ValueError, match="hsr_cache_control"):
        add_options(FastAPI(), hsr_cache_control=value)


@pytest.mark.parametrize("returned", [None, "", "/hsr/users/{id}", 3])
def test_hsr_url_must_return_an_absolute_url(make_client, returned):
    app = target_app(hsr_url=lambda path: returned)
    client = make_client(app, raise_server_exceptions=False)
    assert client.get("/users/1").status_code == 500
    with pytest.raises(ValueError, match="must return an absolute URL"):
        make_client(app).get("/users/1")


# --- describe mode -----------------------------------------------------------------


def test_link_callables_called_without_arguments(make_client):
    """``func()``, even for a callable that declares the endpoint's kwargs."""
    received = []

    def schema(id=None, request: Request | None = None, **kwargs):
        received.append(("schema", id, request, kwargs))
        return "/schemas/item.json"

    async def context(*args, **kwargs):
        received.append(("context", args, kwargs))
        return "/contexts/item.jsonld"

    def error_schema(**kwargs):
        received.append(("error", kwargs))
        return "/schemas/error.json"

    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(
        etag_func=lambda **kwargs: '"v1"',
        schema_link_or_func=schema,
        context_link_or_func=context,
        error=Entity(
            schema_link_or_func=error_schema, context_link_or_func="/e.jsonld"
        ),
    )
    async def get_item(id: int, request: Request):
        return {}

    add_options(app, base_url=BASE)
    hsr = http_schema_resource(app, "/items/{id}")
    assert received == [
        ("schema", None, None, {}),
        ("context", (), {}),
        ("error", {}),
    ]
    assert hsr["get"]["responses"]["412"]["content"]["application/json"]["schema"] == {
        "$ref": "https://api.example/schemas/error.json"
    }
    # The OPTIONS answer reuses the document: no new calls.
    make_client(app).options("/items/1")
    assert len(received) == 3


def test_a_link_callable_needing_arguments_raises():
    app = FastAPI()

    def schema(id):
        return f"/schemas/item/{id}"

    @app.get("/items/{id}")
    @scrudful(schema_link_or_func=schema, context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        return {}

    with pytest.raises(ValueError, match=r"func\(\)") as raised:
        add_options(app, base_url=BASE)
    assert "schema_link_or_func" in str(raised.value)
    assert "get_item" in str(raised.value)
    assert repr(schema) in str(raised.value)


def test_a_link_callable_failing_in_describe_mode_raises(make_client):
    def schema(**kwargs):
        return kwargs["request"].url_for("schema")

    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(schema_link_or_func=schema, context_link_or_func="/c.jsonld")
    async def get_item(id: int, request: Request):
        return {}

    add_options(app, base_url=BASE)
    with pytest.raises(ValueError, match=r"func\(\)") as raised:
        http_schema_resource(app, "/items/{id}")
    message = str(raised.value)
    assert "schema" in message and "get_item()" in message
    assert isinstance(raised.value.__cause__, KeyError)
    with pytest.raises(ValueError, match=r"func\(\)"):
        make_client(app).get("/_hsr/items.id")


def test_an_app_error_callable_needing_the_request_raises():
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        return {}

    error = Entity(
        schema_link_or_func=lambda request: request.url_for("error"),
        context_link_or_func="/e.jsonld",
    )
    add_error_links(app, error)
    add_options(app, base_url=BASE)
    with pytest.raises(ValueError, match=r"func\(\)") as raised:
        http_schema_resource(app, "/items/{id}")
    assert "default response" in str(raised.value)


def test_async_link_callables_are_awaited(make_client):
    def make() -> FastAPI:
        async def schema():
            return "/s.json"

        app = FastAPI()

        @app.get("/items/{id}")
        @scrudful(schema_link_or_func=schema, context_link_or_func="/c.jsonld")
        async def get_item(id: int):
            return {}

        add_options(app, base_url=BASE)
        return app

    built = http_schema_resource(make(), "/items/{id}")
    content = built["get"]["responses"]["200"]["content"]["application/json"]
    assert content["schema"] == {"$ref": "https://api.example/s.json"}
    # Built in the event loop, by the HSR route the OPTIONS answer links to.
    assert options_hsr(make_client(make()), "/items/1") == built
    # And by the sync function called inside a running event loop.
    app = make()

    @app.get("/describe")
    async def describe():
        return http_schema_resource(app, "/items/{id}")

    assert make_client(app).get("/describe").json() == built


# --- entity= ------------------------------------------------------------------------

CSV = Entity(
    media_type="text/csv",
    schema_link_or_func="/schemas/user.csvm.json",
    schema_type_or_func="application/csvm+json",
    context_link_or_func="/contexts/user.csvm.json",
    context_type_or_func="application/csvm+json",
)


def test_entity_option_csv(make_client):
    app = FastAPI()

    @app.get("/users/{id}")
    @scrudful(entity=CSV)
    async def get_user(id: int):
        return Response("id,name\n1,Ann\n", media_type="text/csv")

    add_options(app, base_url=BASE)
    hsr = http_schema_resource(app, "/users/{id}")
    validate_http_schema_resource(hsr)
    assert hsr["get"]["responses"]["200"]["content"] == {
        "text/csv": {
            "x-schema": {
                "href": "https://api.example/schemas/user.csvm.json",
                "type": "application/csvm+json",
            },
            "x-rdf-context": {
                "href": "https://api.example/contexts/user.csvm.json",
                "type": "application/csvm+json",
            },
        }
    }
    response = make_client(app).get("/users/1")
    assert response.headers["content-type"].startswith("text/csv")
    links = parse_links(response.headers["link"])
    assert links[1][1]["rel"] == "https://w3id.org/semantic-http/ns#rdf-context"
    assert link_problems("GET", 200, response.headers, True, hsr) == []


def test_entity_defaults_to_json_from_the_link_options():
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def endpoint():
        return {}

    config = endpoint._scrudful
    assert config.entity == Entity(
        schema_link_or_func="/s.json", context_link_or_func="/c.jsonld"
    )
    assert config.entity.media_type == "application/json"

    @scrudful(entity=CSV)
    async def csv_endpoint():
        return {}

    config = csv_endpoint._scrudful
    assert config.entity is CSV
    assert config.entity.media_type == "text/csv"


def test_entity_replaces_the_link_options():
    with pytest.raises(TypeError, match="entity replaces schema_link_or_func"):
        scrudful(entity=CSV, schema_link_or_func="/s.json")
    with pytest.raises(TypeError, match="entity must be an Entity"):
        scrudful(entity="text/csv")  # type: ignore[arg-type]


# --- completeness: responses= and status_code ------------------------------------


def test_status_code_201_from_route():
    hsr = http_schema_resource(make_app(), "/users")
    assert hsr["post"]["responses"]["201"] == {
        "description": "Created",
        "content": USER_CONTENT,
    }
    assert set(hsr["post"]["responses"]) == {"201", "default"}


def test_responses_kwarg_described():
    hsr = http_schema_resource(make_app(), "/users/{id}")
    # Raised by the endpoint, answered by add_error_links: the error entity.
    assert hsr["get"]["responses"]["404"] == {
        "description": "No such user",
        "content": APP_ERROR_CONTENT,
    }
    assert hsr["head"]["responses"]["404"] == {"description": "No such user"}


def test_responses_statuses_and_media_types():
    app = FastAPI()

    @app.get(
        "/items/{id}",
        responses={
            200: {"content": {"image/png": {}, "application/json; charset=utf-8": {}}},
            "203": {"description": "From a cache", "content": {"text/csv": {}}},
            301: {"description": "Moved"},
            "4XX": {"description": "A client error"},
            "default": {"description": "Anything else"},
        },
    )
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        return {}

    add_error_links(app, APP_ERROR)
    add_options(app, base_url=BASE)
    hsr = http_schema_resource(app, "/items/{id}")
    validate_http_schema_resource(hsr)
    responses = hsr["get"]["responses"]
    assert list(responses) == ["200", "203", "301", "4XX", "default"]
    item = {
        "application/json": {
            "schema": {"$ref": "https://api.example/s.json"},
            "x-rdf-context": {
                "href": "https://api.example/c.jsonld",
                "type": "application/ld+json",
            },
        }
    }
    # Only the entity's media type is described: a declared PNG or CSV
    # is undescribed content, not a JSON-LD-described entry.
    assert responses["200"] == {"description": "OK", "content": item}
    assert responses["203"] == {"description": "From a cache", "content": item}
    assert responses["301"] == {"description": "Moved"}
    assert responses["4XX"] == {
        "description": "A client error",
        "content": APP_ERROR_CONTENT,
    }
    assert responses["default"] == {
        "description": "Error.",
        "content": APP_ERROR_CONTENT,
    }


def test_a_describe_content_error_names_the_entry():
    """A CSV entity whose context type callable returns nothing: a
    ``ValueError`` naming the entry, not a bare one."""
    app = FastAPI()
    csv = Entity(
        media_type="text/csv",
        schema_link_or_func="/schemas/item.csvm.json",
        schema_type_or_func="application/csvm+json",
        context_link_or_func="/contexts/item.csvm.json",
        context_type_or_func=lambda **kwargs: None,
    )

    @app.get("/items/{id}")
    @scrudful(entity=csv)
    async def get_item(id: int):
        return Response("id\n1\n", media_type="text/csv")

    add_options(app, base_url=BASE)
    with pytest.raises(
        ValueError, match=r"get_item\(\)'s 200 response: the context of 'text/csv'"
    ):
        http_schema_resource(app, "/items/{id}")


def test_conditional_statuses_keep_scrudful_entries():
    """``CONDITIONAL_READ_RESPONSES`` declares scrudful's own 304/400/412: their
    entries carry the endpoint's ``error``, not the app-wide entity."""
    app = FastAPI()

    @app.get("/items/{id}", responses=CONDITIONAL_READ_RESPONSES)
    @scrudful(etag_func=lambda **kwargs: '"v1"', **USER_LINKS, error=USER_ERROR)
    async def get_item(id: int):
        return {}

    add_error_links(app, APP_ERROR)
    add_options(app, base_url=BASE)
    responses = http_schema_resource(app, "/items/{id}")["get"]["responses"]
    assert set(responses) == {"200", "304", "400", "412", "default"}
    assert responses["412"]["content"] == USER_ERROR_CONTENT
    assert responses["default"]["content"] == USER_ERROR_CONTENT
    assert "content" not in responses["304"]


def test_status_code_from_the_response_class():
    from fastapi.responses import RedirectResponse

    app = FastAPI()

    @app.get("/old/{id}", response_class=RedirectResponse)
    @scrudful()
    async def old(id: int):
        return "/new"

    add_options(app)
    responses = http_schema_resource(app, "/old/{id}")["get"]["responses"]
    assert responses["307"] == {"description": "Temporary Redirect"}


# --- error entries ----------------------------------------------------------------


def test_default_error_entry_from_add_error_links(make_client):
    app = make_app()
    hsr = http_schema_resource(app, "/users/{id}")
    assert hsr["get"]["responses"]["default"] == {
        "description": "Error.",
        "content": APP_ERROR_CONTENT,
    }
    # The endpoint's own error, which its raised errors get.
    assert hsr["put"]["responses"]["default"] == {
        "description": "Error.",
        "content": USER_ERROR_CONTENT,
    }
    assert hsr["head"]["responses"]["default"] == {"description": "Error."}
    # FastAPI's 401 (a dependency), 404 (raised) and 422 (validation) go
    # through add_error_links' handlers with the same entity: ``default``
    # describes them, no explicit entry is needed.
    client = make_client(app)
    for method, url, headers, status in [
        ("GET", "/users/1", {"Authorization": "Basic x"}, 401),
        ("GET", "/users/404", {}, 404),
        ("GET", "/users/x", {}, 422),
        ("PUT", "/users/1", {"Authorization": "Basic x"}, 401),
        ("PUT", "/users/x", {}, 422),
    ]:
        response = client.request(method, url, headers=headers)
        assert response.status_code == status
        assert link_problems(method, status, response.headers, True, hsr) == []
    for method in ("get", "put"):
        assert "401" not in hsr[method]["responses"]
        assert "422" not in hsr[method]["responses"]


def test_default_error_entry_without_add_error_links():
    """Without add_error_links the raised errors carry no Links: ``default``
    has no content."""
    hsr = http_schema_resource(make_app(error=None), "/users/{id}")
    assert hsr["get"]["responses"]["default"] == {"description": "Error."}
    assert hsr["put"]["responses"]["default"] == {"description": "Error."}
    assert hsr["get"]["responses"]["404"] == {"description": "No such user"}


def test_add_error_links_after_a_build_rebuilds():
    app = make_app(error=None)
    assert (
        "content"
        not in http_schema_resource(app, "/users/{id}")["get"]["responses"]["default"]
    )
    add_error_links(app, APP_ERROR)
    default = http_schema_resource(app, "/users/{id}")["get"]["responses"]["default"]
    assert default["content"] == APP_ERROR_CONTENT


def test_plain_route_on_a_scrudful_path():
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        return {}

    @app.delete("/items/{id}", status_code=202, responses={404: {}})
    async def plain_delete(id: int):
        return {}

    add_error_links(app, APP_ERROR)
    add_options(app, base_url=BASE)
    hsr = http_schema_resource(app, "/items/{id}")
    assert hsr["delete"] == {
        "responses": {
            "202": {"description": "Accepted"},
            "404": {"description": "Not Found", "content": APP_ERROR_CONTENT},
            "default": {"description": "Error.", "content": APP_ERROR_CONTENT},
        }
    }


def test_the_hsr_is_a_deep_copy():
    app = make_app()
    hsr = http_schema_resource(app, "/users/{id}")
    pristine = copy.deepcopy(hsr)
    hsr["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {}
    assert http_schema_resource(app, "/users/{id}") == pristine


# --- the HSR GET route --------------------------------------------------------------


def exposed(headers: Any) -> set[str]:
    return {n.strip() for n in headers["access-control-expose-headers"].split(",")}


def require_token(authorization: str | None = Header(None)) -> str:
    """An app-level dependency: every APIRoute needs ``Bearer <token>``."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token needed")
    return authorization.removeprefix("Bearer ")


def token_app() -> FastAPI:
    """``/users/{id}`` (GET) behind an app-level ``Depends(require_token)``."""
    app = FastAPI(dependencies=[Depends(require_token)])

    @app.get("/users/{id}")
    @scrudful(etag_func=lambda **kwargs: '"v1"', **USER_LINKS)
    async def get_user(id: int):
        return {"id": id}

    add_error_links(app, APP_ERROR)
    add_options(app, base_url=BASE)
    return app


def hsr_problems(response: Any) -> list[str]:
    return hsr_get_problems(
        response.status_code, dict(response.headers), response.content
    )


def test_hsr_route_serves_document(make_client):
    app = make_app()
    client = make_client(app)
    for name, path in (("users.id", "/users/{id}"), ("users", "/users")):
        response = client.get(f"/_hsr/{name}")
        assert hsr_problems(response) == []
        assert response.content == serialize_hsr(http_schema_resource(app, path))
        assert response.headers["etag"] == hsr_etag(response.content)
    response = client.get("/_hsr/users.id")
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["link"] == hsr_response_links()
    assert {"Link", "Allow", "ETag"} <= exposed(response.headers)
    etag = response.headers["etag"]
    for if_none_match in (etag, "W/" + etag, f'"other", {etag}', "*"):
        not_modified = client.get(
            "/_hsr/users.id", headers={"If-None-Match": if_none_match}
        )
        assert not_modified.status_code == 304
        assert not_modified.content == b""
        assert not_modified.headers["etag"] == etag
        assert not_modified.headers["cache-control"] == "public, max-age=300"
        assert "content-type" not in not_modified.headers
    other = client.get("/_hsr/users.id", headers={"If-None-Match": '"other"'})
    assert other.status_code == 200
    head = client.head("/_hsr/users.id")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["etag"] == etag
    assert head.headers["content-type"] == "application/json"
    head_304 = client.head("/_hsr/users.id", headers={"If-None-Match": etag})
    assert head_304.status_code == 304


def test_hsr_etag_follows_the_document(make_client):
    """After the document changes, the old ETag is stale: a 200 with the new
    document and a new ETag, not a 304."""
    app = make_app()
    client = make_client(app)
    before = client.get("/_hsr/users.id")
    add_options(app, base_url="https://changed.example/")
    after = client.get(
        "/_hsr/users.id", headers={"If-None-Match": before.headers["etag"]}
    )
    assert after.status_code == 200
    assert after.content != before.content
    assert after.headers["etag"] != before.headers["etag"]
    assert after.headers["etag"] == hsr_etag(after.content)
    again = client.get(
        "/_hsr/users.id", headers={"If-None-Match": after.headers["etag"]}
    )
    assert again.status_code == 304


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_hsr_route_answers_other_methods_405(make_client, method):
    response = make_client(make_app()).request(method, "/_hsr/users.id")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert response.content == b""
    assert "content-type" not in response.headers
    assert {"Link", "Allow"} <= exposed(response.headers)


def test_hsr_route_unknown_name_is_a_json_404(make_client):
    """An unknown name, with no other route matching, gets the app's 404:
    FastAPI's JSON one, or with add_error_links, that of any unknown URL."""
    for error, content_type in ((None, "application/json"), (APP_ERROR, None)):
        client = make_client(make_app(error=error))
        unknown = client.get("/nowhere")
        for url in ("/_hsr/people.id", "/_hsr/users.name", "/_hsr/_root"):
            response = client.get(url)
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}
            assert response.headers["content-type"] == (
                content_type or unknown.headers["content-type"]
            )
            assert response.headers.get("link") == unknown.headers.get("link")


def test_hsr_route_leaves_other_hsr_paths_to_later_routes(make_client):
    """A name the app has no resource of goes on to its other routes: a plain
    route under /_hsr/, and a sub-app mounted at "" with its own HSR route."""
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    sub = FastAPI()

    @sub.get("/things/{id}")
    @scrudful(**links)
    async def thing(id: int, request: Request):
        return {"target": _hsr_target(request, "/things/{id}")}

    add_options(sub, base_url=BASE)
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(**links)
    async def item(id: int):
        return {}

    add_options(app, base_url=BASE)

    @app.get("/_hsr/status")
    async def status():
        return {"ok": True}

    app.mount("", sub)
    client = make_client(app)
    assert client.get("/_hsr/status").json() == {"ok": True}
    assert client.get("/_hsr/items.id").json() == http_schema_resource(
        app, "/items/{id}"
    )
    target = client.get("/things/1").json()["target"]
    assert target == "https://api.example/_hsr/things.id"
    response = client.get("/_hsr/things.id")
    assert hsr_problems(response) == []
    assert response.json() == http_schema_resource(sub, "/things/{id}")
    assert client.get("/_hsr/nothing").status_code == 404


def test_hsr_route_has_no_dependencies(make_client):
    """An app's, a router's and an ``include_router`` call's dependencies
    apply to their APIRoutes, not to the HSR route."""
    from fastapi.routing import APIRoute

    app = token_app()
    client = make_client(app)
    assert client.get("/users/1").status_code == 401
    authorized = client.get("/users/1", headers={"Authorization": "Bearer a"})
    assert authorized.json() == {"id": 1}
    assert hsr_problems(client.get("/_hsr/users.id")) == []
    route = app.router.routes[0]
    assert isinstance(route, Route) and not isinstance(route, APIRoute)
    assert route.include_in_schema is False
    assert not any("_hsr" in path for path in app.openapi()["paths"])

    router = APIRouter(dependencies=[Depends(require_token)])

    @router.get("/items/{id}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        return {}

    add_options(router, base_url=BASE)
    other = FastAPI(dependencies=[Depends(require_token)])
    other.include_router(router, prefix="/v1", dependencies=[Depends(require_token)])
    client = make_client(other)
    assert client.get("/v1/items/1").status_code == 401
    response = client.get("/v1/_hsr/items.id")
    assert hsr_problems(response) == []
    assert response.json() == http_schema_resource(router, "/items/{id}")


def test_hsr_bytes_identical_across_hosts_and_tokens(make_client):
    """Two apps (each builds its own document), two Hosts, two tokens: the
    same bytes and ETag, while the resources themselves need the token."""
    responses = []
    for host, token in (("a.example", "alice"), ("b.example", "bob")):
        client = make_client(token_app(), base_url=f"http://{host}")
        auth = {"Authorization": f"Bearer {token}", "Host": host}
        assert client.get("/users/1", headers=auth).status_code == 200
        responses.append(client.get("/_hsr/users.id", headers=auth))
    first, second = responses
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert first.headers["etag"] == second.headers["etag"]
    assert b"a.example" not in first.content
    assert first.content == serialize_hsr(
        http_schema_resource(token_app(), "/users/{id}")
    )


def test_hsr_route_resolves_before_every_path(make_client):
    """Wildcard paths, added before or after ``add_options``, would match
    ``/_hsr/<name>`` if they came first."""
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    app = FastAPI()

    @app.get("/{kind}/{id}")
    @scrudful(**links)
    async def pair(kind: str, id: str):
        return {"pair": [kind, id]}

    add_options(app, base_url=BASE)

    @app.get("/{id}")
    @scrudful(**links)
    async def item(id: str):
        return {"id": id}

    client = make_client(app)
    for name, path in (("kind.id", "/{kind}/{id}"), ("id", "/{id}")):
        response = client.get(f"/_hsr/{name}")
        assert response.headers["content-type"] == "application/json"
        assert response.json() == http_schema_resource(app, path)
    assert client.get("/a/b").json() == {"pair": ["a", "b"]}
    assert client.get("/_hsr").json() == {"id": "_hsr"}  # only /_hsr/ is taken


def test_hsr_route_serves_routes_added_later_and_mounted(make_client):
    from fastapi.routing import APIRoute

    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    app = FastAPI()
    add_options(app, base_url=BASE)
    client = make_client(app)
    assert client.get("/_hsr/items.id").status_code == 404

    @app.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int):
        return {}

    app.router.routes.append(
        Mount("/m", routes=[APIRoute("/things/{id}", get_item, methods=["GET"])])
    )
    for name, path in (("items.id", "/items/{id}"), ("m.things.id", "/m/things/{id}")):
        response = client.get(f"/_hsr/{name}")
        assert response.status_code == 200
        assert response.json() == http_schema_resource(app, path)
    # The display template is the first route's, parameter names and all.
    assert client.get("/_hsr/items.item_id").status_code == 404


def test_hsr_route_in_router_mode(make_client):
    """On an ``APIRouter``, the route is the router's: the prefix it is
    included under applies, parameters and all."""
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int):
        return {}

    add_options(router, base_url="https://router.example/")
    app = FastAPI()
    app.include_router(router, prefix="/orgs/{org}")
    client = make_client(app)
    response = client.get("/orgs/acme/_hsr/items.id")
    assert hsr_problems(response) == []
    assert response.json() == http_schema_resource(router, "/items/{id}")
    assert client.get("/_hsr/items.id").status_code == 404  # not on the app
    assert client.post("/orgs/acme/_hsr/items.id").status_code == 405
    assert client.get("/orgs/acme/_hsr/orgs.org.items.id").status_code == 404


def test_hsr_cache_control_option(make_client):
    client = make_client(make_app(hsr_cache_control="public, max-age=60"))
    response = client.get("/_hsr/users.id")
    assert response.headers["cache-control"] == "public, max-age=60"
    etag = response.headers["etag"]
    not_modified = client.get("/_hsr/users.id", headers={"If-None-Match": etag})
    assert not_modified.headers["cache-control"] == "public, max-age=60"


def test_calling_add_options_again_adds_no_route(make_client):
    app = make_app()
    count = len(app.router.routes)
    add_options(app, base_url=BASE)
    assert len(app.router.routes) == count
    client = make_client(app)
    assert client.get("/_hsr/users.id").status_code == 200
    add_options(app, base_url=BASE, hsr_route=False, hsr_url=lambda p: "/x")
    assert client.get("/_hsr/users.id").status_code == 404  # turned off
    add_options(app, base_url=BASE)
    assert len(app.router.routes) == count
    assert client.get("/_hsr/users.id").status_code == 200


def test_reserved_segment(make_client):
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    for owner in (FastAPI(), APIRouter()):

        @owner.get("/_hsr/x")
        @scrudful(**links)
        async def reserved():
            return {}

        with pytest.raises(ValueError, match=r"'/_hsr/x'.*reserved"):
            add_options(owner, base_url=BASE)
        with pytest.raises(ValueError, match="reserved"):
            add_options(owner, base_url=BASE, hsr_route=False, hsr_url=str)
    # A path added after add_options raises on the first request.
    app = FastAPI()
    add_options(app, base_url=BASE)

    @app.get("/_hsr/{name}/x")
    @scrudful(**links)
    async def later(name: str):
        return {}

    with pytest.raises(ValueError, match=r"'/_hsr/\{name\}/x'.*reserved"):
        make_client(app).get("/_hsr/anything")


def test_hsr_route_name_encodes_convertors(make_client):
    """Each convertor but ``str`` is joined to its parameter's name."""
    app = FastAPI()

    @app.get("/items/{id:int}/{rest:path}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int, rest: str):
        return {}

    @app.get("/things/{id:str}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_thing(id: str):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    response = client.get("/_hsr/items.id-int.rest-path")
    assert response.json() == http_schema_resource(app, "/items/{n:int}/{p:path}")
    assert client.get("/_hsr/items.id.rest").status_code == 404
    response = client.get("/_hsr/things.id")
    assert response.json() == http_schema_resource(app, "/things/{x}")


def test_hsr_route_name_clash(make_client):
    """``/a.b`` and ``/a/b`` are two resources with one route name, ``a.b``:
    the naming loses the difference. ``/items/{id}`` beside
    ``/items/{id:int}`` doesn't clash."""
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    def clashing(owner: Any) -> Any:
        @owner.get("/a.b")
        @scrudful(**links)
        async def dotted():
            return {}

        @owner.get("/a/b")
        @scrudful(**links)
        async def nested():
            return {}

        return owner

    clash = r"'/a\.b' and '/a/b'.*'a\.b'"
    with pytest.raises(ValueError, match=clash):
        add_options(clashing(FastAPI()), base_url=BASE)
    with pytest.raises(ValueError, match=clash):
        add_options(clashing(APIRouter()), base_url=BASE)
    # With the route off there is no route name to clash.
    add_options(clashing(FastAPI()), base_url=BASE, hsr_route=False, hsr_url=str)
    # Added after add_options: the first request raises.
    app = FastAPI()

    @app.get("/a.b")
    @scrudful(**links)
    async def dotted():
        return {}

    add_options(app, base_url=BASE)

    @app.get("/a/b")
    @scrudful(**links)
    async def nested():
        return {}

    with pytest.raises(ValueError, match=clash):
        make_client(app).get("/_hsr/a.b")
    # A convertor is part of the name.
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(**links)
    async def by_name(id: str):
        return {}

    @app.get("/items/{id:int}")
    @scrudful(**links)
    async def by_number(id: int):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    for name, path in (("items.id", "/items/{id}"), ("items.id-int", "/items/{n:int}")):
        assert client.get(f"/_hsr/{name}").json() == http_schema_resource(app, path)


def test_hsr_route_false_needs_hsr_url(make_client):
    with pytest.raises(ValueError, match=re.escape("hsr_route=False needs hsr_url=")):
        make_app(hsr_route=False)
    with pytest.raises(ValueError, match=re.escape("hsr_route=False needs hsr_url=")):
        add_options(APIRouter(), hsr_route=False)
    app = make_app(hsr_route=False, hsr_url=lambda p: f"https://docs.example/hsr{p}")
    assert not any(
        "_hsr" in str(getattr(route, "path", "")) for route in app.router.routes
    )
    assert make_client(app).get("/_hsr/users.id").status_code == 404


@pytest.mark.parametrize("owner", ["app", "router"])
def test_hsr_route_none_is_the_default(make_client, owner):
    """``hsr_route`` takes ``bool | None``, as ninja's and DRF's do; ``None``
    (FastAPI has no setting to read) is the default, on."""
    if owner == "app":
        app = make_app(hsr_route=None)
    else:
        router = APIRouter()

        @router.get("/users/{id}")
        @scrudful(**USER_LINKS)
        async def get_user(id: int):
            return {}

        add_options(router, base_url=BASE, hsr_route=None)
        app = FastAPI()
        app.include_router(router)
    client = make_client(app)
    assert hsr_problems(client.get("/_hsr/users.id")) == []
    response = client.get("/users/1")
    assert hsr_link_problems(response.headers, f"{BASE}_hsr/users.id") == []
    with pytest.raises(TypeError, match="hsr_route must be a bool or None"):
        add_options(FastAPI(), hsr_route="yes")


def test_http_schema_resource_endpoint_for_mounting(make_client):
    app = make_app(hsr_route=False, hsr_url=lambda p: f"https://docs.example/hsr{p}")
    endpoint = http_schema_resource_endpoint(app, "/users/{id}")
    docs = FastAPI(dependencies=[Depends(require_token)])
    docs.add_route(
        "/hsr/users", endpoint, methods=["GET", "POST"], include_in_schema=False
    )
    client = make_client(docs)
    response = client.get("/hsr/users")
    assert hsr_problems(response) == []
    assert response.content == serialize_hsr(http_schema_resource(app, "/users/{id}"))
    assert response.headers["cache-control"] == "public, max-age=300"
    etag = response.headers["etag"]
    assert client.get("/hsr/users", headers={"If-None-Match": etag}).status_code == 304
    assert client.head("/hsr/users").status_code == 200
    not_allowed = client.post("/hsr/users")
    assert not_allowed.status_code == 405
    assert not_allowed.headers["allow"] == "GET, HEAD"
    assert "content-type" not in not_allowed.headers
    with pytest.raises(TypeError, match="FastAPI application or an APIRouter"):
        http_schema_resource_endpoint(object(), "/users/{id}")  # type: ignore[arg-type]
    unknown = http_schema_resource_endpoint(app, "/people/{id}")
    docs.add_route("/hsr/people", unknown)
    with pytest.raises(ValueError, match=r"'/people/\{id\}'"):
        client.get("/hsr/people")


# --- the HSR link target ------------------------------------------------------------


def target_app(**options: Any) -> FastAPI:
    """``/users/{id}`` answering ``_hsr_target(request, ...)`` for its own
    resource, by key and by another template of it."""
    options.setdefault("base_url", BASE)
    app = FastAPI()

    @app.get("/users/{id}")
    @scrudful(**USER_LINKS)
    async def get_user(id: int, request: Request):
        return {
            "key": _hsr_target(request, "/users/{}"),
            "template": _hsr_target(request, "/users/{user_id}"),
        }

    add_options(app, **options)
    return app


def test_target_is_the_route_url_against_the_base_url(make_client):
    client = make_client(target_app(), base_url="http://elsewhere.example")
    assert client.get("/users/1").json() == {
        "key": "https://api.example/_hsr/users.id",
        "template": "https://api.example/_hsr/users.id",
    }


def test_target_without_base_url_is_against_the_request(make_client):
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(
        schema_link_or_func="https://s.example/s.json",
        context_link_or_func="https://s.example/c.jsonld",
    )
    async def get_item(id: int, request: Request):
        return {"target": _hsr_target(request, "/items/{id}")}

    add_options(app)
    outer = FastAPI()
    outer.mount("/api", app)
    client = make_client(outer, base_url="http://here.example")
    target = client.get("/api/items/1").json()["target"]
    assert target == "http://here.example/api/_hsr/items.id"
    response = client.get("/api/_hsr/items.id")
    assert response.json() == http_schema_resource(app, "/items/{id}")


def test_target_is_hsr_url_when_given(make_client):
    app = target_app(hsr_url=lambda p: f"https://docs.example/hsr{p}")
    assert make_client(app).get("/users/1").json() == {
        "key": "https://docs.example/hsr/users/{id}",
        "template": "https://docs.example/hsr/users/{id}",
    }
    app = target_app(hsr_route=False, hsr_url=lambda p: f"https://docs.example/off{p}")
    assert make_client(app).get("/users/1").json()["key"] == (
        "https://docs.example/off/users/{id}"
    )


def test_target_in_router_mode_fills_the_prefix(make_client):
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int, request: Request):
        return {
            "target": _hsr_target(request, "/items/{id}"),
            "explicit": _hsr_target(request, "/items/{}", owner=router),
        }

    add_options(router)
    app = FastAPI()
    app.include_router(router, prefix="/orgs/{org}")
    client = make_client(app)
    assert client.get("/orgs/acme/items/1").json() == {
        "target": "http://testserver/orgs/acme/_hsr/items.id",
        "explicit": "http://testserver/orgs/acme/_hsr/items.id",
    }
    add_options(router, base_url="https://router.example/v1/")
    assert client.get("/orgs/acme/items/1").json()["target"] == (
        "https://router.example/orgs/acme/_hsr/items.id"
    )


@pytest.mark.parametrize(
    "template, url, target",
    [
        # The longest prefix whose rest the template matches, "/v1/files/a",
        # isn't the one the router came through.
        ("/files/{p:path}", "/v1/files/a/files/b", "/v1/_hsr/files.p-path"),
        ("/{p:path}/raw", "/v1/a/raw/b/raw", "/v1/_hsr/p-path.raw"),
    ],
)
def test_target_in_router_mode_with_a_path_convertor(
    make_client, template, url, target
):
    """A router template containing a ``path`` parameter matches the rest of
    the URL after several prefixes: the target takes the one the request was
    routed through, whose parameters are the request's."""
    router = APIRouter()

    @router.get(template)
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_file(p: str, request: Request):
        return {"target": _hsr_target(request, template)}

    add_options(router, base_url="http://testserver/")
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    client = make_client(app)
    response = client.get(url)
    assert response.json() == {"target": f"http://testserver{target}"}
    assert hsr_link_problems(response.headers, f"http://testserver{target}") == []
    assert client.get(target).status_code == 200


def target_sub_app() -> FastAPI:
    """An app whose ``/items/{id}`` and (under its own Mount) ``/m/things/{id}``
    answer ``_hsr_target`` for their own resources."""
    from fastapi.routing import APIRoute

    links = dict(
        schema_link_or_func="https://s.example/s.json",
        context_link_or_func="https://s.example/c.jsonld",
    )
    sub = FastAPI()

    @sub.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int, request: Request):
        return {"target": _hsr_target(request, "/items/{id}")}

    async def thing(id: int, request: Request):
        return {"target": _hsr_target(request, "/m/things/{id}")}

    endpoint = scrudful(**links)(thing)
    sub.router.routes.append(
        Mount("/m", routes=[APIRoute("/things/{id}", endpoint, methods=["GET"])])
    )
    add_options(sub)
    return sub


@pytest.mark.parametrize(
    "mount_path, name, prefix",
    [
        ("/api", None, "/api"),
        ("/api", "api", "/api"),
        ("/t/{tenant}", None, "/t/acme"),
        ("/t/{tenant}", "tenants", "/t/acme%20corp"),
        ("", None, ""),
    ],
)
def test_target_under_mounts_is_well_formed_and_served(
    make_client, mount_path, name, prefix
):
    """Under a Mount with parameters, a named Mount, and the app's own Mount:
    the request's path of the app's root, and the target answers."""
    sub = target_sub_app()
    outer = FastAPI()
    outer.mount(mount_path, sub, name=name)
    client = make_client(outer)
    for url, path, hsr_name in (
        (f"{prefix}/items/1", "/items/{id}", "items.id"),
        (f"{prefix}/m/things/1", "/m/things/{id}", "m.things.id"),
    ):
        target = client.get(url).json()["target"]
        assert target == f"http://testserver{prefix}/_hsr/{hsr_name}"
        response = client.get(target)
        assert hsr_problems(response) == []
        assert response.json() == http_schema_resource(sub, path)


def test_target_in_router_mode_under_a_mount_with_parameters(make_client):
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int, request: Request):
        return {"target": _hsr_target(request, "/items/{id}")}

    add_options(router, base_url=BASE)
    sub = FastAPI()
    sub.include_router(router, prefix="/orgs/{org}")
    outer = FastAPI()
    outer.mount("/t/{tenant}", sub, name="t")
    client = make_client(outer)
    target = client.get("/t/a/orgs/b/items/1").json()["target"]
    assert target == "https://api.example/t/a/orgs/b/_hsr/items.id"
    response = client.get("/t/a/orgs/b/_hsr/items.id")
    assert response.json() == http_schema_resource(router, "/items/{id}")


def test_router_resource_added_later_has_no_route_until_add_options(make_client):
    router = APIRouter()
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    @router.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int):
        return {}

    add_options(router, base_url=BASE)
    from fastapi.routing import APIRoute

    async def get_thing(id: int, request: Request):
        return {"target": _hsr_target(request, "/things/{id}", owner=router)}

    # Appended, not added with the router's methods: no route of its own.
    router.routes.append(
        APIRoute("/things/{id}", scrudful(**links)(get_thing), methods=["GET"])
    )
    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    assert client.get("/_hsr/things.id").status_code == 404
    with pytest.raises(ValueError, match="no HTTP Schema Resource route"):
        with pytest.warns(RuntimeWarning, match="no OPTIONS or HTTP Schema"):
            client.get("/things/1")
    add_options(router, base_url=BASE)
    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    assert client.get("/things/1").json()["target"] == (
        "https://api.example/_hsr/things.id"
    )
    assert client.get("/_hsr/things.id").status_code == 200


class InsertingRouter(APIRouter):
    """A router whose ``add_api_route`` inserts the route first, rather than
    appending it."""

    def add_api_route(self, path: str, endpoint: Any, **kwargs: Any) -> None:
        super().add_api_route(path, endpoint, **kwargs)
        self.routes.insert(0, self.routes.pop())


def test_router_resource_inserted_later_is_described(make_client):
    """A route-adding call that inserts its route, rather than appending it,
    adds nothing at the end of the routes: the routes are compared with
    those seen, so the inserted resource still gets its OPTIONS and HSR
    routes."""
    router = InsertingRouter()

    @router.get("/items/{id}")
    @scrudful(**NOTE_LINKS)
    async def get_item(id: int):
        return {}

    add_options(router, base_url=BASE)

    @router.get("/inserted/{id}")
    @scrudful(**NOTE_LINKS)
    async def get_inserted(id: int):
        return {}

    paths = [getattr(route, "path", None) for route in router.routes]
    assert paths.index("/inserted/{id}") < paths.index("/items/{id}")
    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    for name in ("inserted", "items"):
        target = f"{BASE}_hsr/{name}.id"
        options_204(client, f"/{name}/1", "GET, OPTIONS", target)
        assert hsr_link_problems(client.get(f"/{name}/1").headers, target) == []
        assert client.get(target).status_code == 200


def test_router_resource_added_later_is_described_as_it_is_added(make_client):
    """A resource added to a router after its :func:`add_options` (with its
    route decorators) gets its OPTIONS and HSR routes as it is added, as an
    app's later routes are described: its responses link to its HSR."""
    router = APIRouter()
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    @router.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int):
        return {}

    add_options(router, base_url=BASE)

    @router.get("/things/{id}")
    @router.put("/things/{id}")
    @scrudful(etag_func=lambda **kwargs: "v1", **links, error=APP_ERROR)
    async def get_thing(id: int):
        if id == 404:
            raise HTTPException(404)
        return {}

    app = FastAPI()
    app.include_router(router)
    add_error_links(app, APP_ERROR)
    client = make_client(app)
    target = "https://api.example/_hsr/things.id"
    for method, url, headers, status in [
        ("GET", "/things/1", {}, 200),
        ("GET", "/things/1", {"If-None-Match": '"v1"'}, 304),
        ("GET", "/things/1", {"If-Match": '"no"'}, 412),
        ("PUT", "/things/1", {}, 428),
        ("GET", "/things/404", {}, 404),
    ]:
        response = client.request(method, url, headers=headers)
        assert response.status_code == status
        assert hsr_link_problems(response.headers, target) == [], (method, status)
    options_204(client, "/things/1", "GET, PUT, OPTIONS", target)
    assert hsr_problems(client.get("/_hsr/things.id")) == []
    # Calling it again adds nothing more.
    count = len(router.routes)
    add_options(router, base_url=BASE)
    assert len(router.routes) == count


LATE_LINKS = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")


def _late_endpoint() -> Any:
    async def late(id: int):
        return {}

    return scrudful(**LATE_LINKS)(late)


def _described(client: TestClient, url: str, target: str, allow: str) -> None:
    """``url`` links to ``target``, answers OPTIONS and the HSR is served."""
    assert hsr_link_problems(client.get(url).headers, target) == [], url
    options_204(client, url, allow, target)
    assert client.get(target).status_code == 200


def test_router_resource_added_later_to_an_included_router_is_described(
    make_client,
):
    """A route added to a router already included in the router, at any
    depth, gets its routes too. FastAPI versions that copy an included
    router's routes don't serve such a route at all."""
    import scrudful.fastapi as module

    if module._iter_route_contexts is None:
        pytest.skip("this FastAPI copies an included router's routes")
    leaf, child, router = APIRouter(), APIRouter(), APIRouter()
    router.get("/items/{id}")(_late_endpoint())
    child.include_router(leaf, prefix="/l")
    router.include_router(child, prefix="/c")
    add_options(router, base_url=BASE)
    child.get("/late/{id}")(_late_endpoint())
    leaf.get("/later/{id}")(_late_endpoint())
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    client = make_client(app)
    base = "https://api.example/v1/_hsr"
    _described(client, "/v1/c/late/1", f"{base}/c.late.id", "GET, OPTIONS")
    _described(client, "/v1/c/l/later/1", f"{base}/c.l.later.id", "GET, OPTIONS")


def test_a_router_included_twice_is_completed_under_both_prefixes(make_client):
    """A router included twice in the router, under two prefixes, gives its
    routes under each: both are completed, whether included after the call
    or given a route after it (FastAPI versions that copy an included
    router's routes don't serve the latter)."""
    import scrudful.fastapi as module

    child, router = APIRouter(), APIRouter()
    child.get("/k/{id}")(_late_endpoint())
    add_options(router, base_url=BASE)
    router.include_router(child, prefix="/x")
    router.include_router(child, prefix="/y")
    lazy = module._iter_route_contexts is not None
    if lazy:
        child.get("/late/{id}")(_late_endpoint())
    app = FastAPI()
    app.include_router(router, prefix="/v")
    client = make_client(app)
    base = "https://api.example/v/_hsr"
    for prefix in ("x", "y"):
        _described(client, f"/v/{prefix}/k/1", f"{base}/{prefix}.k.id", "GET, OPTIONS")
        if lazy:
            target = f"{base}/{prefix}.late.id"
            _described(client, f"/v/{prefix}/late/1", target, "GET, OPTIONS")


def test_a_route_appended_is_completed_by_the_next_route_added(make_client):
    """A route appended to the router's ``routes`` is completed by the next
    route added with its methods, with no warning."""
    from fastapi.routing import APIRoute

    router = APIRouter()
    add_options(router, base_url=BASE)
    router.get("/a/{id}")(_late_endpoint())
    router.routes.append(APIRoute("/b/{id}", _late_endpoint(), methods=["GET"]))
    router.get("/c/{id}")(_late_endpoint())
    app = FastAPI()
    app.include_router(router, prefix="/v")
    client = make_client(app)
    for name in ("a", "b", "c"):
        target = f"https://api.example/v/_hsr/{name}.id"
        _described(client, f"/v/{name}/1", target, "GET, OPTIONS")


@pytest.mark.parametrize("owner", ["router", "app"])
def test_a_replaced_hsr_route_degrades_until_completed_again(make_client, owner):
    """An HSR route replaced in its router's routes (``routes[i] = ...``), or
    removed from an app's, is forgotten: OPTIONS answers without the HSR
    link (it raised ``NoMatchFound``), the responses carry none, and a
    ``RuntimeWarning`` names the resource for a router; the next route added
    (or ``add_options`` again) gives it back."""
    from fastapi.routing import APIRoute

    import scrudful.fastapi as module

    if owner == "router":
        router = APIRouter()
        router.get("/a/{id}")(_late_endpoint())
        add_options(router, base_url=BASE)
        index = next(i for i, r in enumerate(router.routes) if r.path == "/_hsr/a.id")
        router.routes[index] = APIRoute(
            "/a/{id}", _late_endpoint(), methods=["GET", "PUT"]
        )
        app = FastAPI()
        app.include_router(router)
    else:
        app = FastAPI()
        app.get("/a/{id}")(_late_endpoint())
        add_options(app, base_url=BASE)
        app.router.routes.remove(getattr(app, module._HSR_ROUTE_ATTRIBUTE))
    client = make_client(app)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        response = client.options("/a/1")
        assert response.status_code == 204
        assert "link" not in response.headers
        assert HSR_DOCUMENT_REL not in client.get("/a/1").headers.get("link", "")
    if owner == "router":
        assert [str(w.message) for w in caught if "'/a/{id}'" in str(w.message)]
        router.get("/b/{id}")(_late_endpoint())
        # Included anew: a FastAPI that copies the routes serves those it had.
        app = FastAPI()
        app.include_router(router)
        client = make_client(app)
    else:
        add_options(app, base_url=BASE)
    target = f"{BASE}_hsr/a.id"
    assert hsr_link_problems(client.get("/a/1").headers, target) == []
    assert hsr_link_problems(client.options("/a/1").headers, target) == []
    assert client.get("/_hsr/a.id").status_code == 200


def test_dead_watched_routers_are_pruned_when_scanned(make_client):
    """The routers given ``add_options`` are held weakly, and the dead ones
    dropped when an uncovered endpoint scans them."""
    import gc

    import scrudful.fastapi as module

    router = APIRouter()
    add_options(router, base_url=BASE)
    del router
    gc.collect()
    app = FastAPI()
    app.get("/u/{id}")(_late_endpoint())
    assert make_client(app).get("/u/1").status_code == 200
    assert all(ref() is not None for ref in module._WATCHED)


def test_router_included_after_add_options_is_described(make_client):
    """A router included in the router after the call gets its routes, on
    every FastAPI version (one that copies its routes adds them with
    ``add_api_route``, once each)."""
    other, router = APIRouter(), APIRouter()
    router.get("/items/{id}")(_late_endpoint())
    add_options(router, base_url=BASE)
    other.get("/notes/{id}")(_late_endpoint())
    other.put("/notes/{id}")(_late_endpoint())
    router.include_router(other, prefix="/o")
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    client = make_client(app)
    target = "https://api.example/v1/_hsr/o.notes.id"
    _described(client, "/v1/o/notes/1", target, "GET, PUT, OPTIONS")


def test_a_copying_include_router_completes_once(monkeypatch):
    """An ``include_router`` that adds the routes with ``add_api_route``, as
    FastAPI versions that copy an included router's routes do, completes
    the router once, after all of them, not once per route."""
    import scrudful.fastapi as module

    class CopyingRouter(APIRouter):
        def include_router(self, router: APIRouter, prefix: str = "", **kw: Any):
            for route in router.routes:
                self.add_api_route(
                    prefix + route.path, route.endpoint, methods=list(route.methods)
                )

    other, router = APIRouter(), CopyingRouter()
    router.get("/items/{id}")(_late_endpoint())
    add_options(router, base_url=BASE)
    for index in range(3):
        other.get(f"/notes{index}/{{id}}")(_late_endpoint())
    calls = []
    original = module._complete
    monkeypatch.setattr(
        module, "_complete", lambda *args: calls.append(args) or original(*args)
    )
    router.include_router(other, prefix="/o")
    assert len(calls) == 1
    keys = {route.path for route in router.routes if "OPTIONS" in (route.methods or ())}
    assert keys == {"/items/{id}", "/o/notes0/{id}", "/o/notes1/{id}", "/o/notes2/{id}"}


def test_routes_added_after_add_options_are_completed_alone(monkeypatch):
    """Each route added after the call is completed alone: no walk of every
    route of the router (which would make startup quadratic)."""
    import scrudful.fastapi as module

    calls = {"walks": 0, "checks": 0}
    walks = (module._find_scrudful_paths, module._endpoints)
    check = module._check_describe_call

    def counting(func: Any, kind: str) -> Any:
        def counted(*args: Any, **kwargs: Any) -> Any:
            calls[kind] += 1
            return func(*args, **kwargs)

        return counted

    router = APIRouter()
    router.get("/seed/{id}")(_late_endpoint())
    add_options(router, base_url=BASE)
    for func in walks:
        monkeypatch.setattr(module, func.__name__, counting(func, "walks"))
    monkeypatch.setattr(module, "_check_describe_call", counting(check, "checks"))
    count = 40
    for index in range(count):
        router.get(f"/r{index}/{{id}}")(_late_endpoint())
    assert calls == {"walks": 0, "checks": count}
    monkeypatch.undo()
    options = [r for r in router.routes if r.methods == {"OPTIONS"}]
    hsr = getattr(router, module._HSR_ROUTE_ATTRIBUTE)
    assert len(options) == len(hsr) == count + 1


def test_a_route_added_past_the_router_s_methods_is_announced(make_client):
    """A route added with ``APIRouter.add_api_route(router, ...)`` (past the
    instance's methods, which scrudful completes) is found when it first
    responds: a ``RuntimeWarning`` names its resource, which has no OPTIONS
    or HSR route; a route of a resource that has them links to its HSR."""
    router = APIRouter()
    router.get("/items/{id}")(_late_endpoint())
    add_options(router, base_url=BASE)
    APIRouter.add_api_route(router, "/cls/{id}", _late_endpoint(), methods=["GET"])
    APIRouter.add_api_route(router, "/items/{id}", _late_endpoint(), methods=["PUT"])
    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    with pytest.warns(RuntimeWarning, match=r"'/cls/\{id\}'"):
        response = client.get("/cls/1")
    assert HSR_DOCUMENT_REL not in response.headers.get("link", "")
    response = client.put("/items/1")
    assert hsr_link_problems(response.headers, f"{BASE}_hsr/items.id") == []


def test_add_options_again_on_a_served_router_serves_the_hsr_route(make_client):
    """A router served without the HSR route that gets it from a second
    ``add_options`` serves it (FastAPI's included-router cache is told the
    routes changed). FastAPI versions that copy an included router's routes
    serve only those it had when included."""
    import scrudful.fastapi as module

    if module._iter_route_contexts is None:
        pytest.skip("this FastAPI copies an included router's routes")
    router = APIRouter()
    router.get("/a/{id}")(_late_endpoint())
    add_options(router, hsr_route=False, hsr_url=lambda path: "https://docs.example/a")
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    client = make_client(app)
    assert (
        hsr_link_problems(client.get("/v1/a/1").headers, "https://docs.example/a") == []
    )
    add_options(router, base_url=BASE, hsr_route=True)
    target = "https://api.example/v1/_hsr/a.id"
    assert hsr_link_problems(client.get("/v1/a/1").headers, target) == []
    assert client.get("/v1/_hsr/a.id").status_code == 200


def test_router_resource_added_later_is_checked_as_add_options_checks(make_client):
    router = APIRouter()
    add_options(router, base_url=BASE)
    with pytest.raises(ValueError, match="called as func"):

        @router.get("/things/{id}")
        @scrudful(
            schema_link_or_func=lambda request: "/s.json",
            context_link_or_func="/c.jsonld",
        )
        async def get_thing(id: int):  # pragma: no cover
            return {}


def drop_route(router: APIRouter, sub: APIRouter, path: str) -> None:
    """Remove the route ``path`` of ``sub``, included in ``router``, from
    both (a FastAPI that includes routers lazily is told, as its own methods
    tell it)."""
    for owner in (router, sub):
        owner.routes[:] = [r for r in owner.routes if getattr(r, "path", None) != path]
        changed = getattr(owner, "_mark_routes_changed", None)
        if callable(changed):
            changed()


def test_a_refused_addition_commits_nothing(make_client):
    """A call adding several resources at once that is refused (one under
    ``/_hsr/``) leaves none of them taken as completed: once the offending
    route is gone, the next route added completes the others too, rather
    than leaving them named but without their routes."""
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful(**NOTE_LINKS)
    async def get_item(id: int):
        return {}

    add_options(router, base_url=BASE)
    sub = APIRouter()

    @sub.get("/new/{id}")
    @scrudful(**NOTE_LINKS)
    async def get_new(id: int):
        return {}

    @sub.get("/_hsr/x")
    @scrudful(**NOTE_LINKS)
    async def reserved():  # pragma: no cover
        return {}

    with pytest.raises(ValueError, match="reserved"):
        router.include_router(sub)
    # The project drops the offending route, and goes on adding routes.
    drop_route(router, sub, "/_hsr/x")

    @router.get("/more/{id}")
    @scrudful(**NOTE_LINKS)
    async def get_more(id: int):
        return {}

    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    for name in ("new", "more", "items"):
        target = f"https://api.example/_hsr/{name}.id"
        options_204(client, f"/{name}/1", "GET, OPTIONS", target)
        assert hsr_link_problems(client.get(f"/{name}/1").headers, target) == []
        assert client.get(target).status_code == 200


def test_a_refused_name_clash_commits_no_name(make_client):
    """A resource whose HSR route name clashes, refused, leaves the names of
    the resources added with it free: ``add_options`` (or the next route
    added) names them again, as if the call had not been made."""
    router = APIRouter()

    @router.get("/a.b")
    @scrudful(**NOTE_LINKS)
    async def dotted():
        return {}

    add_options(router, base_url=BASE)
    sub = APIRouter()

    @sub.get("/first/{id}")
    @scrudful(**NOTE_LINKS)
    async def first(id: int):
        return {}

    @sub.get("/a/b")
    @scrudful(**NOTE_LINKS)
    async def clashing():  # pragma: no cover
        return {}

    with pytest.raises(ValueError, match="same HTTP Schema Resource route name"):
        router.include_router(sub)
    drop_route(router, sub, "/a/b")

    @router.get("/second/{id}")
    @scrudful(**NOTE_LINKS)
    async def second(id: int):
        return {}

    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    for name in ("first", "second"):
        target = f"https://api.example/_hsr/{name}.id"
        options_204(client, f"/{name}/1", "GET, OPTIONS", target)


def test_router_resource_the_hooks_miss_is_announced(make_client):
    """A route the router's methods don't add (appended to its ``routes``)
    has no OPTIONS or HSR route: a ``RuntimeWarning`` names it, once, when
    the router's resources are next indexed."""
    from fastapi.routing import APIRoute

    router = APIRouter()
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    @router.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int):
        return {}

    add_options(router, base_url=BASE)

    async def thing(id: int):
        return {}

    router.routes.append(
        APIRoute("/things/{id}", scrudful(**links)(thing), methods=["GET"])
    )
    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    with pytest.warns(RuntimeWarning, match=r"'/things/\{id\}'.*add_options"):
        client.get("/items/1")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client.get("/items/1")
        response = client.get("/things/1")
    assert response.status_code == 200
    assert HSR_DOCUMENT_REL not in response.headers.get("link", "")
    add_options(router, base_url=BASE)
    target = "https://api.example/_hsr/things.id"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert hsr_link_problems(client.get("/things/1").headers, target) == []


class _BuggyConvertor(Convertor):
    """Matches any rest of the path; its ``convert`` has a bug for a value
    without a ``/``."""

    regex = ".+"

    def convert(self, value: str) -> str:
        if "/" not in value:
            raise RuntimeError("convertor bug")
        return value

    def to_string(self, value: str) -> str:  # pragma: no cover
        return value


def test_a_convertor_s_unexpected_error_propagates(make_client):
    """Finding the prefix a router came through converts the parameters each
    candidate gives: a convertor refusing one (``ValueError``) rules the
    candidate out, but another exception is a bug, not hidden behind
    another prefix."""
    register_url_convertor("scrudful_test_buggy", _BuggyConvertor())
    router = APIRouter()

    @router.get("/a/{rest:scrudful_test_buggy}")
    @scrudful(**NOTE_LINKS)
    async def read(rest: str):
        return {}

    add_options(router, base_url=BASE)
    app = FastAPI()
    app.include_router(router)
    with pytest.raises(RuntimeError, match="convertor bug"):
        make_client(app).get("/a/a/b")


def test_target_needs_a_request_to_a_url_of_the_path(make_client):
    app = target_app()

    @app.get("/elsewhere")
    async def elsewhere(request: Request):
        return {"target": _hsr_target(request, "/users/{id}")}

    with pytest.raises(ValueError, match=r"needs a request to a URL of it"):
        make_client(app).get("/elsewhere")


def test_target_of_an_unknown_path_raises(make_client):
    app = target_app()

    @app.get("/elsewhere")
    async def elsewhere(request: Request):
        return {"target": _hsr_target(request, "/people/{id}")}

    with pytest.raises(ValueError, match=r"'/people/\{id\}'"):
        make_client(app).get("/elsewhere")


# --- OPTIONS: a 204 with a per-requester Allow --------------------------------------

#: The roles of each bearer token.
ROLES = {"reader-token": {"reader"}, "admin-token": {"reader", "writer"}}
NOTE_LINKS = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
bearer = HTTPBearer(auto_error=False)


class NotePayload(BaseModel):
    name: str


READER = {"Authorization": "Bearer reader-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
NOTE_TARGET = "https://api.example/_hsr/notes.id"


def options_204(
    client: TestClient,
    url: str,
    allow: str,
    target: str | None = None,
    **kwargs: Any,
) -> Any:
    """The OPTIONS response of ``url``: a 204 with ``Allow: <allow>`` (exactly,
    in its order) and one HSR link (to ``target``, unless ``None``)."""
    response = client.options(url, **kwargs)
    headers = dict(response.headers)
    expected = {method.strip() for method in allow.split(",")}
    problems = options_204_problems(
        response.status_code, headers, response.content, expected
    )
    assert problems == [], (url, response.status_code, response.content)
    assert response.headers["allow"] == allow, url
    assert hsr_link_problems(headers, target) == [], url
    assert {"Link", "Allow"} <= exposed(response.headers)
    return response


def options_hsr(client: TestClient, url: str, **kwargs: Any) -> dict[str, Any]:
    """The HTTP Schema Resource the OPTIONS of ``url`` links to: the 204's HSR
    link, followed by a GET."""
    response = client.options(url, **kwargs)
    headers = dict(response.headers)
    assert options_204_problems(response.status_code, headers, response.content) == []
    (target,) = [
        target
        for target, params in parse_links(headers["link"])
        if params.get("rel") == HSR_DOCUMENT_REL
    ]
    document = client.get(target)
    assert hsr_problems(document) == [], target
    return document.json()


def require_role(role: str) -> Any:
    """A security dependency: a bearer token (``HTTPBearer``) with ``role``."""

    def check(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> str:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Token needed")
        roles = ROLES.get(credentials.credentials)
        if roles is None:
            raise HTTPException(status_code=401, detail="Bad token")
        if role not in roles:
            raise HTTPException(status_code=403, detail=f"{role} needed")
        return credentials.credentials

    return check


def note_app(
    *, head: bool = True, error: Entity | None = None, **options: Any
) -> FastAPI:
    """``/notes/{id}``: GET (and HEAD, with ``head``) for readers, PUT for
    writers; ``/notes``: POST, for anyone."""
    options.setdefault("base_url", BASE)
    app = FastAPI()
    methods = ["GET", "HEAD"] if head else ["GET"]

    @app.api_route("/notes/{id}", methods=methods)
    @scrudful(**NOTE_LINKS)
    async def read(id: int, token: str = Security(require_role("reader"))):
        return {"id": id}

    @app.put("/notes/{id}", dependencies=[Security(require_role("writer"))])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {"id": id}

    @app.post("/notes", status_code=201)
    @scrudful(**NOTE_LINKS)
    async def create():
        return {"id": 1}

    if error is not None:
        add_error_links(app, error)
    add_options(app, **options)
    return app


def counted(calls: list[str], name: str) -> Any:
    """A dependency recording ``name`` in ``calls``."""

    def dependency() -> None:
        calls.append(name)

    return dependency


def test_options_204(make_client):
    client = make_client(make_app())
    response = options_204(
        client,
        "/users/7",
        "GET, PUT, OPTIONS, HEAD",
        "https://api.example/_hsr/users.id",
    )
    assert response.content == b""
    assert "content-type" not in response.headers
    options_204(client, "/users", "POST, OPTIONS", "https://api.example/_hsr/users")
    # The link leads to the document of the path.
    assert options_hsr(client, "/users/7") == http_schema_resource(
        make_app(), "/users/{id}"
    )


def test_allow_per_requester(make_client):
    client = make_client(note_app())
    options_204(client, "/notes/1", "OPTIONS", NOTE_TARGET)
    options_204(client, "/notes/1", "GET, OPTIONS, HEAD", NOTE_TARGET, headers=READER)
    options_204(
        client, "/notes/1", "GET, PUT, OPTIONS, HEAD", NOTE_TARGET, headers=ADMIN
    )
    # Bad credentials: refused by every method's checks, not by OPTIONS.
    bad = {"Authorization": "Bearer nope"}
    options_204(client, "/notes/1", "OPTIONS", NOTE_TARGET, headers=bad)
    # The methods themselves agree.
    assert client.get("/notes/1").status_code == 401
    assert client.get("/notes/1", headers=READER).status_code == 200
    assert client.put("/notes/1", headers=READER).status_code == 403
    assert client.put("/notes/1", headers=ADMIN).status_code == 200


def test_head_is_listed_only_when_routed(make_client):
    """FastAPI's GET route doesn't serve HEAD (a 405): ``Allow`` lists HEAD
    only for a route that does."""
    client = make_client(note_app(head=False))
    assert client.head("/notes/1", headers=ADMIN).status_code == 405
    options_204(client, "/notes/1", "GET, PUT, OPTIONS", NOTE_TARGET, headers=ADMIN)


def test_load_and_allowed_hook_get_object(make_client):
    loaded = []
    seen = []

    def load(request: Request, **params: Any) -> Any:
        loaded.append((request.method, params, dict(request.path_params)))
        return {"id": params.get("id"), "owner": "alice"}

    async def allowed(request: Request, method: str, obj: Any) -> bool:
        seen.append((method, request.method, obj))
        return method in ("GET", "HEAD") or (
            request.headers.get("authorization") == "Bearer alice"
        )

    client = make_client(note_app(load=load, allowed=allowed))
    # The hook alone decides: no security dependency runs (anonymous gets GET).
    options_204(client, "/notes/7", "GET, OPTIONS, HEAD", NOTE_TARGET)
    obj = {"id": "7", "owner": "alice"}
    assert loaded == [("OPTIONS", {"id": "7"}, {"id": "7"})]
    assert seen == [("GET", "GET", obj), ("PUT", "PUT", obj), ("HEAD", "HEAD", obj)]
    alice = {"Authorization": "Bearer alice"}
    options_204(
        client, "/notes/7", "GET, PUT, OPTIONS, HEAD", NOTE_TARGET, headers=alice
    )
    # A path without parameters has no object: load isn't called.
    loaded.clear()
    seen.clear()
    options_204(client, "/notes", "OPTIONS", "https://api.example/_hsr/notes")
    assert loaded == []
    assert seen == [("POST", "POST", None)]


def test_load_is_not_called_without_path_parameters(make_client):
    """A ``load(request, id)`` that can't take a collection's (no)
    parameters: the collection's OPTIONS is still a 204."""
    loaded = []

    def load(request: Request, id: str) -> Any:
        loaded.append(id)
        return {"id": id}

    client = make_client(note_app(load=load))
    options_204(client, "/notes", "POST, OPTIONS", "https://api.example/_hsr/notes")
    options_204(client, "/notes/3", "GET, OPTIONS, HEAD", NOTE_TARGET, headers=READER)
    assert loaded == ["3"]


def test_a_nested_collection_load_gets_its_parent_parameters(make_client):
    loaded = []

    def load(request: Request, **params: Any) -> Any:
        loaded.append(params)

    app = FastAPI()

    @app.post("/orgs/{org}/notes", status_code=201)
    @scrudful(**NOTE_LINKS)
    async def create(org: str):
        return {}

    add_options(app, base_url=BASE, load=load)
    target = "https://api.example/_hsr/orgs.org.notes"
    options_204(make_client(app), "/orgs/acme/notes", "POST, OPTIONS", target)
    assert loaded == [{"org": "acme"}]


def test_allowed_hook_must_return_a_bool(make_client):
    app = note_app(allowed=lambda request, method, obj: "yes")
    with pytest.raises(TypeError, match="must return a bool"):
        make_client(app).options("/notes/1")


def test_missing_object_204_without_load_result(make_client):
    seen = []

    def allowed(request: Request, method: str, obj: Any) -> bool:
        seen.append(obj)
        return obj is None and method == "GET"

    client = make_client(note_app(load=lambda request, id: None, allowed=allowed))
    options_204(client, "/notes/404", "GET, OPTIONS", NOTE_TARGET)
    assert seen == [None, None, None]
    # Without a hook, the security dependencies decide.
    client = make_client(note_app(load=lambda request, id: None))
    options_204(client, "/notes/404", "GET, OPTIONS, HEAD", NOTE_TARGET, headers=READER)


@pytest.mark.parametrize("status", [404, 403, 410])
def test_load_http_exception_answered_with_error_links(make_client, status):
    """An ``HTTPException`` from ``load`` is the app's exception handlers'
    answer, with the error entity's Links and the HSR link."""

    async def load(request: Request, id: str) -> Any:
        raise HTTPException(status_code=status, detail="No such note")

    client = make_client(note_app(load=load, error=APP_ERROR))
    response = client.options("/notes/1", headers=READER)
    assert response.status_code == status
    assert response.json() == {"detail": "No such note"}
    assert response.headers["content-type"] == "application/problem+json"
    headers = dict(response.headers)
    assert link_problems("OPTIONS", status, headers, True) == []
    assert hsr_link_problems(headers, NOTE_TARGET) == []
    assert parse_links(headers["link"])[0][0] == (
        "https://api.example/schemas/problem.json"
    )
    assert {"Link", "Allow"} <= exposed(response.headers)


def test_load_http_exception_through_a_custom_handler(make_client):
    """The app's own handler answers it (here a status handler)."""

    def load(request: Request, id: str) -> Any:
        raise HTTPException(status_code=404)

    app = note_app(load=load)
    app.add_exception_handler(
        404, lambda request, exc: JSONResponse({"gone": True}, status_code=404)
    )
    response = make_client(app).options("/notes/1", headers=READER)
    assert response.status_code == 404
    assert response.json() == {"gone": True}
    assert hsr_link_problems(dict(response.headers), NOTE_TARGET) == []


@pytest.mark.parametrize("status", [401, 403])
def test_a_denial_an_allowed_hook_raises_means_not_allowed(make_client, status):
    """An ``HTTPException`` 401 or 403 from the hook refuses the method,
    as DRF's allowed_func and ninja's allowed= do."""

    def allowed(request: Request, method: str, obj: Any) -> bool:
        if method == "PUT":
            raise HTTPException(status_code=status, detail="No")
        return True

    response = make_client(note_app(allowed=allowed, error=APP_ERROR)).options(
        "/notes/1"
    )
    assert response.status_code == 204
    assert response.headers["allow"] == "GET, OPTIONS, HEAD"


@pytest.mark.parametrize("status", [404, 409])
def test_another_allowed_hook_http_exception_is_answered(make_client, status):
    """A 404 or another status the hook raises is the OPTIONS's
    answer, by the app's handlers."""

    def allowed(request: Request, method: str, obj: Any) -> bool:
        raise HTTPException(status_code=status, detail="No")

    response = make_client(note_app(allowed=allowed, error=APP_ERROR)).options(
        "/notes/1"
    )
    assert response.status_code == status
    assert response.json() == {"detail": "No"}
    assert hsr_link_problems(dict(response.headers), NOTE_TARGET) == []
    assert link_problems("OPTIONS", status, dict(response.headers), True) == []


def test_another_exception_an_allowed_hook_raises_propagates(make_client):
    def allowed(request: Request, method: str, obj: Any) -> bool:
        raise LookupError("broken")

    with pytest.raises(LookupError, match="broken"):
        make_client(note_app(allowed=allowed)).options("/notes/1")


def test_a_check_setting_request_state_leaves_the_others_alone(make_client):
    """Each check gets its own ``request.state``: what one sets reaches
    neither another method's check nor ``load``."""
    seen = []

    def remember(request: Request) -> None:
        seen.append(getattr(request.state, "checked", None))
        request.state.checked = request.method

    def load(request: Request, **params: Any) -> Any:
        seen.append(getattr(request.state, "checked", None))

    app = FastAPI()

    @app.api_route(
        "/notes/{id}", methods=["GET", "PUT"], dependencies=[Security(remember)]
    )
    @scrudful(**NOTE_LINKS)
    async def note(id: int):
        return {}

    add_options(app, base_url=BASE, load=load)
    options_204(make_client(app), "/notes/1", "GET, PUT, OPTIONS", NOTE_TARGET)
    assert seen == [None, None, None]


def test_no_existence_oracle(make_client):
    """A requester whom every method's security dependencies refuse gets 204
    ``Allow: OPTIONS`` for a missing and an existing id alike: nothing is
    loaded."""
    loads = []

    def load(request: Request, id: str) -> Any:
        loads.append(id)
        if id == "404":
            raise HTTPException(status_code=404)
        return {"id": id}

    client = make_client(note_app(load=load, error=APP_ERROR))
    for url in ("/notes/404", "/notes/1"):
        options_204(client, url, "OPTIONS", NOTE_TARGET)
    assert loads == []
    # Once a method passes, the object is loaded: a missing one is a 404.
    assert client.options("/notes/404", headers=READER).status_code == 404
    options_204(client, "/notes/1", "GET, OPTIONS, HEAD", NOTE_TARGET, headers=READER)
    assert loads == ["404", "1"]


def test_an_allowed_hook_counts_as_passing_before_the_load(make_client):
    """A hook needs the object, so it gets it, for every requester."""
    loads = []

    def load(request: Request, id: str) -> Any:
        loads.append(id)
        return {"id": id}

    app = note_app(load=load, allowed=lambda request, method, obj: False)
    options_204(make_client(app), "/notes/1", "OPTIONS", NOTE_TARGET)
    assert loads == ["1"]


@pytest.mark.parametrize(
    "extra", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}]
)
def test_preflight_runs_nothing(make_client, extra):
    calls: list[str] = []

    def load(request: Request, **params: Any) -> Any:
        calls.append("load")

    def allowed(request: Request, method: str, obj: Any) -> bool:
        calls.append("allowed")
        return True

    app = FastAPI(dependencies=[Depends(counted(calls, "app"))])

    @app.get("/notes/{id}", dependencies=[Security(counted(calls, "route"))])
    @scrudful(**NOTE_LINKS)
    async def read(id: int, user: Any = Security(counted(calls, "security"))):
        calls.append("endpoint")
        return {}

    add_options(app, base_url=BASE, load=load, allowed=allowed)
    client = make_client(app)
    preflight = {
        "Origin": "https://app.example",
        "Access-Control-Request-Method": "PUT",
        **extra,
    }
    for _ in range(3):
        response = client.options("/notes/1", headers=preflight)
        assert response.status_code == 204
        assert response.content == b""
        assert "link" not in response.headers
        assert "content-type" not in response.headers
    assert calls == []
    # Without Access-Control-Request-Method, it is an OPTIONS like any other.
    client.options("/notes/1", headers={"Origin": "https://app.example"})
    assert calls == ["load", "allowed"]


def test_checks_run_only_security_dependencies(make_client):
    """The per-method checks run only the security dependencies:
    those declared with ``Security(...)`` (a scope-less one too) and a plain
    ``Depends`` that relies on a security scheme, wherever they are declared
    (the endpoint's parameters, ``dependencies=`` of the route, a router,
    ``include_router`` and the app). A plain ``Depends`` without a scheme
    below it (a throttle, a write) is never run, even in ``dependencies=``,
    and neither is the endpoint."""
    calls: list[str] = []

    def current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> str:
        calls.append("current_user")
        if credentials is None:
            raise HTTPException(status_code=401)
        return credentials.credentials

    def levels(name: str) -> list[Any]:
        return [
            Depends(counted(calls, f"plain {name}")),
            Security(counted(calls, f"security {name}")),
        ]

    router = APIRouter(dependencies=levels("router"))

    @router.get("/notes/{id}", dependencies=levels("route"))
    @scrudful(**NOTE_LINKS)
    async def read(
        id: int,
        plain: None = Depends(counted(calls, "plain parameter")),
        extra: Any = Security(counted(calls, "security parameter")),
        user: str = Depends(current_user),
    ):
        calls.append("endpoint")
        return {}

    app = FastAPI(dependencies=levels("app"))
    app.include_router(router, dependencies=levels("include"))
    add_options(app, base_url=BASE)
    client = make_client(app)
    security = [f"security {level}" for level in ("app", "include", "router", "route")]
    options_204(client, "/notes/1", "OPTIONS", NOTE_TARGET)
    assert calls == [*security, "security parameter", "current_user"]
    calls.clear()
    token = {"Authorization": "Bearer t"}
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=token)
    assert calls == [*security, "security parameter", "current_user"]
    assert not [call for call in calls if call.startswith("plain")]
    # A GET runs them all.
    calls.clear()
    assert client.get("/notes/1", headers=token).status_code == 200
    assert "plain parameter" in calls and "endpoint" in calls
    assert "plain route" in calls and "plain app" in calls


def test_a_scope_less_security_in_dependencies_is_run(make_client):
    """``Security(fn)`` without scopes, in ``dependencies=``, leaves no trace on
    FastAPI's sub-dependant: it is read off the route's ``dependencies``."""
    calls: list[str] = []

    def deny() -> None:
        calls.append("deny")
        raise HTTPException(status_code=403)

    app = FastAPI()

    @app.get("/notes/{id}", dependencies=[Security(deny)])
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @app.put("/notes/{id}", dependencies=[Depends(deny)])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}

    add_options(app, base_url=BASE)
    # The plain Depends(deny) isn't run: PUT is listed.
    options_204(make_client(app), "/notes/1", "PUT, OPTIONS", NOTE_TARGET)
    assert calls == ["deny"]


def test_a_route_whose_dependencies_dont_match_its_dependant_raises(make_client):
    """scrudful maps ``APIRoute.dependencies`` onto the leading sub-dependants
    of ``APIRoute.dependant``: a FastAPI that doesn't build them so is a
    ``RuntimeError`` naming them."""
    app = note_app()
    for route in app.router.routes:
        if getattr(route, "methods", None) == {"PUT"}:
            route.dependencies = [Security(lambda: None)]
    with pytest.raises(RuntimeError, match=r"APIRoute\.dependencies") as raised:
        make_client(app).options("/notes/1", headers=ADMIN)
    # It names the likelier cause first: dependencies changed after the
    # route was built.
    assert "changed after" in str(raised.value)


def test_dependency_overrides_apply_to_the_checks(make_client):
    reader = require_role("reader")
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int, token: str = Security(reader)):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "OPTIONS", NOTE_TARGET)
    app.dependency_overrides[reader] = lambda: "anyone"
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET)


def body_security_app() -> FastAPI:
    """``/notes/{id}``: PUT, POST and PATCH, each with a security dependency
    that needs the request's body (at some depth), and a GET without one."""

    def needs_body(payload: NotePayload) -> None:
        BODY_CALLS.append("needs_body")
        raise HTTPException(status_code=403)

    def needs_field(name: str = Body(embed=True)) -> None:
        BODY_CALLS.append("needs_field")
        raise HTTPException(status_code=403)

    def wraps_body(inner: None = Depends(needs_body)) -> None:
        BODY_CALLS.append("wraps_body")

    app = FastAPI()

    @app.get("/notes/{id}", dependencies=[Security(require_role("reader"))])
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @app.put("/notes/{id}", dependencies=[Security(needs_body), Security(wraps_body)])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}

    @app.post("/notes/{id}", dependencies=[Security(needs_field)])
    @scrudful(**NOTE_LINKS)
    async def act(id: int):
        return {}

    @app.patch(
        "/notes/{id}",
        dependencies=[Security(needs_body), Security(require_role("writer"))],
    )
    @scrudful(**NOTE_LINKS)
    async def patch(id: int):
        return {}

    return app


#: The calls of :func:`body_security_app`'s security dependencies.
BODY_CALLS: list[str] = []


def test_a_security_dependency_needing_the_body_refuses_its_method(make_client):
    """A security dependency that needs the body can't run on OPTIONS, which
    has none: its method is not allowed, for every requester (fail closed),
    and a ``RuntimeWarning`` names each such route, once, when the routes are
    indexed (here by ``add_options``)."""
    BODY_CALLS.clear()
    app = body_security_app()
    with pytest.warns(RuntimeWarning) as caught:
        add_options(app, base_url=BASE)
    messages = sorted(str(warning.message) for warning in caught)
    assert len(messages) == 3
    for method, endpoint in (("PATCH", "patch"), ("POST", "act"), ("PUT", "write")):
        assert any(
            f"{method} '/notes/{{id}}'" in message and f"{endpoint}" in message
            for message in messages
        ), method
    assert all("allowed=" in message for message in messages)
    client = make_client(app)
    # No warning again (the suite runs with warnings as errors).
    options_204(client, "/notes/1", "OPTIONS", NOTE_TARGET)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=READER)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=ADMIN)
    assert BODY_CALLS == []


def test_a_body_below_the_security_dependency_refuses_its_method(make_client):
    """The body is looked for at any depth: a ``Security(...)`` whose own
    dependency takes an optional body (so would resolve, and allow, without
    one) still can't be checked on OPTIONS. Its method is refused for every
    requester, announced once, and nothing runs."""
    BODY_CALLS.clear()

    def optional_body(payload: NotePayload | None = None) -> None:
        BODY_CALLS.append("optional_body")  # pragma: no cover

    def wraps_optional(inner: None = Depends(optional_body)) -> None:
        BODY_CALLS.append("wraps_optional")  # pragma: no cover

    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @app.put("/notes/{id}", dependencies=[Security(wraps_optional)])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}  # pragma: no cover

    with pytest.warns(RuntimeWarning, match=r"PUT '/notes/\{id\}' \(.*write\).*body"):
        add_options(app, base_url=BASE)
    options_204(
        make_client(app), "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=ADMIN
    )
    assert BODY_CALLS == []


def test_a_depends_with_a_scheme_and_a_body_refuses_its_method(make_client):
    """A plain ``Depends`` is a security dependency when a security scheme is
    below it (a ``get_user`` reading an ``HTTPBearer`` token); one that also
    takes the body (a signed payload) can't be checked on OPTIONS either:
    refused, announced once, and not called."""
    BODY_CALLS.clear()

    def get_user(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
        signature: str = Body(embed=True),
    ) -> str:
        BODY_CALLS.append("get_user")  # pragma: no cover
        return "user"  # pragma: no cover

    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @app.put("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def write(id: int, user: str = Depends(get_user)):
        return {}  # pragma: no cover

    with pytest.warns(RuntimeWarning, match=r"PUT '/notes/\{id\}' \(.*write\).*body"):
        add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=ADMIN)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET)
    assert BODY_CALLS == []


def test_an_allowed_hook_decides_a_method_whose_security_needs_the_body(
    make_client,
):
    """With ``allowed``, no security dependency runs: the hook decides, and
    nothing is announced."""
    BODY_CALLS.clear()
    app = body_security_app()
    add_options(app, base_url=BASE, allowed=lambda request, method, obj: True)
    options_204(make_client(app), "/notes/1", "GET, PUT, POST, OPTIONS, PATCH")
    assert BODY_CALLS == []


def test_a_route_added_later_whose_security_needs_the_body_is_announced(
    make_client,
):
    """A route added after ``add_options`` is announced when the routes are
    indexed again (its first request)."""
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    add_options(app, base_url=BASE)

    def needs_body(payload: NotePayload) -> None:  # pragma: no cover
        raise HTTPException(status_code=403)

    @app.put("/notes/{id}", dependencies=[Security(needs_body)])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}

    client = make_client(app)
    with pytest.warns(RuntimeWarning, match=r"PUT '/notes/\{id\}'"):
        options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET)


def test_security_is_read_annotation_by_annotation(make_client):
    """Under ``from __future__ import annotations``, an annotation that can't
    be evaluated (a name imported only under ``TYPE_CHECKING``) doesn't hide
    the ``Security(...)`` of another parameter of the same function, of the
    endpoint or of a dependency: it is resolved, and refuses whom it refuses.
    Nothing is announced (the suite runs with warnings as errors)."""
    security_annotations.CALLS.clear()
    client = make_client(security_annotations.readable_app())
    assert client.put("/notes/1").status_code == 403
    assert client.post("/notes/1").status_code == 403
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET)
    options_204(
        client,
        "/notes/1",
        "GET, PUT, POST, OPTIONS",
        NOTE_TARGET,
        headers=security_annotations.WRITER,
    )
    # The check alone ran, not the plain dependency beside it.
    assert "get_db" not in security_annotations.CALLS


def test_dependencies_that_are_not_functions_are_read_in_their_globals(
    make_client,
):
    """A callable instance, a ``functools.partial`` and a class dependency,
    under ``from __future__ import annotations``, have their annotations
    evaluated in the globals of their ``__call__``, ``func`` or
    ``__init__``: a plain ``Depends`` in them leaves the method listed for
    anyone, a ``Security(...)`` refuses whom it refuses, and nothing is
    announced (the suite runs with warnings as errors)."""
    try:
        app = security_annotations.callables_app()
    except NameError as exc:
        pytest.skip(f"this FastAPI can't evaluate these annotations: {exc}")
    client = make_client(app)
    assert client.put("/notes/1").status_code == 403
    writer = security_annotations.WRITER
    assert client.put("/notes/1", headers=writer).status_code == 200
    options_204(client, "/notes/1", "GET, DELETE, OPTIONS", NOTE_TARGET)
    options_204(
        client,
        "/notes/1",
        "GET, PUT, POST, DELETE, OPTIONS, PATCH",
        NOTE_TARGET,
        headers=security_annotations.WRITER,
    )


@pytest.mark.parametrize(
    "call",
    [
        security_annotations.MixedChecker(),
        functools.partial(security_annotations.mixed_role, "x"),
        security_annotations.MixedUser,
    ],
    ids=["instance", "partial", "class"],
)
def test_resolved_signature_reads_a_non_function_in_its_globals(call):
    """One annotation that can't be evaluated leaves the others of a callable
    instance, a partial or a class evaluated, in their function's globals."""
    from scrudful._adapter import resolved_signature

    parameters = resolved_signature(call).parameters
    assert typing.get_origin(parameters["user"].annotation) is typing.Annotated
    assert parameters["db"].annotation == "Session"


@pytest.mark.parametrize(
    "name, module, security",
    [
        ("CHECK", annotation_globals_base, "writer"),
        ("User", annotation_globals_base, "writer"),
        ("CHECKER", annotation_globals_base, "writer"),
        ("logged_user", annotation_globals_uses, "editor"),
        ("SIGNED", annotation_globals_uses, "editor"),
    ],
    ids=["bound-method", "class", "callable-instance", "wrapped", "module"],
)
def test_annotation_globals_are_those_of_the_signatures_function(
    name, module, security
):
    """Each annotation is evaluated in the globals of the function the
    signature comes from, in another module than the callable's: a bound
    method's function, a class's inherited ``__init__``, a callable
    instance's inherited ``__call__``, a wrapper's ``__wrapped__``; else
    (``__signature__``, and no Python ``__call__``) the callable's module.
    The module the callable is in (or its ``__module__`` names) can't
    evaluate them."""
    from scrudful._adapter import annotation_globals, resolved_signature

    call = getattr(annotation_globals_uses, name)
    assert annotation_globals(call) is vars(module)
    parameters = resolved_signature(call).parameters
    user = parameters["user"].annotation
    assert typing.get_origin(user) is typing.Annotated
    assert typing.get_args(user)[1].dependency is getattr(module, security)
    assert parameters["db"].annotation == "Session"


def test_a_bound_method_dependency_is_read_in_its_functions_globals(make_client):
    """A bound method whose function is another module's (its
    ``__module__`` naming the endpoint's) is read in that function's globals,
    as FastAPI reads it: its ``Security(...)`` refuses whom it refuses, and
    nothing is announced (the suite runs with warnings as errors)."""
    client = make_client(annotation_globals_uses.bound_method_app())
    assert client.put("/notes/1").status_code == 403
    writer = security_annotations.WRITER
    assert client.put("/notes/1", headers=writer).status_code == 200
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET)
    options_204(client, "/notes/1", "GET, PUT, OPTIONS", NOTE_TARGET, headers=writer)


def test_resolved_signature_leaves_an_attribute_error_annotation_a_string():
    """An annotation whose evaluation raises something other than
    ``NameError`` (``decimal.Nope``: an ``AttributeError``) stays a string,
    and the others are evaluated."""
    from scrudful._adapter import resolved_signature

    parameters = resolved_signature(security_annotations.missing_attribute).parameters
    assert parameters["a"].annotation is int
    assert parameters["b"].annotation == "decimal.Nope"


def test_a_security_annotation_raising_attribute_error_refuses_its_method(
    make_client,
):
    """A dependency's ``Security(...)`` annotation that raises
    ``AttributeError`` when evaluated can't be told either: ``add_options``
    doesn't fail, the method is refused for every requester, and the route
    is announced, once."""
    app = security_annotations.missing_attribute_app()
    try:
        with pytest.warns(RuntimeWarning, match=r"PUT '/notes/\{id\}'.*decimal\.Nope"):
            add_options(app, base_url=BASE)
        options_204(
            make_client(app),
            "/notes/1",
            "GET, OPTIONS",
            NOTE_TARGET,
            headers=security_annotations.WRITER,
        )
    finally:
        security_annotations.lookup.__annotations__["user"] = "str"


def test_an_unchecked_route_warning_raised_as_an_error_warns_again():
    """The route is recorded as announced only once the warning was given: a
    warning raised as an error (``-W error``) leaves it unrecorded, so
    calling ``add_options`` again warns (or fails) again, not silently."""
    app = security_annotations.missing_attribute_app()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            with pytest.raises(RuntimeWarning, match=r"PUT '/notes/\{id\}'"):
                add_options(app, base_url=BASE)
        with pytest.warns(RuntimeWarning, match=r"PUT '/notes/\{id\}'"):
            add_options(app, base_url=BASE)
    finally:
        security_annotations.lookup.__annotations__["user"] = "str"


def test_a_removed_hsr_route_warning_raised_as_an_error_warns_again(make_client):
    """An app's removed HSR route is announced again on the next request when
    the warning was raised as an error (``-W error``): a retry isn't silent,
    and once given, the warning isn't repeated."""
    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {"id": id}

    add_options(app, base_url=BASE)
    client = make_client(app)
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not getattr(route, "path", "").startswith("/_hsr/")
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with pytest.raises(RuntimeWarning, match="was removed from its routes"):
            client.get("/notes/1")
    with pytest.warns(RuntimeWarning, match="was removed from its routes"):
        response = client.get("/notes/1")
    assert response.status_code == 200
    assert HSR_DOCUMENT_REL not in (response.headers.get("link") or "")
    # Given once now (the suite runs with warnings as errors).
    assert client.get("/notes/1").status_code == 200


def unreadable_app() -> FastAPI:
    """:func:`tests.fastapi_security_annotations.unreadable_app`, skipping on
    a FastAPI that refuses the annotation itself, when the route is created
    (pydantic's ``PydanticUndefinedAnnotation``, a ``NameError``): there such
    an endpoint can't be written."""
    try:
        return security_annotations.unreadable_app()
    except NameError as exc:
        pytest.skip(f"this FastAPI refuses an annotation it can't evaluate: {exc}")


def test_a_security_annotation_that_cant_be_evaluated_refuses_its_method(
    make_client,
):
    """A ``Security(...)`` in an annotation that can't be evaluated, of the
    endpoint or of a dependency, can't be told apart from a plain one: its
    method is not allowed, for every requester (fail closed), and a
    ``RuntimeWarning`` names each such route, once."""
    security_annotations.CALLS.clear()
    app = unreadable_app()
    with pytest.warns(RuntimeWarning) as caught:
        add_options(app, base_url=BASE)
    messages = sorted(str(warning.message) for warning in caught)
    assert len(messages) == 2, messages
    for method, endpoint in (("PATCH", "patch"), ("PUT", "write")):
        assert any(
            f"{method} '/notes/{{id}}'" in message
            and endpoint in message
            and "can't be evaluated" in message
            and "allowed=" in message
            for message in messages
        ), (method, messages)
    client = make_client(app)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET)
    options_204(
        client,
        "/notes/1",
        "GET, OPTIONS",
        NOTE_TARGET,
        headers=security_annotations.WRITER,
    )
    assert security_annotations.CALLS == []


def test_an_allowed_hook_decides_a_method_whose_security_cant_be_read(
    make_client,
):
    """With ``allowed``, the hook decides: nothing is announced."""
    app = unreadable_app()
    add_options(app, base_url=BASE, allowed=lambda request, method, obj: True)
    options_204(make_client(app), "/notes/1", "GET, PUT, OPTIONS, PATCH")


def test_a_dependency_whose_signature_cant_be_read_refuses_its_method(
    make_client,
):
    """A dependency whose signature can't be read (here, a bad
    ``__signature__`` set after the route was built) can't be told apart
    from a security one: its method is not allowed, and the route is
    announced, once."""

    def lookup(q: str = "") -> str:
        return q

    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @app.put("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def write(id: int, found: str = Depends(lookup)):
        return {}

    lookup.__signature__ = "not a signature"  # type: ignore[attr-defined]
    with pytest.warns(RuntimeWarning, match=r"PUT '/notes/\{id\}'.*signature"):
        add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=ADMIN)


def test_a_check_s_404_refuses_only_its_method(make_client):
    """A security dependency raising 404 for this requester refuses its
    method; it doesn't answer the OPTIONS (as ninja's ``auth=`` and
    ``before``; DRF answers the OPTIONS with a permission check's 404).
    ``load`` runs only once a method passed."""
    loaded = []

    def hidden(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> None:
        raise HTTPException(status_code=404)

    app = FastAPI()

    @app.get("/notes/{id}", dependencies=[Security(hidden)])
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @app.put("/notes/{id}", dependencies=[Security(require_role("writer"))])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}

    add_options(app, base_url=BASE, load=lambda request, **p: loaded.append(p))
    client = make_client(app)
    options_204(client, "/notes/1", "OPTIONS", NOTE_TARGET)
    assert loaded == []
    options_204(client, "/notes/1", "PUT, OPTIONS", NOTE_TARGET, headers=ADMIN)
    assert loaded == [{"id": "1"}]


def test_a_check_whose_yield_dependency_swallows_its_error_refuses(make_client):
    """A check raising 403 inside a dependency with ``yield`` that catches
    it resolves nothing: the method is not allowed, rather than listed."""
    events = []

    def session() -> Any:
        try:
            yield "session"
        except HTTPException as exc:
            events.append(exc.status_code)

    def deny(
        db: str = Depends(session),
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> None:
        raise HTTPException(status_code=403)

    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @app.put("/notes/{id}", dependencies=[Security(deny)])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}

    add_options(app, base_url=BASE)
    options_204(make_client(app), "/notes/1", "GET, OPTIONS", NOTE_TARGET)
    assert events == [403]


def test_a_method_not_routed_at_the_url_is_left_out(make_client):
    """A method of the resource that no route serves at this URL (its route
    is under a ``Host`` the request isn't for) isn't listed, and isn't
    checked; for the Host's requests it is."""
    from fastapi.routing import APIRoute

    async def write(id: int):
        return {}

    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    app.router.routes.append(
        Host(
            "admin.example",
            app=Router(
                routes=[
                    APIRoute(
                        "/notes/{id}",
                        scrudful(**NOTE_LINKS)(write),
                        methods=["PUT"],
                        dependencies=[Security(require_role("writer"))],
                    )
                ]
            ),
        )
    )
    add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=ADMIN)
    admin = make_client(app, base_url="http://admin.example")
    options_204(admin, "/notes/1", "GET, OPTIONS", NOTE_TARGET)
    options_204(admin, "/notes/1", "GET, PUT, OPTIONS", NOTE_TARGET, headers=ADMIN)


def test_a_rebuilt_hsr_is_served_with_its_new_bytes_and_etag(make_client):
    """The HSR route serves the document built now: after a route is added to
    the path, its new bytes, with their ETag, and the old ETag no longer
    matches (a 200, not a 304)."""

    def etag_of(body: bytes) -> str:
        return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'

    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    first = client.get("/_hsr/notes.id")
    assert list(first.json()) == ["get", "options"]
    assert first.headers["etag"] == etag_of(first.content)
    stale = {"If-None-Match": first.headers["etag"]}
    assert client.get("/_hsr/notes.id", headers=stale).status_code == 304

    @app.put("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}

    second = client.get("/_hsr/notes.id", headers=stale)
    assert second.status_code == 200
    assert list(second.json()) == ["get", "put", "options"]
    assert second.headers["etag"] == etag_of(second.content)
    assert second.headers["etag"] != first.headers["etag"]


def test_a_yield_dependency_is_cleaned_up(make_client):
    events = []

    async def session(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> Any:
        events.append("enter")
        try:
            if credentials is None:
                raise HTTPException(status_code=401)
            yield credentials.credentials
        finally:
            events.append("exit")

    def sync_session() -> Any:
        events.append("sync enter")
        try:
            yield
        finally:
            events.append("sync exit")

    app = FastAPI()

    @app.get("/notes/{id}", dependencies=[Security(sync_session)])
    @scrudful(**NOTE_LINKS)
    async def read(id: int, token: str = Security(session)):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "OPTIONS", NOTE_TARGET)
    assert events == ["sync enter", "enter", "exit", "sync exit"]
    events.clear()
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=READER)
    assert events == ["sync enter", "enter", "exit", "sync exit"]


def test_a_check_failing_validation_is_not_allowed(make_client):
    """A security dependency whose own parameters fail validation (a missing
    header) refuses its method, as FastAPI's 422 would."""

    def api_key(x_api_key: str = Header()) -> str:
        return x_api_key

    app = FastAPI()

    @app.get("/notes/{id}", dependencies=[Security(api_key)])
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "OPTIONS", NOTE_TARGET)
    key = {"X-API-Key": "k"}
    options_204(client, "/notes/1", "GET, OPTIONS", NOTE_TARGET, headers=key)


def test_a_check_gets_the_method_and_the_path_parameters(make_client):
    seen = []

    def owner_only(request: Request, id: int) -> None:
        seen.append((request.method, id, dict(request.path_params)))
        if request.method != "GET" and id != 1:
            raise HTTPException(status_code=403)

    app = FastAPI()

    @app.api_route(
        "/notes/{id}", methods=["GET", "DELETE"], dependencies=[Security(owner_only)]
    )
    @scrudful(**NOTE_LINKS)
    async def note(id: int):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "GET, DELETE, OPTIONS", NOTE_TARGET)
    options_204(client, "/notes/2", "GET, OPTIONS", NOTE_TARGET)
    assert seen[0] == ("GET", 1, {"id": "1"})
    assert [method for method, _, _ in seen] == ["GET", "DELETE", "GET", "DELETE"]


def test_another_http_exception_from_a_check_is_answered(make_client):
    """Only 401, 403 and 404 mean "not allowed": another status a security
    check raises (a token check's 429 for a client over its quota) is the
    app's exception handlers' answer, with the HSR link."""

    def rate_limited_token(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> None:
        raise HTTPException(status_code=429, detail="Slow down")

    app = FastAPI()

    @app.get("/notes/{id}", dependencies=[Depends(rate_limited_token)])
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    add_error_links(app, APP_ERROR)
    add_options(app, base_url=BASE)
    response = make_client(app).options("/notes/1")
    assert response.status_code == 429
    assert hsr_link_problems(dict(response.headers), NOTE_TARGET) == []
    assert link_problems("OPTIONS", 429, dict(response.headers), True) == []


def test_options_consumes_no_throttle_quota(make_client):
    """A throttle in ``dependencies=`` (no security scheme below it) isn't run
    by the checks: OPTIONS consumes none of its quota."""
    quota = {"left": 1}

    def throttle() -> None:
        if quota["left"] == 0:
            raise HTTPException(status_code=429)
        quota["left"] -= 1

    app = FastAPI()

    @app.api_route(
        "/notes/{id}",
        methods=["GET", "PUT", "DELETE"],
        dependencies=[Depends(throttle)],
    )
    @scrudful(**NOTE_LINKS)
    async def note(id: int):
        return {}

    add_options(app, base_url=BASE)
    client = make_client(app)
    for _ in range(3):
        options_204(client, "/notes/1", "GET, PUT, DELETE, OPTIONS", NOTE_TARGET)
    assert quota == {"left": 1}
    assert client.get("/notes/1").status_code == 200
    assert client.get("/notes/1").status_code == 429


def test_another_exception_from_a_check_propagates(make_client):
    def broken() -> None:
        raise RuntimeError("broken check")

    app = FastAPI()

    @app.get("/notes/{id}", dependencies=[Security(broken)])
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    add_options(app, base_url=BASE)
    with pytest.raises(RuntimeError, match="broken check"):
        make_client(app).options("/notes/1")


def test_solve_dependencies_is_version_guarded(make_client, monkeypatch):
    """scrudful relies on FastAPI's ``solve_dependencies``: one it can't call
    as it expects is a ``RuntimeError`` naming it, raised by ``add_options``
    (at startup, not by the first OPTIONS) when a route has security
    dependencies to check, and by OPTIONS for one added later."""
    from fastapi.dependencies import utils

    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    add_options(app, base_url=BASE)

    async def solve_dependencies(*, request, dependant, other):  # pragma: no cover
        raise AssertionError

    monkeypatch.setattr(utils, "solve_dependencies", solve_dependencies)
    with pytest.raises(RuntimeError, match=r"fastapi\.dependencies\.utils\.solve_dep"):
        note_app()
    # Nothing to check: neither needs it.
    options_204(make_client(make_app()), "/users", "POST, OPTIONS")
    note_app(allowed=lambda request, method, obj: True)

    @app.put("/notes/{id}", dependencies=[Security(require_role("writer"))])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}

    with pytest.raises(RuntimeError, match=r"fastapi\.dependencies\.utils\.solve_dep"):
        make_client(app).options("/notes/1")


def test_the_solver_guard_costs_options_nothing(make_client):
    """The guard's inspection of ``solve_dependencies`` is done once per
    function, not on each OPTIONS."""
    from scrudful import fastapi as module

    client = make_client(note_app())
    options_204(client, "/notes/1", "GET, PUT, OPTIONS, HEAD", headers=ADMIN)
    misses = module._checked_solver.cache_info().misses
    for _ in range(3):
        options_204(client, "/notes/1", "GET, PUT, OPTIONS, HEAD", headers=ADMIN)
    assert module._checked_solver.cache_info().misses == misses


def test_dependencies_changed_before_add_options_raise_there():
    """The routes' dependencies are read when ``add_options`` is called."""
    app = FastAPI()

    @app.put("/notes/{id}", dependencies=[Security(require_role("writer"))])
    @scrudful(**NOTE_LINKS)
    async def write(id: int):
        return {}

    for route in app.router.routes:
        if getattr(route, "methods", None) == {"PUT"}:
            route.dependencies = [Security(lambda: None)]
    with pytest.raises(RuntimeError, match=r"APIRoute\.dependencies"):
        add_options(app, base_url=BASE)
    # Left as it was: no add_options applies.
    assert not any(
        type(route).__name__ == "_OptionsRoute" for route in app.router.routes
    )


def test_allow_covers_every_route_of_the_resource(make_client):
    """Plain routes of a scrudful path are listed too, with their own checks;
    a Starlette route has none."""

    async def starlette_patch(request: Request) -> Any:
        return JSONResponse({})

    app = FastAPI()

    @app.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @app.put("/notes/{note_id}", dependencies=[Security(require_role("writer"))])
    async def plain_put(note_id: int):
        return {}

    app.add_route("/notes/{id}", starlette_patch, methods=["PATCH"])
    add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "GET, OPTIONS, PATCH", NOTE_TARGET)
    options_204(
        client, "/notes/1", "GET, PUT, OPTIONS, PATCH", NOTE_TARGET, headers=ADMIN
    )


def test_router_mode_options(make_client):
    """``add_options`` on a router: the checks run the dependencies
    ``include_router`` adds, ``load`` gets the prefix's parameters, and the
    HSR link is the router's route under the prefix."""
    loaded = []

    def load(request: Request, **params: Any) -> Any:
        loaded.append(params)
        return params

    router = APIRouter()

    @router.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    @router.put("/notes/{note_id}", dependencies=[Security(require_role("writer"))])
    @scrudful(**NOTE_LINKS)
    async def write(note_id: int):
        return {}

    add_options(router, base_url=BASE, load=load)
    app = FastAPI()
    app.include_router(
        router, prefix="/orgs/{org}", dependencies=[Security(require_role("reader"))]
    )
    client = make_client(app)
    target = "https://api.example/orgs/acme/_hsr/notes.id"
    options_204(client, "/orgs/acme/notes/1", "OPTIONS", target)
    assert loaded == []
    options_204(client, "/orgs/acme/notes/1", "GET, OPTIONS", target, headers=READER)
    options_204(
        client, "/orgs/acme/notes/1", "GET, PUT, OPTIONS", target, headers=ADMIN
    )
    assert loaded == [{"org": "acme", "id": "1"}] * 2
    assert options_hsr(client, "/orgs/acme/notes/1", headers=ADMIN) == (
        http_schema_resource(router, "/notes/{id}")
    )


#: Each level's ``include_router`` prefix: none, literals, parameters.
NESTED_PREFIXES = [
    ("", "", ""),
    ("/v1", "/m", "/l"),
    ("/t/{tenant}", "/orgs/{org}", "/teams/{team}"),
]
#: The value of each prefix parameter in the URLs.
PREFIX_VALUES = {"tenant": "acme", "org": "o1", "team": "red"}


@pytest.mark.parametrize("app_prefix, middle_prefix, leaf_prefix", NESTED_PREFIXES)
def test_router_mode_nested_routers_answer_options(
    make_client, app_prefix, middle_prefix, leaf_prefix
):
    """Every resource reachable through nested ``include_router`` calls,
    three levels deep, answers OPTIONS, links to an HSR its router serves
    and has its include's dependencies checked; ``load`` gets every
    prefix's parameters."""
    loaded = []

    def load(request: Request, **params: Any) -> Any:
        loaded.append(params)
        return params

    leaf, middle, parent = APIRouter(), APIRouter(), APIRouter()

    @leaf.get("/items/{id}")
    @scrudful(**NOTE_LINKS)
    async def read_item(id: int):
        return {}

    @leaf.put("/items/{id}", dependencies=[Security(require_role("writer"))])
    @scrudful(**NOTE_LINKS)
    async def write_item(id: int):
        return {}

    @middle.get("/things")
    @scrudful(**NOTE_LINKS)
    async def things():
        return []

    @parent.get("/top")
    @scrudful(**NOTE_LINKS)
    async def top():
        return {}

    middle.include_router(
        leaf, prefix=leaf_prefix, dependencies=[Security(require_role("reader"))]
    )
    parent.include_router(middle, prefix=middle_prefix)
    add_options(parent, base_url=BASE, load=load)
    app = FastAPI()
    app.include_router(parent, prefix=app_prefix)
    client = make_client(app)

    def check(url: str, template: str, allow: str, **kwargs: Any) -> None:
        url = url.format(**PREFIX_VALUES)
        response = client.get(url, **kwargs)
        assert response.status_code == 200, url
        (target,) = [
            target
            for target, params in parse_links(response.headers["link"])
            if params.get("rel") == HSR_DOCUMENT_REL
        ]
        options_204(client, url, allow, target, **kwargs)
        assert client.get(target).json() == http_schema_resource(parent, template)

    item = f"{app_prefix}{middle_prefix}{leaf_prefix}/items/1"
    item_template = f"{middle_prefix}{leaf_prefix}/items/{{id}}"
    options_204(client, item.format(**PREFIX_VALUES), "OPTIONS")
    check(item, item_template, "GET, OPTIONS", headers=READER)
    check(item, item_template, "GET, PUT, OPTIONS", headers=ADMIN)
    prefixes = app_prefix + middle_prefix + leaf_prefix
    expected = {
        name: value
        for name, value in PREFIX_VALUES.items()
        if "{" + name + "}" in prefixes
    }
    assert loaded == [{**expected, "id": "1"}] * 2
    things_template = f"{middle_prefix}/things"
    check(f"{app_prefix}{things_template}", things_template, "GET, OPTIONS")
    check(f"{app_prefix}/top", "/top", "GET, OPTIONS")


def test_router_mode_nested_route_serving_options_is_kept(make_client):
    """A nested router's own OPTIONS route keeps answering, and calling
    ``add_options`` again adds no second OPTIONS route."""
    leaf, parent = APIRouter(), APIRouter()

    @leaf.get("/items/{id}")
    @scrudful(**NOTE_LINKS)
    async def read_item(id: int):
        return {}

    @leaf.options("/items/{id}")
    async def own_options(id: int):
        return Response(status_code=200, headers={"X-Own": "yes"})

    @leaf.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read_note(id: int):
        return {}

    parent.include_router(leaf, prefix="/l")
    add_options(parent, base_url=BASE)
    add_options(parent, base_url=BASE)
    automatic = [
        route
        for route in parent.routes
        if getattr(getattr(route, "endpoint", None), "_scrudful_options", False)
    ]
    assert [route.path for route in automatic] == ["/l/notes/{id}"]
    app = FastAPI()
    app.include_router(parent)
    client = make_client(app)
    assert client.options("/l/items/1").headers["x-own"] == "yes"
    target = "https://api.example/_hsr/l.notes.id"
    options_204(client, "/l/notes/1", "GET, OPTIONS", target)


def test_router_mode_load_error_links_resolve_against_the_router_base(make_client):
    def load(request: Request, **params: Any) -> Any:
        raise HTTPException(status_code=404)

    router = APIRouter()

    @router.get("/notes/{id}")
    @scrudful(**NOTE_LINKS)
    async def read(id: int):
        return {}

    add_options(router, base_url="https://router.example/", load=load)
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    add_error_links(app, APP_ERROR)
    response = make_client(app).options("/v1/notes/1")
    assert response.status_code == 404
    headers = dict(response.headers)
    assert parse_links(headers["link"])[0][0] == (
        "https://router.example/schemas/problem.json"
    )
    target = "https://router.example/v1/_hsr/notes.id"
    assert hsr_link_problems(headers, target) == []


def test_options_under_a_mount(make_client):
    """App mode, a resource under the app's own Mount: the checks and
    ``load`` get the scope the route gets there."""
    from fastapi.routing import APIRoute

    async def note(id: int):
        return {}

    endpoint = scrudful(**NOTE_LINKS)(note)
    loaded = []
    app = FastAPI()
    app.router.routes.append(
        Mount(
            "/m/{tenant}",
            routes=[
                APIRoute(
                    "/notes/{id}",
                    endpoint,
                    methods=["GET"],
                    dependencies=[Security(require_role("reader"))],
                )
            ],
        )
    )
    add_options(app, base_url=BASE, load=lambda request, **p: loaded.append(p))
    client = make_client(app)
    target = "https://api.example/_hsr/m.tenant.notes.id"
    options_204(client, "/m/acme/notes/1", "OPTIONS", target)
    options_204(client, "/m/acme/notes/1", "GET, OPTIONS", target, headers=READER)
    assert loaded == [{"tenant": "acme", "id": "1"}]


def _driven(flow, results):
    """Run the ``_Flow`` ``flow``, answering each callable it yields with
    ``results[callable]``; its result, and the callables it yielded."""
    called = []
    try:
        func, _ = next(flow)
        while True:
            called.append(func)
            func, _ = flow.send(results[func])
    except StopIteration as stop:
        return stop.value, called


def test_a_build_keeps_a_callables_value_with_its_source():
    """The link callables called for an HTTP Schema Resource are kept by
    ``id`` of their source, with the source itself: a source whose ``id`` an
    earlier, gone one had gets its own callable called, not the earlier
    one's value."""
    from scrudful.fastapi import _field

    def first_link():
        return "https://x/first"

    def second_link():
        return "https://x/second"

    first = Entity(first_link, context_link_or_func="https://x/c")
    second = Entity(second_link, context_link_or_func="https://x/c")
    results = {first_link: "https://x/first", second_link: "https://x/second"}
    memo = {}
    name = "schema_link_or_func"
    assert _driven(_field(first, name, "w", memo), results) == (
        "https://x/first",
        [first_link],
    )
    # Kept: called once per build.
    assert _driven(_field(first, name, "w", memo), results) == (
        "https://x/first",
        [],
    )
    # As if ``first`` were gone and ``second`` had its id.
    memo[(id(second), name)] = memo.pop((id(first), name))
    assert _driven(_field(second, name, "w", memo), results) == (
        "https://x/second",
        [second_link],
    )
