"""The bodies scrudful's ninja adapter writes itself (the early 400, 412 and
428) go through the serving API's renderer, as every other response. OPTIONS
has none: it is a 204, whatever the renderer (the HTTP Schema Resource it links
to is served by its route as compact JSON, see ``test_ninja_hsr``)."""

import functools
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.utils.translation import gettext_lazy

import pytest
from asgiref.sync import async_to_sync
from ninja import NinjaAPI, Router
from ninja.decorators import decorate_view
from ninja.renderers import BaseRenderer, JSONRenderer
from ninja.testing import TestAsyncClient, TestClient
from scrudful.ninja import Entity, ScrudfulRouter, scrudful
from scrudful.problem import PROBLEM_JSON, problem_details

from tests.test_ninja_adapter import (
    LINKS,
    TEST_BASE_URL,
    ItemIn,
    ItemOut,
    Store,
    etag,
    modified,
    update,
)

PROBLEM = Entity(
    schema_link_or_func="/schemas/problem",
    context_link_or_func="/contexts/problem",
    media_type=PROBLEM_JSON,
)


class CompactRenderer(JSONRenderer):
    """Compact separators and UTF-8 non-ASCII: other bytes than JsonResponse's."""

    json_dumps_params = {"separators": (",", ":"), "ensure_ascii": False}


class TextRenderer(BaseRenderer):
    """A renderer that does not write JSON."""

    media_type = "text/plain"

    def render(self, request, data, *, response_status):
        return f"status {response_status}"


class Latin1Renderer(JSONRenderer):
    """Writes non-ASCII as is, in Latin-1."""

    charset = "latin-1"
    json_dumps_params = {"ensure_ascii": False}


class PlainRenderer(BaseRenderer):
    """Plain ``json.dumps``, as orjson: no datetime, Decimal, UUID or lazy
    strings of its own."""

    media_type = "application/json"

    def render(self, request, data, *, response_status):
        return json.dumps(data)


def compact(body: Any) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def accented_problem(request, result):
    return {**problem_details(request, result), "title": "Précondition échouée"}


def router(store: Store, asynchronous: bool = False, **extra: Any) -> Router:
    """GET and PUT on ``/items/{pk}`` with validators, served by a ScrudfulRouter."""
    options = dict(
        load=store.load, etag_func=etag, last_modified_func=modified, **LINKS, **extra
    )
    items = ScrudfulRouter(base_url=TEST_BASE_URL)

    if asynchronous:

        @items.get("/items/{pk}", response=ItemOut)
        @scrudful(**options)
        async def get_item(request, pk: int, obj):
            return obj

        @items.put("/items/{pk}", response=ItemOut)
        @scrudful(**options)
        async def put_item(request, pk: int, payload: ItemIn, obj):
            update(obj, payload.name)
            return obj

    else:

        @items.get("/items/{pk}", response=ItemOut)
        @scrudful(**options)
        def get_item(request, pk: int, obj):
            return obj

        @items.put("/items/{pk}", response=ItemOut)
        @scrudful(**options)
        def put_item(request, pk: int, payload: ItemIn, obj):
            update(obj, payload.name)
            return obj

    return items


