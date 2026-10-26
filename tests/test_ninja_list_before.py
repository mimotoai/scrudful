"""``scrudful_list(before=...)``: a callable run after authentication and before
ninja validates any parameter of the operation, so a list can be authorized
before a bad parameter is answered (DRF's order: authentication, permissions,
then the parameters)."""

import functools
import importlib.util
import inspect
import json
import textwrap
import tokenize
import uuid
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from items.models import Item
from ninja import NinjaAPI, Router
from ninja.decorators import decorate_view
from django.core.cache import cache
from django.test import RequestFactory
from ninja import operation as ninja_operation
from ninja.errors import ConfigError, HttpError
from ninja.security import APIKeyHeader
from ninja.testing import TestAsyncClient, TestClient
from ninja.throttling import AnonRateThrottle as Throttle
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


def test_before_with_an_opaque_view_decorator_is_a_config_error():
    """``before`` can't be installed: fail when the operation is built."""
    router = Router()
    with pytest.raises(ConfigError) as caught:

        @router.get("/p/{parent}/items", response=list[ItemOut])
        @scrudful_list(href_func=href, before=Recorder())
        @decorate_view(opaque)
        def list_items(request, parent: int):
            return Item.objects.order_by("pk")

    message = str(caught.value)
    assert message.startswith("@scrudful_list: test_before_with_an_opaque_")
    assert "decorate_view decorator below @scrudful_list" in message
    assert "functools.wraps" in message
    assert "_run_checks" not in message


def test_opaque_view_decorator_without_before_is_fine(items):
    router = Router()

    @router.get("/p/{parent}/items", response=list[ItemOut])
    @scrudful_list(href_func=href)
    @decorate_view(opaque)
    def list_items(request, parent: int):
        return Item.objects.order_by("pk")

    assert TestClient(api_for(router)).get("/p/7/items").status_code == 200


async def _deny(request):
    raise HttpError(403, "Denied")


def test_sync_before_returning_an_awaitable_is_an_error(items):
    """A sync operation can't await it: never proceed as if authorized."""
    before = lambda request, **params: _deny(request)  # noqa: E731
    client = TestClient(api_for(list_router(before=before)))
    with pytest.raises(TypeError, match="awaitable"):
        client.get("/p/7/items")


@pytest.fixture
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@ASYNC
def test_before_runs_before_throttles(items, fresh_cache, asynchronous):
    """DRF's order: authentication, permissions, throttles. A denied request
    uses none of the throttle's quota."""
    denied = Recorder(HttpError(403, "Denied"))
    api = api_for(list_router(asynchronous, before=denied), throttle=Throttle("1/m"))
    get = getter(asynchronous, api)
    assert [get("/p/7/items").status_code for _ in range(3)] == [403, 403, 403]
    allowed = Recorder()
    api = api_for(list_router(asynchronous, before=allowed), throttle=Throttle("1/m"))
    get = getter(asynchronous, api)
    assert [get("/p/7/items").status_code for _ in range(2)] == [200, 429]
    assert len(allowed.calls) == 2


# --- The copy of ninja's checks (scrudful runs before between auth and
# throttles): guarded against an override or a changed ninja.


class CustomChecks(ninja_operation.Operation):
    def _run_checks(self, request):
        return super()._run_checks(request)


def test_operation_subclass_overriding_checks_is_a_config_error(monkeypatch):
    monkeypatch.setattr(ninja_operation, "Operation", CustomChecks)
    with pytest.raises(ConfigError, match="needs ninja's own Operation._run_checks"):
        list_router(before=Recorder())


def test_operation_subclass_without_its_own_checks_is_fine(monkeypatch, items):
    class Plain(ninja_operation.Operation):
        pass

    monkeypatch.setattr(ninja_operation, "Operation", Plain)
    before = Recorder(HttpError(403, "Denied"))
    assert (
        TestClient(api_for(list_router(before=before))).get("/p/7/items").status_code
        == 403
    )


