"""The HTTP Schema Resource of a ``ScrudfulRouter`` path, built without a request."""

import dataclasses
import functools
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
from django.http import Http404, HttpResponseForbidden
from django.test import Client, override_settings
from ninja import NinjaAPI, Router, Schema, Status
from ninja.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigError,
    HttpError,
)
from ninja.renderers import JSONRenderer
from ninja.security import APIKeyHeader, django_auth
from ninja.throttling import AnonRateThrottle
from django.urls import path
from ninja.testing import TestAsyncClient, TestClient

from scrudful._django import scrudful_setting
from scrudful._hsr import hsr_etag, serialize_hsr
from scrudful.entity import HsrConfigError
from scrudful.problem import PROBLEM_JSON
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
    status_entry_problems,
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
#: (the router- and API-level options); every other key goes to the
#: ``@scrudful`` decorators.
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
    everything = {"GET", "PUT", "OPTIONS"}
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


def test_a_context_type_callable_returning_nothing_is_config_error():
    """A non-JSON context needs a type, which a callable can't be checked for
    when decorating: building the HSR raises ``ConfigError`` naming the
    operation and the entry, not a bare ``ValueError``."""
    csv = Entity(
        "https://api.example/schemas/item.csv",
        context_link_or_func="https://api.example/contexts/item",
        context_type_or_func=lambda request, obj: None,
        media_type="text/csv",
    )
    _, router = make_api(responses={200: csv})
    with pytest.raises(ConfigError, match=r"get_item\(\)'s 200 response.*needs a type"):
        http_schema_resource(router, "/{pk}")


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


NOT_FOUND = Entity(
    schema_link_or_func="https://api.example/schemas/not-found",
    context_link_or_func="https://api.example/contexts/not-found",
)
FORBIDDEN = Entity(
    schema_link_or_func="https://api.example/schemas/forbidden",
    context_link_or_func="https://api.example/contexts/forbidden",
)
UNAUTHORIZED = Entity(
    schema_link_or_func="https://api.example/schemas/unauthorized",
    context_link_or_func="https://api.example/contexts/unauthorized",
)


def declared_errors_api(with_api_entity: bool) -> tuple[NinjaAPI, ScrudfulRouter]:
    """A GET declaring error statuses in ``responses=`` (not in ninja's
    ``response=``) beside its ``error``, ninja's auth and, optionally, an
    API-wide entity. ``/404`` and ``/403`` raise those statuses; a request
    without a key gets ninja's own 401."""

    def load(request, pk: int):
        if pk in (403, 404):
            raise HttpError(pk, "no")
        return {"id": pk, "name": "x"}

    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/{pk}", response=ItemOut, auth=KeyAuth())
    @scrudful(
        load=load,
        responses={404: NOT_FOUND, 403: FORBIDDEN, 401: UNAUTHORIZED},
        error=OP_ERROR,
        **LINKS,
    )
    def get_item(request, pk: int, obj):
        return obj

    api = mount(router)
    if with_api_entity:
        add_error_links(api, API_ERROR)
    return api, router


@pytest.mark.parametrize("with_api_entity", [True, False], ids=["api-wide", "none"])
def test_a_declared_error_status_has_its_entry_and_wins_on_the_wire(with_api_entity):
    """Each error status of ``responses=`` gets an entry of its own, with its
    entity, over ``default`` and over the API-wide 401 and 403 entries; every
    response with that status, ninja's own 401 included, carries it."""
    api, router = declared_errors_api(with_api_entity)
    hsr = http_schema_resource(router, "/{pk}")
    responses = hsr["get"]["responses"]
    for status, entity in ((404, NOT_FOUND), (403, FORBIDDEN), (401, UNAUTHORIZED)):
        assert responses[str(status)]["content"] == entity_content(entity)
        assert responses[str(status)]["description"]
    assert responses["default"]["content"] == entity_content(OP_ERROR)
    if with_api_entity:  # ninja's own 422 and 429 keep the API-wide entity
        assert responses["422"]["content"] == entity_content(API_ERROR)
    client = TestClient(api)
    assert client.get("/_hsr/pk").json() == hsr
    for url, headers, status in (
        ("/404", BOB_KEY, 404),
        ("/403", BOB_KEY, 403),
        ("/1", {}, 401),
    ):
        response = client.get(url, headers=headers)
        assert response.status_code == status
        response_headers = dict(response.headers)
        assert status_entry_problems("GET", status, hsr) == []
        assert link_problems("GET", status, response_headers, True, hsr) == []