def api_for(items: Router, renderer: BaseRenderer | None = None) -> NinjaAPI:
    api = NinjaAPI(renderer=renderer, urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", items)
    return api


def requests(asynchronous: bool, api: NinjaAPI):
    """``request(method, path, **kwargs)`` through the (async) test client."""
    if asynchronous:
        client = TestAsyncClient(api)
        return lambda *args, **kwargs: async_to_sync(client.request)(*args, **kwargs)
    client = TestClient(api)
    return client.request


#: (method, headers, status) of each response scrudful writes itself.
EARLY = [
    ("PUT", {"If-Match": '"v0"'}, 412),
    ("PUT", {}, 428),
    ("PUT", {"If-Match": "v1"}, 400),
]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method, headers, status", EARLY)
def test_custom_renderer_writes_scrudful_bodies(asynchronous, method, headers, status):
    responses = [
        requests(asynchronous, api_for(router(Store(), asynchronous), renderer))(
            method, "/items/1", json={"name": "x"}, headers=headers
        )
        for renderer in (CompactRenderer(), None)
    ]
    response, default = responses
    assert response.status_code == default.status_code == status
    assert response.content == compact(response.json())
    assert response.json() == default.json()
    assert dict(response.headers) == dict(default.headers)
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "renderer",
    [None, CompactRenderer(), TextRenderer(), Latin1Renderer(), PlainRenderer()],
    ids=["default", "compact", "text", "latin-1", "plain"],
)
def test_options_has_no_body_whatever_the_renderer(asynchronous, renderer):
    """OPTIONS was the renderer's: now a 204 without content or Content-Type."""
    request = requests(asynchronous, api_for(router(Store(), asynchronous), renderer))
    response = request("OPTIONS", "/items/1")
    assert response.status_code == 204
    assert response.content == b""
    assert "Content-Type" not in response.headers
    assert response.headers["Allow"] == "GET, PUT, OPTIONS, HEAD"


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method, headers, status", EARLY)
def test_custom_renderer_error_entity_media_type(asynchronous, method, headers, status):
    items = router(
        Store(), asynchronous, error=PROBLEM, error_body_func=accented_problem
    )
    request = requests(asynchronous, api_for(items, CompactRenderer()))
    response = request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.json()["title"] == "Précondition échouée"
    assert response.content == compact(response.json())
    assert response.headers["Content-Type"] == f"{PROBLEM_JSON}; charset=utf-8"
    assert "/schemas/problem" in response.headers["Link"]
    assert "Link" in response.headers["Access-Control-Expose-Headers"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method, headers, status", EARLY)
def test_default_renderer_bodies_unchanged(asynchronous, method, headers, status):
    request = requests(asynchronous, api_for(router(Store(), asynchronous)))
    response = request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.content == json.dumps(response.json()).encode()
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_not_modified_stays_bodiless(asynchronous):
    request = requests(
        asynchronous, api_for(router(Store(), asynchronous), CompactRenderer())
    )
    response = request("GET", "/items/1", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.content == b""


def test_each_mount_uses_its_own_api_renderer():
    """ninja clones the operations of a router mounted in several APIs."""
    items = router(Store())
    compact_api = api_for(items, CompactRenderer())
    default_api = api_for(items)
    for api, dumps in (
        (compact_api, compact),
        (default_api, lambda body: json.dumps(body).encode()),
    ):
        client = TestClient(api)
        for method, headers, status in EARLY:
            response = client.request(
                method, "/items/1", json={"name": "x"}, headers=headers
            )
            assert response.status_code == status
            assert response.content == dumps(response.json())


@pytest.mark.parametrize("method, headers, status", EARLY)
def test_non_json_renderer_falls_back_to_json(method, headers, status):
    client = TestClient(api_for(router(Store()), TextRenderer()))
    response = client.request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.content == json.dumps(response.json()).encode()
    assert response.headers["Content-Type"] == "application/json"


def passthrough(run):
    @functools.wraps(run)
    def wrapped(request, *args, **kwargs):
        return run(request, *args, **kwargs)

    return wrapped


def test_renderer_found_through_earlier_run_decorators():
    """A ``decorate_view`` below scrudful wraps ``Operation.run`` first."""
    store = Store()
    items = Router()

    @items.put("/items/{pk}", response=ItemOut)
    @scrudful(load=store.load, etag_func=etag)
    @decorate_view(passthrough)
    def put_item(request, pk: int, payload: ItemIn, obj):
        update(obj, payload.name)
        return obj

    client = TestClient(api_for(items, CompactRenderer()))
    response = client.put("/items/1", json={"name": "x"})
    assert response.status_code == 428
    assert response.content == compact(response.json())


@pytest.mark.parametrize("method, headers, status", EARLY)
def test_renderer_charset_encodes_and_labels_scrudful_bodies(method, headers, status):
    items = router(Store(), error=PROBLEM, error_body_func=accented_problem)
    client = TestClient(api_for(items, Latin1Renderer()))
    response = client.request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.headers["Content-Type"] == f"{PROBLEM_JSON}; charset=latin-1"
    body = json.loads(response.content.decode("latin-1"))
    assert body["title"] == "Précondition échouée"
    assert "é".encode("latin-1") in response.content


#: Values ``JsonResponse`` (``DjangoJSONEncoder``) writes, plain JSON doesn't.
DJANGO_VALUES = {
    "at": datetime(2026, 10, 26, 7, 45, tzinfo=timezone.utc),
    "amount": Decimal("1.50"),
    "id": uuid.UUID(int=7),
    "detail": gettext_lazy("Precondition failed"),
}


def django_values_problem(request, result):
    return {**problem_details(request, result), **DJANGO_VALUES}


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method, headers, status", EARLY)
def test_plain_renderer_writes_what_json_response_did(
    asynchronous, method, headers, status
):
    items = router(
        Store(), asynchronous, error=PROBLEM, error_body_func=django_values_problem
    )
    request = requests(asynchronous, api_for(items, PlainRenderer()))
    response = request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    expected = json.loads(json.dumps(DJANGO_VALUES, cls=DjangoJSONEncoder))
    assert {key: response.json()[key] for key in DJANGO_VALUES} == expected


class EpochEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, datetime):
            return int(o.timestamp())
        return super().default(o)


class EpochRenderer(JSONRenderer):
    encoder_class = EpochEncoder


class OwnRenderJSONRenderer(JSONRenderer):
    """ninja's renderer class, with a ``render`` of its own."""

    def render(self, request, data, *, response_status):
        return json.dumps(data)


class OtherEncoderRenderer(JSONRenderer):
    encoder_class = json.JSONEncoder


@pytest.mark.parametrize(
    "renderer, reduced",
    [
        (None, False),
        (CompactRenderer(), False),
        (PlainRenderer(), True),
        (OwnRenderJSONRenderer(), True),
        (OtherEncoderRenderer(), True),
        (EpochRenderer(), False),
    ],
    ids=["default", "compact", "plain", "own-render", "other-encoder", "django"],
)
def test_bodies_reduced_only_for_other_renderers(monkeypatch, renderer, reduced):
    """ninja's JSONRenderer with its encoder writes Django's values itself."""
    import scrudful.ninja

    calls = []
    original = scrudful.ninja._json_values

    def counting(data):
        calls.append(data)
        return original(data)

    monkeypatch.setattr(scrudful.ninja, "_json_values", counting)
    client = TestClient(api_for(router(Store()), renderer))
    for method, headers, status in EARLY:
        response = client.request(
            method, "/items/1", json={"name": "x"}, headers=headers
        )
        assert response.status_code == status
    assert len(calls) == (len(EARLY) if reduced else 0)


@pytest.mark.parametrize("method, headers, status", EARLY)
def test_error_media_type_charset_names_the_renderer_s(method, headers, status):
    """An entity's own charset is replaced by the one the bytes are in."""
    entity = Entity(
        schema_link_or_func="/schemas/problem",
        context_link_or_func="/contexts/problem",
        media_type="application/problem+json; charset=UTF-16; profile=x",
    )
    client = TestClient(api_for(router(Store(), error=entity), Latin1Renderer()))
    response = client.request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.headers["Content-Type"] == (
        "application/problem+json; profile=x; charset=latin-1"
    )


class JSONLDLatin1Renderer(Latin1Renderer):
    media_type = "application/ld+json"


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "renderer", [Latin1Renderer, JSONLDLatin1Renderer], ids=["json", "json-ld"]
)
def test_raised_and_early_errors_share_the_content_type(asynchronous, renderer):
    """A raised error's body is the renderer's too: the same label as
    scrudful's own error bodies."""
    items = router(Store(), asynchronous, error=PROBLEM)
    request = requests(asynchronous, api_for(items, renderer()))
    raised = request("GET", "/items/999")  # load raises HttpError(404)
    early = request("PUT", "/items/1", json={"name": "x"}, headers={"If-Match": '"v0"'})
    assert (raised.status_code, early.status_code) == (404, 412)
    expected = f"{PROBLEM_JSON}; charset=latin-1"
    assert raised.headers["Content-Type"] == early.headers["Content-Type"] == expected


