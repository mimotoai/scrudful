"""``before=`` and ``after_throttles=`` on item operations (``@scrudful``) and
creates (``@scrudful_create``): the hooks of :func:`scrudful.ninja.scrudful_list`,
run in DRF's order: authentication, ``before`` (DRF's ``has_permission``),
throttles, ``after_throttles``, then the parameters and body, ``load`` and
the preconditions (412, 428, 304)."""

import asyncio
import contextvars
import functools
import json
import threading
import types
import uuid
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.http.request import RawPostDataException
from django.test import RequestFactory
from ninja import File, Form, NinjaAPI, Router, Schema, UploadedFile
from ninja import operation as ninja_operation
from ninja.decorators import decorate_view
from ninja.errors import ConfigError, HttpError
from ninja.testing import TestAsyncClient, TestClient
from ninja.throttling import AnonRateThrottle
from scrudful.ninja import Entity, add_error_links, scrudful, scrudful_create

from tests.test_ninja_list_after_throttles import (
    Logged,
    LoggedKey,
    LoggedThrottle,
)
from tests.test_ninja_list_before import (
    ASYNC,
    AsyncRecorder,
    CustomChecks,
    OperationDecorator,
    Recorder,
    _deny,
    opaque,
)

ERROR = Entity(
    schema_link_or_func="/schemas/error", context_link_or_func="/contexts/error"
)
ALICE = {"X-API-Key": "alice"}
METHODS = ["GET", "PUT", "PATCH", "DELETE"]
WRITES = ["PUT", "PATCH", "DELETE"]
BODY = {"name": "new"}
HOOKS = ["before", "after_throttles"]


class ItemIn(Schema):
    name: str


class ItemOut(Schema):
    id: int
    name: str
    version: int


def etag(item: dict[str, Any]) -> str:
    return f"v{item['version']}"


