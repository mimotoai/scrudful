"""``scrudful_list(after_throttles=...)``: a callable run after authentication,
``before`` and throttling, and before ninja validates any parameter of the
operation: where DRF's ``list()`` runs its filters (DRF's order:
authentication, permissions, throttles, then the list's own work, then the
page parameters)."""

import asyncio
import json
import types

import pytest
from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory
from items.models import Item
from ninja import Router
from ninja import operation as ninja_operation
from ninja.decorators import decorate_view
from ninja.errors import ConfigError, HttpError
from ninja.testing import TestClient
from ninja.throttling import AnonRateThrottle
from scrudful.ninja import scrudful_list

from tests.test_ninja_adapter import ItemOut
from tests.test_ninja_list import href
from tests.test_ninja_list_before import (
    ASYNC,
    BAD,
    ERROR,
    GOOD,
    AsyncRecorder,
    CustomChecks,
    CustomRun,
    Key,
    OperationDecorator,
    Recorder,
    _awaited,
    _deny,
    api_for,
    getter,
    list_router,
    opaque,
)

TOO_LARGE = HttpError(400, "conditional_list_too_large")


@pytest.fixture
def items(db) -> list[Item]:
    return [Item.objects.create(name=name) for name in ("a", "b", "c")]


@pytest.fixture
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