def test_quoted_media_type_parameters_kept_as_written():
    entity = Entity(
        schema_link_or_func="/schemas/problem",
        context_link_or_func="/contexts/problem",
        media_type='application/problem+json; profile="https://ex.com/a;b"',
    )
    client = TestClient(api_for(router(Store(), error=entity)))
    early = client.put("/items/1", json={"name": "x"}, headers={"If-Match": '"v0"'})
    raised = client.get("/items/999")
    expected = 'application/problem+json; profile="https://ex.com/a;b"; charset=utf-8'
    assert (early.status_code, raised.status_code) == (412, 404)
    assert early.headers["Content-Type"] == raised.headers["Content-Type"] == expected


@pytest.mark.parametrize(
    "media_type, parts",
    [
        ("a/b", ["a/b"]),
        ("a/b; x=1;y=2", ["a/b", " x=1", "y=2"]),
        ('a/b; p="1;2"; q=3', ["a/b", ' p="1;2"', " q=3"]),
        ('a/b; p="say \\"x;y\\""; q', ["a/b", ' p="say \\"x;y\\""', " q"]),
    ],
)
def test_split_parameters_respects_quoted_strings(media_type, parts):
    from scrudful._adapter import split_parameters

    assert split_parameters(media_type) == parts


class JSONLDRenderer(CompactRenderer):
    media_type = "application/ld+json"