class Store:
    """One row, ``1``; ``log`` records ``load`` and handler calls."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.items = {1: {"id": 1, "name": "a", "version": 1}}
        self.log = [] if log is None else log

    def load(self, request, parent: int, pk: int, **params):
        self.log.append("load")
        item = self.items.get(pk)
        if item is None:
            raise HttpError(404, "Not Found")
        return item

    def handled(self, method: str) -> None:
        self.log.append(method)


def item_router(store: Store, asynchronous: bool = False, **hooks: Any) -> Router:
    """GET/PUT/PATCH/DELETE on ``/p/{parent}/items/{pk}`` with an ETag, and a
    create on ``/p/{parent}/items``."""
    options = dict(load=store.load, etag_func=etag, **hooks)
    router = Router()
    item_path = "/p/{parent}/items/{pk}"
    path = "/p/{parent}/items"

    def update(item, payload):
        item["name"] = payload.name
        item["version"] += 1
        return item

    if asynchronous:

        @router.get(item_path, response=ItemOut)
        @scrudful(**options)
        async def get_item(request, parent: int, pk: int, obj):
            store.handled("GET")
            return obj

        @router.put(item_path, response=ItemOut)
        @scrudful(**options)
        async def put_item(request, parent: int, pk: int, payload: ItemIn, obj):
            store.handled("PUT")
            return update(obj, payload)

        @router.patch(item_path, response=ItemOut)
        @scrudful(**options)
        async def patch_item(request, parent: int, pk: int, payload: ItemIn, obj):
            store.handled("PATCH")
            return update(obj, payload)

        @router.delete(item_path, response={204: None})
        @scrudful(**options)
        async def delete_item(request, parent: int, pk: int, obj):
            store.handled("DELETE")
            del store.items[pk]

        @router.post(path, response={201: ItemOut})
        @scrudful_create(location_func=location, etag_func=etag, **hooks)
        async def create_item(request, parent: int, payload: ItemIn):
            store.handled("POST")
            return {"id": 2, "name": payload.name, "version": 1}

    else:

        @router.get(item_path, response=ItemOut)
        @scrudful(**options)
        def get_item(request, parent: int, pk: int, obj):
            store.handled("GET")
            return obj

        @router.put(item_path, response=ItemOut)
        @scrudful(**options)
        def put_item(request, parent: int, pk: int, payload: ItemIn, obj):
            store.handled("PUT")
            return update(obj, payload)

        @router.patch(item_path, response=ItemOut)
        @scrudful(**options)
        def patch_item(request, parent: int, pk: int, payload: ItemIn, obj):
            store.handled("PATCH")
            return update(obj, payload)

        @router.delete(item_path, response={204: None})
        @scrudful(**options)
        def delete_item(request, parent: int, pk: int, obj):
            store.handled("DELETE")
            del store.items[pk]

        @router.post(path, response={201: ItemOut})
        @scrudful_create(location_func=location, etag_func=etag, **hooks)
        def create_item(request, parent: int, payload: ItemIn):
            store.handled("POST")
            return {"id": 2, "name": payload.name, "version": 1}

    return router


def location(request, item):
    return f"/p/7/items/{item['id']}"


def api_for(router: Router, error: Entity | None = None, decorator=None, **options):
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}", **options)
    if error is not None:
        add_error_links(api, error)
    if decorator is not None:
        router.add_decorator(decorator, mode="operation")
    api.add_router("", router)
    return api


def requester(asynchronous: bool, api: NinjaAPI):
    """``request(method, url, json=None, headers=None)`` for ``api``."""
    if asynchronous:
        client = TestAsyncClient(api)

        def request(method, url, json=None, headers=None):
            return async_to_sync(client.request)(
                method, url, json=json, headers=headers or {}
            )

        return request
    client = TestClient(api)
    return lambda method, url, json=None, headers=None: client.request(
        method, url, json=json, headers=headers or {}
    )


def body_for(method: str) -> Any:
    return BODY if method in ("PUT", "PATCH", "POST") else None


def url_for(method: str, pk: str = "1") -> str:
    return "/p/7/items" if method == "POST" else f"/p/7/items/{pk}"


@pytest.fixture
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@ASYNC
@pytest.mark.parametrize("method", [*METHODS, "POST"])
def test_order_is_auth_before_throttles_after_throttles_then_the_operation(
    fresh_cache, asynchronous, method
):
    log: list[str] = []
    store = Store(log)
    router = item_router(
        store,
        asynchronous,
        before=Logged(log, "before"),
        after_throttles=Logged(log, "after_throttles"),
    )
    api = api_for(router, auth=LoggedKey(log), throttle=LoggedThrottle(log, "10/m"))
    request = requester(asynchronous, api)
    headers = {**ALICE, "If-Match": '"v1"'}
    response = request(method, url_for(method), json=body_for(method), headers=headers)
    assert response.status_code < 300
    checks = ["auth", "before", "throttle", "after_throttles"]
    load = [] if method == "POST" else ["load"]
    # A 2xx PUT/PATCH loads again, for the validators of what was written.
    reload = ["load"] if method in ("PUT", "PATCH") else []
    assert log == [*checks, *load, method, *reload]
    log.clear()
    assert request(method, url_for(method)).status_code == 401
    assert log == ["auth"]


@ASYNC
@pytest.mark.parametrize("method", [*METHODS, "POST"])
def test_a_throttled_request_never_calls_after_throttles(
    fresh_cache, asynchronous, method
):
    store = Store()
    before, after = Recorder(), Recorder()
    router = item_router(store, asynchronous, before=before, after_throttles=after)
    api = api_for(router, throttle=AnonRateThrottle("1/m"))
    request = requester(asynchronous, api)
    # A bad path parameter: 422 (once the checks passed), then 429.
    url = "/p/x/items" if method == "POST" else "/p/7/items/x"
    statuses = [request(method, url, json=body_for(method)).status_code]
    statuses.append(request(method, url, json=body_for(method)).status_code)
    assert statuses == [422, 429]
    assert len(before.calls) == 2
    assert len(after.calls) == 1
    assert store.log == []


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("method", WRITES)
def test_hook_error_beats_a_missing_precondition_428(asynchronous, hook, method):
    store = Store()
    denied = Recorder(HttpError(403, "Denied"))
    request = requester(
        asynchronous, api_for(item_router(store, asynchronous, **{hook: denied}))
    )
    plain = requester(asynchronous, api_for(item_router(Store(), asynchronous)))
    url, body = url_for(method), body_for(method)
    assert plain(method, url, json=body).status_code == 428
    response = request(method, url, json=body)
    assert response.status_code == 403
    assert response.json() == {"detail": "Denied"}
    assert "ETag" not in response.headers
    assert store.log == []


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("method", METHODS)
def test_hook_error_beats_a_failed_precondition_412(asynchronous, hook, method):
    store = Store()
    denied = Recorder(HttpError(403, "Denied"))
    request = requester(
        asynchronous, api_for(item_router(store, asynchronous, **{hook: denied}))
    )
    plain = requester(asynchronous, api_for(item_router(Store(), asynchronous)))
    url, body = url_for(method), body_for(method)
    headers = {"If-Match": '"stale"'}
    assert plain(method, url, json=body, headers=headers).status_code == 412
    response = request(method, url, json=body, headers=headers)
    assert response.status_code == 403
    assert "ETag" not in response.headers
    assert store.log == []


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
def test_hook_error_beats_a_304(asynchronous, hook):
    store = Store()
    denied = Recorder(HttpError(401, "Unauthorized"))
    request = requester(
        asynchronous, api_for(item_router(store, asynchronous, **{hook: denied}))
    )
    plain = requester(asynchronous, api_for(item_router(Store(), asynchronous)))
    headers = {"If-None-Match": '"v1"'}
    assert plain("GET", "/p/7/items/1", headers=headers).status_code == 304
    response = request("GET", "/p/7/items/1", headers=headers)
    assert response.status_code == 401
    assert "ETag" not in response.headers
    assert store.log == []


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("method", ["PUT", "PATCH", "POST"])
@pytest.mark.parametrize("body", [{}, {"name": ["x"]}, None])
def test_hook_error_beats_body_validation(asynchronous, hook, method, body):
    store = Store()
    denied = Recorder(HttpError(403, "Denied"))
    request = requester(
        asynchronous, api_for(item_router(store, asynchronous, **{hook: denied}))
    )
    plain = requester(asynchronous, api_for(item_router(Store(), asynchronous)))
    headers = {"If-Match": '"v1"'}
    assert plain(method, url_for(method), json=body, headers=headers).status_code == 422
    response = request(method, url_for(method), json=body, headers=headers)
    assert response.status_code == 403
    assert len(denied.calls) == 1
    assert store.log == []


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("method", [*METHODS, "POST"])
def test_hook_error_beats_a_bad_path_parameter(asynchronous, hook, method):
    denied = Recorder(HttpError(403, "Denied"))
    request = requester(
        asynchronous, api_for(item_router(Store(), asynchronous, **{hook: denied}))
    )
    url = "/p/x/items" if method == "POST" else "/p/x/items/y"
    assert request(method, url, json=body_for(method)).status_code == 403
    [(_, params)] = denied.calls
    assert params == (
        {"parent": "x"} if method == "POST" else {"parent": "x", "pk": "y"}
    )


@pytest.mark.parametrize("hook", HOOKS)
def test_hook_gets_converted_path_parameters(hook):
    """A path converter's value, not the URL's string, as documented."""
    allowed = Recorder()
    router = Router()

    @router.get("/things/{int:pk}", response=ItemOut)
    @scrudful(**{hook: allowed})
    def get_thing(request, pk: int):
        return {"id": pk, "name": "a", "version": 1}

    assert TestClient(api_for(router)).get("/things/5").status_code == 200
    [(_, params)] = allowed.calls
    assert params == {"pk": 5}
    assert type(params["pk"]) is int


@ASYNC
@pytest.mark.parametrize("method", [*METHODS, "POST"])
@pytest.mark.parametrize(
    "case",
    [
        ("1", {}, BODY),
        ("1", {"If-Match": '"stale"'}, BODY),
        ("1", {"If-Match": '"v1"'}, {}),
        ("x", {"If-Match": '"v1"'}, {"name": ["x"]}),
        ("1", {"If-None-Match": '"v1"'}, BODY),
        ("1", {"If-Match": '"v1"'}, BODY),
        ("9", {"If-Match": '"v1"'}, BODY),
    ],
    ids=["no-precondition", "stale", "bad-body", "bad-pk-and-body", "inm", "ok", "404"],
)
def test_allowed_requests_are_answered_as_without_hooks(asynchronous, method, case):
    """Byte for byte: the 422 lists every bad parameter, and the 412, 428 and
    304 are scrudful's."""
    pk, headers, body = case
    body = body if method in ("PUT", "PATCH", "POST") else None
    before, after = Recorder(), Recorder()
    hooked = requester(
        asynchronous,
        api_for(
            item_router(Store(), asynchronous, before=before, after_throttles=after)
        ),
    )(method, url_for(method, pk), json=body, headers=headers)
    plain = requester(asynchronous, api_for(item_router(Store(), asynchronous)))(
        method, url_for(method, pk), json=body, headers=headers
    )
    assert hooked.status_code == plain.status_code
    assert hooked.content == plain.content
    assert dict(hooked.headers) == dict(plain.headers)
    assert len(before.calls) == len(after.calls) == 1


