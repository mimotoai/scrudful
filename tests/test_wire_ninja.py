"""Semantic HTTP 0.1 (spec commit ``37ec1ce``) wire conformance of ``scrudful.ninja``.

Every exchange goes through Django's test client and URL resolver, with
``DEBUG = False`` and scrudful's ``handler404`` / ``handler500`` installed (this
module is the URLconf), and each check is one of ``tests/semantic_http.py``'s;
every case asserts that its problem list is empty. One test per rule family:

- ``test_described_content_links``: one ``describedby`` and one context link on
  content, and exactly one HTTP Schema Resource link (no ``anchor``, the path's
  HSR route) on every response, HEAD, 204 and 304 included;
- ``test_agreement``: the Links agree with the HSR entry of the method, status
  and media type;
- ``test_options``: a non-preflight OPTIONS is a 204 without content, with
  ``Allow`` and the HSR link;
- ``test_hsr_get``: the HSR route's response and document;
- ``test_hsr_invariance``: the HSR's bytes are the same for two ``Host``s and
  two requesters;
- ``test_allow_per_requester``: ``Allow`` lists what this requester may use
  (anonymous, reader, writer, an object-level denial);
- ``test_preflight``: a CORS preflight is a 204 without content or Links,
  running no auth, throttle or load;
- ``test_undescribed``: no undescribed content for an absent ``Accept`` or
  ``Accept: application/json``, across 404, 405 and 500;
- ``test_expose_headers``: ``Link`` and ``Allow`` exposed, ``ETag`` and
  ``Location`` when present.

The scenarios are those of the 0.9.0 audit's harness (its ``agree.py``,
``validate.py`` and ``wire*`` scripts), on two APIs:

- ``WIRE_API`` (``/wire/``), minimal apps for what the example doesn't show: a
  router with its own ``auth`` (a reader and a writer, a ``before`` refusing
  the reader's writes, an ``allowed`` hook denying a DELETE per object), a
  ``@described`` operation, a throttled router, non-JSON and in-band entities,
  a handler setting its own Links, a 500, and ``add_error_links`` with problem
  details;
- the ``examples/ninja`` project's API (``/api/``), whose ``roles`` app needs
  the example's settings: under this suite's settings (the DRF example's),
  ``test_wire_suite_on_the_example_project`` runs this module again, in a
  separate pytest process, with the example's settings, where every family
  covers the example's exchanges too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pytest
from django.apps import apps
from django.http import HttpResponse
from django.test import Client
from django.urls import path
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.security import APIKeyHeader
from ninja.throttling import BaseThrottle

from scrudful import ninja as scrudful_ninja
from scrudful.ninja import (
    Entity,
    ScrudfulRouter,
    add_error_links,
    described,
    scrudful,
)
from scrudful.problem import PROBLEM_JSON, problem_details
from tests.semantic_http import (
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    undescribed_problems,
)

#: Whether this run has the ``examples/ninja`` project's settings (its
#: ``roles`` app), so that the example's API is driven too.
EXAMPLE = apps.is_installed("roles")
ROOT = Path(__file__).resolve().parents[1]
BASE = "http://testserver"
W3ID = "https://w3id.org/semantic-http/0.1/"

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]


# --- The minimal apps (WIRE_API) ---------------------------------------------

THING = Entity("/wire/schemas/thing", context_link_or_func="/wire/contexts/thing")
PROBLEM = Entity(
    f"{W3ID}problem-details.schema.json",
    context_link_or_func=f"{W3ID}problem-details.context.jsonld",
    media_type=PROBLEM_JSON,
)
ROWS = Entity(
    "/wire/schemas/rows",
    schema_type_or_func="application/csvm+json",
    context_link_or_func="/wire/schemas/rows",
    context_type_or_func="application/csvm+json",
    media_type="text/csv",
)
SHAPES = Entity(
    "/wire/schemas/shapes", schema_type_or_func="text/turtle", media_type="text/turtle"
)


class Thing(Schema):
    id: int
    name: str


class ThingIn(Schema):
    name: str


class Store:
    """The things of ``WIRE_API``: 1 is the writer's, 2 someone else's."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.items = {
            1: {"id": 1, "name": "one", "version": 1, "owner": "writer"},
            2: {"id": 2, "name": "two", "version": 1, "owner": "other"},
        }

    def load(self, request: Any, pk: int, **params: Any) -> dict[str, Any]:
        if pk not in self.items:
            raise HttpError(404, "No such thing.")
        return self.items[pk]


