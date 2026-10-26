"""``scrudful_list(before=...)``: a callable run before ``page`` and ``page_size``
are validated, so a list can be authorized before a bad page parameter is
answered (DRF's order: permissions, filtering, then pagination)."""

import json
import uuid
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from items.models import Item
from ninja import NinjaAPI, Router
from ninja.errors import HttpError
from ninja.testing import TestAsyncClient, TestClient
from scrudful.ninja import Entity, add_error_links, scrudful_list

from tests.test_ninja_adapter import ItemOut
from tests.test_ninja_list import href

ERROR = Entity(schema_link_or_func="/schemas/error")

#: Queries whose page parameters ninja rejects (422).
BAD = [
    "page=x",
    "page=0",
    "page_size=0",
    "page_size=5000",
    "page=x&page_size=y",
    "page=1.5&page_size=0",
]
GOOD = ["", "page=1", "page_size=2", "page=2&page_size=2", "page=1.0"]


@pytest.fixture
def items(db) -> list[Item]:
    return [Item.objects.create(name=name) for name in ("a", "b", "c")]


def list_router(asynchronous: bool = False, **options: Any) -> Router:
    """``GET /p/{parent}/items`` with a ``q`` query parameter."""
    router = Router()
    options = {"href_func": href, **options}

    if asynchronous:

        @router.get("/p/{parent}/items", response=list[ItemOut])
        @scrudful_list(**options)
        async def list_items(request, parent: int, q: str = ""):
            return Item.objects.filter(name__contains=q).order_by("pk")

    else:

        @router.get("/p/{parent}/items", response=list[ItemOut])
        @scrudful_list(**options)
        def list_items(request, parent: int, q: str = ""):
            return Item.objects.filter(name__contains=q).order_by("pk")

    return router


def api_for(router: Router, error: Entity | None = None) -> NinjaAPI:
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    if error is not None:
        add_error_links(api, error)
    api.add_router("", router)
    return api


def getter(asynchronous: bool, api: NinjaAPI):
    if asynchronous:
        client = TestAsyncClient(api)
        return lambda url: async_to_sync(client.get)(url)
    return TestClient(api).get


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
def test_before_error_wins_over_bad_page_parameters(items, asynchronous, status, query):
    before = Recorder(HttpError(status, "Denied"))
    get = getter(asynchronous, api_for(list_router(asynchronous, before=before)))
    response = get(f"/p/7/items?{query}")
    assert response.status_code == status
    assert response.json() == {"detail": "Denied"}
    assert len(before.calls) == 1


@ASYNC
@pytest.mark.parametrize("query", BAD)
def test_bad_page_parameters_after_before_are_the_same_422(items, asynchronous, query):
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
@pytest.mark.parametrize("query", GOOD)
def test_before_called_once_with_the_operation_parameters(items, asynchronous, query):
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
    assert params == {"parent": 7, "q": ""}


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


def test_other_parameters_are_still_validated_before_the_operation(items):
    """ninja validates the operation's other parameters before it runs."""
    before = Recorder(HttpError(403, "Denied"))
    response = TestClient(api_for(list_router(before=before))).get("/p/x/items?page=0")
    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [["path", "parent"]]
    assert before.calls == []


def test_before_error_gets_the_operation_error_links(items):
    before = Recorder(HttpError(403, "Denied"))
    api = api_for(list_router(before=before, error=ERROR), error=ERROR)
    response = TestClient(api).get("/p/7/items?page=x")
    assert response.status_code == 403
    assert "/schemas/error" in response.headers["Link"]


def test_bad_page_parameters_keep_api_error_links(items):
    url = "/p/7/items?page=x"
    hooked = TestClient(api_for(list_router(before=Recorder()), error=ERROR)).get(url)
    plain = TestClient(api_for(list_router(), error=ERROR)).get(url)
    assert hooked.status_code == 422
    assert hooked.headers["Link"] == plain.headers["Link"]


def test_custom_validation_error_builder_is_used(items):
    """The API's own ``validation_error_from_error_contexts`` builds the 422."""
    api = api_for(list_router(before=Recorder()))
    seen = []
    original = api.validation_error_from_error_contexts

    def build(contexts):
        seen.extend(
            (context.model.__ninja_param_source__, error["loc"])
            for context in contexts
            for error in context.pydantic_validation_error.errors()
        )
        return original(contexts)

    api.validation_error_from_error_contexts = build
    assert TestClient(api).get("/p/7/items?page=x").status_code == 422
    assert seen == [("query", ("page",))]


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
    """A ``decorate_view`` decorator without ``functools.wraps``."""

    def wrapped(request, *args, **kwargs):
        return run(request, *args, **kwargs)

    return wrapped


def test_bad_page_parameters_without_the_api_still_422(items):
    """When the serving API can't be found, ninja's default builds the 422."""
    from ninja.decorators import decorate_view

    router = Router()

    @router.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=href, before=Recorder())
    @decorate_view(opaque)
    def list_items(request):
        return Item.objects.order_by("pk")

    plain = Router()

    @plain.get("/items", response=list[ItemOut])
    @scrudful_list(href_func=href)
    def plain_items(request):
        return Item.objects.order_by("pk")

    url = "/items?page=x&page_size=0"
    hooked = TestClient(api_for(router)).get(url)
    expected = TestClient(api_for(plain)).get(url)
    assert hooked.status_code == 422
    assert hooked.content == expected.content