@pytest.mark.parametrize("with_api_entity", [True, False], ids=["api-wide", "none"])
def test_a_declared_problem_details_422_reshapes_ninjas_validation_body(
    with_api_entity,
):
    invalid = Entity(
        "https://api.example/schemas/invalid",
        context_link_or_func="https://api.example/contexts/invalid",
        media_type=PROBLEM_JSON,
    )
    api, router = make_api(responses={422: invalid})
    if with_api_entity:
        add_error_links(api, API_ERROR)
    hsr = http_schema_resource(router, "/{pk}")
    assert hsr["get"]["responses"]["422"]["content"] == {
        PROBLEM_JSON: entity_content(invalid)["application/json"]
    }
    response = TestClient(api).get("/not-a-number")
    assert response.status_code == 422
    assert response.headers["Content-Type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["status"] == 422 and isinstance(body["errors"], list)
    assert link_problems("GET", 422, dict(response.headers), True, hsr) == []


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


@dataclasses.dataclass
class LinkBuilder:
    """A link callable object: a dataclass with ``eq`` is unhashable, and so
    is an ``Entity`` holding it."""

    path: str

    def __call__(self, request: Any, obj: Any) -> str:
        return "https://api.example" + self.path


def test_an_unhashable_api_wide_entity_is_described_and_served():
    """The HSRs are kept by the entity they describe, by identity: an
    unhashable one works end to end (GET, ``http_schema_resource`` and
    OPTIONS, whose HSR link is served), and agrees with the wire error."""
    error = Entity(
        schema_link_or_func=LinkBuilder("/schemas/api-error"),
        context_link_or_func="https://api.example/contexts/api-error",
    )
    with pytest.raises(TypeError):
        hash(error)
    api, router = make_api()
    add_error_links(api, error)
    client = TestClient(api)
    assert client.get("/1").status_code == 200
    response = options(client, "/1")
    assert response.status_code == 204
    assert hsr_link_problems(dict(response.headers), f"{BASE_URL}_hsr/pk") == []
    served = client.get("/_hsr/pk")
    assert served.status_code == 200
    hsr = json.loads(served.content)
    assert hsr == http_schema_resource(router, "/{pk}")
    assert hsr["get"]["responses"]["default"]["content"] == entity_content(API_ERROR)
    response = client.get("/1", headers={"If-Match": "garbage"})
    assert response.status_code == 400
    assert link_problems("GET", 400, dict(response.headers), True, hsr) == []
    # Kept: the same document is built once.
    assert router._hsr("/{pk}") is router._hsr("/{pk}")


def test_operation_error_without_api_wide_entity():
    _, router = make_api(error=OP_ERROR)
    responses = http_schema_resource(router, "/{pk}")["get"]["responses"]
    assert responses["default"]["content"] == entity_content(OP_ERROR)
    # ninja's own 401, 403, 422 and 429 carry the operation's, as the
    # operation's own do: under default.
    assert not {"401", "403", "422", "429"} & responses.keys()


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


def test_each_api_mounting_a_router_serves_its_own_error_entries():
    """The ``default`` entry is the API-wide entity of the API serving the
    HSR route: one router on an API with ``add_error_links`` and on one
    without serves each its own document. The first's 404 agrees with its
    entry; the second's carries no entity, and its entry describes none."""

    def load(request, pk):
        raise HttpError(404, "gone")

    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(load=load, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    with_links, without = mount(router), mount(router)
    add_error_links(with_links, API_ERROR)
    for api, content in ((with_links, entity_content(API_ERROR)), (without, None)):
        client = TestClient(api)
        hsr = client.get("/_hsr/pk").json()
        assert hsr["get"]["responses"]["default"].get("content") == content
        assert http_schema_resource(router, "/{pk}", api=api) == hsr
        response = client.get("/1")
        assert response.status_code == 404
        if content is not None:
            headers = dict(response.headers)
            assert link_problems("GET", 404, headers, True, hsr) == []
        else:
            assert "describedby" not in response.headers["Link"]
    # Without api=, the APIs mounting it disagree: say which.
    with pytest.raises(ConfigError, match="api="):
        http_schema_resource(router, "/{pk}")


def test_an_api_mounting_the_router_after_a_build_is_seen():
    """``add_router`` is ninja's, so no cache is cleared when an API mounts
    the router: the document is looked up by the entity it was built with."""
    api, router = make_api()
    add_error_links(api, API_ERROR)
    other = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    add_error_links(other, OP_ERROR)
    default = http_schema_resource(router, "/{pk}")["get"]["responses"]["default"]
    assert default["content"] == entity_content(API_ERROR)
    other.add_router("/again", router)
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


SCHEMA_ALONE = "https://api.example/schemas/item"


@pytest.mark.parametrize(
    "decorate",
    [
        lambda: scrudful(schema_link_or_func=SCHEMA_ALONE),
        lambda: scrudful_create(
            location_func=lambda request, obj: "/1",
            schema_link_or_func=SCHEMA_ALONE,
        ),
        lambda: scrudful_list(
            href_func=lambda request, item: "/1", schema_link_or_func=SCHEMA_ALONE
        ),
        lambda: scrudful_list(
            href_func=lambda request, item: "/1",
            list_schema_link_or_func=SCHEMA_ALONE,
        ),
    ],
    ids=["scrudful", "scrudful_create", "scrudful_list item", "scrudful_list"],
)
def test_a_schema_link_without_a_context_link_is_refused_when_decorating(decorate):
    """The decorators build their entities, so the HSR's missing
    x-rdf-context is refused before any operation is built, as ninja's
    configuration error, chained to the shared ``HsrConfigError``."""
    with pytest.raises(ConfigError, match="no RDF context link") as raised:
        decorate()
    assert isinstance(raised.value.__cause__, HsrConfigError)


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
    with pytest.raises(ConfigError, match="base_url"):  # not a str: as relative
        ScrudfulRouter(base_url=1)
    settings.SCRUDFUL = {"BASE_URL": "relative/"}
    _, router = make_api(base_url=None)
    with pytest.raises(ConfigError, match="BASE_URL"):
        http_schema_resource(router, "/{pk}")


@pytest.mark.parametrize("value", ["", "  "])
def test_empty_hsr_cache_control_is_rejected(settings, value):
    with pytest.raises(ConfigError, match="hsr_cache_control"):
        ScrudfulRouter(hsr_cache_control=value)
    settings.SCRUDFUL = {"HSR_CACHE_CONTROL": value}
    with pytest.raises(ConfigError, match="HSR_CACHE_CONTROL"):
        TestClient(make_api()[0]).get("/_hsr/pk")


def _raising_hsr_url(path):
    raise LookupError(f"no HSR URL registered for {path!r}")


@pytest.mark.parametrize(
    "hsr_url, error",
    [(_raising_hsr_url, LookupError), (lambda path: "/relative", ConfigError)],
    ids=["raises", "relative"],
)
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_an_hsr_link_that_cant_be_resolved_fails_before_the_operation(
    hsr_url, error, asynchronous
):
    """The link is resolved before the operation runs, so a misconfigured
    one fails the request before the handler writes anything."""
    writes = []
    router = ScrudfulRouter(base_url=BASE_URL, hsr_url=hsr_url)
    create = scrudful_create(
        location_func=lambda request, obj: f"/things/{obj['name']}", **LINKS
    )
    if asynchronous:

        @router.post("/things", response={201: ItemIn})
        @create
        async def add(request, payload: ItemIn):
            writes.append(payload.name)
            return {"name": payload.name}

        def post():
            return async_to_sync(TestAsyncClient(mount(router)).post)(
                "/things", json={"name": "a"}
            )

    else:

        @router.post("/things", response={201: ItemIn})
        @create
        def add(request, payload: ItemIn):
            writes.append(payload.name)
            return {"name": payload.name}

        def post():
            return TestClient(mount(router)).post("/things", json={"name": "a"})

    with pytest.raises(error):
        post()
    assert writes == []


def test_the_hsr_link_is_resolved_once_per_request():
    calls = []

    def hsr_url(path):
        calls.append(path)
        return "https://docs.example/hsr/item"

    client = TestClient(make_api(hsr_url=hsr_url)[0])
    for send in (lambda: client.get("/1"), lambda: options(client, "/1")):
        calls.clear()
        response = send()
        assert response.status_code in (200, 204)
        target = "https://docs.example/hsr/item"
        assert hsr_link_problems(dict(response.headers), target) == []
        assert calls == ["/{pk}"]


@pytest.mark.parametrize("returned", [None, "", "/hsr/{pk}", 3])
def test_hsr_url_must_return_an_absolute_url(rf, returned):
    router = target_router("a", hsr_route=False, hsr_url=lambda path: returned)
    with pytest.raises(ConfigError, match="must return an absolute URL"):
        _hsr_target(rf.get("/1/target"), router, "/{pk}")


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


def test_router_options_are_checked_when_set():
    def hsr_url(path):
        return f"https://docs.example{path}"

    router = ScrudfulRouter(base_url=BASE_URL)
    with pytest.raises(ConfigError, match="base_url"):
        router.base_url = "not-absolute"
    with pytest.raises(TypeError, match="hsr_route must be a bool"):
        router.hsr_route = "no"
    with pytest.raises(ConfigError, match=re.escape("hsr_route=False needs hsr_url=")):
        router.hsr_route = False
    with pytest.raises(TypeError, match="hsr_url must be a callable"):
        router.hsr_url = "https://docs.example/"
    with pytest.raises(ConfigError, match="hsr_cache_control"):
        router.hsr_cache_control = ""
    with pytest.raises(TypeError, match="hsr_cache_control must be a str"):
        router.hsr_cache_control = 300
    with pytest.raises(TypeError, match="allowed must be a callable"):
        router.allowed = True
    assert (router.base_url, router.hsr_route, router.hsr_url) == (BASE_URL, None, None)
    assert (router.hsr_cache_control, router.allowed) == (None, None)
    router.hsr_url = hsr_url
    router.hsr_route = False
    with pytest.raises(ConfigError, match=re.escape("hsr_route=False needs hsr_url=")):
        router.hsr_url = None
    assert (router.hsr_route, router.hsr_url) == (False, hsr_url)


def test_a_new_base_url_discards_the_built_hsrs():
    _, router = make_api()
    assert "https://api.example/schemas/item" in json.dumps(
        http_schema_resource(router, "/{pk}")
    )
    router.base_url = "https://other.example/"
    assert "https://other.example/schemas/item" in json.dumps(
        http_schema_resource(router, "/{pk}")
    )


def test_hsr_route_is_set_before_the_operations():
    """A path's HSR route is added with its first operation: turning the
    option over later would leave the router's paths half one way."""
    _, router = make_api()
    with pytest.raises(ConfigError, match="before adding"):
        router.hsr_route = True
    assert router.hsr_route is None


def test_a_settings_change_discards_the_built_hsrs(settings):
    settings.SCRUDFUL = {"BASE_URL": "https://one.example/"}
    _, router = make_api(base_url=None)
    assert "https://one.example/schemas/item" in json.dumps(
        http_schema_resource(router, "/{pk}")
    )
    settings.SCRUDFUL = {"BASE_URL": "https://two.example/"}
    assert "https://two.example/schemas/item" in json.dumps(
        http_schema_resource(router, "/{pk}")
    )


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


def test_hsr_etag_follows_the_document():
    """After the document changes, the old ETag is stale: a 200 with the new
    document and a new ETag, not a 304."""
    api, _ = make_api()
    client = TestClient(api)
    before = client.get("/_hsr/pk")
    add_error_links(api, API_ERROR)
    after = client.get("/_hsr/pk", headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert after.content != before.content
    assert after.headers["ETag"] != before.headers["ETag"]
    assert after.headers["ETag"] == hsr_etag(after.content)
    again = client.get("/_hsr/pk", headers={"If-None-Match": after.headers["ETag"]})
    assert again.status_code == 304


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


def test_each_mount_keeps_the_path_views_scrudful_state():
    """ninja clones each path's ``PathView`` per mount: the clone of a path
    and of an HSR route keep what scrudful set on the router's."""
    from scrudful.ninja import _ScrudfulPathView

    api, router = make_api()
    expected = {
        "/{pk}": (router, "/{pk}", False),
        "/_hsr/pk": (router, "/_hsr/pk", True),
    }
    [bound] = [b for b in api._get_bound_routers() if b.template is router]
    for key, values in expected.items():
        for path_view in (router.path_operations[key], bound.path_operations[key]):
            assert isinstance(path_view, _ScrudfulPathView)
            attributes = _ScrudfulPathView._SCRUDFUL_ATTRIBUTES
            assert tuple(getattr(path_view, name) for name in attributes) == values


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


def shadowing_api(where: str) -> NinjaAPI:
    """An API serving a scrudful ``"/{pk}"`` on a ``ScrudfulRouter`` mounted
    at ``""`` after a plain ``"/{kind}/{pk}"``, which would match its HSR
    route ``/_hsr/pk`` first: on a sibling router mounted before it, or on
    the API itself (its default router, which ninja mounts first)."""
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    owner = api if where == "api" else Router()

    @owner.get("/{kind}/{pk}")
    def kind_item(request, kind: str, pk: str):
        return {"plain": f"{kind}/{pk}"}

    if where == "sibling":
        api.add_router("", owner)
    router = ScrudfulRouter(base_url=BASE_URL)
    _, _, item = split_options({})

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    api.add_router("", router)
    return api


@pytest.mark.parametrize("where", ["sibling", "api"])
def test_a_shadowed_hsr_route_is_a_config_error(where):
    """Django tries the API's patterns in order, so an earlier router's (or
    the API's own) path matching ``/_hsr/<name>`` would answer the HSR link
    with something that isn't the HSR: building the API's URLs refuses it,
    naming the operation and the way out."""
    api = shadowing_api(where)
    message = (
        r"the HTTP Schema Resource route '_hsr/pk' of '/\{pk\}'.*"
        r"GET '<kind>/<pk>' \(.*kind_item\(\)\).*hsr_route=False.*hsr_url="
    )
    with pytest.raises(ConfigError, match=message):
        api.urls
    with pytest.raises(ConfigError, match=message):
        TestClient(api).get("/_hsr/pk")


def test_an_earlier_hsr_route_of_the_same_name_shadows_it():
    """Two ``ScrudfulRouter`` mounted at one prefix with one HSR route name:
    the second's would be the first's."""
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    _, _, item = split_options({})
    for _ in range(2):
        router = ScrudfulRouter(base_url=BASE_URL)
        router.get("/{pk}", response=ItemOut)(scrudful(**item)(pk_handler))
        api.add_router("", router)
    with pytest.raises(
        ConfigError,
        match=r"shadowed by the HTTP Schema Resource route .*of another ScrudfulRouter",
    ):
        api.urls


def test_routes_of_other_shapes_dont_shadow_the_hsr_route():
    """A sibling router before it, a prefix with a parameter and paths that
    can't match ``_hsr/<name>``: served."""
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")

    @api.get("/{kind}/{pk}/{rest}")
    def deeper(request, kind: str, pk: str, rest: str):
        return {}

    @api.get("/{int:number}/{pk}")
    def numbered(request, number: int, pk: str):
        return {}

    router = ScrudfulRouter(base_url=BASE_URL)
    _, _, item = split_options({})

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, org: str, pk: int, obj):
        return obj

    api.add_router("/orgs/{org}", router)
    client = TestClient(api)
    r = client.get("/orgs/acme/_hsr/pk")
    assert hsr_get_problems(r.status_code, dict(r.headers), r.content) == []
    assert json.loads(r.content) == http_schema_resource(router, "/{pk}")


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
    """The setting's callable gets the route Django resolved the request to;
    for an API that isn't in the root URLconf, under ninja's ``TestClient``,
    the route below the API's root."""
    seen = []

    def hsr_url(route):
        seen.append(route)
        return "https://docs.example/s"

    settings.SCRUDFUL = {"HSR_URL": hsr_url}
    api = mount(target_router("a"))
    response = TestClient(api).get("/1")
    assert hsr_link_problems(dict(response.headers), "https://docs.example/s") == []
    assert seen == ["<pk>"]


def test_the_route_a_path_got_stays_its_target(settings):
    """Whether a path has an HSR route is decided when its first operation
    is added: the setting turned off later neither removes the route nor
    breaks the link to it, and one turned on later adds no route."""
    router = target_router("a")
    client = TestClient(mount(router))
    settings.SCRUDFUL = {"HSR_ROUTE": False}
    assert client.get("/1/target").json() == {"target": "http://testlocation/_hsr/pk"}
    assert client.get("/_hsr/pk").status_code == 200
    settings.SCRUDFUL = {"HSR_ROUTE": False, "HSR_URL": lambda p: "https://x/"}
    late = target_router("late")
    settings.SCRUDFUL = {}
    with pytest.raises(ConfigError, match="no HTTP Schema Resource route"):
        TestClient(mount(late)).get("/1/target")


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


def hsr_url_setting(route):
    """A project-wide ``SCRUDFUL["HSR_URL"]``, of the route resolved."""
    return "https://docs.example/hsr/" + route.replace("<", "").replace(">", "")


SETTING_ITEMS = target_router("items")
SETTING_USERS = target_router("users")
SETTING_API = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
SETTING_API.add_router("/items", SETTING_ITEMS)
SETTING_API.add_router("/users", SETTING_USERS)
#: One router, mounted twice in one API.
TWICE = target_router("twice")
TWICE_API = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
TWICE_API.add_router("/plain", TWICE, url_name_prefix="plain")
TWICE_API.add_router("/other", TWICE, url_name_prefix="other")
TWICE_API.add_router("/orgs/{org}/items", TWICE, url_name_prefix="orgs")
urlpatterns += [path("setting/", SETTING_API.urls), path("twice/", TWICE_API.urls)]


@pytest.mark.urls(__name__)
def test_the_hsr_url_setting_tells_routers_apart(client, settings, db):
    """Two routers' ``"/{pk}"``: the setting's callable gets each one's full
    route (the URLconf's, the API's and the router's prefixes), so each
    resource links to its own HTTP Schema Resource."""
    settings.SCRUDFUL = {"HSR_URL": hsr_url_setting}
    for prefix in ("items", "users"):
        target = f"https://docs.example/hsr/setting/{prefix}/pk"
        for response in (
            client.get(f"/setting/{prefix}/1"),
            client.options(f"/setting/{prefix}/1"),
        ):
            assert hsr_link_problems(dict(response.items()), target) == [], prefix


@pytest.mark.urls(__name__)
def test_a_router_mounted_twice_links_the_mount_of_the_request(client, settings, db):
    settings.ALLOWED_HOSTS = ["a.example"]
    for mount_path in ("plain", "other", "orgs/7/items"):
        target = f"http://a.example/twice/{mount_path}/_hsr/pk"
        url = f"/twice/{mount_path}/1"
        assert client.get(f"{url}/target", HTTP_HOST="a.example").json() == {
            "target": target
        }
        response = client.options(url, HTTP_HOST="a.example")
        assert hsr_link_problems(dict(response.items()), target) == [], mount_path
        r = client.get(f"/twice/{mount_path}/_hsr/pk", HTTP_HOST="a.example")
        assert hsr_get_problems(r.status_code, dict(r.items()), r.content) == []
    # Under ninja's TestClient too.
    test_client = TestClient(TWICE_API)
    for mount_path in ("plain", "other", "orgs/7/items"):
        target = test_client.get(f"/{mount_path}/1/target").json()["target"]
        assert target == f"http://testlocation/{mount_path}/_hsr/pk"
    # The setting gets the mount's route.
    settings.SCRUDFUL = {"HSR_URL": hsr_url_setting}
    for mount_path, route in (
        ("plain", "plain/pk"),
        ("orgs/7/items", "orgs/org/items/pk"),
    ):
        response = client.get(f"/twice/{mount_path}/1", HTTP_HOST="a.example")
        target = f"https://docs.example/hsr/twice/{route}"
        assert hsr_link_problems(dict(response.items()), target) == [], mount_path


def two_paths_router(name: str) -> ScrudfulRouter:
    """``/search`` and ``/{pk}``, each with its own entity."""
    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/search", response=ItemOut)
    @scrudful(
        schema_link_or_func=f"https://api.example/schemas/{name}-search",
        context_link_or_func=f"https://api.example/contexts/{name}-search",
    )
    def search(request):
        return {"id": 1, "name": "x", "version": 1}

    @router.get("/{pk}", response=ItemOut)
    @scrudful(load=lambda request, pk: {"id": pk, "name": "x", "version": 1}, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    return router


def without_wraps(func):
    """An operation decorator that doesn't use ``functools.wraps``: each
    operation it wraps is named ``wrapper``."""

    def wrapper(request, *args, **kwargs):
        return func(request, *args, **kwargs)

    return wrapper


#: Operation decorators without functools.wraps, on a router and on an API.
NO_WRAPS_ROUTER = two_paths_router("router")
NO_WRAPS_ROUTER.add_decorator(without_wraps, mode="operation")
NO_WRAPS_ROUTER_API = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
NO_WRAPS_ROUTER_API.add_router("/r", NO_WRAPS_ROUTER)
NO_WRAPS_API_ROUTER = two_paths_router("api")
NO_WRAPS_API = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
NO_WRAPS_API.add_decorator(without_wraps, mode="operation")
NO_WRAPS_API.add_router("/r", NO_WRAPS_API_ROUTER, url_name_prefix="one")
NO_WRAPS_API.add_router("/s", NO_WRAPS_API_ROUTER, url_name_prefix="two")
urlpatterns += [path("nw1/", NO_WRAPS_ROUTER_API.urls), path("nw2/", NO_WRAPS_API.urls)]


@pytest.mark.urls(__name__)
def test_hsr_links_dont_depend_on_operation_decorators_keeping_names(client, db):
    """An ``add_decorator(mode="operation")`` decorator without
    ``functools.wraps`` renames the operations it wraps: the HSR routes'
    names are scrudful's own, per mount, so each link still reverses to its
    path's HSR route."""
    for prefix in ("/nw1/r", "/nw2/r", "/nw2/s"):
        for url, name in ((f"{prefix}/1", "pk"), (f"{prefix}/search", "search")):
            target = f"https://api.example{prefix}/_hsr/{name}"
            for response in (client.get(url), client.options(url)):
                assert response.status_code in (200, 204), url
                assert hsr_link_problems(dict(response.items()), target) == [], url
            r = client.get(f"{prefix}/_hsr/{name}")
            assert hsr_get_problems(r.status_code, dict(r.items()), r.content) == []
    # Under ninja's TestClient too.
    test_client = TestClient(NO_WRAPS_API)
    response = test_client.get("/s/1")
    assert hsr_link_problems(dict(response.headers), f"{BASE_URL}s/_hsr/pk") == []


#: The documented project-wide map, keyed by the routes Django resolves.
DOC_HSR_URLS = {
    "doc/items/<pk>": "https://api.example.com/schemas/items/item",
    "doc/items/search": "https://api.example.com/schemas/items/search",
}
DOC_SCRUDFUL = {"HSR_ROUTE": False, "HSR_URL": DOC_HSR_URLS.__getitem__}
with override_settings(SCRUDFUL=DOC_SCRUDFUL):
    DOC_ROUTER = two_paths_router("doc")
DOC_API = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
DOC_API.add_router("/items", DOC_ROUTER)
urlpatterns += [path("doc/", DOC_API.urls)]


@pytest.mark.urls(__name__)
def test_the_hsr_url_setting_gets_one_route_under_django_and_the_test_client(
    client, settings, db
):
    """Under ninja's ``TestClient``, which resolves among the API's patterns,
    the route is prefixed with the API's route in the root URLconf: the
    documented map serves the tests as it serves the app."""
    settings.SCRUDFUL = DOC_SCRUDFUL
    test_client = TestClient(DOC_API)
    for url, key in (
        ("/items/1", "doc/items/<pk>"),
        ("/items/search", "doc/items/search"),
    ):
        for response in (
            client.get(f"/doc{url}"),
            client.options(f"/doc{url}"),
            test_client.get(url),
            test_client.request("OPTIONS", url),
        ):
            assert response.status_code in (200, 204), url
            assert hsr_link_problems(_headers(response), DOC_HSR_URLS[key]) == [], url


def _headers(response: Any) -> dict[str, str]:
    """The header fields of a Django or a ninja test client's response."""
    items = getattr(response, "items", None)
    return dict(items()) if callable(items) else dict(response.headers)


def test_the_hsr_url_setting_needs_a_resolved_request(rf, settings):
    settings.SCRUDFUL = {"HSR_URL": lambda route: "https://docs.example/h"}
    router = target_router("unresolved")
    with pytest.raises(ConfigError, match=r"resolver_match\.route.*has none"):
        _hsr_target(rf.get("/1"), router, "/{pk}")


class Message(Schema):
    detail: str


def test_a_declared_status_the_handler_returns_keeps_its_links():
    """The wrapper gives a response it returns its entity's Links, with the
    loaded object; scrudful doesn't apply them again for the declared
    status (the callable would get ``None``)."""

    def schema(request, obj):
        return f"https://api.example/schemas/gone-{obj['id']}"

    gone = Entity(schema, context_link_or_func="https://api.example/contexts/gone")
    router = ScrudfulRouter(base_url=BASE_URL)

    @router.get("/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(
        load=lambda request, pk: {"id": pk, "name": "x"}, responses={404: gone}, **LINKS
    )
    def get_item(request, pk: int, obj):
        return Status(404, {"detail": "gone"})

    response = TestClient(mount(router)).get("/7")
    assert response.status_code == 404
    assert "<https://api.example/schemas/gone-7>" in response.headers["Link"]


# --- the ninja internals the HSR routes and links rely on ----------------------


@pytest.fixture
def fresh_ninja_code(monkeypatch):
    """The check of ninja's code is memoized per function, and warns once
    without the source: start afresh."""
    import scrudful.ninja

    scrudful.ninja._is_ninja_code.cache_clear()
    monkeypatch.setattr(scrudful.ninja, "_sourceless_warned", False)
    yield
    scrudful.ninja._is_ninja_code.cache_clear()


#: The warning of a ninja install without the source of its internals.
SOURCELESS = "ignore:scrudful can't read the source:RuntimeWarning"


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
        ConfigError, match=rf"relies on ninja's BoundRouter\.{method} here, .*upgrade"
    ):
        make_api()


def test_hsr_routes_need_a_bound_router(monkeypatch, fresh_ninja_code):
    monkeypatch.delattr(ninja.router, "BoundRouter")
    with pytest.raises(ConfigError, match=r"ninja's BoundRouter\._bind_operations"):
        make_api()


@pytest.mark.filterwarnings(SOURCELESS)
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
    # The client keeps the patterns; no response has reversed a route yet.
    assert client.urls
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

#: The HSR link target of ``make_api``'s ``"/{pk}"`` (absolute against its
#: base URL, never the request).
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
            r.status_code, dict(r.headers), r.content, {"GET", "PUT", "OPTIONS"}
        )
        == []
    )
    assert hsr_link_problems(dict(r.headers), PK_TARGET) == []
    assert r.headers["Allow"] == "GET, PUT, OPTIONS"
    assert {"Link", "Allow"} <= exposed(r.headers)