STORE = Store()


def version(item: dict[str, Any]) -> str:
    return f"v{item['version']}"


class Key(APIKeyHeader):
    """``X-Key: reader`` or ``X-Key: writer``."""

    param_name = "X-Key"

    def authenticate(self, request: Any, key: str | None) -> str | None:
        return key if key in ("reader", "writer") else None


class DenyAll(BaseThrottle):
    """Refuses every request it is asked about, and counts them."""

    calls = 0

    def allow_request(self, request: Any) -> bool:
        DenyAll.calls += 1
        return False

    def wait(self) -> float:
        return 60


def writers_only(request: Any, **params: Any) -> None:
    if request.auth != "writer":
        raise HttpError(403, "Only writers write.")


def owner_may_delete(request: Any, method: str, obj: Any) -> bool:
    return obj is not None and obj["owner"] == getattr(request, "auth", None)


ITEM = dict(
    load=STORE.load,
    etag_func=version,
    error=PROBLEM,
    error_body_func=problem_details,
    schema_link_or_func=THING.schema_link_or_func,
    context_link_or_func=THING.context_link_or_func,
)

#: An authenticated router (its own ``auth=``).
secured = ScrudfulRouter(auth=Key(), base_url=f"{BASE}/")


@secured.api_operation(["GET", "HEAD"], "/{pk}", response=Thing)
@scrudful(**ITEM)
def get_thing(request: Any, pk: int, obj: dict[str, Any]) -> Any:
    return obj


@secured.put("/{pk}", response=Thing)
@scrudful(**ITEM, before=writers_only)
def put_thing(request: Any, pk: int, payload: ThingIn, obj: dict[str, Any]) -> Any:
    obj.update(name=payload.name, version=obj["version"] + 1)
    return obj


@secured.delete("/{pk}", response={204: None})
@scrudful(**ITEM, allowed=owner_may_delete)
def delete_thing(request: Any, pk: int, obj: dict[str, Any]) -> Any:
    if obj["owner"] != request.auth:
        raise HttpError(403, "Not yours.")
    del STORE.items[pk]
    return None


@secured.api_operation(["GET", "HEAD"], "/{pk}/summary", response=Thing)
@described(THING)
def summary(request: Any, pk: int) -> Any:
    return {"id": pk, "name": "summary"}


#: A router whose throttle refuses everything.
throttled = ScrudfulRouter(throttle=DenyAll(), base_url=f"{BASE}/")


@throttled.get("/{pk}", response=Thing)
@scrudful(**ITEM)
def get_throttled(request: Any, pk: int, obj: dict[str, Any]) -> Any:
    return obj


#: Non-JSON and in-band entities, the handler's own Links and a 500.
media = ScrudfulRouter(base_url=f"{BASE}/")


@media.get("/csv/{pk}", response=Thing)
@scrudful(load=STORE.load, responses={200: ROWS})
def csv(request: Any, pk: int, obj: dict[str, Any]) -> Any:
    return HttpResponse(f"id,name\n{pk},{obj['name']}\n", content_type="text/csv")


@media.get("/turtle/{pk}", response=Thing)
@scrudful(load=STORE.load, responses={200: SHAPES})
def turtle(request: Any, pk: int, obj: dict[str, Any]) -> Any:
    return HttpResponse(f"<urn:thing:{pk}> a <urn:Thing> .", content_type="text/turtle")


@media.get("/own/{pk}", response=Thing)
@scrudful(
    load=STORE.load,
    schema_link_or_func=THING.schema_link_or_func,
    context_link_or_func=THING.context_link_or_func,
)
def own_links(
    request: Any, pk: int, obj: dict[str, Any], response: HttpResponse
) -> Any:
    # A schema link of the handler's own (another case of the relation)
    # replaces scrudful's; one of another relation replaces nothing.
    response["Link"] = (
        f'<{BASE}/wire/schemas/thing>; rel="DescribedBy"; '
        'type="application/schema+json", </wire/things?page=2>; rel="next"'
    )
    return obj


@media.get("/boom", response=Thing)
@described(THING)
def boom(request: Any) -> Any:
    raise RuntimeError("boom")


WIRE_API = NinjaAPI(urls_namespace="wire")
WIRE_API.add_router("/secured", secured)
WIRE_API.add_router("/throttled", throttled)
WIRE_API.add_router("/media", media)
add_error_links(WIRE_API, PROBLEM)