@pytest.mark.parametrize(
    "cls", [ninja_operation.Operation, ninja_operation.AsyncOperation]
)
def test_changed_ninja_checks_are_a_config_error(monkeypatch, cls):
    """A ninja whose checks do something the copy doesn't."""

    def _run_checks(self, request):
        error = self._check_permissions(request)
        if error:
            return error
        return None

    monkeypatch.setattr(cls, "_run_checks", _run_checks)
    with pytest.raises(ConfigError) as caught:
        list_router(cls is ninja_operation.AsyncOperation, before=Recorder())
    message = str(caught.value)
    assert "needs ninja's own Operation._run_checks" in message
    assert "ninja 1.7.x" in message
    assert "upgrade scrudful" in message
    assert "pin" not in message
    assert "1.6" not in message


def _same_names_run_checks(self, request):
    """ninja's sync checks with an authentication failure ignored: the same
    names, locals and arguments, another behaviour."""
    if self.csrf_exempt:
        request._ninja_csrf_exempt = True
    if self.auth_callbacks:
        error = self._run_authentication(request)
    if self.throttle_objects:
        error = self._check_throttles(request)
        if error:
            return error
    return None


def _code_fingerprint(func):
    code = func.__code__
    return code.co_names, code.co_varnames, code.co_argcount


def test_changed_checks_with_the_same_names_are_a_config_error(monkeypatch):
    original = ninja_operation.Operation._run_checks
    assert _code_fingerprint(_same_names_run_checks) == _code_fingerprint(original)
    monkeypatch.setattr(
        ninja_operation.Operation, "_run_checks", _same_names_run_checks
    )
    with pytest.raises(ConfigError, match="needs ninja's own Operation._run_checks"):
        list_router(before=Recorder())


@pytest.fixture
def fresh_checks_cache():
    """The check of ninja's checks is memoized per function: start afresh."""
    import scrudful.ninja

    scrudful.ninja._is_ninja_checks.cache_clear()
    yield
    scrudful.ninja._is_ninja_checks.cache_clear()


def _different_names_run_checks(self, request):
    return self._check_permissions(request)


def test_without_source_the_code_fingerprint_is_checked(
    monkeypatch, items, fresh_checks_cache
):
    """Where the source isn't available (e.g. only .pyc installed)."""
    import scrudful.ninja

    monkeypatch.setattr(scrudful.ninja, "_source_digest", lambda func: None)
    before = Recorder(HttpError(403, "Denied"))
    response = TestClient(api_for(list_router(before=before))).get("/p/7/items")
    assert response.status_code == 403
    monkeypatch.setattr(
        ninja_operation.Operation, "_run_checks", _different_names_run_checks
    )
    with pytest.raises(ConfigError, match="needs ninja's own Operation._run_checks"):
        list_router(before=Recorder())


@pytest.mark.parametrize(
    "error",
    [tokenize.TokenError("EOF in multi-line statement"), IndentationError("x")],
)
def test_tokenize_failure_falls_back_to_the_code_fingerprint(
    monkeypatch, items, fresh_checks_cache, error
):
    import scrudful.ninja

    def fail(readline):
        raise error

    monkeypatch.setattr(scrudful.ninja.tokenize, "generate_tokens", fail)
    assert scrudful.ninja._source_digest(ninja_operation.Operation._run_checks) is None
    before = Recorder(HttpError(403, "Denied"))
    response = TestClient(api_for(list_router(before=before))).get("/p/7/items")
    assert response.status_code == 403
    monkeypatch.setattr(
        ninja_operation.Operation, "_run_checks", _different_names_run_checks
    )
    with pytest.raises(ConfigError, match="needs ninja's own Operation._run_checks"):
        list_router(before=Recorder())


def _edited_checks(tmp_path, cls, edit):
    """``cls._run_checks`` as a module function whose source is ninja's with
    ``edit`` applied to its lines (so ``inspect.getsource`` finds it)."""
    lines = textwrap.dedent(inspect.getsource(cls._run_checks)).splitlines()
    module = tmp_path / f"edited_{cls.__name__}.py"
    module.write_text(
        "from typing import Optional\n"
        "from django.http import HttpRequest, HttpResponse\n\n\n"
        + "\n".join(edit(lines))
        + "\n"
    )
    spec = importlib.util.spec_from_file_location(module.stem, module)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded._run_checks


