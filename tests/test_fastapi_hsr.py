"""The FastAPI HTTP Schema Resource: built per path template, without a request."""

from __future__ import annotations

import copy
import re
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
from starlette.routing import Mount, Route, Router

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
    doesn't walk every route of the app (the final review's F1)."""
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

    app.include_router(APIRouter())  # a copying FastAPI needs a new include
    if module._iter_route_contexts is None:
        app.include_router(router, prefix="/v2")
        url, name = "/v2/more/1", "v2.more.id"
    else:
        url, name = "/v1/more/1", "v1.more.id"
    response = client.get(url)
    assert hsr_link_problems(response.headers, f"{BASE}_hsr/{name}") == []
    assert client.get(f"/_hsr/{name}").status_code == 200


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
    other URLs, so it is another resource (R20a)."""
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
    # Each resource has its own HSR route (R22).
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


# --- base URL ----------------------------------------------------------------------


def test_relative_link_without_base_url_raises(make_client):
    app = make_app(base_url=None)
    with pytest.raises(ValueError, match="relative URL '/schemas/user.json'") as raised:
        http_schema_resource(app, "/users/{id}")
    assert type(raised.value) is ValueError
    assert isinstance(raised.value.__cause__, HsrConfigError)
    assert str(raised.value) == str(raised.value.__cause__)
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
    with pytest.raises(TypeError, match=name):
        add_options(FastAPI(), **{name: value})


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
    assert config.entity is None
    assert config.media_type == "application/json"

    @scrudful(entity=CSV)
    async def csv_endpoint():
        return {}

    config = csv_endpoint._scrudful
    assert config.entity is CSV
    assert config.media_type == "text/csv"
    assert config.schema_link_or_func == CSV.schema_link_or_func
    assert config.context_type_or_func == CSV.context_type_or_func


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
    # Only the entity's media type is described (R21): a declared PNG or CSV
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
    """Each convertor but ``str`` is joined to its parameter's name (R22)."""
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
    ``/items/{id:int}`` doesn't clash (R22)."""
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

    @router.get("/things/{id}")
    @scrudful(**links)
    async def get_thing(id: int, request: Request):
        return {"target": _hsr_target(request, "/things/{id}", owner=router)}

    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    assert client.get("/_hsr/things.id").status_code == 404
    with pytest.raises(ValueError, match="no HTTP Schema Resource route"):
        client.get("/things/1")
    add_options(router, base_url=BASE)
    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    assert client.get("/things/1").json()["target"] == (
        "https://api.example/_hsr/things.id"
    )
    assert client.get("/_hsr/things.id").status_code == 200


def test_router_resource_added_later_is_undescribed_until_add_options(make_client):
    """A router resource added after :func:`add_options` has neither an HSR
    route nor an OPTIONS route: it isn't described, so its responses carry no
    HSR link (rather than each failing), until add_options is called again."""
    router = APIRouter()
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    @router.get("/items/{id}")
    @scrudful(**links)
    async def get_item(id: int):
        return {}

    add_options(router, base_url=BASE)

    @router.get("/things/{id}")
    @scrudful(etag_func=lambda **kwargs: "v1", **links, error=APP_ERROR)
    async def get_thing(id: int):
        if id == 404:
            raise HTTPException(404)
        return {}

    def client_of() -> TestClient:
        app = FastAPI()
        app.include_router(router)
        add_error_links(app, APP_ERROR)
        return make_client(app)

    client = client_of()
    for url, headers, status in [
        ("/things/1", {}, 200),
        ("/things/1", {"If-None-Match": '"v1"'}, 304),
        ("/things/1", {"If-Match": '"no"'}, 412),
        ("/things/404", {}, 404),
    ]:
        response = client.get(url, headers=headers)
        assert response.status_code == status
        assert HSR_DOCUMENT_REL not in response.headers.get("link", "")
    assert client.options("/things/1").status_code == 405
    assert (
        hsr_link_problems(
            client.get("/items/1").headers, "https://api.example/_hsr/items.id"
        )
        == []
    )
    add_options(router, base_url=BASE)
    client = client_of()
    assert (
        hsr_link_problems(
            client.get("/things/1").headers, "https://api.example/_hsr/things.id"
        )
        == []
    )


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
    only for a route that does (R9)."""
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
    # A path without parameters has no object: load isn't called (R26).
    loaded.clear()
    seen.clear()
    options_204(client, "/notes", "OPTIONS", "https://api.example/_hsr/notes")
    assert loaded == []
    assert seen == [("POST", "POST", None)]


def test_load_is_not_called_without_path_parameters(make_client):
    """A ``load(request, id)`` that can't take a collection's (no)
    parameters: the collection's OPTIONS is still a 204 (R26)."""
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
    answer, with the error entity's Links and the HSR link (R11)."""

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


def test_allowed_hook_http_exception_is_answered(make_client):
    def allowed(request: Request, method: str, obj: Any) -> bool:
        raise HTTPException(status_code=403, detail="No")

    response = make_client(note_app(allowed=allowed, error=APP_ERROR)).options(
        "/notes/1"
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "No"}
    assert hsr_link_problems(dict(response.headers), NOTE_TARGET) == []
    assert link_problems("OPTIONS", 403, dict(response.headers), True) == []


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
    loaded (R17)."""
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
    """A hook needs the object, so it gets it, for every requester (R17a)."""
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
    """The per-method checks run only the security dependencies (R25):
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


def test_body_reading_dependency_is_skipped_and_method_allowed(make_client):
    """A security dependency that needs the body can't run without it: it is skipped,
    and its method allowed (exact object-level ``Allow`` needs
    ``allowed=``). The method's other dependencies still run."""
    calls = []

    def needs_body(payload: NotePayload) -> None:
        calls.append("needs_body")
        raise HTTPException(status_code=403)

    def needs_field(name: str = Body(embed=True)) -> None:
        calls.append("needs_field")
        raise HTTPException(status_code=403)

    def wraps_body(inner: None = Depends(needs_body)) -> None:
        calls.append("wraps_body")

    app = FastAPI()

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

    add_options(app, base_url=BASE)
    client = make_client(app)
    options_204(client, "/notes/1", "PUT, POST, OPTIONS", NOTE_TARGET)
    options_204(
        client, "/notes/1", "PUT, POST, OPTIONS, PATCH", NOTE_TARGET, headers=ADMIN
    )
    assert calls == []


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
    by the checks: OPTIONS consumes none of its quota (R25)."""
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
    as it expects is a ``RuntimeError`` naming it."""
    from fastapi.dependencies import utils

    async def solve_dependencies(*, request, dependant, other):  # pragma: no cover
        raise AssertionError

    monkeypatch.setattr(utils, "solve_dependencies", solve_dependencies)
    with pytest.raises(RuntimeError, match=r"fastapi\.dependencies\.utils\.solve_dep"):
        make_client(note_app()).options("/notes/1")
    # A method without dependencies to check needs none of it.
    options_204(make_client(make_app()), "/users", "POST, OPTIONS")


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