#: The error entity of Django's 404 and 500 (plain links): the example's
#: error documents, ``application/json`` as every HSR here describes it.
PAGE_ERROR = Entity(
    f"{BASE}/api/schemas/error", context_link_or_func=f"{BASE}/api/contexts/error"
)
handler404 = scrudful_ninja.handler404(PAGE_ERROR)
handler500 = scrudful_ninja.handler500(PAGE_ERROR)

urlpatterns = [path("wire/", WIRE_API.urls)]
if EXAMPLE:
    from scrudful_ninja.api import api as EXAMPLE_API

    urlpatterns.append(path("api/", EXAMPLE_API.urls))


# --- The HSR route of each path ------------------------------------------------

HSR_ROUTES = {
    "secured": "/wire/secured/_hsr/pk",
    "summary": "/wire/secured/_hsr/pk.summary",
    "throttled": "/wire/throttled/_hsr/pk",
    "csv": "/wire/media/_hsr/csv.pk",
    "turtle": "/wire/media/_hsr/turtle.pk",
    "own": "/wire/media/_hsr/own.pk",
    "boom": "/wire/media/_hsr/boom",
}
if EXAMPLE:
    HSR_ROUTES.update(
        item="/api/roles/_hsr/pk",
        collection="/api/roles/_hsr/_root",
        search="/api/roles/_hsr/search",
    )

READER = {"X-Key": "reader"}
WRITER = {"X-Key": "writer"}
ACME = {"X-Rbac-Context": "acme"}


@dataclass(frozen=True)
class Exchange:
    """A request and the status it gets. ``{pk}``, ``{other}`` (a row the
    requester may not see), ``{missing}``, ``{etag}`` and ``{list_etag}`` in
    the URL and headers are the fixture's (see :class:`World`)."""

    label: str
    kind: str  # the key of the path's HSR route
    method: str
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None

    @property
    def id(self) -> str:
        return f"{self.kind}-{self.label}"


def on(kind: str, default_url: str) -> Callable[..., Exchange]:
    """An :class:`Exchange` factory for the ``kind`` path, at ``default_url``
    unless ``url=`` is given."""

    def exchange(
        label: str,
        method: str,
        status: int,
        headers: dict[str, str] | None = None,
        body: Any = None,
        url: str = default_url,
    ) -> Exchange:
        return Exchange(label, kind, method, url, status, headers or {}, body)

    return exchange


def wire_exchanges() -> list[Exchange]:
    thing = on("secured", "/wire/secured/1")
    summary_ = on("summary", "/wire/secured/1/summary")
    fresh = {"If-None-Match": '"v1"', **READER}
    stale = {"If-Match": '"no"', **READER}
    current = {"If-Match": '"v1"', **WRITER}
    name = {"name": "x"}
    return [
        thing("GET 200", "GET", 200, READER),
        thing("HEAD 200", "HEAD", 200, READER),
        thing("GET 304", "GET", 304, fresh),
        thing("HEAD 304", "HEAD", 304, fresh),
        thing("GET 400", "GET", 400, {"If-None-Match": 'x"', **READER}),
        thing("GET 412", "GET", 412, stale),
        thing("HEAD 412", "HEAD", 412, stale),
        thing("GET 401", "GET", 401),
        thing("HEAD 401", "HEAD", 401),
        thing("GET 404", "GET", 404, READER, url="/wire/secured/9"),
        thing("PUT 403 before", "PUT", 403, {"If-Match": '"v1"', **READER}, name),
        thing("PUT 428", "PUT", 428, WRITER, name),
        thing("PUT 412", "PUT", 412, {"If-Match": '"no"', **WRITER}, name),
        thing("PUT 422 problem", "PUT", 422, current, {"name": 1}),
        thing("PUT 200", "PUT", 200, current, name),
        thing("DELETE 403", "DELETE", 403, current, url="/wire/secured/2"),
        thing("DELETE 204", "DELETE", 204, current),
        thing("PATCH 405", "PATCH", 405, READER, name),
        summary_("GET described", "GET", 200, READER),
        summary_("HEAD described", "HEAD", 200, READER),
        summary_("GET described 401", "GET", 401),
        on("throttled", "/wire/throttled/1")("GET 429", "GET", 429),
        on("csv", "/wire/media/csv/1")("GET text/csv", "GET", 200),
        on("turtle", "/wire/media/turtle/1")("GET text/turtle", "GET", 200),
        on("own", "/wire/media/own/1")("GET own Links", "GET", 200),
        on("own", "/wire/media/own/9")("GET 404 load", "GET", 404),
    ]


