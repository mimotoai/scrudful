"""Links on error responses: errors raised in an operation, ``add_error_links``."""

import functools
import json
import uuid
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.http import Http404, HttpResponse
from django.urls import path
from jsonschema import Draft202012Validator
from ninja import NinjaAPI, Router, Status
from ninja import errors as ninja_errors
from ninja.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigError,
    HttpError,
)
from ninja.security import APIKeyHeader
from ninja.testing import TestAsyncClient, TestClient

from scrudful._hsr import hsr_link
from scrudful.ninja import (
    Entity,
    ScrudfulRouter,
    add_error_links,
    described,
    scrudful,
    scrudful_list,
)
from tests.semantic_http import (
    PROBLEM_DETAILS_SCHEMA,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    undescribed_problems,
    validate_http_schema_resource,
)
from tests.test_ninja_adapter import (
    ALICE,
    CONTEXT_LINK,
    LINKS,
    MISSING,
    SCHEMA_LINK,
    TEST_BASE_URL,
    Deny,
    ItemIn,
    ItemOut,
    Message,
    Store,
    etag,
    expose,
    item_router,
    make_api,
    modified,
)

ERR = Entity("/schemas/error", context_link_or_func="/contexts/error")
ERR_LINK = (
    '<http://testlocation/schemas/error>; rel="describedby"; '
    'type="application/schema+json", '
    '<http://testlocation/contexts/error>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
ERR404 = Entity("/schemas/not-found", context_link_or_func="/contexts/not-found")
ERR404_LINK = (
    '<http://testlocation/schemas/not-found>; rel="describedby"; '
    'type="application/schema+json", '
    "<http://testlocation/contexts/not-found>; "
    'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)
API_ERR = Entity("/schemas/api-error", context_link_or_func="/contexts/api-error")
API_ERR_LINK = (
    '<http://testlocation/schemas/api-error>; rel="describedby"; '
    'type="application/schema+json", '
    "<http://testlocation/contexts/api-error>; "
    'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)


def with_hsr(links: str, route: str = "/_hsr/items.pk") -> str:
    """``links`` followed by the HTTP Schema Resource link of a path of a
    ``ScrudfulRouter(base_url=TEST_BASE_URL)``, whose HSR route is ``route``."""
    return f"{links}, {hsr_link(f'http://testlocation{route}')}"


@pytest.fixture
def store() -> Store:
    return Store()


def get(client: Any, path: str, headers: dict[str, str] | None = None) -> Any:
    request = client.get
    if isinstance(client, TestAsyncClient):
        request = async_to_sync(request)
    return request(path, headers=headers or {})


def raising_router(asynchronous: bool, **options: Any) -> Router:
    """``GET /items/{pk}`` whose handler raises ``HttpError(404, "x")``."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
    decorate = router.get("/items/{pk}", response={200: ItemOut, 404: Message})
    if asynchronous:

        async def get_item(request, pk: int):
            raise HttpError(404, "x")

    else:

        def get_item(request, pk: int):
            raise HttpError(404, "x")

    decorate(scrudful(**LINKS, **options)(get_item))
    return router


def client_for(router: Router, asynchronous: bool) -> Any:
    return (TestAsyncClient if asynchronous else TestClient)(router)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_raised_http_error_uses_declared_entity(asynchronous):
    router = raising_router(asynchronous, responses={404: ERR404}, error=ERR)
    response = get(client_for(router, asynchronous), "/items/1")
    assert response.status_code == 404
    assert response.json() == {"detail": "x"}
    assert response.headers["Link"] == with_hsr(ERR404_LINK)
    assert expose(response) == {"Link", "Allow"}


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_raised_http404_uses_error_entity(store, asynchronous):
    def load(request, pk: int):
        raise Http404("no such item")

    router = Router()
    if asynchronous:

        @router.get("/items/{pk}")
        @scrudful(load=load, etag_func=etag, error=ERR, **LINKS)
        async def get_item(request, pk: int, obj):
            raise AssertionError("not reached")

    else:

        @router.get("/items/{pk}")
        @scrudful(load=load, etag_func=etag, error=ERR, **LINKS)
        def get_item(request, pk: int, obj):
            raise AssertionError("not reached")

    response = get(client_for(router, asynchronous), "/items/1")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert response.headers["Link"] == ERR_LINK


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_list_invalid_page_has_error_links(store, asynchronous):
    router = Router()
    options = dict(
        href_func=lambda request, item: f"/items/{item['id']}",
        list_schema_link_or_func="/schemas/items",
        list_context_link_or_func="/contexts/items",
        error=ERR,
    )
    if asynchronous:

        @router.get("/items", response=list[ItemOut])
        @scrudful_list(**options)
        async def list_items(request):
            return list(store.items.values())

    else:

        @router.get("/items", response=list[ItemOut])
        @scrudful_list(**options)
        def list_items(request):
            return list(store.items.values())

    client = client_for(router, asynchronous)
    response = get(client, "/items?page=99")
    assert response.status_code == 404
    assert response.json() == {"detail": "Invalid page."}
    assert response.headers["Link"] == ERR_LINK
    assert get(client, "/items").status_code == 200


def test_raised_error_without_entity_has_the_hsr_link_alone(store):
    """No entity's Links; the HTTP Schema Resource link, with Link and Allow
    exposed."""
    router = raising_router(False)
    response = get(TestClient(router), "/items/1")
    assert response.status_code == 404
    assert response.headers["Link"] == hsr_link("http://testlocation/_hsr/items.pk")
    assert expose(response) == {"Link", "Allow"}


def test_raised_error_link_callables_get_none(store):
    seen = []

    def schema(request, obj):
        seen.append(obj)
        return "/schemas/error"

    router = raising_router(False, error=Entity(schema))
    response = get(TestClient(router), "/items/1")
    assert response.headers["Link"] == with_hsr(
        '<http://testlocation/schemas/error>; rel="describedby"; '
        'type="application/schema+json"'
    )
    assert seen == [None]


def test_raised_error_keeps_the_custom_handler(store):
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")

    @api.exception_handler(HttpError)
    def teapot(request, exc):
        return api.create_response(request, {"oops": str(exc)}, status=418)

    api.add_router("", raising_router(False, responses={418: ERR404}, error=ERR))
    response = get(TestClient(api), "/items/1")
    assert response.status_code == 418
    assert response.json() == {"oops": "x"}
    assert response.headers["Link"] == with_hsr(ERR404_LINK)


def test_raised_error_still_reaches_operation_decorators(store):
    """The exception propagates as before, e.g. for a transaction decorator."""
    seen = []

    def recording(view_func):
        def wrapped(request, **kwargs):
            try:
                return view_func(request, **kwargs)
            except HttpError as exc:
                seen.append(exc.status_code)
                raise

        return wrapped

    router = raising_router(False, error=ERR)
    router.add_decorator(recording, mode="operation")
    response = get(TestClient(router), "/items/1")
    assert response.status_code == 404
    assert response.headers["Link"] == with_hsr(ERR_LINK)
    assert seen == [404]


def test_raised_error_on_every_mount(store):
    router = raising_router(False, error=ERR)
    # The HSR link of either mount's route: they serve one document (one
    # URL name for both, see _hsr_target).
    links = {with_hsr(ERR_LINK, f"{mount}/_hsr/items.pk") for mount in ("/one", "/two")}
    for api in (NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}") for _ in "ab"):
        api.add_router("/one", router, url_name_prefix="one")
        api.add_router("/two", router, url_name_prefix="two")
        client = TestClient(api)
        for prefix in ("/one", "/two"):
            response = get(client, f"{prefix}/items/1")
            assert response.status_code == 404
            assert response.headers["Link"] in links


def test_agreement_for_declared_404(store):
    router = raising_router(False, responses={404: ERR404}, error=ERR)
    client = TestClient(router)
    options_body = client.get("/_hsr/items.pk").json()
    response = get(client, "/items/1")
    assert response.status_code == 404
    assert link_problems("GET", 404, response.headers, True, hsr=options_body) == []


def test_agreement_for_conditional_400(store):
    """A raised 400 on a conditional GET: ``error``, as the HTTP Schema
    Resource describes it."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    def load(request, pk: int):
        raise HttpError(400, "bad")

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=load, etag_func=etag, responses={400: ERR404}, error=ERR, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    client = TestClient(router)
    options_body = client.get("/_hsr/items.pk").json()
    response = get(client, "/items/1")
    assert response.status_code == 400
    assert response.headers["Link"] == with_hsr(ERR_LINK)
    assert link_problems("GET", 400, response.headers, True, hsr=options_body) == []


def error_api(store: Store, **options: Any) -> NinjaAPI:
    api = make_api(item_router(store), store, **options)

    @api.put("/plain/{pk}")
    def plain_put(request, pk: int, payload: ItemIn):
        raise Http404("plain")

    return api


def test_add_error_links_validation_and_auth(store):
    api = error_api(store)
    add_error_links(api, ERR)
    client = TestClient(api)

    response = client.put(
        "/items/1", json={"name": 1}, headers={"If-Match": '"v1"', **ALICE}
    )
    assert response.status_code == 422
    assert response.headers["Link"] == ERR_LINK
    assert expose(response) == {"Link", "Allow"}

    response = client.get("/items/1")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["Link"] == ERR_LINK

    response = client.get("/items/1", headers=ALICE)
    assert response.status_code == 200
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"

    response = client.put("/plain/1", json={"name": "a"}, headers=ALICE)
    assert response.status_code == 404
    assert response.headers["Link"] == ERR_LINK


def test_add_error_links_throttled(store):
    api = error_api(store, throttle=Deny(store))
    add_error_links(api, ERR)
    response = TestClient(api).get("/items/1", headers=ALICE)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.headers["Link"] == ERR_LINK


def test_add_error_links_authorization_error(store):
    api = error_api(store)

    @api.get("/forbidden")
    def forbidden(request):
        raise AuthorizationError()

    add_error_links(api, ERR)
    response = TestClient(api).get("/forbidden", headers=ALICE)
    assert response.status_code == 403
    assert response.headers["Link"] == ERR_LINK


def test_add_error_links_idempotent_with_operation_links(store):
    router = Router()

    @router.put("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, last_modified_func=modified, error=ERR)
    def put_item(request, pk: int, payload: ItemIn, obj):
        raise AssertionError("not reached")

    api = make_api(router, store)
    add_error_links(api, API_ERR)
    add_error_links(api, API_ERR)
    client = TestClient(api)

    response = client.put(
        "/items/1", json={"name": "a"}, headers={"If-Match": '"stale"', **ALICE}
    )
    assert response.status_code == 412
    links = parse_links(response.headers["Link"])
    assert [params["rel"] for _, params in links].count("describedby") == 1
    assert response.headers["Link"] == ERR_LINK

    # Called twice, on an error it covers: one Link of each kind.
    response = client.put("/items/1", json={"name": "a"})
    assert response.status_code == 401
    assert response.headers["Link"] == API_ERR_LINK


def test_add_error_links_again_replaces_the_earlier_entity(store):
    calls: list[str] = []

    def link(name: str) -> Any:
        def func(request, obj):
            calls.append(name)
            return f"/schemas/{name}"

        return func

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")

    @api.get("/conflict")
    def conflict(request):
        raise HttpError(409, "taken")

    add_error_links(api, Entity(link("a")))
    add_error_links(api, Entity(link("b"), media_type="application/problem+json"))
    response = TestClient(api).get("/conflict")
    assert response.status_code == 409
    assert response.headers["Link"] == (
        '<http://testlocation/schemas/b>; rel="describedby"; '
        'type="application/schema+json"'
    )
    assert response.headers["Content-Type"] == "application/problem+json; charset=utf-8"
    assert calls == ["b"]


def test_add_error_links_operation_entity_wins(store):
    """A raised error gets the operation's entity, else the API's."""
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/declared", raising_router(False, responses={404: ERR404}))
    api.add_router("/error", raising_router(False, error=ERR))
    api.add_router("/none", raising_router(False))
    add_error_links(api, API_ERR)
    client = TestClient(api)
    for prefix, link in (
        ("/declared", ERR404_LINK),
        ("/error", ERR_LINK),
        ("/none", API_ERR_LINK),
    ):
        response = get(client, f"{prefix}/items/1")
        assert response.status_code == 404
        route = f"{prefix}/_hsr/items.pk"
        assert response.headers["Link"] == with_hsr(link, route), prefix


def test_add_error_links_async_operation(store):
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/error", raising_router(True, error=ERR))
    api.add_router("/none", raising_router(True))
    add_error_links(api, API_ERR)
    client = TestAsyncClient(api)
    error_route, none_route = "/error/_hsr/items.pk", "/none/_hsr/items.pk"
    assert get(client, "/error/items/1").headers["Link"] == with_hsr(
        ERR_LINK, error_route
    )
    assert get(client, "/none/items/1").headers["Link"] == with_hsr(
        API_ERR_LINK, none_route
    )
    response = get(client, "/none/items/one")
    assert response.status_code == 422
    assert response.headers["Link"] == with_hsr(API_ERR_LINK, none_route)


def test_add_error_links_keeps_custom_handlers(store):
    api = error_api(store)

    @api.exception_handler(HttpError)
    def teapot(request, exc):
        response = api.create_response(request, {"oops": str(exc)}, status=418)
        response["Link"] = '</schemas/teapot>; rel="describedby"'
        return response

    add_error_links(api, ERR)
    client = TestClient(api)
    response = client.get("/items/1")  # AuthenticationError is an HttpError
    assert response.status_code == 418
    assert response.json() == {"oops": "Unauthorized"}
    # The handler's describedby suppresses the schema link, not the context.
    assert response.headers["Link"] == (
        '</schemas/teapot>; rel="describedby", '
        "<http://testlocation/contexts/error>; "
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )


def test_add_error_links_link_callables_get_none(store):
    seen = []

    def schema(request, obj):
        seen.append(obj)
        return "/schemas/error"

    api = error_api(store)
    add_error_links(api, Entity(schema))
    response = TestClient(api).get(f"/items/{MISSING}")
    assert response.status_code == 401
    assert seen == [None]


def test_add_error_links_arguments(store):
    api = error_api(store)
    with pytest.raises(TypeError, match="Entity"):
        add_error_links(api, "/schemas/error")
    with pytest.raises(TypeError, match="NinjaAPI"):
        add_error_links(Router(), ERR)

    async def schema(request, obj):
        return "/schemas/error"

    with pytest.raises(TypeError, match="async"):
        add_error_links(api, Entity(schema))


def test_raised_error_turned_into_a_success_gets_no_error_links(store):
    def swallow(view_func):
        def wrapped(request, **kwargs):
            try:
                return view_func(request, **kwargs)
            except HttpError:
                return {"id": 0, "name": "none", "version": 0}

        return wrapped

    router = raising_router(False, responses={200: ERR404}, error=ERR)
    router.add_decorator(swallow, mode="operation")
    response = get(TestClient(router), "/items/1")
    assert response.status_code == 200
    # Neither the error's nor the 200's entity: the HSR link alone.
    assert response.headers["Link"] == hsr_link("http://testlocation/_hsr/items.pk")


def test_add_error_links_then_a_later_http_error_handler(store):
    """A handler registered later replaces the wrapped one, subclasses included."""
    api = error_api(store, throttle=Deny(store))
    add_error_links(api, ERR)

    @api.exception_handler(HttpError)
    def teapot(request, exc):
        return api.create_response(request, {"oops": str(exc)}, status=418)

    client = TestClient(api)
    for headers in ({}, ALICE):  # 401, then 429 (both HttpError subclasses)
        response = client.get("/items/1", headers=headers)
        assert response.status_code == 418
        assert "Link" not in response.headers


def test_add_error_links_wraps_an_exact_subclass_handler(store):
    api = error_api(store)

    @api.exception_handler(AuthenticationError)
    def unauthorized(request, exc):
        return api.create_response(request, {"login": True}, status=401)

    add_error_links(api, ERR)
    response = TestClient(api).get("/items/1")
    assert response.status_code == 401
    assert response.json() == {"login": True}
    assert response.headers["Link"] == ERR_LINK
    # The subclass entry is wrapped once, not also through HttpError's.
    assert api._exception_handlers[AuthenticationError].__wrapped__ is unauthorized


# --- The error entity's media type --------------------------------------------

PROBLEM = Entity(
    "/schemas/problem",
    context_link_or_func="/contexts/problem",
    media_type="application/problem+json",
)
PROBLEM_LINK = (
    '<http://testlocation/schemas/problem>; rel="describedby"; '
    'type="application/schema+json", '
    "<http://testlocation/contexts/problem>; "
    'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)


def test_early_errors_are_served_as_the_error_media_type(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, error=PROBLEM, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    @router.put("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, error=PROBLEM, **LINKS)
    def put_item(request, pk: int, payload: ItemIn, obj):
        return obj

    client = TestClient(router)
    hsr = client.get("/_hsr/items.pk").json()
    validate_http_schema_resource(hsr)
    payload = {"name": "x"}
    early = [
        ("GET", client.get("/items/1", headers={"If-Match": '"x"'})),
        ("PUT", client.put("/items/1", json=payload)),
        ("PUT", client.put("/items/1", json=payload, headers={"If-Match": "x"})),
        (
            "GET",
            client.get("/items/1", headers={"If-Match": '"x"', "Accept": "text/html"}),
        ),
    ]
    assert [response.status_code for _, response in early] == [412, 428, 400, 412]
    for method, response in early:
        assert (
            response.headers["Content-Type"]
            == "application/problem+json; charset=utf-8"
        )
        assert response.headers["Link"] == with_hsr(PROBLEM_LINK)
        assert "detail" in response.json()
        problems = link_problems(
            method, response.status_code, response.headers, True, hsr=hsr
        )
        assert problems == []
    etag_ = client.get("/items/1").headers["ETag"]
    not_modified = client.get("/items/1", headers={"If-None-Match": etag_})
    assert not_modified.status_code == 304
    assert "Content-Type" not in not_modified.headers
    # No error entity's Links (no body): the HSR link alone.
    assert not_modified.headers["Link"] == hsr_link("http://testlocation/_hsr/items.pk")


def test_early_errors_without_error_are_json(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    response = TestClient(router).get("/items/1", headers={"If-Match": '"x"'})
    assert response.status_code == 412
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    # No error entity's Links: the HSR link alone.
    assert response.headers["Link"] == hsr_link("http://testlocation/_hsr/items.pk")


@pytest.mark.parametrize("media_type", ["text/turtle", "text/csv", "application/xml"])
def test_error_media_type_must_be_json(media_type):
    context = (
        {}
        if media_type == "text/turtle"
        else {
            "context_link_or_func": "/contexts/error",
            "context_type_or_func": "text/x",
        }
    )
    error = Entity("/schemas/error", media_type=media_type, **context)
    with pytest.raises(ValueError, match="scrudful's JSON error bodies"):
        scrudful(error=error)
    with pytest.raises(ValueError, match=media_type):
        scrudful_list(href_func=lambda request, item: "/", error=error)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_raised_error_is_served_as_the_error_media_type(store, asynchronous):
    """A handler-raised 400 agrees with the HTTP Schema Resource, which lists
    it as problem+json."""
    router = ScrudfulRouter(base_url=TEST_BASE_URL)
    if asynchronous:

        @router.get("/items/{pk}", response=ItemOut)
        @scrudful(load=store.load, etag_func=etag, error=PROBLEM, **LINKS)
        async def get_item(request, pk: int, obj):
            raise HttpError(400, "bad")

    else:

        @router.get("/items/{pk}", response=ItemOut)
        @scrudful(load=store.load, etag_func=etag, error=PROBLEM, **LINKS)
        def get_item(request, pk: int, obj):
            raise HttpError(400, "bad")

    client = client_for(router, asynchronous)
    # The HSR route is a sync view: ninja's async test client can't call it.
    hsr = TestClient(router).get("/_hsr/items.pk").json()
    validate_http_schema_resource(hsr)
    assert list(hsr["get"]["responses"]["400"]["content"]) == [
        "application/problem+json"
    ]
    response = get(client, "/items/1")
    assert response.status_code == 400
    assert response.json() == {"detail": "bad"}
    assert response.headers["Content-Type"] == "application/problem+json; charset=utf-8"
    assert response.headers["Link"] == with_hsr(PROBLEM_LINK)
    assert link_problems("GET", 400, response.headers, True, hsr=hsr) == []


def test_add_error_links_serves_the_error_media_type(store):
    api = error_api(store)
    add_error_links(api, PROBLEM)
    client = TestClient(api)
    responses = {
        422: client.put(
            "/items/1", json={"name": 1}, headers={"If-Match": '"v1"', **ALICE}
        ),
        401: client.get("/items/1"),
        404: client.put("/plain/1", json={"name": "a"}, headers=ALICE),
    }
    for status, response in responses.items():
        assert response.status_code == status
        assert (
            response.headers["Content-Type"]
            == "application/problem+json; charset=utf-8"
        )
        assert response.headers["Link"] == PROBLEM_LINK
        assert response.json()
    # A success keeps its own media type.
    response = client.get("/items/1", headers=ALICE)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/json")


def test_add_error_links_leaves_non_json_content_alone(store):
    api = error_api(store)

    @api.exception_handler(Http404)
    def plain_not_found(request, exc):
        return HttpResponse("gone", status=404, content_type="text/plain")

    add_error_links(api, PROBLEM)
    response = TestClient(api).put("/plain/1", json={"name": "a"}, headers=ALICE)
    assert response.status_code == 404
    assert response.headers["Content-Type"] == "text/plain"


@pytest.mark.parametrize("media_type", ["text/csv", "text/turtle", "application/xml"])
def test_add_error_links_media_type_must_be_json(store, media_type):
    context = (
        {}
        if media_type == "text/turtle"
        else {
            "context_link_or_func": "/contexts/error",
            "context_type_or_func": "text/x",
        }
    )
    error = Entity("/schemas/error", media_type=media_type, **context)
    with pytest.raises(ValueError, match="scrudful's JSON error bodies"):
        add_error_links(error_api(store), error)


# --- The HTTP Schema Resource link on every response ----------------------------

#: The base URL of ``hsr_api``'s router, and its path's HSR link target
#: (resolved against the base URL, never the request).
HSR_BASE = "https://api.example/"
HSR_TARGET = "https://api.example/_hsr/pk"
HSR_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"
HSR_LINK = f'<{HSR_TARGET}>; rel="{HSR_REL}"; type="application/json"'
W3ID = "https://w3id.org/semantic-http/0.1/"
PROBLEM_ERROR = Entity(
    f"{W3ID}problem-details.schema.json",
    context_link_or_func=f"{W3ID}problem-details.context.jsonld",
    media_type="application/problem+json",
)
#: The If-* header and credentials of a conditional PUT that passes.
AUTH_PRECONDITIONS = {"If-Match": '"v1"', **ALICE}


class ApiKey(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request, key):
        return key if key == "alice" else None


def hsr_api(asynchronous: bool = False, **options: Any) -> NinjaAPI:
    """GET/HEAD and PUT on ``"/{pk}"`` of a ``ScrudfulRouter``, authenticated,
    with both validators and an ``error`` entity."""
    store = Store()
    item = dict(
        load=store.load,
        etag_func=etag,
        last_modified_func=modified,
        error=ERR,
        **LINKS,
        **options,
    )
    router = ScrudfulRouter(base_url=HSR_BASE)
    if asynchronous:

        @router.api_operation(["GET", "HEAD"], "/{pk}", response=ItemOut)
        @scrudful(**item)
        async def get_item(request, pk: int, obj):
            return obj

        @router.put("/{pk}", response=ItemOut)
        @scrudful(**item)
        async def put_item(request, pk: int, payload: ItemIn, obj):
            return obj

    else:

        @router.api_operation(["GET", "HEAD"], "/{pk}", response=ItemOut)
        @scrudful(**item)
        def get_item(request, pk: int, obj):
            return obj

        @router.put("/{pk}", response=ItemOut)
        @scrudful(**item)
        def put_item(request, pk: int, payload: ItemIn, obj):
            return obj

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}", auth=ApiKey())
    api.add_router("", router)
    return api


#: ``(method, path, headers, json, status)``: a response of each kind.
CASES = [
    ("GET", "/1", ALICE, None, 200),
    ("HEAD", "/1", ALICE, None, 200),
    ("GET", "/1", {"If-None-Match": '"v1"', **ALICE}, None, 304),
    ("GET", "/1", {"If-None-Match": 'x"', **ALICE}, None, 400),
    ("GET", "/1", {"If-Match": '"stale"', **ALICE}, None, 412),
    ("PUT", "/1", ALICE, {"name": "x"}, 428),
    ("GET", f"/{MISSING}", ALICE, None, 404),
    ("GET", "/1", {}, None, 401),
    ("PUT", "/1", AUTH_PRECONDITIONS, {"bad": True}, 422),
    ("PUT", "/1", AUTH_PRECONDITIONS, {"name": "x"}, 200),
    ("OPTIONS", "/1", ALICE, None, 204),
]


def call(client: Any, method: str, url: str, headers: Any, body: Any) -> Any:
    kwargs: dict[str, Any] = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    request = functools.partial(client.request, method, url, **kwargs)
    if isinstance(client, TestAsyncClient):
        return async_to_sync(request)()
    return request()


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("with_api_error", [False, True], ids=["plain", "errors"])
def test_every_response_carries_the_hsr_link(asynchronous, with_api_error):
    api = hsr_api(asynchronous)
    if with_api_error:
        add_error_links(api, API_ERR)
    client = (TestAsyncClient if asynchronous else TestClient)(api)
    for method, url, headers, body, status in CASES:
        response = call(client, method, url, headers, body)
        assert response.status_code == status, (method, status)
        assert hsr_link_problems(dict(response.headers), HSR_TARGET) == [], (
            method,
            status,
        )
        assert {"Link", "Allow"} <= expose(response), (method, status)


def test_hsr_link_comes_after_the_entitys_links():
    response = TestClient(hsr_api()).get("/1", headers=ALICE)
    schema, context = (
        link.replace("http://testlocation/", HSR_BASE)
        for link in (SCHEMA_LINK, CONTEXT_LINK)
    )
    assert response.headers["Link"] == f"{schema}, {context}, {HSR_LINK}"


def test_a_handlers_own_hsr_link_wins():
    router = ScrudfulRouter(base_url=HSR_BASE)
    own = f'<https://docs.example/hsr>; rel="{HSR_REL}"; type="application/json"'

    @router.get("/{pk}", response=ItemOut)
    @scrudful(**LINKS)
    def get_item(request, pk: int, response: HttpResponse):
        response["Link"] = own
        return {"id": pk, "name": "a", "version": 1}

    response = TestClient(router).get("/1")
    assert response.status_code == 200
    assert hsr_link_problems(dict(response.headers), "https://docs.example/hsr") == []


def test_preflight_still_has_no_links():
    response = TestClient(hsr_api()).request(
        "OPTIONS",
        "/1",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert response.status_code == 204
    assert "Link" not in response.headers


def test_scrudful_operation_on_a_plain_router_gets_no_hsr_link(store):
    response = TestClient(item_router(store)).get("/items/1")
    assert response.status_code == 200
    assert response.headers["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"


def test_raised_error_without_entity_on_a_plain_router_exposes_link_and_allow():
    router = Router()

    @router.get("/items/{pk}", response={200: ItemOut, 404: Message})
    @scrudful(**LINKS)
    def get_item(request, pk: int):
        raise HttpError(404, "x")

    response = get(TestClient(router), "/items/1")
    assert response.status_code == 404
    assert "Link" not in response.headers
    assert expose(response) == {"Link", "Allow"}


# --- @described operations send their Links (they agree with the HSR) ----------

PLAIN = Entity(
    schema_link_or_func="/schemas/plain", context_link_or_func="/contexts/plain"
)
PLAIN_TARGET = "https://api.example/_hsr/plain"


def described_router(asynchronous: bool = False) -> ScrudfulRouter:
    router = ScrudfulRouter(base_url=HSR_BASE)
    if asynchronous:

        @router.api_operation(["GET", "HEAD"], "/plain", response=Message)
        @described(PLAIN)
        async def plain(request):
            return {"detail": "ok"}

    else:

        @router.api_operation(["GET", "HEAD"], "/plain", response=Message)
        @described(PLAIN)
        def plain(request):
            return {"detail": "ok"}

    @router.delete("/plain", response={204: None})
    @described(PLAIN)
    def remove(request):
        return Status(204, None)

    return router


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_described_get_sends_its_links_and_the_hsr_link(asynchronous):
    router = described_router(asynchronous)
    hsr = TestClient(router).get("/_hsr/plain").json()
    client = client_for(router, asynchronous)
    for method in ("GET", "HEAD"):
        response = call(client, method, "/plain", {}, None)
        assert response.status_code == 200
        headers = dict(response.headers)
        assert link_problems(method, 200, headers, method == "GET", hsr) == []
        assert hsr_link_problems(headers, PLAIN_TARGET) == []
        assert {"Link", "Allow"} <= expose(response)


def test_described_204_gets_the_hsr_link_alone():
    response = TestClient(described_router()).delete("/plain")
    assert response.status_code == 204
    assert response.headers["Link"] == (
        f'<{PLAIN_TARGET}>; rel="{HSR_REL}"; type="application/json"'
    )


def test_described_link_callables_get_the_request_and_no_object():
    seen = []

    def schema(request, obj):
        seen.append((request is not None, obj))
        return "/schemas/plain"

    router = ScrudfulRouter(base_url=HSR_BASE)

    @router.get("/plain", response=Message)
    @described(Entity(schema, context_link_or_func="/contexts/plain"))
    def plain(request):
        return {"detail": "ok"}

    client = TestClient(router)
    client.get("/_hsr/plain")
    assert seen == [(False, None)]  # described
    client.get("/plain")
    assert seen[1:] == [(True, None)]


def test_described_async_callables_need_an_async_operation():
    async def schema(request, obj):
        return "/schemas/plain"

    router = ScrudfulRouter(base_url=HSR_BASE)
    with pytest.raises(TypeError, match="schema_link_or_func is async"):

        @router.get("/plain", response=Message)
        @described(Entity(schema, context_link_or_func="/contexts/plain"))
        def plain(request):
            return {"detail": "ok"}

    @router.get("/async", response=Message)
    @described(Entity(schema, context_link_or_func="/contexts/plain"))
    async def awaited(request):
        return {"detail": "ok"}

    response = async_to_sync(TestAsyncClient(router).get)("/async")
    assert response.headers["Link"].startswith(
        '<https://api.example/schemas/plain>; rel="describedby"'
    )


def test_described_only_path_answers_options():
    """Semantic HTTP requirement 3: the path supports OPTIONS, as its HTTP
    Schema Resource says."""
    client = TestClient(described_router())
    assert "options" in client.get("/_hsr/plain").json()
    response = client.request("OPTIONS", "/plain")
    assert (
        options_204_problems(
            response.status_code,
            dict(response.headers),
            response.content,
            {"GET", "HEAD", "DELETE", "OPTIONS"},
        )
        == []
    )
    assert hsr_link_problems(dict(response.headers), PLAIN_TARGET) == []


# --- The JSON 405 -------------------------------------------------------------


def test_unrouted_method_is_json_405():
    client = TestClient(hsr_api())
    hsr = client.get("/_hsr/pk").json()
    r = client.request("TRACE", "/1", headers={"Accept": "application/json"})
    assert r.status_code == 405 and r.headers["Content-Type"].startswith("application/")
    assert r.json() == {"detail": "Method not allowed"}
    assert r.headers["Allow"] == "GET, PUT, OPTIONS, HEAD"
    assert undescribed_problems("application/json", dict(r.headers), hsr) == []
    assert hsr_link_problems(dict(r.headers), HSR_TARGET) == []
    assert {"Link", "Allow"} <= expose(r)


def test_405_allow_lists_head_only_when_routed():
    """``Allow`` lists HEAD only where it is routed; make_api's path routes
    none."""
    from tests.test_ninja_hsr import PK_TARGET
    from tests.test_ninja_hsr import make_api as make_hsr_api

    api, _ = make_hsr_api()
    r = TestClient(api).post("/1", json={})
    assert r.status_code == 405
    assert r.headers["Allow"] == "GET, PUT, OPTIONS"
    assert hsr_link_problems(dict(r.headers), PK_TARGET) == []


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_405_carries_the_api_wide_error_entity(asynchronous):
    api = hsr_api(asynchronous)
    add_error_links(api, PROBLEM_ERROR)
    client = (TestAsyncClient if asynchronous else TestClient)(api)
    r = call(client, "PATCH", "/1", ALICE, {"name": "x"})
    assert r.status_code == 405
    assert r.headers["Content-Type"] == "application/problem+json; charset=utf-8"
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(r.json())
    hsr = TestClient(api).get("/_hsr/pk").json()
    headers = dict(r.headers)
    assert link_problems("PATCH", 405, headers, True) == []
    assert undescribed_problems(None, headers, hsr) == []
    assert hsr_link_problems(headers, HSR_TARGET) == []


def test_405_goes_through_a_custom_http_error_handler():
    api = hsr_api()

    @api.exception_handler(HttpError)
    def shaped(request, exc):
        return api.create_response(
            request, {"message": str(exc)}, status=exc.status_code
        )

    r = TestClient(api).delete("/1")
    assert r.status_code == 405
    assert r.json() == {"message": "Method not allowed"}
    assert r.headers["Allow"] == "GET, PUT, OPTIONS, HEAD"


def test_405_of_the_hsr_route_is_json_too():
    r = TestClient(hsr_api()).post("/_hsr/pk")
    assert r.status_code == 405
    assert r.json() == {"detail": "Method not allowed"}
    assert r.headers["Allow"] == "GET, HEAD"
    assert "Link" not in r.headers


HSR_URLS_API = hsr_api()
add_error_links(HSR_URLS_API, API_ERR)
urlpatterns = [path("v1/", HSR_URLS_API.urls)]


@pytest.mark.urls(__name__)
def test_405_through_djangos_url_resolver(client, settings, db):
    settings.ALLOWED_HOSTS = ["a.example"]
    r = client.generic("TRACE", "/v1/1", HTTP_HOST="a.example")
    assert r.status_code == 405
    assert r["Allow"] == "GET, PUT, OPTIONS, HEAD"
    headers = dict(r.items())
    assert hsr_link_problems(headers, "https://api.example/v1/_hsr/pk") == []
    assert link_problems("TRACE", 405, headers, True) == []


@pytest.fixture
def fresh_ninja_code():
    """The check of ninja's code is memoized per function: start afresh."""
    import scrudful.ninja

    scrudful.ninja._is_ninja_code.cache_clear()
    yield
    scrudful.ninja._is_ninja_code.cache_clear()


@pytest.mark.parametrize("method", ["clone", "get_view", "_sync_view", "_async_view"])
def test_405_needs_ninjas_path_view(monkeypatch, fresh_ninja_code, method):
    import ninja.operation

    original = getattr(ninja.operation.PathView, method)

    def changed(*args, **kwargs):
        return original(*args, **kwargs)

    monkeypatch.setattr(ninja.operation.PathView, method, changed)
    pattern = rf"ninja's PathView\.{method} here, .*upgrade"
    with pytest.raises(ConfigError, match=pattern):
        hsr_api()


@pytest.mark.filterwarnings("ignore:scrudful can't read the source:RuntimeWarning")
def test_without_source_the_path_view_fingerprints_are_checked(
    monkeypatch, fresh_ninja_code
):
    import scrudful.ninja

    monkeypatch.setattr(scrudful.ninja, "_source_digest", lambda func: None)
    r = TestClient(hsr_api()).request("TRACE", "/1")
    assert r.status_code == 405 and r.json() == {"detail": "Method not allowed"}


def test_405_needs_ninjas_own_path_view(monkeypatch):
    import ninja.operation
    import ninja.router

    class OtherPathView(ninja.operation.PathView):
        pass

    monkeypatch.setattr(ninja.router, "PathView", OtherPathView)
    with pytest.raises(ConfigError, match=r"made a .*OtherPathView.*upgrade"):
        hsr_api()


def test_validation_422_problem_without_a_json_renderer():
    """An API whose renderer writes another media type: the problem details
    of the JSON 422 its handler made are still JSON."""
    from ninja.renderers import BaseRenderer

    class Text(BaseRenderer):
        media_type = "text/plain"

        def render(self, request, data, *, response_status):
            return str(data)

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}", renderer=Text())

    @api.exception_handler(ninja_errors.ValidationError)
    def json_422(request, exc):
        return HttpResponse(
            json.dumps({"detail": exc.errors}),
            status=422,
            content_type="application/json",
        )

    @api.post("/things")
    def create(request, payload: ItemIn):
        return {}

    add_error_links(api, PROBLEM_ERROR)
    r = TestClient(api).post("/things", json={})
    assert r.status_code == 422
    body = json.loads(r.content)
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(body)
    assert body["errors"]


# --- Problem-details 422 ------------------------------------------------------


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_validation_422_is_problem_details(asynchronous):
    api = hsr_api(asynchronous)
    add_error_links(api, PROBLEM_ERROR)
    client = (TestAsyncClient if asynchronous else TestClient)(api)
    r = call(client, "PUT", "/1", AUTH_PRECONDITIONS, {"bad": True})
    assert r.status_code == 422
    assert r.headers["Content-Type"] == "application/problem+json; charset=utf-8"
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(r.json())
    body = r.json()
    assert body["errors"]
    assert body["errors"][0]["loc"] == ["body", "payload", "name"]
    assert {key: body[key] for key in ("type", "title", "status", "detail")} == {
        "type": "about:blank",
        "title": "Unprocessable Content",
        "status": 422,
        "detail": "Request validation failed",
    }


def test_validation_422_keeps_ninjas_body_for_other_entities():
    api = hsr_api()
    add_error_links(api, API_ERR)
    r = TestClient(api).put("/1", json={"bad": True}, headers=AUTH_PRECONDITIONS)
    assert r.status_code == 422
    assert list(r.json()) == ["detail"] and isinstance(r.json()["detail"], list)


def test_validation_422_leaves_a_custom_handlers_body_alone():
    api = hsr_api()

    @api.exception_handler(ninja_errors.ValidationError)
    def custom(request, exc):
        return api.create_response(request, {"invalid": len(exc.errors)}, status=422)

    add_error_links(api, PROBLEM_ERROR)
    r = TestClient(api).put("/1", json={"bad": True}, headers=AUTH_PRECONDITIONS)
    assert r.status_code == 422
    assert r.json() == {"invalid": 1}