@ASYNC
@pytest.mark.parametrize("method", ["PUT", "PATCH", "POST"])
def test_a_hook_may_read_the_body(asynchronous, method):
    bodies = []

    def before(request, **params):
        bodies.append(json.loads(request.body))

    store = Store()
    request = requester(
        asynchronous, api_for(item_router(store, asynchronous, after_throttles=before))
    )
    response = request(method, url_for(method), json=BODY, headers={"If-Match": '"v1"'})
    assert response.status_code < 300
    assert response.json()["name"] == "new"
    assert bodies == [BODY]


def serve(api: NinjaAPI, request) -> HttpResponse:
    """Serve a real Django ``request`` (ninja's ``TestClient`` mocks it)."""
    path = request.path.lstrip("/")
    for pattern in api.urls[0]:
        match = pattern.resolve(path)
        if match:
            return match.func(request, *match.args, **match.kwargs)
    raise AssertionError(f"no route for {path}")


@pytest.mark.parametrize("parsed", [False, True], ids=["unparsed", "parsed-by-auth"])
def test_a_hook_may_read_an_upload_through_post_and_files(parsed):
    """A hook reads a multipart body as ``Form`` and ``File`` parameters do,
    also once the authentication parsed it (as ninja's CSRF check of a cookie
    auth does), and the operation still gets it; ``request.body`` can't be
    read then, as documented."""
    seen = []

    def before(request, **params):
        seen.append((request.POST["name"], request.FILES["file"].name))

    def auth(request):
        if parsed:
            request.POST.get("csrfmiddlewaretoken")  # as ninja's CSRF check
        return "alice"

    router = Router()

    @router.post("/p/{parent}/uploads", response={201: ItemOut}, auth=auth)
    @scrudful_create(location_func=location, before=before)
    def upload(request, parent: int, name: Form[str], file: File[UploadedFile]):
        return {"id": 2, "name": f"{name}:{file.read().decode()}", "version": 1}

    request = RequestFactory().post(
        "/p/7/uploads", {"name": "n", "file": SimpleUploadedFile("a.txt", b"data")}
    )
    response = serve(api_for(router), request)
    assert response.status_code == 201
    assert json.loads(response.content)["name"] == "n:data"
    assert seen == [("n", "a.txt")]
    with pytest.raises(RawPostDataException):
        request.body


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
def test_hook_error_never_reaches_operation_decorators(asynchronous, hook):
    decorator = OperationDecorator()
    router = item_router(Store(), asynchronous, **{hook: Recorder(HttpError(403, "x"))})
    request = requester(asynchronous, api_for(router, decorator=decorator))
    assert request("PUT", "/p/7/items/1", json={}).status_code == 403
    assert decorator.calls == 0