def example_exchanges() -> list[Exchange]:
    """The audit's exchanges with the example (``wire1``, ``agree``)."""
    role = on("item", "/api/roles/{pk}")
    roles = on("collection", "/api/roles")
    search = on("search", "/api/roles/search")
    fresh = {"If-None-Match": "{etag}", **ACME}
    current = {"If-Match": "{etag}", **ACME}
    stale = {"If-Match": '"stale"', **ACME}
    writable = {"name": "x", "rbac_context": "acme"}
    elsewhere = {"name": "x", "rbac_context": "z"}
    return [
        role("GET 200", "GET", 200, ACME),
        role("HEAD 200", "HEAD", 200, ACME),
        role("GET 304", "GET", 304, fresh),
        role("HEAD 304", "HEAD", 304, fresh),
        role("GET 400", "GET", 400, {"If-None-Match": 'bad"', **ACME}),
        role("GET 412", "GET", 412, stale),
        role("HEAD 412", "HEAD", 412, stale),
        role("GET 401", "GET", 401),
        role("HEAD 401", "HEAD", 401),
        role("GET 404 load", "GET", 404, ACME, url="/api/roles/{missing}"),
        role("GET 404 other context", "GET", 404, ACME, url="/api/roles/{other}"),
        role("PUT 428", "PUT", 428, ACME, writable),
        role("PUT 412", "PUT", 412, stale, writable),
        role("PUT 422", "PUT", 422, current, {"name": 1}),
        role("PUT 403", "PUT", 403, current, elsewhere),
        role("PUT 200", "PUT", 200, current, writable),
        role("PATCH 412", "PATCH", 412, stale, {"name": "y"}),
        role("PATCH 200", "PATCH", 200, current, {"name": "y"}),
        role("DELETE 428", "DELETE", 428, ACME),
        role("DELETE 412", "DELETE", 412, stale),
        role("DELETE 204", "DELETE", 204, current),
        role("POST 405", "POST", 405, ACME, {}),
        role("TRACE 405", "TRACE", 405, ACME),
        roles("GET list", "GET", 200, ACME),
        roles("HEAD list", "HEAD", 200, ACME),
        roles("GET list 304", "GET", 304, {"If-None-Match": "{list_etag}", **ACME}),
        roles("GET list 412", "GET", 412, stale),
        roles("GET list 400", "GET", 400, {"If-Match": "n", **ACME}),
        roles("GET page past last", "GET", 404, ACME, url="/api/roles?page=9"),
        roles("GET page 0", "GET", 422, ACME, url="/api/roles?page=0"),
        roles("PUT list 405", "PUT", 405, ACME, {}),
        roles("POST 201", "POST", 201, ACME, writable),
        roles("POST 403", "POST", 403, ACME, elsewhere),
        roles("POST 412", "POST", 412, stale, writable),
        roles("POST 400", "POST", 400, {"If-Match": "x", **ACME}, writable),
        roles("POST 422", "POST", 422, ACME, {"name": 5}),
        search("search 200", "POST", 200, ACME, {"name": "n"}),
        search("search 422", "POST", 422, ACME, {}),
        search("GET search 405", "GET", 405, ACME),
    ]


EXCHANGES = wire_exchanges() + (example_exchanges() if EXAMPLE else [])
#: The exchanges whose Links agree with an HSR entry: not a 405, which
#: answers a method the HSR has no entry for, nor a 304 (a HEAD's too),
#: whose entry has no content.
AGREEING = [exchange for exchange in EXCHANGES if exchange.status not in (304, 405)]


# --- Fixtures and helpers ------------------------------------------------------


@pytest.fixture(autouse=True)
def configured(settings):
    """The example's base URL (Django's test client serves testserver), two
    more hosts, and the production ``DEBUG``."""
    settings.SCRUDFUL = {"BASE_URL": f"{BASE}/"}
    settings.ALLOWED_HOSTS = ["testserver", "a.example", "b.example"]
    settings.DEBUG = False
    STORE.reset()