class Logged(Recorder):
    """A hook appending ``name`` to ``log`` when called."""

    def __init__(self, log: list[str], name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.log = log
        self.name = name

    def __call__(self, request, **params):
        self.log.append(self.name)
        return super().__call__(request, **params)


class LoggedKey(Key):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self.log = log

    def authenticate(self, request, key):
        self.log.append("auth")
        return super().authenticate(request, key)


class LoggedThrottle(AnonRateThrottle):
    def __init__(self, log: list[str], rate: str) -> None:
        super().__init__(rate)
        self.log = log

    def allow_request(self, request) -> bool:
        self.log.append("throttle")
        return super().allow_request(request)


ALICE = {"X-API-Key": "alice"}


@ASYNC
def test_order_is_auth_before_throttles_after_throttles_parameters(
    items, fresh_cache, asynchronous
):
    log: list[str] = []
    router = list_router(
        asynchronous,
        before=Logged(log, "before"),
        after_throttles=Logged(log, "after_throttles"),
    )
    api = api_for(router, auth=LoggedKey(log), throttle=LoggedThrottle(log, "10/m"))
    get = getter(asynchronous, api)
    assert get("/p/7/items?page=x", headers=ALICE).status_code == 422
    assert log == ["auth", "before", "throttle", "after_throttles"]
    log.clear()
    assert get("/p/7/items", headers=ALICE).status_code == 200
    assert log == ["auth", "before", "throttle", "after_throttles"]
    log.clear()
    assert get("/p/7/items?page=x").status_code == 401
    assert log == ["auth"]


@ASYNC
def test_a_throttled_request_never_calls_after_throttles(
    items, fresh_cache, asynchronous
):
    before = Recorder()
    after = Recorder()
    router = list_router(asynchronous, before=before, after_throttles=after)
    api = api_for(router, throttle=AnonRateThrottle("1/m"))
    get = getter(asynchronous, api)
    assert [get("/p/7/items?page=x").status_code for _ in range(2)] == [422, 429]
    assert len(before.calls) == 2
    assert len(after.calls) == 1


@ASYNC
def test_after_throttles_alone_runs_after_throttles(items, fresh_cache, asynchronous):
    log: list[str] = []
    router = list_router(asynchronous, after_throttles=Logged(log, "after_throttles"))
    api = api_for(router, throttle=LoggedThrottle(log, "1/m"))
    get = getter(asynchronous, api)
    assert [get("/p/7/items").status_code for _ in range(2)] == [200, 429]
    assert log == ["throttle", "after_throttles", "throttle"]


@ASYNC
def test_after_throttles_error_uses_the_throttle_quota(
    items, fresh_cache, asynchronous
):
    """As in DRF, where the throttles run before ``list()``."""
    after = Recorder(TOO_LARGE)
    router = list_router(asynchronous, after_throttles=after)
    get = getter(asynchronous, api_for(router, throttle=AnonRateThrottle("1/m")))
    assert [get("/p/7/items").status_code for _ in range(2)] == [400, 429]
    assert len(after.calls) == 1


@ASYNC
@pytest.mark.parametrize("query", BAD)
def test_after_throttles_error_wins_over_bad_parameters(items, asynchronous, query):
    after = Recorder(TOO_LARGE)
    get = getter(
        asynchronous, api_for(list_router(asynchronous, after_throttles=after))
    )
    response = get(f"/p/7/items?{query}")
    assert response.status_code == 400
    assert response.json() == {"detail": "conditional_list_too_large"}
    assert len(after.calls) == 1


@ASYNC
def test_after_throttles_error_wins_over_a_bad_path_parameter(items, asynchronous):
    after = Recorder(TOO_LARGE)
    get = getter(
        asynchronous, api_for(list_router(asynchronous, after_throttles=after))
    )
    assert get("/p/x/items?page=0").status_code == 400
    [(_, params)] = after.calls
    assert params == {"parent": "x"}


@ASYNC
def test_before_error_never_calls_after_throttles(items, asynchronous):
    before = Recorder(HttpError(403, "Denied"))
    after = Recorder(TOO_LARGE)
    router = list_router(asynchronous, before=before, after_throttles=after)
    assert getter(asynchronous, api_for(router))("/p/7/items").status_code == 403
    assert after.calls == []


@ASYNC
@pytest.mark.parametrize("hooks", ["after_throttles", "both"])
@pytest.mark.parametrize("query", BAD)
def test_bad_parameters_after_the_hooks_are_the_same_422(
    items, asynchronous, hooks, query
):
    """One 422 listing every bad parameter, byte for byte as without hooks."""
    after = Recorder()
    options = {"after_throttles": after}
    if hooks == "both":
        options["before"] = Recorder()
    url = f"/p/7/items?{query}"
    hooked = getter(asynchronous, api_for(list_router(asynchronous, **options)))(url)
    plain = getter(asynchronous, api_for(list_router(asynchronous)))(url)
    assert plain.status_code == hooked.status_code == 422
    assert hooked.content == plain.content
    assert dict(hooked.headers) == dict(plain.headers)
    assert len(after.calls) == 1


@ASYNC
def test_combined_bad_parameters_listed_in_one_422(items, asynchronous):
    after = Recorder()
    router = list_router(asynchronous, before=Recorder(), after_throttles=after)
    response = getter(asynchronous, api_for(router))(
        "/p/x/items?n=z&page=x&page_size=0"
    )
    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [
        ["path", "parent"],
        ["query", "n"],
        ["query", "page"],
        ["query", "page_size"],
    ]
    assert len(after.calls) == 1


@ASYNC
@pytest.mark.parametrize("query", GOOD)
def test_after_throttles_called_once_with_the_path_parameters(
    items, asynchronous, query
):
    after = Recorder()
    url = f"/p/7/items?q=&{query}"
    hooked = getter(
        asynchronous, api_for(list_router(asynchronous, after_throttles=after))
    )(url)
    plain = getter(asynchronous, api_for(list_router(asynchronous)))(url)
    assert hooked.status_code == plain.status_code == 200
    assert hooked.content == plain.content
    [(request, params)] = after.calls
    assert request.method == "GET"
    assert params == {"parent": "7"}


@ASYNC
def test_each_hook_gets_its_own_path_parameters(items, asynchronous):
    def before(request, **params):
        params["parent"] = "changed"

    after = Recorder()
    router = list_router(asynchronous, before=before, after_throttles=after)
    assert getter(asynchronous, api_for(router))("/p/7/items").status_code == 200
    [(_, params)] = after.calls
    assert params == {"parent": "7"}


@ASYNC
def test_after_throttles_error_never_reaches_operation_decorators(items, asynchronous):
    decorator = OperationDecorator()
    router = list_router(asynchronous, after_throttles=Recorder(TOO_LARGE))
    api = api_for(router, decorator=decorator)
    assert getter(asynchronous, api)("/p/7/items").status_code == 400
    assert decorator.calls == 0


@ASYNC
@pytest.mark.parametrize("query", BAD)
def test_bad_parameters_never_reach_operation_decorators(items, asynchronous, query):
    decorator = OperationDecorator()
    router = list_router(asynchronous, after_throttles=Recorder())
    api = api_for(router, decorator=decorator)
    assert getter(asynchronous, api)(f"/p/7/items?{query}").status_code == 422
    assert decorator.calls == 0


@ASYNC
def test_operation_decorators_still_run_for_good_requests(items, asynchronous):
    decorator = OperationDecorator()
    router = list_router(asynchronous, before=Recorder(), after_throttles=Recorder())
    api = api_for(router, decorator=decorator)
    assert getter(asynchronous, api)("/p/7/items").status_code == 200
    assert decorator.calls == 1


def test_after_throttles_error_gets_the_operation_error_links(items):
    router = list_router(after_throttles=Recorder(TOO_LARGE), error=ERROR)
    response = TestClient(api_for(router, error=ERROR)).get("/p/7/items?page=x")
    assert response.status_code == 400
    assert "/schemas/error" in response.headers["Link"]


def test_after_throttles_error_without_error_entity_has_no_links(items):
    router = list_router(after_throttles=Recorder(TOO_LARGE))
    response = TestClient(api_for(router)).get("/p/7/items")
    assert response.status_code == 400
    assert "Link" not in response.headers


def test_router_mounted_twice_runs_after_throttles_in_each_mount(items):
    after = Recorder(TOO_LARGE)
    router = list_router(after_throttles=after)
    for api in (api_for(router), api_for(router)):
        assert TestClient(api).get("/p/7/items?page=x").status_code == 400
    assert len(after.calls) == 2


def test_async_after_throttles_in_async_operation(items):
    after = AsyncRecorder(TOO_LARGE)
    get = getter(True, api_for(list_router(True, after_throttles=after)))
    assert get("/p/7/items?page=x").status_code == 400
    assert len(after.calls) == 1


def test_async_after_throttles_rejected_for_sync_operation():
    with pytest.raises(TypeError, match="after_throttles is async"):
        list_router(False, after_throttles=AsyncRecorder())


def test_after_throttles_must_be_callable():
    with pytest.raises(TypeError, match="after_throttles must be callable"):
        scrudful_list(href_func=href, after_throttles="nope")


@ASYNC
@pytest.mark.parametrize(
    "refusal",
    [HttpResponse(status=403), False, True],
    ids=["response", "False", "True"],
)
def test_after_throttles_returning_anything_but_none_fails_closed(
    items, asynchronous, refusal
):
    after = Recorder(result=refusal)
    get = getter(
        asynchronous, api_for(list_router(asynchronous, after_throttles=after))
    )
    with pytest.raises(
        TypeError, match="^after_throttles returned .*never by returning"
    ):
        get("/p/7/items")
    assert len(after.calls) == 1


def test_sync_after_throttles_returning_an_awaitable_is_an_error(items):
    coroutines = []

    def after(request, **params):
        coroutines.append(_deny(request))
        return coroutines[-1]

    client = TestClient(api_for(list_router(after_throttles=after)))
    with pytest.raises(TypeError, match="^after_throttles returned an awaitable"):
        client.get("/p/7/items")
    [coroutine] = coroutines
    assert coroutine.cr_frame is None  # closed, never awaited


def test_sync_after_throttles_returning_a_future_cancels_it(items):
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        after = lambda request, **params: future  # noqa: E731
        client = TestClient(api_for(list_router(after_throttles=after)))
        with pytest.raises(TypeError, match="awaitable"):
            client.get("/p/7/items")
        assert future.cancelled()
    finally:
        loop.close()


def test_async_after_throttles_returning_an_awaitable_fails_closed(items):
    async def after(request, **params):
        return _deny(request)

    get = getter(True, api_for(list_router(True, after_throttles=after)))
    with pytest.raises(TypeError, match="never by returning"):
        get("/p/7/items")


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_openapi_unchanged_by_after_throttles(asynchronous):
    hooked = api_for(
        list_router(asynchronous, before=Recorder(), after_throttles=Recorder())
    )
    plain = api_for(list_router(asynchronous))
    schema = [
        json.dumps(api.get_openapi_schema(path_prefix=""), sort_keys=True)
        for api in (hooked, plain)
    ]
    assert schema[0] == schema[1]


# --- The same build-time and per-request guards as ``before``.


def test_after_throttles_with_an_opaque_view_decorator_is_a_config_error():
    router = Router()
    with pytest.raises(ConfigError) as caught:

        @router.get("/p/{parent}/items", response=list[ItemOut])
        @scrudful_list(href_func=href, after_throttles=Recorder())
        @decorate_view(opaque)
        def list_items(request, parent: int):
            return Item.objects.order_by("pk")

    message = str(caught.value)
    assert message.startswith("@scrudful_list: test_after_throttles_with_an_opaque_")
    assert "after_throttles= can't be installed" in message
    assert "decorate_view decorator below @scrudful_list" in message


def test_operation_subclass_overriding_checks_is_a_config_error(monkeypatch):
    monkeypatch.setattr(ninja_operation, "Operation", CustomChecks)
    with pytest.raises(
        ConfigError, match="after_throttles= needs ninja's own Operation._run_checks"
    ):
        list_router(after_throttles=Recorder())


def test_operation_subclass_overriding_run_is_a_config_error(monkeypatch):
    monkeypatch.setattr(ninja_operation, "Operation", CustomRun)
    with pytest.raises(ConfigError, match="CustomRun overrides run of ninja's"):
        list_router(after_throttles=Recorder())


def test_both_hooks_are_named_in_the_config_error(monkeypatch):
    monkeypatch.setattr(ninja_operation, "Operation", CustomChecks)
    with pytest.raises(
        ConfigError,
        match="before= and after_throttles= need ninja's own Operation._run_checks",
    ):
        list_router(before=Recorder(), after_throttles=Recorder())


@pytest.mark.parametrize(
    "cls", [ninja_operation.Operation, ninja_operation.AsyncOperation]
)
def test_changed_ninja_checks_are_a_config_error(monkeypatch, cls):
    def _run_checks(self, request):
        return self._check_permissions(request)

    monkeypatch.setattr(cls, "_run_checks", _run_checks)
    with pytest.raises(ConfigError) as caught:
        list_router(cls is ninja_operation.AsyncOperation, after_throttles=Recorder())
    message = str(caught.value)
    assert "after_throttles= needs ninja's own Operation._run_checks" in message
    assert "upgrade scrudful" in message


@ASYNC
@pytest.mark.parametrize("hooks", ["after_throttles", "both"])
def test_clone_without_run_decorators_fails_closed(
    monkeypatch, items, asynchronous, hooks
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
    after = Recorder()
    options = {"after_throttles": after}
    if hooks == "both":
        options["before"] = Recorder()
    name = "before= and after_throttles=" if hooks == "both" else "after_throttles="
    get = getter(asynchronous, api_for(list_router(asynchronous, **options)))
    with pytest.raises(RuntimeError, match=f": {name} didn't run"):
        get("/p/7/items")
    assert after.calls == []


@ASYNC
def test_checks_outside_operation_run_never_call_after_throttles(asynchronous):
    after = Recorder()
    router = list_router(asynchronous, after_throttles=after)
    [operation] = [
        op
        for path_view in router.path_operations.values()
        for op in path_view.operations
    ]
    operation.auth_callbacks = []
    with pytest.raises(RuntimeError, match="can't get the path parameters"):
        result = operation._run_checks(RequestFactory().get("/p/7/items"))
        if asynchronous:
            from asgiref.sync import async_to_sync

            async_to_sync(_awaited)(result)
    assert after.calls == []


def test_before_alone_is_unchanged_by_after_throttles_support(items, fresh_cache):
    """``before`` still runs before the throttles, ``after_throttles`` unset."""
    log: list[str] = []
    router = list_router(before=Logged(log, "before"))
    api = api_for(router, throttle=LoggedThrottle(log, "10/m"))
    assert TestClient(api).get("/p/7/items").status_code == 200
    assert log == ["before", "throttle"]
