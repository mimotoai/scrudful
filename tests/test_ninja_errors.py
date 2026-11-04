"""Links on error responses: errors raised in an operation, ``add_error_links``."""

import uuid
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.http import Http404, HttpResponse
from ninja import NinjaAPI, Router
from ninja.errors import AuthenticationError, AuthorizationError, HttpError
from ninja.testing import TestAsyncClient, TestClient

from scrudful.ninja import (
    Entity,
    ScrudfulRouter,
    add_error_links,
    scrudful,
    scrudful_list,
)
from tests.semantic_http import (
    link_problems,
    parse_links,
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
    assert response.headers["Link"] == ERR404_LINK
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


def test_raised_error_without_entity_has_no_links(store):
    router = raising_router(False)
    response = get(TestClient(router), "/items/1")
    assert response.status_code == 404
    assert "Link" not in response.headers
    assert "Access-Control-Expose-Headers" not in response.headers


def test_raised_error_link_callables_get_none(store):
    seen = []

    def schema(request, obj):
        seen.append(obj)
        return "/schemas/error"

    router = raising_router(False, error=Entity(schema))
    response = get(TestClient(router), "/items/1")
    assert response.headers["Link"] == (
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
    assert response.headers["Link"] == ERR404_LINK


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
    assert response.headers["Link"] == ERR_LINK
    assert seen == [404]


def test_raised_error_on_every_mount(store):
    router = raising_router(False, error=ERR)
    for api in (NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}") for _ in "ab"):
        api.add_router("/one", router, url_name_prefix="one")
        api.add_router("/two", router, url_name_prefix="two")
        client = TestClient(api)
        for prefix in ("/one", "/two"):
            response = get(client, f"{prefix}/items/1")
            assert response.status_code == 404
            assert response.headers["Link"] == ERR_LINK


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
    assert response.headers["Link"] == ERR_LINK
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
        assert response.headers["Link"] == link, prefix


def test_add_error_links_async_operation(store):
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("/error", raising_router(True, error=ERR))
    api.add_router("/none", raising_router(True))
    add_error_links(api, API_ERR)
    client = TestAsyncClient(api)
    assert get(client, "/error/items/1").headers["Link"] == ERR_LINK
    assert get(client, "/none/items/1").headers["Link"] == API_ERR_LINK
    response = get(client, "/none/items/one")
    assert response.status_code == 422
    assert response.headers["Link"] == API_ERR_LINK


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
    assert "Link" not in response.headers


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
        assert response.headers["Link"] == PROBLEM_LINK
        assert "detail" in response.json()
        problems = link_problems(
            method, response.status_code, response.headers, True, hsr=hsr
        )
        assert problems == []
    etag_ = client.get("/items/1").headers["ETag"]
    not_modified = client.get("/items/1", headers={"If-None-Match": etag_})
    assert not_modified.status_code == 304
    assert "Content-Type" not in not_modified.headers
    assert "Link" not in not_modified.headers


def test_early_errors_without_error_are_json(store):
    router = ScrudfulRouter(base_url=TEST_BASE_URL)

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    response = TestClient(router).get("/items/1", headers={"If-Match": '"x"'})
    assert response.status_code == 412
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    assert "Link" not in response.headers


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
    assert response.headers["Link"] == PROBLEM_LINK
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