class World:
    """The values of an :class:`Exchange`'s placeholders."""

    def __init__(self, client: Client) -> None:
        self.values = {"missing": "999999"}
        if EXAMPLE:
            from roles.models import Role

            role = Role.objects.create(name="admin", rbac_context="acme")
            other = Role.objects.create(name="auditor", rbac_context="globex")
            listed = client.get("/api/roles", headers=ACME)
            self.values.update(
                pk=str(role.pk),
                other=str(other.pk),
                etag=f'"{role.etag}"',
                list_etag=listed["ETag"],
            )

    def format(self, text: str) -> str:
        return text.format(**self.values) if "{" in text else text


@pytest.fixture
def client() -> Client:
    return Client(raise_request_exception=False)


@pytest.fixture
def world(client) -> World:
    return World(client)


def send(client: Client, world: World, exchange: Exchange) -> Any:
    """``exchange``'s request; its response, once it has the status."""
    headers = {name: world.format(value) for name, value in exchange.headers.items()}
    kwargs: dict[str, Any] = {"headers": headers}
    if exchange.body is not None:
        kwargs.update(data=json.dumps(exchange.body), content_type="application/json")
    response = client.generic(exchange.method, world.format(exchange.url), **kwargs)
    assert response.status_code == exchange.status, (
        exchange.label,
        response.content[:300],
    )
    return response


def headers_of(response: Any) -> dict[str, str]:
    return dict(response.items())


def hsr_of(client: Client, kind: str) -> dict[str, Any]:
    """The HTTP Schema Resource of the ``kind`` path, by a GET of its route."""
    response = client.get(HSR_ROUTES[kind])
    assert response.status_code == 200
    return json.loads(response.content)


def target(kind: str) -> str:
    return BASE + HSR_ROUTES[kind]


def has_content(exchange: Exchange, response: Any) -> bool:
    return exchange.method != "HEAD" and bool(response.content)


# --- The rule families -----------------------------------------------------------


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda e: e.id)
def test_described_content_links(client, world, exchange):
    response = send(client, world, exchange)
    headers = headers_of(response)
    content = has_content(exchange, response)
    assert content == (exchange.method != "HEAD" and exchange.status not in (204, 304))
    assert link_problems(exchange.method, exchange.status, headers, content) == []
    assert hsr_link_problems(headers, target(exchange.kind)) == []


@pytest.mark.parametrize("exchange", AGREEING, ids=lambda e: e.id)
def test_agreement(client, world, exchange):
    hsr = hsr_of(client, exchange.kind)
    response = send(client, world, exchange)
    content = has_content(exchange, response)
    problems = link_problems(
        exchange.method, exchange.status, headers_of(response), content, hsr
    )
    assert problems == []


#: ``(kind, url, headers)`` of an OPTIONS each path answers with a 204.
OPTIONS = [
    ("secured", "/wire/secured/1", WRITER),
    ("summary", "/wire/secured/1/summary", READER),
    ("csv", "/wire/media/csv/1", {}),
    ("turtle", "/wire/media/turtle/1", {}),
    ("own", "/wire/media/own/1", {}),
    ("boom", "/wire/media/boom", {}),
] + (
    [
        ("item", "/api/roles/{pk}", ACME),
        ("collection", "/api/roles", ACME),
        ("search", "/api/roles/search", ACME),
    ]
    if EXAMPLE
    else []
)


@pytest.mark.parametrize("kind, url, headers", OPTIONS, ids=[o[0] for o in OPTIONS])
def test_options(client, world, kind, url, headers):
    response = client.options(world.format(url), headers=headers)
    problems = options_204_problems(
        response.status_code, headers_of(response), response.content
    )
    assert problems == []
    assert hsr_link_problems(headers_of(response), target(kind)) == []


@pytest.mark.parametrize("kind", sorted(HSR_ROUTES))
def test_hsr_get(client, kind):
    response = client.get(HSR_ROUTES[kind])
    problems = hsr_get_problems(
        response.status_code, headers_of(response), response.content
    )
    assert problems == []
    # A conditional GET with its ETag: 304.
    again = client.get(HSR_ROUTES[kind], headers={"If-None-Match": response["ETag"]})
    assert again.status_code == 304


#: The credentials of a requester of each kind of path.
REQUESTERS = {"wire": ({}, WRITER), "api": ({}, ACME)}


