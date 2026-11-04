"""The HTTP Schema Resource of a ``ScrudfulRouter`` path, built without a request."""

import json
import re
import types
import uuid
from typing import Any, List

import ninja.operation
import ninja.params.models
import ninja.router
import ninja.testing.client
import pytest
from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import Http404
from django.test import Client
from ninja import NinjaAPI, Router
from ninja.errors import AuthenticationError, ConfigError, HttpError
from ninja.renderers import JSONRenderer
from ninja.security import APIKeyHeader, django_auth
from ninja.throttling import AnonRateThrottle
from django.urls import path
from ninja.testing import TestAsyncClient, TestClient

from scrudful._django import scrudful_setting
from scrudful._hsr import hsr_etag, serialize_hsr
from scrudful.ninja import (
    Entity,
    ScrudfulRouter,
    _hsr_target,
    add_error_links,
    described,
    http_schema_resource,
    http_schema_resource_view,
    scrudful,
    scrudful_create,
    scrudful_list,
)
from tests.semantic_http import (
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    validate_http_schema_resource,
)
from tests.test_ninja_adapter import ItemIn, ItemOut, Store, etag, modified, update

BASE_URL = "https://api.example/"
ALICE = "alice"
BOB = "bob"
BOB_KEY = {"X-API-Key": BOB}
LINKS = dict(schema_link_or_func="/schemas/item", context_link_or_func="/contexts/item")
OP_ERROR = Entity(
    schema_link_or_func="https://api.example/schemas/op-error",
    context_link_or_func="https://api.example/contexts/op-error",
)
API_ERROR = Entity(
    schema_link_or_func="https://api.example/schemas/api-error",
    context_link_or_func="https://api.example/contexts/api-error",
)

#: ``make_api`` keys for ``ScrudfulRouter(...)`` and for ``NinjaAPI(...)``
#: (Ruling R2); every other key goes to the ``@scrudful`` decorators.
ROUTER_KEYS = frozenset(
    {"base_url", "hsr_route", "hsr_url", "hsr_cache_control", "allowed"}
)
API_KEYS = frozenset({"auth", "throttle"})


def split_options(
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``config`` as the router's, the API's and the operations' options."""
    router = {key: config.pop(key) for key in ROUTER_KEYS & config.keys()}
    router.setdefault("base_url", BASE_URL)
    api = {key: config.pop(key) for key in API_KEYS & config.keys()}
    store = Store()
    item = {
        "load": store.load,
        "etag_func": etag,
        "last_modified_func": modified,
        **LINKS,
        **config,
    }
    return router, api, item


def mount(router: Router, **api_options: Any) -> NinjaAPI:
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}", **api_options)
    api.add_router("", router)
    return api


def make_api(**config: Any) -> tuple[NinjaAPI, ScrudfulRouter]:
    """A ``ScrudfulRouter`` with a ``@scrudful`` GET and PUT on ``"/{pk}"``."""
    router_options, api_options, item = split_options(config)
    router = ScrudfulRouter(**router_options)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    @router.put("/{pk}", response=ItemOut)
    @scrudful(**item)
    def put_item(request, pk: int, payload: ItemIn, obj):
        update(obj, payload.name)
        return obj

    return mount(router, **api_options), router


def options(client: TestClient, path: str, **kwargs: Any) -> Any:
    return client.request("OPTIONS", path, **kwargs)


def entity_content(entity: Entity) -> dict[str, Any]:
    """The ``content`` of a JSON ``entity`` with absolute links."""
    return {
        "application/json": {
            "schema": {"$ref": entity.schema_link_or_func},
            "x-rdf-context": {
                "href": entity.context_link_or_func,
                "type": "application/ld+json",
            },
        }
    }


class KeyAuth(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request, key):
        return key if key in (ALICE, BOB) else None


# --- requester independence ----------------------------------------------------

#: Two APIs of identical routers, so that each request builds its router's
#: HTTP Schema Resource (it is built once, by the first request): one
#: answers an anonymous requester on one host, the other an authenticated
#: one on another.
HOST_A_API, HOST_A_ROUTER = make_api(auth=KeyAuth())
HOST_B_API, HOST_B_ROUTER = make_api(auth=KeyAuth())

# For Django's test client: real hosts, which ninja's TestClient ignores.
urlpatterns = [path("a/", HOST_A_API.urls), path("b/", HOST_B_API.urls)]