def test_allow_reflects_requester():
    api, _ = make_api(auth=KeyAuth(), before=deny_put_unless_alice)
    client = TestClient(api)
    assert options(client, "/1").headers["Allow"] == "OPTIONS"
    assert (
        options(client, "/1", headers={"X-API-Key": BOB}).headers["Allow"]
        == "GET, OPTIONS"
    )
    assert (
        options(client, "/1", headers={"X-API-Key": ALICE}).headers["Allow"]
        == "GET, PUT, OPTIONS"
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
    assert options(TestClient(api), "/1").headers["Allow"] == "GET, OPTIONS"
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
    assert options(client, "/1").headers["Allow"] == "GET, PUT, OPTIONS"
    assert sorted(seen) == [("GET", "GET", None), ("PUT", "PUT", None)]


def test_async_before_is_honoured():
    api, _ = make_async_api(before=async_deny_put)
    r = async_options(api, "/1")
    assert options_204_problems(r.status_code, dict(r.headers), r.content) == []
    assert "PUT" not in r.headers["Allow"]
    assert r.headers["Allow"] == "GET, OPTIONS"


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
    assert options(TestClient(api), "/1").headers["Allow"] == "GET, OPTIONS"


@pytest.mark.parametrize(
    "error",
    [
        HttpError(401, "no"),
        HttpError(403, "no"),
        AuthenticationError(),
        AuthorizationError(),
        PermissionDenied("no"),
    ],
    ids=["401", "403", "AuthenticationError", "AuthorizationError", "PermissionDenied"],
)
def test_a_denial_an_allowed_hook_raises_means_not_allowed(error):
    """A 401, 403 or ``PermissionDenied`` the hook raises refuses the method,
    as DRF's allowed_func and FastAPI's allowed= do."""

    def allowed(request, method, obj):
        if method == "PUT":
            raise error
        return True

    api, _ = make_api(allowed=allowed)
    r = options(TestClient(api), "/1")
    assert r.status_code == 204
    assert r.headers["Allow"] == "GET, OPTIONS"


@pytest.mark.parametrize(
    "error", [HttpError(404, "gone"), Http404("gone")], ids=["404", "Http404"]
)
def test_a_404_an_allowed_hook_raises_is_the_answer(error):
    def allowed(request, method, obj):
        raise error

    api, _ = make_api(allowed=allowed)
    assert options(TestClient(api), "/1").status_code == 404


def test_another_error_an_allowed_hook_raises_propagates():
    def allowed(request, method, obj):
        if method == "PUT":
            raise HttpError(409, "conflict")
        return True

    api, _ = make_api(allowed=allowed)
    r = options(TestClient(api), "/1")
    assert r.status_code == 409 and r.json() == {"detail": "conflict"}

    def broken(request, method, obj):
        raise LookupError("broken")

    api, _ = make_api(allowed=broken)
    with pytest.raises(LookupError, match="broken"):
        options(TestClient(api), "/1")


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
    """OPTIONS itself is throttled once, as ninja would; computing Allow (a
    check per method) consumes nothing more."""
    api, _ = make_api(throttle=[AnonRateThrottle("2/day")])
    client = TestClient(api)
    assert options(client, "/1").status_code == 204  # one of the two
    assert client.get("/1").status_code == 200  # the per-method checks ran none
    throttled = client.get("/1")
    assert throttled.status_code == 429


def test_options_is_throttled_as_ninja_would(clear_cache):
    api, _ = make_api(throttle=[AnonRateThrottle("1/day")])
    client = TestClient(api)
    assert options(client, "/1").status_code == 204
    throttled = options(client, "/1")
    get = client.get("/1")
    # ninja's own 429, for OPTIONS as for the GET.
    assert throttled.status_code == get.status_code == 429
    assert throttled.content == get.content
    assert throttled.headers["Retry-After"] == get.headers["Retry-After"]


def test_options_is_throttled_with_the_mounts_throttles(clear_cache):
    router_options, _, item = split_options({})
    router = ScrudfulRouter(**router_options)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**item)
    def get_item(request, pk: int, obj):
        return obj

    api = NinjaAPI(
        urls_namespace=f"api-{uuid.uuid4().hex}", throttle=[AnonRateThrottle("9/day")]
    )
    api.add_router("/open", router, url_name_prefix="open")
    api.add_router(
        "/tight", router, throttle=[AnonRateThrottle("1/day")], url_name_prefix="tight"
    )
    client = TestClient(api)
    assert [options(client, "/tight/1").status_code for _ in range(2)] == [204, 429]
    assert [options(client, "/open/1").status_code for _ in range(2)] == [204, 204]