@pytest.mark.parametrize("kind", sorted(HSR_ROUTES))
def test_hsr_invariance(client, world, kind):
    """Two Hosts and two requesters (anonymous, authenticated): one HSR, as
    bytes and ETag. Each request is answered from the document built once,
    so a first build for one requester can't hide another's."""
    anonymous, authenticated = REQUESTERS[HSR_ROUTES[kind].split("/")[1]]
    responses = [
        client.get(HSR_ROUTES[kind], headers={"Host": host, **credentials})
        for host in ("a.example", "b.example")
        for credentials in (anonymous, authenticated)
    ]
    assert {response.status_code for response in responses} == {200}
    assert len({response.content for response in responses}) == 1
    assert len({response["ETag"] for response in responses}) == 1
    # Built without a request: no Host leaks into it.
    assert b"a.example" not in responses[0].content
    assert b"b.example" not in responses[0].content


ALL_ITEM = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
#: ``(label, url, headers, Allow)``.
ALLOW = [
    ("anonymous", "/wire/secured/1", {}, {"OPTIONS"}),
    ("reader", "/wire/secured/1", READER, {"GET", "HEAD", "OPTIONS"}),
    ("writer", "/wire/secured/1", WRITER, ALL_ITEM),
    ("writer, not the owner", "/wire/secured/2", WRITER, ALL_ITEM - {"DELETE"}),
    ("reader, not the owner", "/wire/secured/2", READER, {"GET", "HEAD", "OPTIONS"}),
    ("anonymous described", "/wire/secured/1/summary", {}, {"OPTIONS"}),
    ("reader described", "/wire/secured/1/summary", READER, {"GET", "HEAD", "OPTIONS"}),
    ("anonymous media", "/wire/media/csv/1", {}, {"GET", "OPTIONS"}),
] + (
    [
        ("anonymous role", "/api/roles/{pk}", {}, {"OPTIONS"}),
        (
            "acme role",
            "/api/roles/{pk}",
            ACME,
            {"GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"},
        ),
        ("anonymous roles", "/api/roles", {}, {"OPTIONS"}),
        ("acme roles", "/api/roles", ACME, {"GET", "HEAD", "POST", "OPTIONS"}),
        ("acme search", "/api/roles/search", ACME, {"POST", "OPTIONS"}),
    ]
    if EXAMPLE
    else []
)


@pytest.mark.parametrize("label, url, headers, allow", ALLOW, ids=[a[0] for a in ALLOW])
def test_allow_per_requester(client, world, label, url, headers, allow):
    response = client.options(world.format(url), headers=headers)
    problems = options_204_problems(
        response.status_code, headers_of(response), response.content, allow
    )
    assert problems == []


def test_allow_of_a_hidden_object_is_the_loaders_404(client, world):
    """Ruling R11: the loader's 404 (scoped-rbac's unreadable object) is the
    answer, with the error Links and the HSR link, not an object-less
    Allow."""
    url, kind, headers = "/wire/secured/9", "secured", READER
    if EXAMPLE:
        url, kind, headers = world.format("/api/roles/{other}"), "item", ACME
    response = client.options(url, headers=headers)
    assert response.status_code == 404
    assert "Allow" not in response
    assert link_problems("OPTIONS", 404, headers_of(response), True) == []
    assert hsr_link_problems(headers_of(response), target(kind)) == []


PREFLIGHT = {"Origin": "https://app.example", "Access-Control-Request-Method": "PUT"}
#: ``(url, credentials)`` of preflights: locked, throttled and open paths,
#: with and without credentials (a browser sends none; a cookie is ignored).
PREFLIGHTS = [
    ("/wire/secured/1", {}),
    ("/wire/secured/1", {**WRITER, "Cookie": "sessionid=x"}),
    ("/wire/secured/1/summary", {}),
    ("/wire/throttled/1", {}),
    ("/wire/media/csv/1", {}),
] + (
    [("/api/roles/{pk}", {}), ("/api/roles", ACME), ("/api/roles/search", {})]
    if EXAMPLE
    else []
)


@pytest.mark.parametrize(
    "url, credentials", PREFLIGHTS, ids=[f"{u}-{bool(c)}" for u, c in PREFLIGHTS]
)
def test_preflight(client, world, url, credentials):
    throttled_before = DenyAll.calls
    for _ in range(3):  # never throttled
        response = client.options(
            world.format(url), headers={**PREFLIGHT, **credentials}
        )
        problems = []
        if response.status_code != 204:
            problems.append(f"preflight answered {response.status_code}")
        if response.content:
            problems.append("preflight has content")
        if "Link" in response:
            problems.append("preflight has Links")
        if "Content-Type" in response:
            problems.append("preflight has a Content-Type")
        assert problems == []
    assert DenyAll.calls == throttled_before


