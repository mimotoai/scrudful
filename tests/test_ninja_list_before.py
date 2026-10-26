"""``scrudful_list(before=...)``: a callable run after authentication and before
ninja validates any parameter of the operation, so a list can be authorized
before a bad parameter is answered (DRF's order: authentication, permissions,
then the parameters)."""

import json
import uuid
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from items.models import Item
from ninja import NinjaAPI, Router
from ninja.decorators import decorate_view
from ninja.errors import HttpError
from ninja.security import APIKeyHeader
from ninja.testing import TestAsyncClient, TestClient
from scrudful.ninja import Entity, add_error_links, scrudful_list

from tests.test_ninja_adapter import ItemOut
from tests.test_ninja_list import href

ERROR = Entity(schema_link_or_func="/schemas/error")

#: Queries with a parameter ninja rejects (422): page parameters, the
#: operation's own ``n``, and both.
BAD = [
    "page=x",
    "page=0",
    "page_size=0",
    "page_size=5000",
    "page=x&page_size=y",
    "page=1.5&page_size=0",
    "n=z",
    "n=z&page=x",
    "page=0&n=z&page_size=y",
]
GOOD = ["", "page=1", "page_size=2", "page=2&page_size=2", "page=1.0", "n=3"]


@pytest.fixture
def items(db) -> list[Item]:
    return [Item.objects.create(name=name) for name in ("a", "b", "c")]


class OperationDecorator:
    """An ``add_decorator(mode="operation")`` decorator recording its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, func):
        if _is_coroutine(func):

            async def decorated(request, *args, **kwargs):
                self.calls += 1
                return await func(request, *args, **kwargs)

        else:

            def decorated(request, *args, **kwargs):
                self.calls += 1
                return func(request, *args, **kwargs)

        return decorated


def _is_coroutine(func) -> bool:
    import inspect

    return inspect.iscoroutinefunction(func)


def list_router(asynchronous: bool = False, **options: Any) -> Router:
    """``GET /p/{parent}/items`` with the query parameters ``q`` and ``n``."""
    router = Router()
    options = {"href_func": href, **options}

    if asynchronous:

        @router.get("/p/{parent}/items", response=list[ItemOut])
        @scrudful_list(**options)
        async def list_items(request, parent: int, q: str = "", n: int = 0):
            return Item.objects.filter(name__contains=q).order_by("pk")

    else:

        @router.get("/p/{parent}/items", response=list[ItemOut])
        @scrudful_list(**options)
        def list_items(request, parent: int, q: str = "", n: int = 0):
            return Item.objects.filter(name__contains=q).order_by("pk")

    return router


def api_for(
    router: Router, error: Entity | None = None, decorator: Any = None, **options
) -> NinjaAPI:
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}", **options)
    if error is not None:
        add_error_links(api, error)
    if decorator is not None:
        router.add_decorator(decorator, mode="operation")
    api.add_router("", router)
    return api


def getter(asynchronous: bool, api: NinjaAPI):
    if asynchronous:
        client = TestAsyncClient(api)
        return lambda url, **kw: async_to_sync(client.get)(url, **kw)
    client = TestClient(api)
    return client.get


class Recorder:
    """A ``before`` hook recording its calls, raising ``error`` if set."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.error = error

    def __call__(self, request, **params):
        self.calls.append((request, params))
        if self.error is not None:
            raise self.error


class AsyncRecorder(Recorder):
    async def __call__(self, request, **params):
        return super().__call__(request, **params)


ASYNC = pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])


@ASYNC
@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("query", BAD)
def test_before_error_wins_over_bad_parameters(items, asynchronous, status, query):
    before = Recorder(HttpError(status, "Denied"))
    get = getter(asynchronous, api_for(list_router(asynchronous, before=before)))
    response = get(f"/p/7/items?{query}")
    assert response.status_code == status
    assert response.json() == {"detail": "Denied"}
    assert len(before.calls) == 1


@ASYNC
def test_before_error_wins_over_a_bad_path_parameter(items, asynchronous):
    before = Recorder(HttpError(403, "Denied"))
    get = getter(asynchronous, api_for(list_router(asynchronous, before=before)))
    assert get("/p/x/items?page=0").status_code == 403
    [(_, params)] = before.calls
    assert params == {"parent": "x"}


@ASYNC
@pytest.mark.parametrize("query", BAD)
def test_bad_parameters_after_before_are_the_same_422(items, asynchronous, query):
    """One 422 listing every bad parameter, byte for byte as without ``before``."""
    before = Recorder()
    url = f"/p/7/items?{query}"
    hooked = getter(asynchronous, api_for(list_router(asynchronous, before=before)))(
        url
    )
    plain = getter(asynchronous, api_for(list_router(asynchronous)))(url)
    assert plain.status_code == hooked.status_code == 422
    assert hooked.content == plain.content
    assert dict(hooked.headers) == dict(plain.headers)
    assert len(before.calls) == 1


@ASYNC
def test_combined_bad_parameters_listed_in_one_422(items, asynchronous):
    before = Recorder()
    get = getter(asynchronous, api_for(list_router(asynchronous, before=before)))
    response = get("/p/x/items?n=z&page=x&page_size=0")
    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [
        ["path", "parent"],
        ["query", "n"],
        ["query", "page"],
        ["query", "page_size"],
    ]
    assert len(before.calls) == 1


@ASYNC
@pytest.mark.parametrize("query", BAD)
def test_bad_parameters_never_reach_operation_decorators(items, asynchronous, query):
    decorator = OperationDecorator()
    before = Recorder()
    api = api_for(list_router(asynchronous, before=before), decorator=decorator)
    response = getter(asynchronous, api)(f"/p/7/items?{query}")
    assert response.status_code == 422
    assert decorator.calls == 0
    assert len(before.calls) == 1