def test_preflight_never_consumes_quota(clear_cache):
    api, _ = make_api(throttle=[AnonRateThrottle("1/day")])
    client = TestClient(api)
    preflight = {
        "Origin": "https://app.example",
        "Access-Control-Request-Method": "PUT",
    }
    for _ in range(3):
        assert options(client, "/1", headers=preflight).status_code == 204
    assert client.get("/1").status_code == 200


def deny(view_func):
    @functools.wraps(view_func)
    def denying(request, *args, **kwargs):
        raise HttpError(403, "Forbidden")

    return denying


def test_operation_mode_decorators_never_gate_options():
    """``add_decorator(mode="operation")`` wraps each operation's function,
    which OPTIONS never calls: neither a preflight nor OPTIONS is refused."""
    api, router = make_api()
    router.add_decorator(deny)
    client = TestClient(api)
    assert client.get("/1").status_code == 403
    preflight = options(
        client,
        "/1",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert preflight.status_code == 204 and preflight.content == b""
    assert "Link" not in preflight.headers
    r = options(client, "/1")
    expected = {"GET", "PUT", "OPTIONS"}
    assert (
        options_204_problems(r.status_code, dict(r.headers), r.content, expected) == []
    )


def test_view_mode_decorators_run_first():
    """``add_decorator(mode="view")`` wraps ``Operation.run`` itself: it runs
    before OPTIONS is answered, a preflight too (documented)."""

    def deny_view(run):
        @functools.wraps(run)
        def denying(request, *args, **kwargs):
            return HttpResponseForbidden()

        return denying

    api, router = make_api()
    router.add_decorator(deny_view, mode="view")
    client = TestClient(api)
    preflight = options(
        client,
        "/1",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert preflight.status_code == options(client, "/1").status_code == 403


def test_options_view_refuses_without_the_run_decorator():
    _, router = make_api()
    [operation] = [
        op
        for op in router.path_operations["/{pk}"].operations
        if "OPTIONS" in op.methods
    ]
    with pytest.raises(RuntimeError, match="Operation.run decorator"):
        operation.view_func(None, pk="1")


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
    expected = {"GET", "POST", "OPTIONS"}
    assert (
        options_204_problems(r.status_code, dict(r.headers), r.content, expected) == []
    )
    assert hsr_link_problems(dict(r.headers), "https://api.example/_hsr/items") == []
    seen = []

    def allowed(request, method, obj):
        seen.append((method, obj))
        return method == "GET"

    client = TestClient(mount(collection_router(allowed=allowed)))
    assert options(client, "/items").headers["Allow"] == "GET, OPTIONS"
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


def test_head_is_listed_only_when_routed_and_checked_per_requester():
    """ninja answers an unrouted HEAD with 405: Allow lists HEAD only for a
    path whose operation lists it, checked with that operation's checks."""
    api, _ = make_api()
    client = TestClient(api)
    assert client.request("HEAD", "/1").status_code == 405
    assert options(client, "/1").headers["Allow"] == "GET, PUT, OPTIONS"

    def no_head_for_bob(request, **params):
        if request.method == "HEAD" and request.auth == BOB:
            raise HttpError(403, "Forbidden")

    router = ScrudfulRouter(base_url=BASE_URL)

    @router.api_operation(["GET", "HEAD"], "/{pk}", response=ItemOut)
    @scrudful(load=Store().load, before=no_head_for_bob, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    client = TestClient(mount(router, auth=KeyAuth()))
    assert client.request("HEAD", "/1", headers={"X-API-Key": ALICE}).status_code == 200
    alice = options(client, "/1", headers={"X-API-Key": ALICE})
    assert (
        options_204_problems(
            alice.status_code,
            dict(alice.headers),
            alice.content,
            {"GET", "HEAD", "OPTIONS"},
        )
        == []
    )
    assert alice.headers["Allow"] == "GET, OPTIONS, HEAD"
    assert options(client, "/1", headers=BOB_KEY).headers["Allow"] == "GET, OPTIONS"
    assert options(client, "/1").headers["Allow"] == "OPTIONS"


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
    assert options(client, "/1").headers["Allow"] == "GET, OPTIONS"
    assert (
        options(client, "/1", headers={"X-API-Key": ALICE}).headers["Allow"]
        == "GET, DELETE, OPTIONS"
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
    assert options(client, "/open/1").headers["Allow"] == "GET, OPTIONS"
    assert options(client, "/locked/1").headers["Allow"] == "OPTIONS"
    alice = options(client, "/locked/1", headers={"X-API-Key": ALICE})
    assert alice.headers["Allow"] == "GET, OPTIONS"


@pytest.mark.parametrize("parent_class", [Router, ScrudfulRouter])
def test_a_parent_routers_auth_decides_a_nested_routers_allow(parent_class):
    """The ``auth=`` a nested router's operations inherit from a parent router
    (not the API) is the one their OPTIONS checks."""
    _, child = make_api()
    parent = parent_class(auth=KeyAuth())
    parent.add_router("/items", child)
    client = TestClient(mount(parent))
    anonymous = options(client, "/items/1")
    assert anonymous.status_code == 204 and anonymous.headers["Allow"] == "OPTIONS"
    alice = options(client, "/items/1", headers={"X-API-Key": ALICE})
    assert alice.headers["Allow"] == "GET, PUT, OPTIONS"
    assert client.get("/items/1").status_code == 401


def test_options_awaits_an_async_load():
    """The object an ``async def`` loader returns reaches the hook, and its
    404 answers the OPTIONS."""
    store, seen = Store(), []

    async def load(request, pk, **params):
        return store.load(request, pk, **params)

    async def allowed(request, method, obj):
        seen.append(obj)
        return True

    api, _ = make_async_api(load=load, allowed=allowed)
    add_error_links(api, API_ERROR)
    r = async_options(api, "/1")
    assert r.status_code == 204 and r.headers["Allow"] == "GET, PUT, OPTIONS"
    assert seen == [store.items[1]] * 2
    r = async_options(api, "/99")
    assert r.status_code == 404
    assert link_problems("OPTIONS", 404, dict(r.headers), True) == []
    assert hsr_link_problems(dict(r.headers), PK_TARGET) == []


def owner_router(asynchronous: bool, owned_status: int = 403, **put: Any) -> Any:
    """GET loads any item; PUT loads it only for its owner (alice), raising
    ``owned_status`` for anyone else. ``calls`` records each PUT load's
    requester and method."""
    store, calls = Store(), []

    def owned(request, pk, **params):
        calls.append((getattr(request, "auth", None), request.method))
        if request.auth != ALICE:
            raise HttpError(owned_status, "not yours")
        return store.load(request, pk, **params)

    async def async_owned(request, pk, **params):
        return owned(request, pk, **params)

    async def async_load(request, pk, **params):
        return store.load(request, pk, **params)

    router = ScrudfulRouter(base_url=BASE_URL)
    get_load = async_load if asynchronous else store.load
    put_load = async_owned if asynchronous else owned
    if asynchronous:

        @router.get("/{pk}", response=ItemOut)
        @scrudful(load=get_load, etag_func=etag, **LINKS)
        async def get_item(request, pk: int, obj):
            return obj

        @router.put("/{pk}", response=ItemOut, **put)
        @scrudful(load=put_load, etag_func=etag, **LINKS)
        async def put_item(request, pk: int, payload: ItemIn, obj):
            return obj

    else:

        @router.get("/{pk}", response=ItemOut)
        @scrudful(load=get_load, etag_func=etag, **LINKS)
        def get_item(request, pk: int, obj):
            return obj

        @router.put("/{pk}", response=ItemOut, **put)
        @scrudful(load=put_load, etag_func=etag, **LINKS)
        def put_item(request, pk: int, payload: ItemIn, obj):
            return obj

    return mount(router, auth=KeyAuth()), calls


def options_of(api: NinjaAPI, asynchronous: bool, path: str, **kwargs: Any) -> Any:
    if asynchronous:
        return async_options(api, path, **kwargs)
    return options(TestClient(api), path, **kwargs)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("owned_status", [401, 403, 404])
def test_options_runs_each_methods_own_load(asynchronous, owned_status):
    """Authorization in a method's own ``load`` shapes ``Allow``: a refusal
    (401, 403 or 404) of PUT's load leaves PUT out for that requester only,
    and the OPTIONS is still a 204 (only the loader's 404 answers it)."""
    api, calls = owner_router(asynchronous, owned_status)
    bob = options_of(api, asynchronous, "/1", headers=BOB_KEY)
    assert bob.status_code == 204 and bob.headers["Allow"] == "GET, OPTIONS"
    assert calls == [(BOB, "PUT")]
    alice = options_of(api, asynchronous, "/1", headers={"X-API-Key": ALICE})
    assert alice.status_code == 204
    assert alice.headers["Allow"] == "GET, PUT, OPTIONS"
    assert calls[-1] == (ALICE, "PUT")


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_options_loads_a_methods_own_object_only_once_it_passed(asynchronous):
    """A method whose ``auth=`` refuses the requester never loads: nothing
    loads for a method the requester may not use."""
    api, calls = owner_router(asynchronous, auth=DenyAll())
    bob = options_of(api, asynchronous, "/1", headers=BOB_KEY)
    assert bob.headers["Allow"] == "GET, OPTIONS"
    assert calls == []


class DenyAll(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request, key):
        return None


def test_options_answers_the_loaders_404_before_any_methods_own_load():
    api, calls = owner_router(False)
    response = options(TestClient(api), "/99", headers=BOB_KEY)
    assert response.status_code == 404
    assert calls == []


def test_options_shares_the_loaders_load():
    """A method with the loader's own ``load`` doesn't load again."""
    store, calls = Store(), []

    def load(request, pk, **params):
        calls.append(request.method)
        return store.load(request, pk, **params)

    api, _ = make_api(load=load)
    response = options(TestClient(api), "/1")
    assert response.headers["Allow"] == "GET, PUT, OPTIONS"
    assert calls == ["GET"]


def test_options_with_an_allowed_hook_runs_no_methods_own_load():
    """An ``allowed`` hook alone decides: a method's own ``load`` doesn't
    run, and the hook gets the loader's object."""
    seen = []

    def allowed(request, method, obj):
        seen.append((method, obj is not None))
        return True

    store, calls = Store(), []

    def owned(request, pk, **params):
        calls.append(request.method)
        raise HttpError(403, "not yours")

    router = ScrudfulRouter(base_url=BASE_URL, allowed=allowed)

    @router.get("/{pk}", response=ItemOut)
    @scrudful(load=store.load, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    @router.put("/{pk}", response=ItemOut)
    @scrudful(load=owned, **LINKS)
    def put_item(request, pk: int, payload: ItemIn, obj):
        return obj

    response = options(TestClient(mount(router)), "/1")
    assert response.headers["Allow"] == "GET, PUT, OPTIONS"
    assert calls == [] and seen == [("GET", True), ("PUT", True)]


@pytest.mark.parametrize("hook", ["before", "after_throttles"])
def test_options_never_runs_after_throttles(hook):
    """``after_throttles`` (a quota, an audit log) never runs for OPTIONS, nor
    shapes its ``Allow``; ``before`` does."""
    calls = []

    def refuse(request, **params):
        calls.append(request.method)
        raise HttpError(403, "no")

    api, _ = make_api(**{hook: refuse})
    client = TestClient(api)
    r = options(client, "/1")
    assert r.status_code == 204
    if hook == "after_throttles":
        assert r.headers["Allow"] == "GET, PUT, OPTIONS" and calls == []
        assert client.get("/1").status_code == 403
        assert calls == ["GET"]
    else:
        assert r.headers["Allow"] == "OPTIONS" and calls


@pytest.mark.parametrize("where", ["before", "auth"])
def test_a_per_method_check_raising_a_404_refuses_the_method(where):
    """ninja's choice (FastAPI's too; DRF answers the OPTIONS with it): a 404
    of a per-method check refuses that method, it doesn't answer the
    OPTIONS; the loader still runs for the method allowed."""

    def check(request, **params):
        if request.method == "PUT":
            raise HttpError(404, "Not Found")
        return "someone"

    calls = []
    store = Store()

    def load(request, pk, **params):
        calls.append(request.method)
        return store.load(request, pk, **params)

    if where == "before":
        api, _ = make_api(
            load=load, before=lambda request, **kw: check(request) and None
        )
    else:
        api, _ = make_api(load=load, auth=check)
    r = options(TestClient(api), "/1")
    assert r.status_code == 204
    assert r.headers["Allow"] == "GET, OPTIONS"
    assert calls == ["GET"]


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
            r.status_code, dict(r.items()), r.content, {"GET", "PUT", "OPTIONS"}
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
    expected = {"GET", "OPTIONS"}
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
    assert options(client, "/orgs/7/1").headers["Allow"] == "GET, OPTIONS"
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
        ("Operation", "_check_throttles"),
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
    pattern = rf"relies on ninja's {owner}\.{method} here, "
    with pytest.raises(ConfigError, match=pattern):
        send(api, "/1")


@pytest.mark.parametrize(
    "owner, method",
    [
        ("Operation", "_run_authentication"),
        ("AsyncOperation", "_run_authentication"),
        ("NinjaAPI", "_get_bound_routers"),
        ("ParamModel", "resolve"),
        ("PathModel", "get_request_data"),
        ("Operation", "_check_throttles"),
        ("BoundRouter", "_bind_operations"),
    ],
)
def test_ninjas_internals_are_checked_when_options_is_added(
    monkeypatch, fresh_ninja_code, owner, method
):
    """At startup, when a path gets its OPTIONS, not on its first OPTIONS:
    an upgraded ninja fails the app, not its OPTIONS requests. The HSR route
    is off, which needs BoundRouter's methods for itself."""
    cls = {
        "Operation": ninja.operation.Operation,
        "AsyncOperation": ninja.operation.AsyncOperation,
        "NinjaAPI": NinjaAPI,
        "ParamModel": ninja.params.models.ParamModel,
        "PathModel": ninja.params.models.PathModel,
        "BoundRouter": ninja.router.BoundRouter,
    }[owner]
    original = vars(cls)[method]
    if isinstance(original, classmethod):
        replaced = classmethod(wrapping(original.__func__))
    else:
        replaced = wrapping(original)
    monkeypatch.setattr(cls, method, replaced)
    with pytest.raises(ConfigError, match=rf"relies on ninja's {owner}\.{method} "):
        make_api(hsr_route=False, hsr_url=lambda path: "https://docs.example/h")


def test_without_the_source_of_ninjas_internals_scrudful_warns_once(
    monkeypatch, fresh_ninja_code
):
    import scrudful.ninja

    monkeypatch.setattr(scrudful.ninja, "_source_digest", lambda func: None)
    with pytest.warns(RuntimeWarning, match="can't read the source") as caught:
        api, _ = make_api()
        make_api()
        assert options(TestClient(api), "/1").status_code == 204
    assert len(caught) == 1


@pytest.mark.parametrize("shape", [[], {}])
def test_options_needs_the_bound_path_operations(monkeypatch, shape):
    api, _ = make_api()
    client = TestClient(api)
    # The URL patterns are built and kept, and the HSR link, resolved first,
    # has the API's resolver.
    assert client.get("/1").status_code == 200
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


def test_options_needs_ninjas_operation_clone(monkeypatch, fresh_ninja_code):
    """The run decorator answering OPTIONS reaches each mount's clone only
    through ``Operation.clone``: checked when the operation is added."""
    cls = ninja.operation.Operation
    monkeypatch.setattr(cls, "clone", wrapping(cls.clone))
    with pytest.raises(ConfigError, match=r"relies on ninja's Operation\.clone here, "):
        make_api()


def test_options_needs_the_bound_routers_throttle(monkeypatch, clear_cache):
    api, _ = make_api(throttle=[AnonRateThrottle("1/day")])
    client = TestClient(api)
    assert client.urls  # the URL patterns are built, and kept
    for bound in api._get_bound_routers():
        monkeypatch.delattr(bound, "throttle")
    with pytest.raises(ConfigError, match=r"BoundRouter\.throttle"):
        options(client, "/1")


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


@pytest.mark.filterwarnings(SOURCELESS)
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
    assert r.status_code == 204 and r.headers["Allow"] == "GET, PUT, OPTIONS"
    cls = ninja.params.models.ParamModel
    monkeypatch.setattr(cls, "resolve", classmethod(wrapping(cls.resolve.__func__)))
    with pytest.raises(ConfigError, match=r"ninja's ParamModel\.resolve"):
        options(TestClient(make_api()[0]), "/1")