def _comments_and_blank_lines(lines):
    out = [lines[0], "    # an added comment", ""]
    for line in lines[1:]:
        out.append(line + "  # trailing" if line.strip().endswith(":") else line)
        if not line.strip():
            continue
        out.append("")
    return out


@ASYNC
def test_comment_and_blank_line_edits_of_ninja_checks_pass(
    monkeypatch, tmp_path, items, asynchronous, fresh_checks_cache
):
    cls = ninja_operation.AsyncOperation if asynchronous else ninja_operation.Operation
    edited = _edited_checks(tmp_path, cls, _comments_and_blank_lines)
    assert inspect.getsource(edited) != inspect.getsource(cls._run_checks)
    monkeypatch.setattr(cls, "_run_checks", edited)
    before = Recorder(HttpError(403, "Denied"))
    get = getter(asynchronous, api_for(list_router(asynchronous, before=before)))
    assert get("/p/7/items").status_code == 403


def test_reordered_ninja_checks_fail(monkeypatch, tmp_path, fresh_checks_cache):
    """Throttles before authentication: the same tokens, another order."""

    def reorder(lines):
        text = "\n".join(lines)
        auth = text[text.index("    # auth:") : text.index("    # Throttling:")]
        throttle = text[text.index("    # Throttling:") : text.index("    return None")]
        return text.replace(auth + throttle, throttle + auth).splitlines()

    cls = ninja_operation.Operation
    edited = _edited_checks(tmp_path, cls, reorder)
    monkeypatch.setattr(cls, "_run_checks", edited)
    with pytest.raises(ConfigError, match="needs ninja's own Operation._run_checks"):
        list_router(before=Recorder())


def test_ninja_checks_are_checked_once_per_function(monkeypatch, fresh_checks_cache):
    import scrudful.ninja

    calls = []
    original = scrudful.ninja._source_digest

    def counting(func):
        calls.append(func)
        return original(func)

    monkeypatch.setattr(scrudful.ninja, "_source_digest", counting)
    routers = [list_router(before=Recorder()) for _ in range(3)]
    for router in routers:
        api_for(router)
        api_for(router)
    assert calls == [ninja_operation.Operation._run_checks]


def test_router_mounted_with_and_without_auth(items):
    """ninja applies a mount's auth to its clone of the operation."""
    before = Recorder()
    router = list_router(before=before)
    open_api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    open_api.add_router("", router)
    auth_api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    auth_api.add_router("", router, auth=Key())
    assert TestClient(open_api).get("/p/7/items").status_code == 200
    assert len(before.calls) == 1
    assert TestClient(auth_api).get("/p/7/items").status_code == 401
    assert len(before.calls) == 1
    response = TestClient(auth_api).get("/p/7/items", headers={"X-API-Key": "alice"})
    assert response.status_code == 200
    assert len(before.calls) == 2
    assert before.calls[-1][0].auth == "alice"
    assert getattr(before.calls[0][0], "auth", None) is None


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("csrf_exempt", [False, True])
def test_checks_copy_sets_the_csrf_flag_as_ninja(asynchronous, csrf_exempt):
    router = list_router(asynchronous, before=Recorder())
    [operation] = [
        op
        for path_view in router.path_operations.values()
        for op in path_view.operations
    ]
    operation.csrf_exempt = csrf_exempt
    operation.auth_callbacks = []
    operation.throttle_objects = []
    flags = []
    for checks in (
        operation._run_checks,
        functools.partial(type(operation)._run_checks, operation),
    ):
        request = RequestFactory().get("/p/7/items")
        result = checks(request)
        if asynchronous:
            result = async_to_sync(_awaited)(result)
        assert result is None
        flags.append(getattr(request, "_ninja_csrf_exempt", None))
    assert flags[0] == flags[1] == (True if csrf_exempt else None)


async def _awaited(awaitable):
    return await awaitable