@pytest.mark.parametrize("method, headers, status", EARLY)
def test_json_ld_renderer_writes_scrudful_bodies(method, headers, status):
    client = TestClient(api_for(router(Store()), JSONLDRenderer()))
    response = client.request(method, "/items/1", json={"name": "x"}, headers=headers)
    assert response.status_code == status
    assert response.content == compact(response.json())
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"


class CachingAPI(NinjaAPI):
    """An API whose create_response marks every response publicly cacheable."""

    def create_response(self, request, data, *args, **kwargs):
        response = super().create_response(request, data, *args, **kwargs)
        response["Cache-Control"] = "public, max-age=60"
        return response


@pytest.mark.parametrize("method, headers, status", EARLY)
def test_overridden_create_response_never_adds_headers_to_early_errors(
    method, headers, status
):
    """A shared cache must not store a 400, 412 or 428."""
    api = CachingAPI(renderer=CompactRenderer(), urls_namespace=uuid.uuid4().hex)
    api.add_router("", router(Store()))
    response = TestClient(api).request(
        method, "/items/1", json={"name": "x"}, headers=headers
    )
    assert response.status_code == status
    assert "Cache-Control" not in response.headers
    assert response.content == compact(response.json())


def test_django_encoder_renderer_writes_scrudful_bodies_its_own_way():
    """The API's own encoding of Django's values, as in its other responses."""
    items = router(Store(), error=PROBLEM, error_body_func=django_values_problem)
    client = TestClient(api_for(items, EpochRenderer()))
    response = client.put("/items/1", json={"name": "x"}, headers={"If-Match": '"v0"'})
    assert response.status_code == 412
    assert response.json()["at"] == int(DJANGO_VALUES["at"].timestamp())
    assert response.json()["amount"] == "1.50"