#: ``(label, method, url, headers, HSR kind)``: Django's 404 for an unknown
#: URL, scrudful's JSON 405 and Django's 500, each described or accepted.
UNDESCRIBED = [
    ("unknown URL 404", "GET", "/wire/nope", {}, "secured"),
    ("JSON 405", "PATCH", "/wire/secured/1", READER, "secured"),
    ("JSON 405 of the HSR route", "POST", "/wire/secured/_hsr/pk", {}, "secured"),
    ("500", "GET", "/wire/media/boom", {}, "boom"),
] + (
    [
        ("unknown URL 404", "GET", "/api/nope", ACME, "item"),
        ("JSON 405", "TRACE", "/api/roles/{pk}", ACME, "item"),
    ]
    if EXAMPLE
    else []
)


@pytest.mark.parametrize("accept", [None, "application/json"], ids=["absent", "json"])
@pytest.mark.parametrize(
    "label, method, url, headers, kind",
    UNDESCRIBED,
    ids=[f"{u[2]}-{u[0]}" for u in UNDESCRIBED],
)
def test_undescribed(client, world, accept, label, method, url, headers, kind):
    hsr = hsr_of(client, kind)
    extra = {} if accept is None else {"Accept": accept}
    response = client.generic(method, world.format(url), headers={**headers, **extra})
    assert response.status_code >= 400, label
    response_headers = headers_of(response)
    assert undescribed_problems(accept, response_headers, hsr) == []
    assert link_problems(method, response.status_code, response_headers, True) == []


def test_undescribed_html_only_when_accepted(client):
    """A browser asking for HTML gets Django's page, with ``Vary: Accept``."""
    accept = "text/html,application/xhtml+xml,*/*;q=0.8"
    response = client.get("/wire/nope", headers={"Accept": accept})
    assert response.status_code == 404
    assert response["Content-Type"].startswith("text/html")
    assert "Accept" in response["Vary"]
    hsr = hsr_of(client, "secured")
    assert undescribed_problems(accept, headers_of(response), hsr) == []
    assert undescribed_problems("application/json", headers_of(response), hsr) != []


def exposed(response: Any) -> set[str]:
    value = response.get("Access-Control-Expose-Headers", "")
    return {name.strip() for name in value.split(",") if name.strip()}


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda e: e.id)
def test_expose_headers(client, world, exchange):
    response = send(client, world, exchange)
    expected = {"Link", "Allow"} | {
        name for name in ("ETag", "Location") if name in response
    }
    assert expected - exposed(response) == set()


@pytest.mark.parametrize("kind, url, headers", OPTIONS, ids=[o[0] for o in OPTIONS])
def test_expose_headers_of_options(client, world, kind, url, headers):
    response = client.options(world.format(url), headers=headers)
    assert {"Link", "Allow"} <= exposed(response)


def test_the_405_problem_and_422_problem_are_problem_details(client):
    """With a problem-details ``add_error_links`` entity, ninja's 422 and the
    JSON 405 are problem details, as the HSR's ``default`` entry says."""
    response = client.put(
        "/wire/secured/1",
        json.dumps({"name": 1}),
        content_type="application/json",
        headers={"If-Match": '"v1"', **WRITER},
    )
    body = json.loads(response.content)
    assert response["Content-Type"].startswith(PROBLEM_JSON)
    assert isinstance(body["detail"], str) and body["errors"]
    response = client.patch("/wire/secured/1", headers=READER)
    assert response["Content-Type"].startswith(PROBLEM_JSON)
    assert response["Allow"] == "GET, PUT, DELETE, OPTIONS, HEAD"
    links = [link for link, _ in parse_links(response["Link"])]
    assert links == [
        PROBLEM.schema_link_or_func,
        PROBLEM.context_link_or_func,
        target("secured"),
    ]


# --- The example project --------------------------------------------------------


@pytest.mark.skipif(EXAMPLE, reason="this run is the example project's")
def test_wire_suite_on_the_example_project():
    """Run this module with the example's settings, where every rule family
    covers the example's API too."""
    example = ROOT / "examples" / "ninja"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(example), str(ROOT)])}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__).relative_to(ROOT)),
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--ds=scrudful_ninja.settings",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Only this test is skipped there.
    summary = result.stdout.strip().splitlines()[-1]
    assert re.match(r"\d+ passed, 1 skipped in ", summary), result.stdout