@ASYNC
def test_before_error_never_reaches_operation_decorators(items, asynchronous):
    decorator = OperationDecorator()
    before = Recorder(HttpError(403, "Denied"))
    api = api_for(list_router(asynchronous, before=before), decorator=decorator)
    assert getter(asynchronous, api)("/p/7/items").status_code == 403
    assert decorator.calls == 0


@ASYNC
def test_operation_decorators_still_run_for_good_requests(items, asynchronous):
    decorator = OperationDecorator()
    api = api_for(list_router(asynchronous, before=Recorder()), decorator=decorator)
    assert getter(asynchronous, api)("/p/7/items").status_code == 200
    assert decorator.calls == 1


@ASYNC
@pytest.mark.parametrize("query", GOOD)
def test_before_called_once_with_the_path_parameters(items, asynchronous, query):
    before = Recorder()
    url = f"/p/7/items?q=&{query}"
    hooked = getter(asynchronous, api_for(list_router(asynchronous, before=before)))(
        url
    )
    plain = getter(asynchronous, api_for(list_router(asynchronous)))(url)
    assert hooked.status_code == plain.status_code == 200
    assert hooked.content == plain.content
    [(request, params)] = before.calls
    assert request.method == "GET"
    # As the URL gives them: not validated yet.
    assert params == {"parent": "7"}


class Key(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request, key):
        return key if key == "alice" else None


@ASYNC
def test_before_runs_after_authentication(items, asynchronous):
    before = Recorder()
    api = api_for(list_router(asynchronous, before=before), auth=Key())
    get = getter(asynchronous, api)
    assert get("/p/7/items?page=x").status_code == 401
    assert before.calls == []
    response = get("/p/7/items?page=x", headers={"X-API-Key": "alice"})
    assert response.status_code == 422
    [(request, _)] = before.calls
    assert request.auth == "alice"


def test_async_before_in_async_operation(items):
    before = AsyncRecorder(HttpError(403, "Denied"))
    get = getter(True, api_for(list_router(True, before=before)))
    assert get("/p/7/items?page=x").status_code == 403
    assert len(before.calls) == 1


def test_async_before_rejected_for_sync_operation():
    with pytest.raises(TypeError, match="before"):
        list_router(False, before=AsyncRecorder())


def test_before_must_be_callable():
    with pytest.raises(TypeError, match="before must be callable"):
        scrudful_list(href_func=href, before="nope")


def test_before_error_gets_the_operation_error_links(items):
    before = Recorder(HttpError(403, "Denied"))
    api = api_for(list_router(before=before, error=ERROR), error=ERROR)
    response = TestClient(api).get("/p/7/items?page=x")
    assert response.status_code == 403
    assert "/schemas/error" in response.headers["Link"]


def test_before_error_without_error_entity_has_no_links(items):
    before = Recorder(HttpError(403, "Denied"))
    response = TestClient(api_for(list_router(before=before))).get("/p/7/items")
    assert response.status_code == 403
    assert "Link" not in response.headers


def test_bad_parameters_keep_api_error_links(items):
    url = "/p/7/items?page=x&n=z"
    hooked = TestClient(api_for(list_router(before=Recorder()), error=ERROR)).get(url)
    plain = TestClient(api_for(list_router(), error=ERROR)).get(url)
    assert hooked.status_code == 422
    assert hooked.headers["Link"] == plain.headers["Link"]


def test_router_mounted_twice_runs_before_in_each_mount(items):
    before = Recorder(HttpError(403, "Denied"))
    router = list_router(before=before)
    for api in (api_for(router), api_for(router)):
        assert TestClient(api).get("/p/7/items?page=x").status_code == 403
    assert len(before.calls) == 2


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_openapi_unchanged_by_before(asynchronous):
    hooked = api_for(list_router(asynchronous, before=Recorder()))
    plain = api_for(list_router(asynchronous))
    schema = [
        json.dumps(api.get_openapi_schema(path_prefix=""), sort_keys=True)
        for api in (hooked, plain)
    ]
    assert schema[0] == schema[1]
    parameters = plain.get_openapi_schema(path_prefix="")["paths"]["/p/{parent}/items"][
        "get"
    ]["parameters"]
    page = {p["name"]: p["schema"] for p in parameters}
    assert page["page"] == {
        "default": 1,
        "minimum": 1,
        "title": "Page",
        "type": "integer",
    }
    assert page["page_size"] == {
        "default": 100,
        "maximum": 1000,
        "minimum": 1,
        "title": "Page Size",
        "type": "integer",
    }


def opaque(run):
    """A ``decorate_view`` decorator without ``functools.wraps``: scrudful can't
    find the operation through it."""

    def wrapped(request, *args, **kwargs):
        return run(request, *args, **kwargs)

    return wrapped


def opaque_router(before) -> Router:
    router = Router()

    @router.get("/p/{parent}/items", response=list[ItemOut])
    @scrudful_list(href_func=href, before=before)
    @decorate_view(opaque)
    def list_items(request, parent: int):
        return Item.objects.order_by("pk")

    return router


def test_before_never_skipped_when_the_operation_cannot_be_found(items):
    """Then it runs in the operation, after ninja validated the parameters."""
    before = Recorder(HttpError(403, "Denied"))
    client = TestClient(api_for(opaque_router(before)))
    assert client.get("/p/7/items").status_code == 403
    [(_, params)] = before.calls
    assert params == {"parent": "7"}
    allowed = Recorder()
    client = TestClient(api_for(opaque_router(allowed)))
    assert client.get("/p/7/items").status_code == 200
    assert client.get("/p/7/items?page=x").status_code == 422
    assert len(allowed.calls) == 1