@pytest.mark.urls(__name__)
def test_hsr_is_identical_across_hosts_and_requesters(client, settings, db):
    settings.ALLOWED_HOSTS = ["a.example", "b.example"]
    assert client.get("/a/1", HTTP_HOST="a.example").status_code == 401
    assert (
        client.get("/b/1", HTTP_HOST="b.example", HTTP_X_API_KEY=ALICE).status_code
        == 200
    )
    # Each request builds its router's document (the first to need it).
    a = client.get("/a/_hsr/pk", HTTP_HOST="a.example")
    b = client.get("/b/_hsr/pk", HTTP_HOST="b.example", HTTP_X_API_KEY=ALICE)
    assert a.status_code == b.status_code == 200
    assert a.content == b.content
    assert a["ETag"] == b["ETag"]
    assert json.loads(a.content) == http_schema_resource(HOST_A_ROUTER, "/{pk}")
    # OPTIONS: the Allow is the requester's; the HSR link isn't the host's.
    a = client.options("/a/1", HTTP_HOST="a.example")
    b = client.options("/b/1", HTTP_HOST="b.example", HTTP_X_API_KEY=ALICE)
    everything = {"GET", "HEAD", "PUT", "OPTIONS"}
    assert (
        options_204_problems(a.status_code, dict(a.items()), a.content, {"OPTIONS"})
        == []
    )
    assert (
        options_204_problems(b.status_code, dict(b.items()), b.content, everything)
        == []
    )
    assert hsr_link_problems(dict(a.items()), "https://api.example/a/_hsr/pk") == []
    assert hsr_link_problems(dict(b.items()), "https://api.example/b/_hsr/pk") == []
    assert (
        http_schema_resource(HOST_A_ROUTER, "/{pk}")["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        == "https://api.example/schemas/item"
    )


def test_options_has_no_body_and_the_route_serves_the_built_hsr():
    api, router = make_api()
    client = TestClient(api)
    assert options(client, "/1").content == b""
    response = client.get("/_hsr/pk")
    assert json.loads(response.content) == http_schema_resource(router, "/{pk}")


def test_relative_link_without_base_url_is_config_error():
    _, router = make_api(base_url=None)
    with pytest.raises(ConfigError, match="relative URL '/schemas/item'"):
        http_schema_resource(router, "/{pk}")


def test_relative_link_error_names_the_operation():
    _, router = make_api(base_url=None)
    with pytest.raises(ConfigError, match=r"get_item\(\)'s .*schema link"):
        http_schema_resource(router, "/{pk}")


def test_relative_link_resolves_against_base_url_on_the_wire_too():
    api, router = make_api()
    response = TestClient(api).get("/1")
    assert response.status_code == 200
    assert "<https://api.example/schemas/item>" in response.headers["Link"]
    assert "<https://api.example/contexts/item>" in response.headers["Link"]
    body = json.dumps(http_schema_resource(router, "/{pk}"))
    assert "https://api.example/schemas/item" in body
    assert "https://api.example/contexts/item" in body


def test_error_links_resolve_against_base_url_on_the_wire_too():
    api, router = make_api(auth=KeyAuth())
    relative = Entity(
        schema_link_or_func="/schemas/error", context_link_or_func="/contexts/error"
    )
    add_error_links(api, relative)
    response = TestClient(api).get("/1")
    assert response.status_code == 401
    assert "<https://api.example/schemas/error>" in response.headers["Link"]
    default = http_schema_resource(router, "/{pk}")["get"]["responses"]["default"]
    assert default["content"]["application/json"]["schema"]["$ref"] == (
        "https://api.example/schemas/error"
    )


def test_without_base_url_the_wire_resolves_against_the_request(settings):
    settings.SCRUDFUL = {}
    api, _ = make_api(base_url=None)
    response = TestClient(api).get("/1")
    assert "<http://testlocation/schemas/item>" in response.headers["Link"]


# --- describe-mode link callables ----------------------------------------------


def test_link_callables_are_called_in_describe_mode():
    calls = []

    def schema(request, obj):
        calls.append((request, obj))
        return "https://x/s"

    _, router = make_api(schema_link_or_func=schema)
    http_schema_resource(router, "/{pk}")
    assert calls == [(None, None)] * len(calls) and calls


def test_hsr_is_built_once_per_path():
    calls = []

    def schema(request, obj):
        calls.append((request, obj))
        return "https://x/s"

    api, router = make_api(schema_link_or_func=schema)
    first = http_schema_resource(router, "/{pk}")
    built = len(calls)
    client = TestClient(api)
    client.get("/_hsr/pk")
    client.get("/_hsr/pk")
    options(client, "/1")
    assert http_schema_resource(router, "/{pk}") == first
    assert len(calls) == built


def test_http_schema_resource_returns_a_copy():
    _, router = make_api()
    http_schema_resource(router, "/{pk}")["get"] = "changed"
    assert http_schema_resource(router, "/{pk}")["get"] != "changed"


def test_callable_needing_request_names_itself():
    _, router = make_api(
        schema_link_or_func=lambda request, obj: request.build_absolute_uri("/s")
    )
    with pytest.raises(ConfigError, match=r"func\(None, None\)"):
        http_schema_resource(router, "/{pk}")


def test_callable_error_message():
    def schema(request, obj):
        raise LookupError("no request")

    _, router = make_api(schema_link_or_func=schema)
    with pytest.raises(ConfigError) as info:
        http_schema_resource(router, "/{pk}")
    assert str(info.value) == (
        "make_api.<locals>.get_item(): link callables must work without a "
        "request or object when describing (called as func(None, None)): "
        "no request"
    )
    assert isinstance(info.value.__cause__, LookupError)


def test_relative_url_from_a_callable_is_config_error():
    _, router = make_api(base_url=None, schema_link_or_func=lambda request, obj: "/s")
    with pytest.raises(ConfigError, match="relative URL '/s'"):
        http_schema_resource(router, "/{pk}")


def test_async_link_callables_are_awaited():
    calls = []

    async def schema(request, obj):
        calls.append((request, obj))
        return "https://x/async"

    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(
        load=lambda request, pk: None,
        schema_link_or_func=schema,
        context_link_or_func="/contexts/item",
    )
    async def get_item(request, pk: int, obj):
        return obj

    hsr = http_schema_resource(router, "/{pk}")
    assert hsr["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "https://x/async"
    }
    assert calls and set(calls) == {(None, None)}


# --- the options entry, errors, completeness -----------------------------------


def test_options_entry_is_204():
    _, router = make_api()
    assert http_schema_resource(router, "/{pk}")["options"] == {
        "responses": {"204": {"description": "No Content"}}
    }


def test_default_and_explicit_error_entries():
    api, router = make_api(error=OP_ERROR)
    add_error_links(api, API_ERROR)
    responses = http_schema_resource(router, "/{pk}")["get"]["responses"]
    assert responses["default"]["content"] == entity_content(OP_ERROR)
    assert (
        responses["401"]["content"]
        == responses["422"]["content"]
        == entity_content(API_ERROR)
    )
    assert responses["429"]["content"] == entity_content(API_ERROR)


def test_default_error_entry_is_the_api_wide_entity():
    api, router = make_api()
    add_error_links(api, API_ERROR)
    hsr = http_schema_resource(router, "/{pk}")
    for method in ("get", "put"):
        responses = hsr[method]["responses"]
        assert responses["default"]["content"] == entity_content(API_ERROR)
        assert not {"401", "422", "429"} & responses.keys()


def test_same_entity_needs_no_explicit_error_entries():
    api, router = make_api(error=API_ERROR)
    add_error_links(api, API_ERROR)
    responses = http_schema_resource(router, "/{pk}")["put"]["responses"]
    assert responses["default"]["content"] == entity_content(API_ERROR)
    assert not {"401", "422", "429"} & responses.keys()


def test_operation_error_without_api_wide_entity():
    _, router = make_api(error=OP_ERROR)
    responses = http_schema_resource(router, "/{pk}")["get"]["responses"]
    assert responses["default"]["content"] == entity_content(OP_ERROR)
    # ninja's own 401, 422 and 429 carry no error entity: no content.
    for key in ("401", "422", "429"):
        assert "content" not in responses[key] and responses[key]["description"]


def test_no_error_entity_gives_a_default_entry_without_content():
    _, router = make_api()
    default = http_schema_resource(router, "/{pk}")["get"]["responses"]["default"]
    assert default == {"description": "Error."}


def test_head_error_entries_have_no_content():
    router = ScrudfulRouter(base_url=BASE_URL)

    # ninja routes HEAD only to an operation listing it.
    @router.api_operation(["GET", "HEAD"], "/{pk}", response=ItemOut)
    @scrudful(load=lambda request, pk: None, error=OP_ERROR, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    add_error_links(mount(router), API_ERROR)
    hsr = http_schema_resource(router, "/{pk}")
    assert hsr["get"]["responses"]["401"]["content"] == entity_content(API_ERROR)
    responses = hsr["head"]["responses"]
    assert set(responses) == set(hsr["get"]["responses"])
    assert responses["default"] == {"description": "Error."}
    assert all("content" not in entry for entry in responses.values())


def test_api_wide_entity_differing_between_apis_is_config_error():
    api, router = make_api()
    other = mount(router)
    add_error_links(api, API_ERROR)
    add_error_links(other, OP_ERROR)
    with pytest.raises(ConfigError, match="add_error_links"):
        http_schema_resource(router, "/{pk}")


def test_api_wide_entity_of_a_nested_router():
    parent = ScrudfulRouter(base_url=BASE_URL)
    _, child = make_api()
    parent.add_router("/items", child)
    api = mount(parent)
    add_error_links(api, API_ERROR)
    responses = http_schema_resource(child, "/{pk}")["get"]["responses"]
    assert responses["default"]["content"] == entity_content(API_ERROR)


@pytest.mark.parametrize(
    "owner, attribute, internal",
    [
        ("api", "_router_registrations", "NinjaAPI._router_registrations"),
        ("router", "_routers", "Router._routers"),
    ],
)
def test_finding_the_apis_is_version_guarded(monkeypatch, owner, attribute, internal):
    api, router = make_api()
    add_error_links(api, API_ERROR)
    monkeypatch.setattr({"api": api, "router": router}[owner], attribute, {})
    with pytest.raises(ConfigError, match=re.escape(internal)):
        http_schema_resource(router, "/{pk}")


def test_add_error_links_after_a_build_rebuilds():
    api, router = make_api()
    assert (
        "content"
        not in http_schema_resource(router, "/{pk}")["get"]["responses"]["default"]
    )
    add_error_links(api, API_ERROR)
    default = http_schema_resource(router, "/{pk}")["get"]["responses"]["default"]
    assert default["content"] == entity_content(API_ERROR)


def test_undecorated_operation_on_scrudful_router_needs_entity():
    router = ScrudfulRouter(base_url="https://api.example/")
    with pytest.raises(ConfigError, match="scrudful_entity"):

        @router.get("/plain")
        def plain(request):
            return {}

    assert router.path_operations == {}


def test_described_plain_operation():
    router = ScrudfulRouter(base_url=BASE_URL)
    entity = Entity(
        schema_link_or_func="/schemas/plain", context_link_or_func="/contexts/plain"
    )

    @router.get("/plain")
    @described(entity)
    def plain(request):
        return {}

    @router.post("/plain", response={201: ItemOut})
    @described(entity)
    def create(request, payload: ItemIn):
        return 201, {}

    hsr = http_schema_resource(router, "/plain")
    validate_http_schema_resource(hsr)
    content = {
        "application/json": {
            "schema": {"$ref": "https://api.example/schemas/plain"},
            "x-rdf-context": {
                "href": "https://api.example/contexts/plain",
                "type": "application/ld+json",
            },
        }
    }
    assert hsr["get"]["responses"]["200"]["content"] == content
    assert hsr["post"]["requestBody"]["content"] == content
    assert hsr["post"]["responses"]["201"]["content"] == content
    assert hsr["get"]["responses"]["default"] == {"description": "Error."}
    assert "parameters" not in hsr["post"]


def test_scrudful_entity_on_add_api_operation():
    router = ScrudfulRouter(base_url=BASE_URL)
    entity = Entity(
        schema_link_or_func="/schemas/plain", context_link_or_func="/contexts/plain"
    )

    def plain(request):
        return {}

    router.add_api_operation("/plain", ["GET"], plain, scrudful_entity=entity)
    content = http_schema_resource(router, "/plain")["get"]["responses"]["200"][
        "content"
    ]
    assert content["application/json"]["schema"] == {
        "$ref": "https://api.example/schemas/plain"
    }


def test_described_rejects_misuse():
    with pytest.raises(TypeError, match="Entity"):
        described("not an entity")
    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/x")
    @scrudful(**LINKS)
    def scrudful_op(request):
        return {}

    with pytest.raises(TypeError, match="below the operation decorator"):
        described(Entity())(scrudful_op)
    with pytest.raises(TypeError, match="scrudful decorator"):
        described(Entity())(scrudful(**LINKS)(lambda request: {}))
    with pytest.raises(ConfigError, match="scrudful_entity"):
        router.add_api_operation(
            "/y", ["GET"], described(Entity())(lambda r: {}), scrudful_entity=Entity()
        )


def test_undecorated_options_operation_needs_no_entity():
    router = ScrudfulRouter(base_url=BASE_URL)

    @router.api_operation(["OPTIONS"], "/plain")
    def options(request):
        return {}

    assert http_schema_resource(router, "/plain") == {
        "options": {"responses": {"204": {"description": "No Content"}}}
    }


def test_unknown_path_is_config_error():
    _, router = make_api()
    with pytest.raises(ConfigError, match="'/nope'"):
        http_schema_resource(router, "/nope")
    with pytest.raises(TypeError, match="ScrudfulRouter"):
        http_schema_resource(Router(), "/{pk}")


def test_hsr_validates_against_spec_schema():
    _, router = make_api()
    validate_http_schema_resource(http_schema_resource(router, "/{pk}"))


def test_hsr_with_error_entries_validates_against_spec_schema():
    api, router = make_api(error=OP_ERROR)
    add_error_links(api, API_ERROR)
    validate_http_schema_resource(http_schema_resource(router, "/{pk}"))


# --- configuration -------------------------------------------------------------


def test_settings_base_url(settings):
    settings.SCRUDFUL = {"BASE_URL": "https://set.example/"}
    _, router = make_api(base_url=None)
    assert "https://set.example/schemas/item" in json.dumps(
        http_schema_resource(router, "/{pk}")
    )


def test_router_base_url_overrides_the_setting(settings):
    settings.SCRUDFUL = {"BASE_URL": "https://set.example/"}
    _, router = make_api(base_url="https://router.example/")
    assert "https://router.example/schemas/item" in json.dumps(
        http_schema_resource(router, "/{pk}")
    )


def test_relative_base_url_is_rejected(settings):
    with pytest.raises(ConfigError, match="base_url"):
        ScrudfulRouter(base_url="/relative/")
    with pytest.raises(TypeError, match="base_url"):
        ScrudfulRouter(base_url=1)
    settings.SCRUDFUL = {"BASE_URL": "relative/"}
    _, router = make_api(base_url=None)
    with pytest.raises(ConfigError, match="BASE_URL"):
        http_schema_resource(router, "/{pk}")


def test_router_stores_its_hsr_options():
    def hsr_url(path):
        return f"https://docs.example{path}"

    def allowed(request, method, obj):
        return True

    router = ScrudfulRouter(
        base_url=BASE_URL,
        hsr_route=False,
        hsr_url=hsr_url,
        hsr_cache_control="no-cache",
        allowed=allowed,
        tags=["items"],
    )
    assert (router.base_url, router.hsr_route, router.hsr_url) == (
        BASE_URL,
        False,
        hsr_url,
    )
    assert (router.hsr_cache_control, router.allowed) == ("no-cache", allowed)
    assert router.tags == ["items"]
    defaults = ScrudfulRouter()
    assert (defaults.base_url, defaults.hsr_route, defaults.hsr_url) == (
        None,
        None,
        None,
    )
    assert (defaults.hsr_cache_control, defaults.allowed) == (None, None)


@pytest.mark.parametrize(
    "options",
    [
        {"hsr_route": "yes"},
        {"hsr_url": "x"},
        {"hsr_cache_control": 300},
        {"allowed": True},
    ],
)
def test_router_rejects_wrong_option_types(options):
    with pytest.raises(TypeError, match=next(iter(options))):
        ScrudfulRouter(**options)


def test_scrudful_setting(settings):
    assert scrudful_setting("BASE_URL", "fallback") == "fallback"  # no SCRUDFUL
    settings.SCRUDFUL = {"HSR_ROUTE": False}
    assert scrudful_setting("HSR_ROUTE", True) is False
    assert scrudful_setting("BASE_URL", None) is None
    settings.SCRUDFUL = {"BASE_ULR": "https://typo.example/"}
    with pytest.raises(ImproperlyConfigured, match="BASE_ULR"):
        scrudful_setting("BASE_URL", None)
    settings.SCRUDFUL = ["BASE_URL"]
    with pytest.raises(ImproperlyConfigured, match="dict"):
        scrudful_setting("BASE_URL", None)


# --- the HSR GET route ---------------------------------------------------------


def head(client: TestClient, path: str, **kwargs: Any) -> Any:
    # ninja's TestClient has no head().
    return client.request("HEAD", path, **kwargs)


def exposed(headers: Any) -> set[str]:
    return {n.strip() for n in headers["Access-Control-Expose-Headers"].split(",")}


def test_hsr_route_serves_the_document():
    api, router = make_api()
    client = TestClient(api)
    r = client.get("/_hsr/pk")
    assert hsr_get_problems(r.status_code, dict(r.headers), r.content) == []
    assert json.loads(r.content) == http_schema_resource(router, "/{pk}")
    assert r.headers["Content-Type"] == "application/json"
    assert r.headers["Cache-Control"] == "public, max-age=300"
    assert {"Link", "Allow", "ETag"} <= exposed(r.headers)
    etag_ = r.headers["ETag"]
    not_modified = client.get("/_hsr/pk", headers={"If-None-Match": etag_})
    assert not_modified.status_code == 304
    assert not_modified.headers["ETag"] == etag_
    assert not_modified.headers["Cache-Control"] == "public, max-age=300"
    weak = client.get("/_hsr/pk", headers={"If-None-Match": "W/" + etag_})
    assert weak.status_code == 304
    other = client.get("/_hsr/pk", headers={"If-None-Match": '"other"'})
    assert other.status_code == 200
    assert head(client, "/_hsr/pk").status_code == 200
    assert head(client, "/_hsr/pk").headers["ETag"] == etag_


def test_hsr_body_is_the_bytes_the_etag_hashes():
    class IndentedRenderer(JSONRenderer):
        json_dumps_params = {"indent": 4}

    router_options, _, item = split_options({})
    router = ScrudfulRouter(**router_options)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    api = NinjaAPI(
        urls_namespace=f"api-{uuid.uuid4().hex}", renderer=IndentedRenderer()
    )
    api.add_router("", router)
    r = TestClient(api).get("/_hsr/pk")
    assert r.content == serialize_hsr(http_schema_resource(router, "/{pk}"))
    assert r.headers["ETag"] == hsr_etag(r.content)


@pytest.fixture
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_hsr_route_needs_no_auth_and_ignores_throttles(clear_cache):
    api, _ = make_api(auth=KeyAuth(), throttle=[AnonRateThrottle("1/day")])
    client = TestClient(api)
    assert [client.get("/_hsr/pk").status_code for _ in range(3)] == [200, 200, 200]
    assert client.get("/1").status_code == 401


def test_hsr_route_ignores_router_auth_and_throttles(clear_cache):
    router_options, _, item = split_options({})
    router = ScrudfulRouter(
        auth=KeyAuth(), throttle=[AnonRateThrottle("1/day")], **router_options
    )

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    client = TestClient(mount(router))
    assert [client.get("/_hsr/pk").status_code for _ in range(3)] == [200, 200, 200]


def test_hsr_route_is_csrf_exempt_and_not_in_openapi():
    api, router = make_api()
    [operation] = router.path_operations["/_hsr/pk"].operations
    assert operation.csrf_exempt
    assert operation.methods == ["GET", "HEAD"]
    paths = api.get_openapi_schema(path_prefix="")["paths"]
    assert "/{pk}" in paths
    assert not any("_hsr" in p for p in paths)


def test_hsr_route_resolves_before_item_path():
    api, _ = make_api()
    response = TestClient(api).get("/_hsr/pk")
    assert response.headers["Content-Type"] == "application/json"


def wildcard_router() -> ScrudfulRouter:
    """Paths that would swallow ``/_hsr/<name>`` if they came first: a
    ``@described`` ``"/{x}/{y}"`` and a scrudful ``"/{kind}/{pk}"``, then a
    ``"/{pk}"`` added after them."""
    router = ScrudfulRouter(base_url=BASE_URL)
    _, _, item = split_options({})

    @router.get("/{x}/{y}")
    @described(Entity(**LINKS))
    def pair(request, x: str, y: str):
        return {"pair": [x, y]}

    @router.get("/{kind}/{pk}/{rest}", response=ItemOut)
    @scrudful(**item)
    def kind_item(request, kind: str, pk: int, rest: str, obj):
        return obj

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    return router


@pytest.mark.parametrize(
    "hsr_path, path",
    [
        ("/_hsr/pk", "/{pk}"),
        ("/_hsr/x.y", "/{x}/{y}"),
        ("/_hsr/kind.pk.rest", "/{kind}/{pk}/{rest}"),
    ],
)
def test_hsr_routes_resolve_before_every_path_of_the_router(hsr_path, path):
    router = wildcard_router()
    r = TestClient(mount(router)).get(hsr_path)
    assert hsr_get_problems(r.status_code, dict(r.headers), r.content) == []
    assert json.loads(r.content) == http_schema_resource(router, path)


def test_described_only_path_gets_an_hsr_route():
    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/health")
    @described(Entity(**LINKS))
    def health(request):
        return {}

    @router.api_operation(["OPTIONS"], "/bare")
    def bare(request):
        return {}

    client = TestClient(mount(router))
    r = client.get("/_hsr/health")
    assert hsr_get_problems(r.status_code, dict(r.headers), r.content) == []
    assert json.loads(r.content) == http_schema_resource(router, "/health")
    assert "/_hsr/bare" not in router.path_operations


def handler(request, obj):
    return obj


def pk_handler(request, pk: int, obj):
    return obj


def id_handler(request, id: int, obj):
    return obj


def x_handler(request, x: str, obj):
    return obj


def plain(request):
    return {}


def test_reserved_segment_and_name_clash():
    router = ScrudfulRouter(base_url=BASE_URL)
    _, _, item = split_options({})
    with pytest.raises(ConfigError, match="reserved"):
        router.get("/_hsr/x")(scrudful(**item)(handler))
    with pytest.raises(ConfigError, match="reserved"):
        router.get("_hsr/x")(described(Entity(**LINKS))(plain))
    assert router.path_operations == {}
    router.get("/{pk}")(scrudful(**item)(pk_handler))
    router.get("/{id}")(scrudful(**item)(id_handler))  # "pk" vs "id": no clash
    assert {"/_hsr/pk", "/_hsr/id"} <= router.path_operations.keys()
    router.get("/a/{x}")(scrudful(**item)(x_handler))
    with pytest.raises(ConfigError, match=r"'/a/\{x\}/'.*'/a/\{x\}'.*'a\.x'"):
        router.get("/a/{x}/")(scrudful(**item)(x_handler))
    with pytest.raises(ConfigError, match="'pk'"):
        router.get("/{int:pk}")(scrudful(**item)(pk_handler))
    router.put("/{pk}")(scrudful(**item)(pk_handler))  # the same path: no clash
    assert "/{int:pk}" not in router.path_operations


def test_hsr_route_false_needs_hsr_url():
    with pytest.raises(ConfigError, match=re.escape("hsr_route=False needs hsr_url=")):
        make_api(hsr_route=False)


def test_hsr_route_setting(settings):
    settings.SCRUDFUL = {"HSR_ROUTE": False}
    with pytest.raises(ConfigError, match="hsr_url"):
        ScrudfulRouter(base_url=BASE_URL)
    settings.SCRUDFUL = {"HSR_ROUTE": False, "HSR_URL": lambda p: f"/docs{p}"}
    api, router = make_api()
    assert router.path_operations.keys() == {"/{pk}"}
    with pytest.raises(Exception, match="Cannot resolve"):
        TestClient(api).get("/_hsr/pk")
    _, router = make_api(hsr_route=True)  # the router's argument wins
    assert "/_hsr/pk" in router.path_operations
    settings.SCRUDFUL = {"HSR_ROUTE": "no"}
    with pytest.raises(ConfigError, match="HSR_ROUTE"):
        ScrudfulRouter()
    settings.SCRUDFUL = {"HSR_URL": "https://docs.example/"}
    with pytest.raises(ConfigError, match="HSR_URL"):
        ScrudfulRouter()


def test_hsr_cache_control(settings):
    def cache_control(api: NinjaAPI) -> str:
        return TestClient(api).get("/_hsr/pk").headers["Cache-Control"]

    api, _ = make_api(hsr_cache_control="public, max-age=60")
    assert cache_control(api) == "public, max-age=60"
    settings.SCRUDFUL = {"HSR_CACHE_CONTROL": "public, max-age=10"}
    assert cache_control(make_api()[0]) == "public, max-age=10"
    assert cache_control(api) == "public, max-age=60"  # the router's wins
    settings.SCRUDFUL = {"HSR_CACHE_CONTROL": 10}
    with pytest.raises(ConfigError, match="HSR_CACHE_CONTROL"):
        cache_control(make_api()[0])


def test_http_schema_resource_view_for_mounting(rf):
    _, router = make_api(
        hsr_route=False, hsr_url=lambda p: f"https://docs.example/hsr{p}"
    )
    view = http_schema_resource_view(router, "/{pk}")
    r = view(rf.get("/anywhere"))
    assert hsr_get_problems(r.status_code, dict(r.items()), r.content) == []
    assert r.content == serialize_hsr(http_schema_resource(router, "/{pk}"))
    assert view(rf.get("/anywhere", HTTP_IF_NONE_MATCH=r["ETag"])).status_code == 304
    assert view(rf.head("/anywhere")).status_code == 200
    not_allowed = view(rf.post("/anywhere"))
    assert not_allowed.status_code == 405
    assert not_allowed["Allow"] == "GET, HEAD"
    assert view.csrf_exempt
    with pytest.raises(TypeError, match="ScrudfulRouter"):
        http_schema_resource_view(Router(), "/{pk}")


# --- the HSR link target -------------------------------------------------------


def target_router(name: str, **router_options: Any) -> ScrudfulRouter:
    """A ``"/{pk}"`` with its own entity, and ``"/{pk}/target"`` answering
    ``_hsr_target(request, router, "/{pk}")``."""
    router = ScrudfulRouter(**router_options)
    links = dict(
        schema_link_or_func=f"https://api.example/schemas/{name}",
        context_link_or_func=f"https://api.example/contexts/{name}",
    )

    @router.get("/{pk}", response=ItemOut)
    @scrudful(load=Store().load, etag_func=etag, **links)
    def get_item(request, pk: int, obj):
        return obj

    @router.get("/{pk}/target")
    @described(Entity(**links))
    def target(request, pk: int):
        return {"target": _hsr_target(request, router, "/{pk}")}

    return router


def two_routers(
    **router_options: Any,
) -> tuple[NinjaAPI, ScrudfulRouter, ScrudfulRouter]:
    """Two routers each with ``"/{pk}"``, under one API at ``/a`` and ``/b``."""
    a = target_router("a", **router_options)
    b = target_router("b", **router_options)
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/a", a)
    api.add_router("/b", b)
    return api, a, b


def test_two_routers_with_one_path_have_their_own_route_and_target():
    api, a, b = two_routers()
    client = TestClient(api)
    assert http_schema_resource(a, "/{pk}") != http_schema_resource(b, "/{pk}")
    for prefix, router in (("a", a), ("b", b)):
        r = client.get(f"/{prefix}/_hsr/pk")
        assert hsr_get_problems(r.status_code, dict(r.headers), r.content) == []
        assert json.loads(r.content) == http_schema_resource(router, "/{pk}")
        target = client.get(f"/{prefix}/1/target").json()["target"]
        assert target == f"http://testlocation/{prefix}/_hsr/pk"
    names = [p.name for p in api.urls[0] if "_hsr" in str(p.pattern)]
    assert len(names) == len(set(names)) == 4  # pk and pk.target, per router


def test_target_resolves_against_the_base_url():
    api, _, _ = two_routers(base_url="https://api.example/v1/")
    target = TestClient(api).get("/b/1/target").json()["target"]
    assert target == "https://api.example/b/_hsr/pk"


def test_target_is_hsr_url_when_given():
    router = target_router(
        "a", hsr_route=False, hsr_url=lambda p: f"https://docs.example/hsr{p}"
    )
    target = TestClient(mount(router)).get("/1/target").json()["target"]
    assert target == "https://docs.example/hsr/{pk}"


def test_target_from_the_hsr_url_setting(settings):
    settings.SCRUDFUL = {"HSR_URL": lambda p: f"https://docs.example/s{p}"}
    router = target_router("a")
    target = TestClient(mount(router)).get("/1/target").json()["target"]
    assert target == "https://docs.example/s/{pk}"


def test_target_with_the_route_turned_off_later_is_config_error(settings):
    router = target_router("a")
    client = TestClient(mount(router))
    settings.SCRUDFUL = {"HSR_ROUTE": False}
    with pytest.raises(ConfigError, match="hsr_url"):
        client.get("/1/target")


def test_target_outside_an_operation_is_config_error(rf):
    router = target_router("a")
    with pytest.raises(ConfigError, match=re.escape("'/{pk}'")):
        _hsr_target(rf.get("/1/target"), router, "/{pk}")


TWO_API, TWO_A, TWO_B = two_routers()
ORG_ROUTER = target_router("org")
ORG_API = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
ORG_API.add_router("/orgs/{org}/items", ORG_ROUTER)
urlpatterns += [path("two/", TWO_API.urls), path("t/<str:tenant>/", ORG_API.urls)]


@pytest.mark.urls(__name__)
def test_targets_reverse_through_django_url_resolver(client, settings, db):
    settings.ALLOWED_HOSTS = ["a.example"]
    for prefix, router in (("a", TWO_A), ("b", TWO_B)):
        target = client.get(f"/two/{prefix}/1/target", HTTP_HOST="a.example").json()
        assert target == {"target": f"http://a.example/two/{prefix}/_hsr/pk"}
        r = client.get(f"/two/{prefix}/_hsr/pk", HTTP_HOST="a.example")
        assert hsr_get_problems(r.status_code, dict(r.items()), r.content) == []
        assert json.loads(r.content) == http_schema_resource(router, "/{pk}")
    r = client.head("/two/a/_hsr/pk", HTTP_HOST="a.example")
    assert r.status_code == 200 and r.content == b""


@pytest.mark.urls(__name__)
def test_target_under_parameterized_prefixes(client, settings, db):
    settings.ALLOWED_HOSTS = ["a.example"]
    url = "/t/acme/orgs/7/items/1/target"
    target = client.get(url, HTTP_HOST="a.example").json()["target"]
    assert target == "http://a.example/t/acme/orgs/7/items/_hsr/pk"
    r = client.get("/t/acme/orgs/7/items/_hsr/pk", HTTP_HOST="a.example")
    assert hsr_get_problems(r.status_code, dict(r.items()), r.content) == []


# --- the ninja internals the HSR routes and links rely on ----------------------


@pytest.fixture
def fresh_ninja_code():
    """The check of ninja's code is memoized per function: start afresh."""
    import scrudful.ninja

    scrudful.ninja._is_ninja_code.cache_clear()
    yield
    scrudful.ninja._is_ninja_code.cache_clear()


def wrapping(func):
    """A function behaving as ``func`` whose code is not ninja's."""

    def changed(*args, **kwargs):
        return func(*args, **kwargs)

    return changed


@pytest.mark.parametrize("method", ["_bind_operations", "urls_paths"])
def test_hsr_routes_need_ninjas_bound_router(monkeypatch, fresh_ninja_code, method):
    """The routes' order and URL names are kept by these two methods."""
    cls = ninja.router.BoundRouter
    monkeypatch.setattr(cls, method, wrapping(getattr(cls, method)))
    with pytest.raises(
        ConfigError, match=rf"rely on ninja's BoundRouter\.{method}: .*upgrade"
    ):
        make_api()


def test_hsr_routes_need_a_bound_router(monkeypatch, fresh_ninja_code):
    monkeypatch.delattr(ninja.router, "BoundRouter")
    with pytest.raises(ConfigError, match=r"ninja's BoundRouter\._bind_operations"):
        make_api()


def test_without_source_the_bound_router_fingerprint_is_checked(
    monkeypatch, fresh_ninja_code
):
    import scrudful.ninja

    monkeypatch.setattr(scrudful.ninja, "_source_digest", lambda func: None)
    api, _ = make_api()
    assert TestClient(api).get("/_hsr/pk").status_code == 200
    cls = ninja.router.BoundRouter
    monkeypatch.setattr(cls, "urls_paths", wrapping(cls.urls_paths))
    with pytest.raises(ConfigError, match=r"ninja's BoundRouter\.urls_paths"):
        make_api()


def test_target_under_test_client_needs_ninjas_resolve(monkeypatch, fresh_ninja_code):
    cls = ninja.testing.client.NinjaClientBase
    monkeypatch.setattr(cls, "_resolve", wrapping(cls._resolve))
    client = TestClient(mount(target_router("a")))
    with pytest.raises(ConfigError, match=r"ninja's NinjaClientBase\._resolve"):
        client.get("/1/target")


def test_target_under_test_client_needs_ninjas_urls(monkeypatch):
    client = TestClient(mount(target_router("a")))
    assert client.get("/1").status_code == 200  # the client keeps the patterns
    monkeypatch.setattr(NinjaAPI, "urls", property(lambda self: {"patterns": []}))
    with pytest.raises(ConfigError, match=r"NinjaAPI\.urls.*upgrade"):
        client.get("/1/target")


def test_target_needs_the_operations_api():
    from scrudful.ninja import _operation_api

    with pytest.raises(ConfigError, match=r"ninja's Operation\.api.*upgrade"):
        _operation_api(types.SimpleNamespace())
    with pytest.raises(ConfigError, match=r"Operation\.api"):
        _operation_api(types.SimpleNamespace(api=None))
    with pytest.raises(ConfigError, match="functools.wraps"):
        _operation_api(None)


# --- OPTIONS: a 204 with a per-requester Allow ---------------------------------

#: The HSR link target of ``make_api``'s ``"/{pk}"`` (Ruling R6).
PK_TARGET = "https://api.example/_hsr/pk"


def make_async_api(**config: Any) -> tuple[NinjaAPI, ScrudfulRouter]:
    """``make_api`` with ``async def`` operations."""
    router_options, api_options, item = split_options(config)
    router = ScrudfulRouter(**router_options)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    async def get_item(request, pk: int, obj):
        return obj

    @router.put("/{pk}", response=ItemOut)
    @scrudful(**item)
    async def put_item(request, pk: int, payload: ItemIn, obj):
        update(obj, payload.name)
        return obj

    return mount(router, **api_options), router


def async_options(api: NinjaAPI, path: str, **kwargs: Any) -> Any:
    return async_to_sync(TestAsyncClient(api).request)("OPTIONS", path, **kwargs)


def deny_put_unless_alice(request, **params):
    if request.method == "PUT" and request.auth != ALICE:
        raise HttpError(403, "Forbidden")


async def async_deny_put(request, **params):
    if request.method == "PUT":
        raise HttpError(403, "Forbidden")


def raise_http_404(request, **params):
    raise HttpError(404, "Not Found")


class CountingAuth:
    """An ``auth=`` callable noting each call in ``calls``."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __call__(self, request):
        self.calls.append("auth")
        return "someone"


def counting_load(calls: list[str]) -> Any:
    store = Store()

    def load(request, pk, **params):
        calls.append("load")
        return store.load(request, pk, **params)

    return load


def test_options_is_204_with_hsr_link():
    api, router = make_api()
    r = options(TestClient(api), "/1", headers={"X-API-Key": ALICE})
    assert (
        options_204_problems(
            r.status_code, dict(r.headers), r.content, {"GET", "HEAD", "PUT", "OPTIONS"}
        )
        == []
    )
    assert hsr_link_problems(dict(r.headers), PK_TARGET) == []
    assert r.headers["Allow"] == "GET, PUT, OPTIONS, HEAD"
    assert {"Link", "Allow"} <= exposed(r.headers)


def test_allow_reflects_requester():
    api, _ = make_api(auth=KeyAuth(), before=deny_put_unless_alice)
    client = TestClient(api)
    assert options(client, "/1").headers["Allow"] == "OPTIONS"
    assert (
        options(client, "/1", headers={"X-API-Key": BOB}).headers["Allow"]
        == "GET, OPTIONS, HEAD"
    )
    assert (
        options(client, "/1", headers={"X-API-Key": ALICE}).headers["Allow"]
        == "GET, PUT, OPTIONS, HEAD"
    )
    # OPTIONS itself isn't gated: an anonymous requester gets a 204, not a 401.
    anonymous = options(client, "/1")
    assert (
        options_204_problems(
            anonymous.status_code,
            dict(anonymous.headers),
            anonymous.content,
            {"OPTIONS"},
        )
        == []
    )


def test_allow_hook_overrides_and_gets_object():
    seen = []

    def allowed(request, method, obj):
        seen.append((method, obj))
        return method == "GET"

    api, _ = make_api(allowed=allowed)
    assert options(TestClient(api), "/1").headers["Allow"] == "GET, OPTIONS, HEAD"
    assert {m for m, _ in seen} == {"GET", "PUT"}
    assert all(o is not None for _, o in seen)
    assert all(o["id"] == 1 for _, o in seen)  # the loaded object


def test_operations_allow_hook_wins_over_the_routers():
    router_options, _, item = split_options(
        {"allowed": lambda request, method, obj: False}
    )
    router = ScrudfulRouter(**router_options)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    @router.put("/{pk}", response=ItemOut)
    @scrudful(allowed=lambda request, method, obj: method == "PUT", **item)
    def put_item(request, pk: int, payload: ItemIn, obj):
        return obj

    assert options(TestClient(mount(router)), "/1").headers["Allow"] == "PUT, OPTIONS"


def test_allow_hook_must_return_a_bool():
    api, _ = make_api(allowed=lambda request, method, obj: "yes")
    with pytest.raises(TypeError, match="allowed.*bool"):
        options(TestClient(api), "/1")


def test_allow_hook_gets_the_authenticated_requester():
    seen = []

    def allowed(request, method, obj):
        seen.append((method, request.method, request.auth))
        return True

    api, _ = make_api(auth=KeyAuth(), allowed=allowed)
    client = TestClient(api)
    assert options(client, "/1", headers={"X-API-Key": BOB}).status_code == 204
    assert sorted(seen) == [("GET", "GET", BOB), ("PUT", "PUT", BOB)]
    seen.clear()
    # The hook alone decides: it is asked about an anonymous requester too.
    assert options(client, "/1").headers["Allow"] == "GET, PUT, OPTIONS, HEAD"
    assert sorted(seen) == [("GET", "GET", None), ("PUT", "PUT", None)]


def test_async_before_is_honoured():
    api, _ = make_async_api(before=async_deny_put)
    r = async_options(api, "/1")
    assert options_204_problems(r.status_code, dict(r.headers), r.content) == []
    assert "PUT" not in r.headers["Allow"]
    assert r.headers["Allow"] == "GET, OPTIONS, HEAD"


async def deny_all(request, method, obj):
    return False


async def no_one(request):
    return None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("config", [{"allowed": deny_all}, {"auth": no_one}])
def test_async_checks_are_awaited_never_taken_as_allowed(asynchronous, config):
    """An async ``allowed`` hook or ``auth`` callback returns a coroutine,
    which is truthy: it must be awaited, never taken for its truth."""
    if asynchronous:
        r = async_options(make_async_api(**config)[0], "/1")
    else:
        r = options(TestClient(make_api(**config)[0]), "/1")
    assert r.status_code == 204
    assert r.headers["Allow"] == "OPTIONS"


def test_sync_before_returning_an_awaitable_is_not_allowed():
    async def later(request):
        return None

    def before(request, **params):
        return later(request)

    api, _ = make_api(before=before)
    assert options(TestClient(api), "/1").headers["Allow"] == "OPTIONS"


def test_missing_object_still_204_without_object_checks():
    api, _ = make_api(load=lambda request, pk: None)
    r = options(TestClient(api), "/404")
    assert r.status_code == 204
    assert options_204_problems(r.status_code, dict(r.headers), r.content) == []
    seen = []

    def allowed(request, method, obj):
        seen.append(obj)
        return True

    api, _ = make_api(load=lambda request, pk: None, allowed=allowed)
    assert options(TestClient(api), "/404").status_code == 204
    assert seen == [None, None]


def test_loader_gets_the_validated_path_parameters():
    seen = []

    def load(request, pk, **params):
        seen.append((pk, params, request.auth))
        return None

    api, _ = make_api(auth=KeyAuth(), load=load)
    client = TestClient(api)
    assert options(client, "/7", headers={"X-API-Key": BOB}).status_code == 204
    assert seen == [(7, {}, BOB)]  # an int, as the GET gets it
    seen.clear()
    # Not authenticated for the loading operation: nothing is loaded.
    assert options(client, "/7").headers["Allow"] == "OPTIONS"
    assert seen == []
    # A path parameter the GET would refuse (422): no object.
    r = options(client, "/x", headers={"X-API-Key": BOB})
    assert r.status_code == 204 and seen == []


def test_loader_404_is_answered_with_error_links():
    api, _ = make_api(load=raise_http_404)
    add_error_links(api, API_ERROR)
    r = options(TestClient(api), "/1")
    assert r.status_code == 404
    assert link_problems("OPTIONS", 404, dict(r.headers), True) == []
    assert hsr_link_problems(dict(r.headers)) == []
    assert hsr_link_problems(dict(r.headers), PK_TARGET) == []
    assert "<https://api.example/schemas/api-error>" in r.headers["Link"]
    assert "Allow" not in r.headers
    assert {"Link", "Allow"} <= exposed(r.headers)


def test_loader_http404_is_answered_too():
    def load(request, pk):
        raise Http404("gone")

    api, _ = make_api(load=load)
    r = options(TestClient(api), "/1")
    assert r.status_code == 404
    assert hsr_link_problems(dict(r.headers), PK_TARGET) == []


def test_other_loader_errors_propagate():
    def load(request, pk):
        raise LookupError("broken")

    api, _ = make_api(load=load)
    with pytest.raises(LookupError, match="broken"):
        options(TestClient(api), "/1")


def test_other_check_errors_propagate():
    def before(request, **params):
        raise LookupError("broken")

    api, _ = make_api(before=before)
    with pytest.raises(LookupError, match="broken"):
        options(TestClient(api), "/1")


def test_an_http_error_of_another_status_propagates():
    def before(request, **params):
        raise HttpError(409, "conflict")

    api, _ = make_api(before=before)
    # ninja answers it, as it answers what an operation raises.
    r = options(TestClient(api), "/1")
    assert r.status_code == 409 and r.json() == {"detail": "conflict"}


@pytest.mark.parametrize(
    "error",
    [
        HttpError(401, "no"),
        HttpError(403, "no"),
        HttpError(404, "no"),
        Http404("no"),
        PermissionDenied("no"),
        AuthenticationError(),
    ],
    ids=["401", "403", "404", "Http404", "PermissionDenied", "AuthenticationError"],
)
@pytest.mark.parametrize("where", ["before", "auth"])
def test_denials_of_a_check(error, where):
    def check(request, **params):
        if request.method == "PUT":
            raise error
        return "someone"

    if where == "before":
        api, _ = make_api(before=lambda request, **params: check(request) and None)
    else:
        api, _ = make_api(auth=check)
    assert options(TestClient(api), "/1").headers["Allow"] == "GET, OPTIONS, HEAD"


@pytest.mark.parametrize(
    "extra", [{}, {"Cookie": "sessionid=x"}, {"X-API-Key": "wrong"}]
)
def test_preflight_runs_nothing(extra, clear_cache):
    calls = []
    api, _ = make_api(
        auth=CountingAuth(calls),
        throttle=[AnonRateThrottle("1/day")],
        load=counting_load(calls),
        before=lambda request, **params: calls.append("before"),
        allowed=lambda request, method, obj: calls.append("allowed") or True,
    )
    client = TestClient(api)
    for _ in range(3):
        r = options(
            client,
            "/1",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "PUT",
                **extra,
            },
        )
        assert r.status_code == 204 and r.content == b"" and "Link" not in r.headers
        assert "Content-Type" not in r.headers
    assert calls == []


def test_non_preflight_options_runs_the_checks():
    calls = []
    api, _ = make_api(auth=CountingAuth(calls), load=counting_load(calls))
    assert options(TestClient(api), "/1").status_code == 204
    assert calls.count("load") == 1 and "auth" in calls


def test_options_consumes_no_throttle_for_allow(clear_cache):
    api, _ = make_api(throttle=[AnonRateThrottle("2/day")])
    client = TestClient(api)
    options(client, "/1")
    assert client.get("/1").status_code == 200  # the per-method checks ran no throttle
    assert client.get("/1").status_code == 200
    assert client.get("/1").status_code == 429
    assert options(client, "/1").status_code == 204  # nor is OPTIONS throttled


def test_automatic_options_is_csrf_exempt_unthrottled_and_unauthenticated():
    _, router = make_api()
    [operation] = [
        op
        for op in router.path_operations["/{pk}"].operations
        if "OPTIONS" in op.methods
    ]
    assert operation.csrf_exempt
    assert operation.auth_param is None
    assert operation.throttle_param == []


def collection_router(**router_options: Any) -> ScrudfulRouter:
    router = ScrudfulRouter(base_url=BASE_URL, **router_options)

    @router.get("/items", response=List[ItemOut])
    @scrudful_list(href_func=lambda request, item: f"/items/{item['id']}", **LINKS)
    def list_items(request):
        return []

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(location_func=lambda request, obj: "/items/1", **LINKS)
    def create_item(request, payload: ItemIn):
        return {"id": 1, "name": payload.name, "version": 1}

    return router


def test_collection_path_allow_without_object():
    client = TestClient(mount(collection_router()))
    r = options(client, "/items")
    expected = {"GET", "HEAD", "POST", "OPTIONS"}
    assert (
        options_204_problems(r.status_code, dict(r.headers), r.content, expected) == []
    )
    assert hsr_link_problems(dict(r.headers), "https://api.example/_hsr/items") == []
    seen = []

    def allowed(request, method, obj):
        seen.append((method, obj))
        return method == "GET"

    client = TestClient(mount(collection_router(allowed=allowed)))
    assert options(client, "/items").headers["Allow"] == "GET, OPTIONS, HEAD"
    assert sorted(seen) == [("GET", None), ("POST", None)]


def test_create_and_list_take_their_own_allow_hook():
    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/items", response=List[ItemOut])
    @scrudful_list(
        href_func=lambda request, item: f"/items/{item['id']}",
        allowed=lambda request, method, obj: False,
        **LINKS,
    )
    def list_items(request):
        return []

    @router.post("/items", response={201: ItemOut})
    @scrudful_create(
        location_func=lambda request, obj: "/items/1",
        allowed=lambda request, method, obj: True,
        **LINKS,
    )
    def create_item(request, payload: ItemIn):
        return {"id": 1, "name": payload.name, "version": 1}

    allow = options(TestClient(mount(router)), "/items").headers["Allow"]
    assert allow == "POST, OPTIONS"


@pytest.mark.parametrize(
    "factory, required",
    [
        (scrudful, {}),
        (scrudful_create, {"location_func": lambda request, obj: "/"}),
        (scrudful_list, {"href_func": lambda request, item: "/"}),
    ],
)
def test_allowed_must_be_callable(factory, required):
    with pytest.raises(TypeError, match="allowed must be callable"):
        factory(allowed="GET", **required)


def test_described_operation_uses_its_auth():
    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(load=Store().load, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    @router.delete("/{pk}", auth=KeyAuth(), response={204: None})
    @described(Entity())
    def delete_item(request, pk: int):
        return 204, None

    client = TestClient(mount(router))
    assert options(client, "/1").headers["Allow"] == "GET, OPTIONS, HEAD"
    assert (
        options(client, "/1", headers={"X-API-Key": ALICE}).headers["Allow"]
        == "GET, DELETE, OPTIONS, HEAD"
    )


def test_router_mounted_twice_uses_each_mounts_auth():
    router_options, _, item = split_options({})
    router = ScrudfulRouter(**router_options)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/open", router, url_name_prefix="open")
    api.add_router("/locked", router, auth=KeyAuth(), url_name_prefix="locked")
    client = TestClient(api)
    assert options(client, "/open/1").headers["Allow"] == "GET, OPTIONS, HEAD"
    assert options(client, "/locked/1").headers["Allow"] == "OPTIONS"
    alice = options(client, "/locked/1", headers={"X-API-Key": ALICE})
    assert alice.headers["Allow"] == "GET, OPTIONS, HEAD"


SESSION_API, _ = make_api(auth=django_auth)
urlpatterns += [path("session/", SESSION_API.urls)]


@pytest.mark.urls(__name__)
def test_cookie_auth_checks_skip_csrf_for_allow(django_user_model, settings, db):
    """The per-method checks of a cookie ``auth=``, CSRF-checked on the PUT
    itself, don't fail CSRF for the OPTIONS: ``Allow`` is about authorization.
    And under Django's URL resolver the HSR link reverses in the namespace."""
    settings.ALLOWED_HOSTS = ["a.example"]
    client = Client(enforce_csrf_checks=True, HTTP_HOST="a.example")
    assert client.options("/session/1").headers["Allow"] == "OPTIONS"
    client.force_login(django_user_model.objects.create_user("carol"))
    assert client.put("/session/1", "{}", "application/json").status_code == 403
    r = client.options("/session/1")
    assert (
        options_204_problems(
            r.status_code, dict(r.items()), r.content, {"GET", "HEAD", "PUT", "OPTIONS"}
        )
        == []
    )
    assert (
        hsr_link_problems(dict(r.items()), "https://api.example/session/_hsr/pk") == []
    )


@pytest.mark.urls(__name__)
def test_options_under_parameterized_prefixes(client, settings, db):
    """The prefixes' parameters reach the hooks, the path's are validated for
    the loader, and the HSR link reverses with the prefixes'."""
    settings.ALLOWED_HOSTS = ["a.example"]
    r = client.options("/t/acme/orgs/7/items/1", HTTP_HOST="a.example")
    expected = {"GET", "HEAD", "OPTIONS"}
    assert (
        options_204_problems(r.status_code, dict(r.items()), r.content, expected) == []
    )
    target = "http://a.example/t/acme/orgs/7/items/_hsr/pk"
    assert hsr_link_problems(dict(r.items()), target) == []
    # Store().load raises a 404 for a missing item: the pk reached it as an int.
    missing = client.options("/t/acme/orgs/7/items/3", HTTP_HOST="a.example")
    assert missing.status_code == 404
    assert hsr_link_problems(dict(missing.items()), target) == []


def test_hooks_get_the_urls_parameters():
    seen = []

    def before(request, **params):
        seen.append(params)

    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(load=Store().load, before=before, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/orgs/{org}", router)
    client = TestClient(api)
    assert client.get("/orgs/7/1").status_code == 200
    assert options(client, "/orgs/7/1").headers["Allow"] == "GET, OPTIONS, HEAD"
    # The GET's, then the OPTIONS' (loading, then checking GET): the same.
    assert seen == [{"org": "7", "pk": "1"}] * 3


# --- the ninja internals OPTIONS relies on --------------------------------------


@pytest.mark.parametrize(
    "owner, method",
    [
        ("Operation", "_run_authentication"),
        ("AsyncOperation", "_run_authentication"),
        ("NinjaAPI", "_get_bound_routers"),
        ("ParamModel", "resolve"),
        ("PathModel", "get_request_data"),
    ],
)
def test_options_needs_ninjas_internals(monkeypatch, fresh_ninja_code, owner, method):
    cls = {
        "Operation": ninja.operation.Operation,
        "AsyncOperation": ninja.operation.AsyncOperation,
        "NinjaAPI": NinjaAPI,
        "ParamModel": ninja.params.models.ParamModel,
        "PathModel": ninja.params.models.PathModel,
    }[owner]
    if owner == "AsyncOperation":
        api = make_async_api()[0]
        send = async_options
    else:
        api = make_api()[0]
        client = TestClient(api)

        def send(api, path):
            return options(client, path)

    assert send(api, "/1").status_code == 204
    original = vars(cls)[method]
    if isinstance(original, classmethod):
        replaced = classmethod(wrapping(original.__func__))
    else:
        replaced = wrapping(original)
    monkeypatch.setattr(cls, method, replaced)
    with pytest.raises(ConfigError, match=rf"rely on ninja's {owner}\.{method}: "):
        send(api, "/1")


@pytest.mark.parametrize("shape", [[], {}])
def test_options_needs_the_bound_path_operations(monkeypatch, shape):
    api, _ = make_api()
    client = TestClient(api)
    assert client.urls  # the URL patterns are built, and kept
    for bound in api._get_bound_routers():
        monkeypatch.setattr(bound, "path_operations", shape)
    with pytest.raises(ConfigError, match=r"BoundRouter\.path_operations"):
        options(client, "/1")


def test_options_needs_the_path_models(monkeypatch):
    api, router = make_api()
    [get] = [
        op for op in router.path_operations["/{pk}"].operations if "GET" in op.methods
    ]
    monkeypatch.setattr(get, "models", None)
    with pytest.raises(ConfigError, match=r"Operation\.models"):
        options(TestClient(api), "/1")


def test_options_needs_the_auth_callbacks(monkeypatch):
    api, _ = make_api()
    client = TestClient(api)
    assert client.urls  # the URL patterns are built, and kept
    for bound in api._get_bound_routers():
        for path_view in bound.path_operations.values():
            for operation in path_view.operations:
                if "GET" in operation.methods:
                    monkeypatch.setattr(operation, "auth_callbacks", object())
    with pytest.raises(ConfigError, match=r"Operation\.auth_callbacks"):
        options(client, "/1")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_without_source_the_options_fingerprints_are_checked(
    monkeypatch, fresh_ninja_code, asynchronous
):
    import scrudful.ninja

    monkeypatch.setattr(scrudful.ninja, "_source_digest", lambda func: None)
    if asynchronous:
        r = async_options(make_async_api(auth=KeyAuth())[0], "/1", headers=BOB_KEY)
    else:
        r = options(TestClient(make_api(auth=KeyAuth())[0]), "/1", headers=BOB_KEY)
    assert r.status_code == 204 and r.headers["Allow"] == "GET, PUT, OPTIONS, HEAD"
    cls = ninja.params.models.ParamModel
    monkeypatch.setattr(cls, "resolve", classmethod(wrapping(cls.resolve.__func__)))
    with pytest.raises(ConfigError, match=r"ninja's ParamModel\.resolve"):
        options(TestClient(make_api()[0]), "/1")