@ASYNC
def test_bad_body_after_hooks_never_reaches_operation_decorators(asynchronous):
    decorator = OperationDecorator()
    router = item_router(
        Store(), asynchronous, before=Recorder(), after_throttles=Recorder()
    )
    request = requester(asynchronous, api_for(router, decorator=decorator))
    assert request("PUT", "/p/7/items/1", json={}).status_code == 422
    assert decorator.calls == 0
    response = request("PUT", "/p/7/items/1", json=BODY, headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert decorator.calls == 1


@pytest.mark.parametrize("hook", HOOKS)
def test_hook_error_gets_the_operation_error_links(hook):
    store = Store()
    router = Router()

    @router.get("/p/{parent}/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, error=ERROR, **{hook: Recorder(HttpError(403, "x"))})
    def get_item(request, parent: int, pk: int, obj):
        return obj

    response = TestClient(api_for(router)).get("/p/7/items/1")
    assert response.status_code == 403
    assert "/schemas/error" in response.headers["Link"]


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize(
    "refusal",
    [HttpResponse(status=403), False, True],
    ids=["response", "False", "True"],
)
def test_hook_returning_anything_but_none_fails_closed(asynchronous, hook, refusal):
    store = Store()
    recorder = Recorder(result=refusal)
    request = requester(
        asynchronous, api_for(item_router(store, asynchronous, **{hook: recorder}))
    )
    with pytest.raises(TypeError, match=f"^{hook} returned .*never by returning"):
        request("GET", "/p/7/items/1")
    assert store.log == []


@pytest.mark.parametrize("hook", HOOKS)
def test_sync_hook_returning_an_awaitable_is_an_error(hook):
    coroutines = []

    def returns_coroutine(request, **params):
        coroutines.append(_deny(request))
        return coroutines[-1]

    store = Store()
    client = TestClient(api_for(item_router(store, **{hook: returns_coroutine})))
    with pytest.raises(TypeError, match=f"^{hook} returned an awaitable"):
        client.get("/p/7/items/1")
    [coroutine] = coroutines
    assert coroutine.cr_frame is None
    assert store.log == []


@pytest.mark.parametrize("hook", HOOKS)
def test_sync_hook_returning_a_future_cancels_it(hook):
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        hooks = {hook: lambda request, **params: future}
        client = TestClient(api_for(item_router(Store(), **hooks)))
        with pytest.raises(TypeError, match="awaitable"):
            client.delete("/p/7/items/1", headers={"If-Match": '"v1"'})
        assert future.cancelled()
    finally:
        loop.close()


@pytest.mark.parametrize("hook", HOOKS)
def test_async_hook_in_async_operation(hook):
    denied = AsyncRecorder(HttpError(403, "Denied"))
    request = requester(True, api_for(item_router(Store(), True, **{hook: denied})))
    assert request("PATCH", "/p/7/items/1", json={}).status_code == 403
    assert request("POST", "/p/7/items", json={}).status_code == 403
    assert len(denied.calls) == 2


@pytest.mark.parametrize("decorator", [scrudful, scrudful_create])
@pytest.mark.parametrize("hook", HOOKS)
def test_async_hook_rejected_for_sync_operation(decorator, hook):
    router = Router()
    options = {"location_func": location} if decorator is scrudful_create else {}
    with pytest.raises(TypeError, match=f"{hook} is async"):

        @router.get("/x")
        @decorator(**options, **{hook: AsyncRecorder()})
        def operation(request):
            return None


@pytest.mark.parametrize("decorator", [scrudful, scrudful_create])
@pytest.mark.parametrize("hook", HOOKS)
def test_hook_must_be_callable(decorator, hook):
    options = {"location_func": location} if decorator is scrudful_create else {}
    with pytest.raises(TypeError, match=f"{hook} must be callable"):
        decorator(**options, **{hook: "nope"})


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_openapi_unchanged_by_hooks(asynchronous):
    hooked = api_for(
        item_router(
            Store(), asynchronous, before=Recorder(), after_throttles=Recorder()
        )
    )
    plain = api_for(item_router(Store(), asynchronous))
    schema = [
        json.dumps(api.get_openapi_schema(path_prefix=""), sort_keys=True)
        for api in (hooked, plain)
    ]
    assert schema[0] == schema[1]


# --- The build-time and per-request guards.


@pytest.mark.parametrize(
    "decorator, name",
    [(scrudful, "@scrudful"), (scrudful_create, "@scrudful_create")],
)
@pytest.mark.parametrize("hook", HOOKS)
def test_hook_with_an_opaque_view_decorator_is_a_config_error(decorator, name, hook):
    router = Router()
    options = {"location_func": location} if decorator is scrudful_create else {}
    with pytest.raises(ConfigError) as caught:

        @router.post("/p/{parent}/items", response={201: ItemOut})
        @decorator(**options, **{hook: Recorder()})
        @decorate_view(opaque)
        def operation(request, parent: int):
            return None

    message = str(caught.value)
    assert message.startswith(f"{name}: test_hook_with_an_opaque_")
    assert f"{hook}= can't be installed" in message
    assert f"decorate_view decorator below {name} wraps it" in message


@pytest.mark.parametrize("hook", HOOKS)
def test_operation_subclass_overriding_checks_is_a_config_error(monkeypatch, hook):
    monkeypatch.setattr(ninja_operation, "Operation", CustomChecks)
    with pytest.raises(
        ConfigError, match=f"{hook}= needs ninja's own Operation._run_checks"
    ):
        item_router(Store(), **{hook: Recorder()})


@pytest.mark.parametrize(
    "cls", [ninja_operation.Operation, ninja_operation.AsyncOperation]
)
def test_changed_ninja_checks_are_a_config_error(monkeypatch, cls):
    def _run_checks(self, request):
        return self._check_permissions(request)

    monkeypatch.setattr(cls, "_run_checks", _run_checks)
    with pytest.raises(ConfigError, match="upgrade scrudful"):
        item_router(Store(), cls is ninja_operation.AsyncOperation, before=Recorder())


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("method", [*METHODS, "POST"])
def test_clone_without_run_decorators_fails_closed(
    monkeypatch, asynchronous, hook, method
):
    for cls in (ninja_operation.Operation, ninja_operation.AsyncOperation):
        original = cls.clone

        def clone(self, original=original):
            copy = original(self)
            copy.run = types.MethodType(type(copy).run, copy)
            vars(copy).pop("_run_checks", None)
            copy._run_decorators = []
            return copy

        monkeypatch.setattr(cls, "clone", clone)
    store = Store()
    recorder = Recorder()
    request = requester(
        asynchronous, api_for(item_router(store, asynchronous, **{hook: recorder}))
    )
    with pytest.raises(RuntimeError, match=f": {hook}= didn't run"):
        request(
            method, url_for(method), json=body_for(method), headers={"If-Match": "*"}
        )
    assert recorder.calls == []
    assert store.log == []


def test_item_without_hooks_keeps_ninja_checks():
    """No hook: ninja's own checks, untouched."""
    router = item_router(Store())
    for path_view in router.path_operations.values():
        for operation in path_view.operations:
            assert "_run_checks" not in vars(operation)


def nested_router(asynchronous: bool, hook: str, caller_hooks: bool, log: list[str]):
    """``/b/{pk}``, whose ``hook`` refuses every request (403), and ``/a/{pk}``,
    whose function calls ``/b/{pk}``'s decorated handler directly; ``/a``'s
    own ``before`` allows every request, when ``caller_hooks``."""

    def refuse(request, **params):
        log.append(f"b.{hook}")
        raise HttpError(403, "b refuses")

    def allow(request, **params):
        log.append("a.before")

    def load(request, pk, **params):
        return {"id": pk, "name": "secret", "version": 1}

    callee = {hook: refuse}
    caller = {"before": allow} if caller_hooks else {}
    router = Router()
    if asynchronous:

        @router.get("/b/{pk}", response=ItemOut)
        @scrudful(load=load, **callee)
        async def get_b(request, pk: int, response: HttpResponse, obj):
            log.append("b.handler")
            return obj

        @router.get("/a/{pk}", response=ItemOut)
        @scrudful(load=load, **caller)
        async def get_a(request, pk: int, response: HttpResponse, obj):
            log.append("a.handler")
            return await get_b(request, pk=pk, response=response)

    else:

        @router.get("/b/{pk}", response=ItemOut)
        @scrudful(load=load, **callee)
        def get_b(request, pk: int, response: HttpResponse, obj):
            log.append("b.handler")
            return obj

        @router.get("/a/{pk}", response=ItemOut)
        @scrudful(load=load, **caller)
        def get_a(request, pk: int, response: HttpResponse, obj):
            log.append("a.handler")
            return get_b(request, pk=pk, response=response)

    return router


@ASYNC
@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("caller_hooks", [True, False], ids=["hooked", "hookless"])
def test_a_handler_with_hooks_called_from_another_operation_fails_closed(
    asynchronous, hook, caller_hooks
):
    """A handler whose ``before=`` or ``after_throttles=`` refuses a request
    never runs for it when another operation's function calls the decorated
    handler directly: those hooks didn't run for the request (only the
    caller's did), so the call raises ``RuntimeError``, naming both
    operations and the fix, whatever the caller's own hooks."""
    log: list[str] = []
    request = requester(
        asynchronous, api_for(nested_router(asynchronous, hook, caller_hooks, log))
    )
    direct = request("GET", "/b/1")
    assert direct.status_code == 403
    assert log == [f"b.{hook}"]
    log.clear()
    with pytest.raises(RuntimeError, match="called directly from inside") as raised:
        request("GET", "/a/1")
    message = str(raised.value)
    assert "get_b()" in message and "get_a()" in message and f"{hook}=" in message
    assert "plain function" in message
    assert log == (["a.before", "a.handler"] if caller_hooks else ["a.handler"])


def reentering_router(asynchronous: bool, log: list[Any]) -> Router:
    """``/i/{pk}``, whose ``before`` allows only pk 1, and whose handler for
    pk 1 calls itself, the decorated handler, for pk 2 with ``?peek=1``."""

    def only_one(request, pk, **params):
        log.append(("before", pk))
        if int(pk) != 1:
            raise HttpError(403, "only pk 1")

    def load(request, pk, **params):
        log.append(("load", pk))
        return {"id": pk, "name": "secret", "version": 1}

    router = Router()
    if asynchronous:

        @router.get("/i/{pk}", response=ItemOut)
        @scrudful(load=load, before=only_one)
        async def get_item(request, pk: int, response: HttpResponse, obj):
            if pk == 1 and request.GET.get("peek"):
                return await get_item(request, pk=2, response=response)
            return obj

    else:

        @router.get("/i/{pk}", response=ItemOut)
        @scrudful(load=load, before=only_one)
        def get_item(request, pk: int, response: HttpResponse, obj):
            if pk == 1 and request.GET.get("peek"):
                return get_item(request, pk=2, response=response)
            return obj

    return router


@ASYNC
def test_a_handler_with_hooks_calling_itself_fails_closed(asynchronous):
    """A handler with hooks calling itself, the decorated handler, while its
    operation runs (for other path values) raises the nested-call
    ``RuntimeError``: its ``before`` ran for the request's values only."""
    log: list[Any] = []
    request = requester(asynchronous, api_for(reentering_router(asynchronous, log)))
    assert request("GET", "/i/2").status_code == 403
    assert request("GET", "/i/1").status_code == 200
    log.clear()
    with pytest.raises(RuntimeError, match="called directly from inside") as raised:
        request("GET", "/i/1?peek=1")
    message = str(raised.value)
    assert "get_item()" in message and "before=" in message
    assert "plain function" in message
    assert log == [("before", "1"), ("load", 1)]


def test_an_async_handler_calling_itself_after_its_run_fails_closed():
    """A task the handler spawns and leaves running, calling the decorated
    handler once the operation's run ended (its context still holds the
    request's record), raises ``RuntimeError``: its ``before`` ran for the
    request's path parameters, not for that call's."""
    log: list[Any] = []
    holder: dict[str, Any] = {}

    def only_one(request, pk, **params):
        log.append(("before", pk))
        if int(pk) != 1:
            raise HttpError(403, "only pk 1")

    def load(request, pk, **params):
        log.append(("load", pk))
        return {"id": pk, "name": "secret", "version": 1}

    router = Router()

    @router.get("/i/{pk}", response=ItemOut)
    @scrudful(load=load, before=only_one)
    async def get_item(request, pk: int, response: HttpResponse, obj):
        if pk == 1 and request.GET.get("later"):

            async def later():
                await asyncio.sleep(0.01)
                return await get_item(request, pk=2, response=response)

            holder["task"] = asyncio.create_task(later())
        return obj

    client = TestAsyncClient(api_for(router))

    async def main():
        assert (await client.get("/i/2")).status_code == 403
        assert (await client.get("/i/1?later=1")).status_code == 200
        await holder["task"]

    log.clear()
    with pytest.raises(RuntimeError, match="after its operation's run ended"):
        asyncio.run(main())
    assert ("load", 2) not in log


def test_a_sync_handler_calling_itself_after_its_run_fails_closed():
    """The same from a thread the handler starts in a copy of its context,
    which calls the decorated handler once the operation's run ended."""
    log: list[Any] = []
    holder: dict[str, Any] = {}
    go = threading.Event()

    def only_one(request, pk, **params):
        log.append(("before", pk))
        if int(pk) != 1:
            raise HttpError(403, "only pk 1")

    def load(request, pk, **params):
        log.append(("load", pk))
        return {"id": pk, "name": "secret", "version": 1}

    router = Router()

    @router.get("/i/{pk}", response=ItemOut)
    @scrudful(load=load, before=only_one)
    def get_item(request, pk: int, response: HttpResponse, obj):
        if pk == 1 and request.GET.get("later"):

            def later():
                go.wait(5)
                try:
                    holder["result"] = get_item(request, pk=2, response=response)
                except RuntimeError as exc:
                    holder["error"] = exc

            context = contextvars.copy_context()
            holder["thread"] = threading.Thread(target=context.run, args=(later,))
            holder["thread"].start()
        return obj

    client = TestClient(api_for(router))
    assert client.get("/i/2").status_code == 403
    assert client.get("/i/1?later=1").status_code == 200
    go.set()
    holder["thread"].join(5)
    assert "result" not in holder
    assert "after its operation's run ended" in str(holder["error"])
    assert ("load", 2) not in log


def test_an_operation_decorator_may_run_the_handler_twice():
    """Calling the handler again once it returned (an operation decorator
    retrying it) isn't a nested call."""

    def twice(func):
        @functools.wraps(func)
        def retried(request, *args, **kwargs):
            func(request, *args, **kwargs)
            return func(request, *args, **kwargs)

        return retried

    log: list[Any] = []
    api = api_for(reentering_router(False, log), decorator=twice)
    assert TestClient(api).get("/i/1").status_code == 200
    assert log == [("before", "1"), ("load", 1), ("load", 1)]


def test_a_handler_called_from_another_operation_notes_nothing_of_it():
    """A handler without hooks may be called from inside another operation,
    but what it raised or answered is that operation's to describe: it
    notes nothing on the running operation's record."""
    from scrudful.ninja import _RAISED, _Config, _note_answered, _note_raised
    from scrudful.ninja import _Raised

    running, called = _Config(kind="item"), _Config(kind="item")
    raised = _Raised(running)
    token = _RAISED.set(raised)
    try:
        _note_raised(HttpError(404, "gone"), called)
        _note_answered(called)
        assert not raised.raised_error and not raised.answered
        _note_raised(HttpError(404, "gone"), running)
        _note_answered(running)
        assert raised.raised_error and raised.answered
    finally:
        _RAISED.reset(token)
