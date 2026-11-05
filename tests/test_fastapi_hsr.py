"""The FastAPI HTTP Schema Resource: built per path template, without a request."""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
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
    hsr_get_problems,
    link_problems,
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
    anonymous = make_client(make_app()).options(
        "/users/7", headers={"Host": "a.example"}
    )
    authenticated = make_client(make_app(), base_url="http://b.example").options(
        "/users/7", headers={"Authorization": "Bearer alice"}
    )
    assert anonymous.status_code == authenticated.status_code == 200
    built = http_schema_resource(make_app(), "/users/{id}")
    assert anonymous.json() == authenticated.json() == built
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
    assert make_client(app).options("/items/1").json() == second
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
    # /items/{id} and /items/{id:int} would both have the route items.id.
    add_options(app, base_url=BASE, hsr_route=False, hsr_url=str)
    hsr = http_schema_resource(app, "/items/{id}")
    assert list(hsr) == ["get", "put", "options", "patch"]
    assert http_schema_resource(app, "/items/{item_id}") == hsr
    assert http_schema_resource(app, "/items/{y:str}") == hsr
    assert list(http_schema_resource(app, "/items/{n:int}")) == ["delete", "options"]
    client = make_client(app)
    response = client.options("/items/abc")
    assert response.json() == hsr
    assert response.headers["allow"] == "GET, PUT, OPTIONS, PATCH"
    assert client.delete("/items/abc").status_code == 405  # not this resource's


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
    response = client.options("/items/abc")
    assert response.headers["allow"] == "GET, OPTIONS"
    assert client.put("/items/abc").status_code == 405
    assert client.options("/items/5").headers["allow"] == "PUT, OPTIONS"


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
    assert client.options("/items/new").json() == http_schema_resource(
        app, "/items/new"
    )
    assert client.options("/items/5").json() == http_schema_resource(app, "/items/{id}")


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
    assert client.options("/about/us").json() == hsr
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
    assert client.options("/n/z").json() == hsr
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
        assert client.options(url).json() == hsr


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
    assert client.options("/v1/items/1").json() == hsr
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
    with pytest.raises(ValueError, match="set a base URL"):
        make_client(app).options("/users/1")


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
        make_client(app).options("/items/1")


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
    # Built in the event loop, by the OPTIONS answer.
    assert make_client(make()).options("/items/1").json() == built
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
    client = make_client(make_app())
    for url in ("/_hsr/people.id", "/_hsr/users.name", "/_hsr/_root"):
        response = client.get(url)
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"detail": "Not Found"}


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
    assert type(route) is Route and not isinstance(route, APIRoute)
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


def test_hsr_route_name_leaves_out_convertors(make_client):
    app = FastAPI()

    @app.get("/items/{id:int}/{rest:path}")
    @scrudful(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")
    async def get_item(id: int, rest: str):
        return {}

    add_options(app, base_url=BASE)
    response = make_client(app).get("/_hsr/items.id.rest")
    assert response.json() == http_schema_resource(app, "/items/{n:int}/{p:path}")


def test_hsr_route_name_clash(make_client):
    """``/items/{id}`` and ``/items/{id:int}`` are two resources with one
    route name, ``items.id``."""
    links = dict(schema_link_or_func="/s.json", context_link_or_func="/c.jsonld")

    def clashing() -> FastAPI:
        app = FastAPI()

        @app.get("/items/{id}")
        @scrudful(**links)
        async def by_name(id: str):
            return {}

        @app.get("/items/{id:int}")
        @scrudful(**links)
        async def by_number(id: int):
            return {}

        return app

    clash = r"'/items/\{id\}' and '/items/\{id:int\}'.*'items\.id'"
    with pytest.raises(ValueError, match=clash):
        add_options(clashing(), base_url=BASE)
    # With the route off there is no route name to clash.
    add_options(clashing(), base_url=BASE, hsr_route=False, hsr_url=str)
    # Added after add_options: the first request raises.
    app = FastAPI()

    @app.get("/items/{id}")
    @scrudful(**links)
    async def by_name(id: str):
        return {}

    add_options(app, base_url=BASE)

    @app.get("/items/{id:int}")
    @scrudful(**links)
    async def by_number(id: int):
        return {}

    with pytest.raises(ValueError, match=clash):
        make_client(app).get("/_hsr/items.id")


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
    """``make_app`` with ``/users/{user_id}/target`` answering
    ``_hsr_target(request, ...)`` for ``/users/{id}``, by key and by template."""
    app = make_app(**options)

    @app.get("/users/{user_id}/target")
    async def target(user_id: int, request: Request):
        return {
            "key": _hsr_target(request, "/users/{}"),
            "template": _hsr_target(request, "/users/{user_id}"),
        }

    return app


def test_target_is_the_route_url_against_the_base_url(make_client):
    client = make_client(target_app(), base_url="http://elsewhere.example")
    assert client.get("/users/1/target").json() == {
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
    assert make_client(app).get("/users/1/target").json() == {
        "key": "https://docs.example/hsr/users/{id}",
        "template": "https://docs.example/hsr/users/{id}",
    }
    app = target_app(hsr_route=False, hsr_url=lambda p: f"https://docs.example/off{p}")
    assert make_client(app).get("/users/1/target").json()["key"] == (
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


def test_target_of_an_unknown_path_raises(make_client):
    app = target_app()

    @app.get("/elsewhere")
    async def elsewhere(request: Request):
        return {"target": _hsr_target(request, "/people/{id}")}

    with pytest.raises(ValueError, match=r"'/people/\{id\}'"):
        make_client(app).get("/elsewhere")
