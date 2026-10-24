"""Links on error responses: errors raised in an operation, ``add_error_links``."""

import uuid
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.http import Http404
from ninja import NinjaAPI, Router
from ninja.errors import AuthorizationError, HttpError
from ninja.testing import TestAsyncClient, TestClient

from scrudful.ninja import (
    Entity,
    ScrudfulRouter,
    add_error_links,
    scrudful,
    scrudful_list,
)
from tests.semantic_http import link_problems, parse_links
from tests.test_ninja_adapter import (
    ALICE,
    CONTEXT_LINK,
    LINKS,
    MISSING,
    SCHEMA_LINK,
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
    router = ScrudfulRouter()
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
    assert expose(response) == {"Link"}


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
    options_body = client.request("OPTIONS", "/items/1").json()
    response = get(client, "/items/1")
    assert response.status_code == 404
    assert link_problems("GET", 404, response.headers, True, hsr=options_body) == []


def test_agreement_for_conditional_400(store):
    """A raised 400 on a conditional GET: ``error``, as OPTIONS describes it."""
    router = ScrudfulRouter()

    def load(request, pk: int):
        raise HttpError(400, "bad")

    @router.get("/items/{pk}", response=ItemOut)
    @scrudful(load=load, etag_func=etag, responses={400: ERR404}, error=ERR, **LINKS)
    def get_item(request, pk: int, obj):
        return obj

    client = TestClient(router)
    options_body = client.request("OPTIONS", "/items/1").json()
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
    assert expose(response) == {"Link"}

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
