"""Semantic HTTP 0.1 (spec commit ``37ec1ce``) wire conformance of
``scrudful.rest_framework``.

Every exchange goes through Django's test client and URL resolver, with
``DEBUG = False``, the example project's settings (its JSON-only renderers and
parsers, and scrudful's ``exception_handler``) and scrudful's ``handler404`` /
``handler500`` installed (this module is the URLconf); each check is one of
``tests/semantic_http.py``'s, and every case asserts that its problem list is
empty. One test per rule family:

- ``test_described_content_links``: one ``describedby`` and one context link on
  content, and exactly one HTTP Schema Resource link (no ``anchor``, the route's
  ``hsr_urls()`` URL) on every response, HEAD, 204, 304 and errors included;
- ``test_agreement``: the Links agree with the HSR entry of the method, status
  and media type;
- ``test_options``: a non-preflight OPTIONS is a 204 without content, with
  ``Allow`` and the HSR link;
- ``test_hsr_get``: the HSR route's response and document;
- ``test_hsr_invariance``: the HSR's bytes are the same for two ``Host``s and
  two requesters, each request building the document afresh;
- ``test_allow_per_requester``: ``Allow`` lists what this requester may use
  (anonymous, reader, writer, admin, an object-level denial, an
  ``allowed_func``), and a requester refused every method can't tell a
  missing object from an existing one (nothing is loaded);
- ``test_preflight``: a CORS preflight is a 204 without content or Links,
  running no auth, throttle or load, a missing object's included;
- ``test_undescribed``: no undescribed content for an absent ``Accept`` or
  ``Accept: application/json``, across 404, 405 and 500;
- ``test_expose_headers``: ``Link`` and ``Allow`` exposed, ``ETag`` and
  ``Location`` when present.

The scenarios are those of the 0.9.0 audit's DRF harness (its ``run.py``,
``extra.py``, ``probe2.py`` and ``probe3.py``), on two sets of views:

- the ``examples/scrudful_rest_framework`` project's (``items.urls``: the item
  viewsets and ``/stats/``), whose settings are this suite's;
- minimal views under ``/x/`` for what the example doesn't show: an
  authenticated viewset (a reader and a writer, an object-level denial,
  problem details, pagination, an ``@action`` answering 200 to POST), an
  admin-only one, one authorizing through ``allowed_func``, a throttled one,
  the browsable API, a non-JSON entity, a handler setting its own Links, a
  class-body ``@scrudful`` ``APIView``, a ``scrudful_api_view`` answering 201
  and 204, and one raising (a 500).

Excluded, and documented by ``test_format_api_is_undescribed_html``: the
browsable API's ``?format=api``, which DRF serves as HTML whatever the
``Accept`` (Semantic HTTP's "Undescribed content" forbids that for a request
that doesn't accept HTML). scrudful sends ``Vary: Accept`` on such a view; its
docs recommend dropping ``BrowsableAPIRenderer`` in production, as the example
does, or keeping it reachable only through ``Accept: text/html``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import include, path, resolve
from items import views as example
from items.models import Item
from items.serializers import ItemSerializer
from jsonschema import Draft202012Validator
from rest_framework import permissions, viewsets
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.renderers import BaseRenderer, BrowsableAPIRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.routers import SimpleRouter
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView

import scrudful.rest_framework as scrudful_drf
from scrudful.problem import PROBLEM_JSON, problem_details, problem_from_errors
from scrudful.rest_framework import (
    Entity,
    ScrudfulPagination,
    hsr_urls,
    http_schema_resource,
    scrudful,
    scrudful_api_view,
    scrudful_viewset,
)
from tests.semantic_http import (
    PROBLEM_DETAILS_SCHEMA,
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    undescribed_problems,
)

BASE = "http://testserver"
W3ID = "https://w3id.org/semantic-http/0.1/"

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]


# --- The minimal views (/x/) ---------------------------------------------------

PROBLEM = Entity(
    f"{W3ID}problem-details.schema.json",
    context_link_or_func=f"{W3ID}problem-details.context.jsonld",
    media_type=PROBLEM_JSON,
)
ROWS = Entity(
    "/schemas/rows.json",
    schema_type_or_func="application/csvm+json",
    context_link_or_func="/schemas/rows.json",
    context_type_or_func="application/csvm+json",
    media_type="text/csv",
)

#: The requesters of the ``X-Key`` header: unsaved users, no database row.
USERS = {
    name: User(username=name, is_staff=name == "admin")
    for name in ("reader", "writer", "admin")
}


class Key(BaseAuthentication):
    """``X-Key: reader``, ``writer`` or ``admin``; another key is refused
    (401, as the scheme has a ``WWW-Authenticate`` challenge)."""

    def authenticate(self, request: Any) -> Any:
        key = request.headers.get("X-Key")
        if key is None:
            return None
        if key not in USERS:
            raise AuthenticationFailed("Unknown key.")
        return USERS[key], key

    def authenticate_header(self, request: Any) -> str:
        return 'Key realm="wire"'


class WritersWrite(permissions.BasePermission):
    """Anyone reads; only the writer writes."""

    def has_permission(self, request: Any, view: Any) -> bool:
        if request.method in permissions.SAFE_METHODS:
            return True
        return getattr(request.user, "username", None) == "writer"


class KeepTwo(permissions.BasePermission):
    """Nobody deletes the item named "Two" (an object-level denial)."""

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        return request.method != "DELETE" or obj.name != "Two"


#: Every ``get_object()`` of the views that count them (see :class:`Counted`).
LOADS: list[str] = []


class Counted:
    """Counts its loads, to tell whether OPTIONS loaded the object."""

    def get_object(self) -> Any:
        LOADS.append(type(self).__name__)
        return super().get_object()  # type: ignore[misc]


class DenyAll(BaseThrottle):
    """Refuses every request it is asked about, and counts them."""

    calls = 0

    def allow_request(self, request: Any, view: Any) -> bool:
        DenyAll.calls += 1
        return False

    def wait(self) -> float:
        return 60


def href(view: Any, request: Any, obj: Any) -> str:
    return f"{BASE}/x/perm-items/{obj.pk}/"


class ProblemMeta(example.ItemMeta):
    """The example's item configuration, with problem details for errors."""

    error = PROBLEM
    list_error = PROBLEM
    error_body_func = staticmethod(problem_details)
    list_error_body_func = staticmethod(problem_details)
    href_func = staticmethod(href)


class WireViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    lookup_value_regex = "[0-9]+"
    authentication_classes = [Key]


@scrudful_viewset
class PermItemViewSet(Counted, WireViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, WritersWrite, KeepTwo]
    pagination_class = ScrudfulPagination
    Meta = ProblemMeta

    @action(detail=True, methods=["get", "post"])
    @scrudful(
        schema_link_or_func=example.stats_schema_url,
        context_link_or_func=example.stats_context_url,
        error=PROBLEM,
        statuses={"post": [200]},
    )
    def touch(self, request: Any, pk: Any = None, **kwargs: Any) -> Any:
        return Response({"count": int(request.method == "POST"), "latest_update": None})


@scrudful_viewset
class StaffItemViewSet(Counted, WireViewSet):
    permission_classes = [permissions.IsAdminUser]
    Meta = ProblemMeta


def writer_writes(view: Any, request: Any, method: str, obj: Any) -> bool:
    return method in ("GET", "HEAD") or request.user.username == "writer"


class HookedMeta(example.ItemMeta):
    allowed_func = staticmethod(writer_writes)


@scrudful_viewset
class HookedItemViewSet(Counted, WireViewSet):
    """Authorizes only through ``allowed_func`` (for OPTIONS) and its
    handlers' own checks."""

    Meta = HookedMeta


@scrudful_viewset
class ThrottledItemViewSet(Counted, WireViewSet):
    throttle_classes = [DenyAll]
    Meta = example.ItemMeta


@scrudful_viewset
class BrowsableItemViewSet(WireViewSet):
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer]
    Meta = example.ItemMeta


class CSVRenderer(BaseRenderer):
    media_type = "text/csv"
    format = "csv"

    def render(self, data: Any, *args: Any, **kwargs: Any) -> bytes:
        return f"id,name\n{data['id']},{data['name']}\n".encode()


class CsvMeta:
    entity = ROWS
    error = example.ERROR


@scrudful_viewset
class CsvItemViewSet(viewsets.ReadOnlyModelViewSet):
    """A non-JSON entity (CSV on the Web metadata as its schema and context)."""

    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    lookup_value_regex = "[0-9]+"
    renderer_classes = [CSVRenderer]
    Meta = CsvMeta


@scrudful_viewset
class OwnLinksViewSet(viewsets.ReadOnlyModelViewSet):
    """A handler setting Links of its own: a schema link (another case of the
    relation) replaces scrudful's, one of another relation replaces none."""

    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    lookup_value_regex = "[0-9]+"
    Meta = example.ItemMeta

    def retrieve(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        response = super().retrieve(request, *args, **kwargs)
        response["Link"] = (
            f'<{BASE}/schemas/item.json>; rel="DescribedBy"; '
            f'type="application/schema+json", <{BASE}/x/own-links/>; rel="up"'
        )
        return response


class ItemAPIView(APIView):
    """A class-body ``@scrudful`` handler."""

    authentication_classes = [Key]

    @scrudful(
        etag_func=lambda view, request, pk: example.item_etag(view, request, pk),
        schema_link_or_func=example.item_schema_url,
        context_link_or_func=example.item_context_url,
        error=example.ERROR,
    )
    def get(self, request: Any, pk: int) -> Any:
        item = Item.objects.get(pk=pk)
        return Response(ItemSerializer(item, context={"request": request}).data)

    @scrudful(
        etag_func=lambda view, request, pk: example.item_etag(view, request, pk),
        schema_link_or_func=example.item_schema_url,
        context_link_or_func=example.item_context_url,
        error=example.ERROR,
    )
    def put(self, request: Any, pk: int) -> Any:
        return Response({})

    def get_queryset(self) -> Any:
        return Item.objects.all()


STATS = dict(
    schema_link_or_func=example.stats_schema_url,
    context_link_or_func=example.stats_context_url,
    error=example.ERROR,
)


@scrudful_api_view(["GET", "POST", "DELETE"], **STATS)
def writes(request: Any) -> Any:
    if request.method == "DELETE":
        return Response(status=204)
    status = 201 if request.method == "POST" else 200
    return Response({"count": 0, "latest_update": None}, status=status)


@scrudful_api_view(["GET"], **STATS)
def boom(request: Any) -> Any:
    raise RuntimeError("boom")


router = SimpleRouter()
for prefix, viewset, basename in (
    ("perm-items", PermItemViewSet, "perm-item"),
    ("staff-items", StaffItemViewSet, "staff-item"),
    ("hooked-items", HookedItemViewSet, "hooked-item"),
    ("throttled-items", ThrottledItemViewSet, "throttled-item"),
    ("browsable-items", BrowsableItemViewSet, "browsable-item"),
    ("csv-items", CsvItemViewSet, "csv-item"),
    ("own-links", OwnLinksViewSet, "own-links"),
):
    router.register(prefix, viewset, basename=basename)

#: The error entity of Django's 404 and 500: the example's error documents,
#: resolved against ``SCRUDFUL["BASE_URL"]``, ``application/json`` as every
#: HSR here describes it.
PAGE_ERROR = Entity(
    "/schemas/error.json", context_link_or_func="/contexts/error.jsonld"
)
handler404 = scrudful_drf.handler404(PAGE_ERROR)
handler500 = scrudful_drf.handler500(PAGE_ERROR)

urlpatterns = [
    path("", include("items.urls")),
    path("x/", include(router.urls)),
    path("x/apiview/<int:pk>/", ItemAPIView.as_view(), name="apiview"),
    path("x/writes/", writes, name="writes"),
    path("x/boom/", boom, name="boom"),
    path("", include(hsr_urls())),
]


# --- The HSR route of each kind of route -----------------------------------------

#: The URL name of the route of each kind.
VIEW_NAMES = {
    "item": "item-detail",
    "items": "item-list",
    "readonly": "readonly-item-detail",
    "readonly-list": "readonly-item-list",
    "stats": "item-stats",
    "perm": "perm-item-detail",
    "perm-list": "perm-item-list",
    "touch": "perm-item-touch",
    "staff": "staff-item-detail",
    "hooked": "hooked-item-detail",
    "throttled": "throttled-item-detail",
    "browsable": "browsable-item-detail",
    "csv": "csv-item-detail",
    "own": "own-links-detail",
    "apiview": "apiview",
    "writes": "writes",
    "boom": "boom",
}
HSR_ROUTES = {kind: f"/_hsr/{name}" for kind, name in VIEW_NAMES.items()}

#: A URL of the route of each kind.
URLS = {
    "item": "/items/{one}/",
    "items": "/items/",
    "readonly": "/readonly-items/{one}/",
    "readonly-list": "/readonly-items/",
    "stats": "/stats/",
    "perm": "/x/perm-items/{one}/",
    "perm-list": "/x/perm-items/",
    "touch": "/x/perm-items/{one}/touch/",
    "staff": "/x/staff-items/{one}/",
    "hooked": "/x/hooked-items/{one}/",
    "throttled": "/x/throttled-items/{one}/",
    "browsable": "/x/browsable-items/{one}/",
    "csv": "/x/csv-items/{one}/",
    "own": "/x/own-links/{one}/",
    "apiview": "/x/apiview/{one}/",
    "writes": "/x/writes/",
    "boom": "/x/boom/",
}
READER = {"X-Key": "reader"}

WRITER = {"X-Key": "writer"}
ADMIN = {"X-Key": "admin"}
JSON = {"Accept": "application/json"}


@dataclass(frozen=True)
class Exchange:
    """A request and the status it gets. ``{one}``, ``{two}`` (the item named
    "Two"), ``{missing}`` and ``{list_etag}`` in the URL and headers are the
    fixture's (see :class:`World`)."""

    label: str
    kind: str  # the key of the route's HSR
    method: str
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    content_type: str = "application/json"

    @property
    def id(self) -> str:
        return f"{self.kind}-{self.label}"


def on(kind: str, default_url: str) -> Callable[..., Exchange]:
    """An :class:`Exchange` factory for the ``kind`` route, at ``default_url``
    unless ``url=`` is given."""

    def exchange(
        label: str,
        method: str,
        status: int,
        headers: dict[str, str] | None = None,
        body: Any = None,
        url: str = default_url,
        content_type: str = "application/json",
    ) -> Exchange:
        return Exchange(
            label, kind, method, url, status, headers or {}, body, content_type
        )

    return exchange


def example_exchanges() -> list[Exchange]:
    """The audit's exchanges with the example project (``run.py``)."""
    item = on("item", "/items/{one}/")
    items = on("items", "/items/")
    fresh = {"If-None-Match": '"v1"'}
    stale = {"If-Match": '"nope"'}
    current = {"If-Match": '"v1"'}
    name = {"name": "x"}
    return [
        item("GET 200", "GET", 200, JSON),
        item("GET 200 no Accept", "GET", 200),
        item("HEAD 200", "HEAD", 200, JSON),
        item("GET 304", "GET", 304, fresh),
        item("HEAD 304", "HEAD", 304, fresh),
        item("GET 400 malformed", "GET", 400, {"If-None-Match": "v1"}),
        item("GET 412", "GET", 412, stale),
        item("HEAD 412", "HEAD", 412, stale),
        item("GET 404", "GET", 404, JSON, url="/items/{missing}/"),
        item("GET 404 no Accept", "GET", 404, url="/items/{missing}/"),
        item("GET 404 format=api", "GET", 404, JSON, url="/items/{one}/?format=api"),
        item("GET 406", "GET", 406, {"Accept": "text/csv"}),
        item("PUT 428", "PUT", 428, {}, name),
        item("PUT 412", "PUT", 412, stale, name),
        item("PUT 400 malformed", "PUT", 400, {"If-Match": "v1"}, name),
        item("PUT 400 serializer", "PUT", 400, current, {"name": ""}),
        item("PUT 415 form", "PUT", 415, current, "name=x", content_type="text/x"),
        item("PUT 200", "PUT", 200, current, name),
        item("PATCH 200", "PATCH", 200, current, name),
        item("DELETE 428", "DELETE", 428),
        item("DELETE 204", "DELETE", 204, current),
        item("TRACE 405", "TRACE", 405),
        item("POST 405", "POST", 405, {}, name),
        items("GET list", "GET", 200),
        items("HEAD list", "HEAD", 200),
        items("GET list 304", "GET", 304, {"If-None-Match": "{list_etag}"}),
        items("GET list 412", "GET", 412, stale),
        items("POST 201", "POST", 201, {}, {"name": "Three"}),
        items("POST 400 serializer", "POST", 400, {}, {}),
        items("POST 412", "POST", 412, stale, {"name": "Three"}),
        on("readonly", "/readonly-items/{one}/")("GET 200", "GET", 200),
        on("readonly", "/readonly-items/{one}/")("PUT 405", "PUT", 405, {}, name),
        on("readonly-list", "/readonly-items/")("POST 405", "POST", 405, {}, name),
        on("stats", "/stats/")("GET 200", "GET", 200),
        on("stats", "/stats/")("HEAD 200", "HEAD", 200),
        on("stats", "/stats/")("GET 304", "GET", 304, {"If-None-Match": "{list_etag}"}),
        on("stats", "/stats/")("PUT 405", "PUT", 405, {}, {}),
    ]


def wire_exchanges() -> list[Exchange]:
    """The audit's exchanges with its extra views (``extra.py``, ``probe*``)."""
    perm = on("perm", "/x/perm-items/{one}/")
    listing = on("perm-list", "/x/perm-items/")
    touch = on("touch", "/x/perm-items/{one}/touch/")
    staff = on("staff", "/x/staff-items/{one}/")
    current = {"If-Match": '"v1"', **WRITER}
    name = {"name": "x"}
    return [
        perm("GET anonymous", "GET", 200),
        perm("GET reader", "GET", 200, READER),
        perm("GET bad key 401", "GET", 401, {"X-Key": "nobody"}),
        perm("GET 404", "GET", 404, READER, url="/x/perm-items/{missing}/"),
        perm("PUT anonymous 401", "PUT", 401, {"If-Match": '"v1"'}, name),
        perm("PUT reader 403", "PUT", 403, {"If-Match": '"v1"', **READER}, name),
        perm("PUT writer 428", "PUT", 428, WRITER, name),
        perm("PUT writer 412", "PUT", 412, {"If-Match": '"no"', **WRITER}, name),
        perm("PUT 400 problem", "PUT", 400, current, {"name": ""}),
        perm("PUT 200", "PUT", 200, current, name),
        perm("DELETE Two 403", "DELETE", 403, current, url="/x/perm-items/{two}/"),
        perm("DELETE 204", "DELETE", 204, current),
        listing("GET page 1", "GET", 200),
        listing("GET page x 400", "GET", 400, url="/x/perm-items/?page=x"),
        listing("GET page 99 404", "GET", 404, url="/x/perm-items/?page=99"),
        listing("POST 201", "POST", 201, WRITER, {"name": "Three"}),
        listing("POST 400 problem", "POST", 400, WRITER, {}),
        touch("GET action", "GET", 200),
        touch("POST action 200", "POST", 200, WRITER, {}),
        touch("POST action 401", "POST", 401, {}, {}),
        staff("GET anonymous 401", "GET", 401),
        staff("GET reader 403", "GET", 403, READER),
        staff("GET admin", "GET", 200, ADMIN),
        on("hooked", "/x/hooked-items/{one}/")("GET", "GET", 200),
        on("throttled", "/x/throttled-items/{one}/")("GET 429", "GET", 429),
        on("throttled", "/x/throttled-items/{one}/")("OPTIONS 429", "OPTIONS", 429),
        on("browsable", "/x/browsable-items/{one}/")("GET JSON", "GET", 200, JSON),
        on("browsable", "/x/browsable-items/{one}/")("GET no Accept", "GET", 200),
        on("browsable", "/x/browsable-items/{one}/")("PUT 428", "PUT", 428, {}, name),
        on("csv", "/x/csv-items/{one}/")("GET text/csv", "GET", 200),
        on("own", "/x/own-links/{one}/")("GET own Links", "GET", 200),
        on("apiview", "/x/apiview/{one}/")("GET", "GET", 200),
        on("apiview", "/x/apiview/{one}/")("HEAD", "HEAD", 200),
        on("apiview", "/x/apiview/{one}/")("PUT 428", "PUT", 428, {}, {}),
        on("writes", "/x/writes/")("GET", "GET", 200),
        on("writes", "/x/writes/")("POST 201", "POST", 201, {}, {}),
        on("writes", "/x/writes/")("DELETE 204", "DELETE", 204),
    ]


EXCHANGES = example_exchanges() + wire_exchanges()
#: The exchanges whose Links agree with an HSR entry: all but the errors
#: answering OPTIONS (the HSR's ``options`` entry is exactly a 204's).
AGREEING = [exchange for exchange in EXCHANGES if exchange.method != "OPTIONS"]


# --- Fixtures and helpers ------------------------------------------------------


@pytest.fixture(autouse=True)
def configured(settings):
    """The example's base URL (Django's test client serves testserver), two
    more hosts, and the production ``DEBUG``."""
    settings.SCRUDFUL = {"BASE_URL": f"{BASE}/"}
    settings.ALLOWED_HOSTS = ["testserver", "a.example", "b.example"]
    settings.DEBUG = False
    LOADS.clear()


class World:
    """The values of an :class:`Exchange`'s placeholders."""

    def __init__(self) -> None:
        one = Item.objects.create(name="One", description="first")
        two = Item.objects.create(name="Two", description="second")
        self.values = {
            "one": str(one.pk),
            "two": str(two.pk),
            "missing": "999999",
            "list_etag": f'"{example.collection_etag(None, None)}"',
        }

    def format(self, text: str) -> str:
        return text.format(**self.values) if "{" in text else text


@pytest.fixture
def client() -> Client:
    return Client(raise_request_exception=False)


@pytest.fixture
def world() -> World:
    return World()


def send(client: Client, world: World, exchange: Exchange) -> Any:
    """``exchange``'s request; its response, once it has the status."""
    headers = {name: world.format(value) for name, value in exchange.headers.items()}
    kwargs: dict[str, Any] = {"headers": headers}
    if exchange.body is not None:
        data = exchange.body
        if exchange.content_type == "application/json":
            data = json.dumps(data)
        kwargs.update(data=data, content_type=exchange.content_type)
    response = client.generic(exchange.method, world.format(exchange.url), **kwargs)
    assert response.status_code == exchange.status, (
        exchange.label,
        response.content[:300],
    )
    return response


def headers_of(response: Any) -> dict[str, str]:
    return dict(response.items())


def hsr_of(client: Client, kind: str) -> dict[str, Any]:
    """The HTTP Schema Resource of the ``kind`` route, by a GET of its route."""
    response = client.get(HSR_ROUTES[kind])
    assert response.status_code == 200, response.content
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


#: ``(kind, url, headers)`` of an OPTIONS each route answers with a 204.
OPTIONS = [
    ("item", "/items/{one}/", {}),
    ("item", "/items/{one}/", JSON),
    ("items", "/items/", {}),
    ("readonly", "/readonly-items/{one}/", {}),
    ("readonly-list", "/readonly-items/", {}),
    ("stats", "/stats/", {}),
    ("perm", "/x/perm-items/{one}/", WRITER),
    ("perm-list", "/x/perm-items/", {}),
    ("touch", "/x/perm-items/{one}/touch/", {}),
    ("staff", "/x/staff-items/{one}/", {}),
    ("hooked", "/x/hooked-items/{one}/", {}),
    ("browsable", "/x/browsable-items/{one}/", {"Accept": "text/html"}),
    ("csv", "/x/csv-items/{one}/", {}),
    ("own", "/x/own-links/{one}/", {}),
    ("apiview", "/x/apiview/{one}/", {}),
    ("writes", "/x/writes/", {}),
    ("boom", "/x/boom/", {}),
]


@pytest.mark.parametrize(
    "kind, url, headers", OPTIONS, ids=[f"{o[0]}-{bool(o[2])}" for o in OPTIONS]
)
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
    # The document of the route's view function.
    view_func = resolve(URLS[kind].format(one=1)).func
    assert json.loads(response.content) == http_schema_resource(view_func)
    # A conditional GET with its ETag: 304.
    again = client.get(HSR_ROUTES[kind], headers={"If-None-Match": response["ETag"]})
    assert again.status_code == 304


#: The credentials of two requesters: anonymous, and authenticated (the
#: wire views' key, the example's session cookie and Basic credentials).
REQUESTERS = ({}, {**WRITER, "Authorization": "Basic d3JpdGVyOnB3", "Cookie": "a=b"})


@pytest.mark.parametrize("kind", sorted(HSR_ROUTES))
def test_hsr_invariance(client, kind):
    """Two Hosts and two requesters: one HSR, as bytes and ETag. The built
    documents are discarded before each request, so each builds its own:
    a document that depended on the requester or its Host would differ."""
    responses = []
    for host in ("a.example", "b.example"):
        for credentials in REQUESTERS:
            scrudful_drf._HSRS.clear()
            responses.append(
                client.get(HSR_ROUTES[kind], headers={"Host": host, **credentials})
            )
    assert {response.status_code for response in responses} == {200}
    assert len({response.content for response in responses}) == 1
    assert len({response["ETag"] for response in responses}) == 1
    # Built without a request: no Host leaks into it.
    assert b"a.example" not in responses[0].content
    assert b"b.example" not in responses[0].content


ITEM_METHODS = {"GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"}
READ = {"GET", "HEAD", "OPTIONS"}
#: ``(label, url, headers, Allow)``.
ALLOW = [
    ("example item", "/items/{one}/", {}, ITEM_METHODS),
    ("example list", "/items/", {}, {"GET", "HEAD", "POST", "OPTIONS"}),
    ("example readonly", "/readonly-items/{one}/", {}, READ),
    ("example stats", "/stats/", {}, READ),
    ("anonymous", "/x/perm-items/{one}/", {}, READ),
    ("reader", "/x/perm-items/{one}/", READER, READ),
    ("writer", "/x/perm-items/{one}/", WRITER, ITEM_METHODS),
    ("writer, Two", "/x/perm-items/{two}/", WRITER, ITEM_METHODS - {"DELETE"}),
    ("anonymous list", "/x/perm-items/", {}, READ),
    ("writer list", "/x/perm-items/", WRITER, {"GET", "HEAD", "POST", "OPTIONS"}),
    ("anonymous action", "/x/perm-items/{one}/touch/", {}, READ),
    ("writer action", "/x/perm-items/{one}/touch/", WRITER, READ | {"POST"}),
    ("anonymous staff", "/x/staff-items/{one}/", {}, {"OPTIONS"}),
    ("reader staff", "/x/staff-items/{one}/", READER, {"OPTIONS"}),
    ("admin staff", "/x/staff-items/{one}/", ADMIN, ITEM_METHODS),
    ("anonymous hooked", "/x/hooked-items/{one}/", {}, READ),
    ("writer hooked", "/x/hooked-items/{one}/", WRITER, ITEM_METHODS),
    ("anonymous apiview", "/x/apiview/{one}/", {}, {"GET", "HEAD", "PUT", "OPTIONS"}),
    ("anonymous writes", "/x/writes/", {}, {"GET", "POST", "DELETE", "OPTIONS"}),
]


@pytest.mark.parametrize("label, url, headers, allow", ALLOW, ids=[a[0] for a in ALLOW])
def test_allow_per_requester(client, world, label, url, headers, allow):
    response = client.options(world.format(url), headers=headers)
    problems = options_204_problems(
        response.status_code, headers_of(response), response.content, allow
    )
    assert problems == []


@pytest.mark.parametrize("headers", [{}, READER], ids=["anonymous", "reader"])
def test_allow_is_no_existence_oracle_for_a_requester_refused_everything(
    client, world, headers
):
    """The admin-only view refuses this requester every method,
    so OPTIONS loads nothing, and a missing object answers as an existing one
    does."""
    answers = []
    for url in ("/x/staff-items/{one}/", "/x/staff-items/{missing}/"):
        response = client.options(world.format(url), headers=headers)
        problems = options_204_problems(
            response.status_code, headers_of(response), response.content, {"OPTIONS"}
        )
        assert problems == []
        assert hsr_link_problems(headers_of(response), target("staff")) == []
        answers.append((response.status_code, response["Allow"], response["Link"]))
    assert answers[0] == answers[1]
    assert LOADS == []


def test_allow_of_a_missing_object_is_its_404(client, world):
    """A requester who may use a method gets the 404 of a
    missing object, with the error Links and the HSR link, not an
    object-less Allow."""
    response = client.options(world.format("/x/perm-items/{missing}/"))
    assert response.status_code == 404
    # DRF's own Allow, every method the view serves, as on all its responses.
    assert response["Allow"] == "GET, PUT, PATCH, DELETE, HEAD, OPTIONS"
    assert link_problems("OPTIONS", 404, headers_of(response), True) == []
    assert hsr_link_problems(headers_of(response), target("perm")) == []


def test_allow_with_an_allowed_func_loads_the_object_for_anyone(client, world):
    """A documented exposure: a method with an ``allowed_func`` needs the
    object for every requester, so OPTIONS of a missing object is its 404
    whoever asks. Only a queryset hiding the objects keeps them secret."""
    response = client.options(world.format("/x/hooked-items/{missing}/"))
    assert response.status_code == 404
    assert LOADS == ["HookedItemViewSet"]
    assert link_problems("OPTIONS", 404, headers_of(response), True) == []
    assert hsr_link_problems(headers_of(response), target("hooked")) == []


PREFLIGHT = {"Origin": "https://app.example", "Access-Control-Request-Method": "PUT"}
#: ``(url, credentials)`` of preflights: open, locked, throttled and hooked
#: routes, missing objects, with and without credentials (a browser sends
#: none; a bad key, a cookie or Basic credentials are ignored).
PREFLIGHTS = [
    ("/items/{one}/", {}),
    ("/items/{missing}/", {}),
    ("/stats/", {}),
    ("/x/perm-items/{one}/", {"X-Key": "nobody", "Cookie": "sessionid=x"}),
    ("/x/perm-items/{one}/", {"Authorization": "Basic Zm9vOmJhcg=="}),
    ("/x/staff-items/{one}/", {}),
    ("/x/staff-items/{missing}/", {}),
    ("/x/hooked-items/{one}/", {}),
    ("/x/hooked-items/{missing}/", {}),
    ("/x/throttled-items/{one}/", {}),
    ("/x/apiview/{one}/", {}),
    ("/x/writes/", {}),
]


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
    assert LOADS == []


#: ``(label, method, url, headers, HSR kind)``: Django's 404 for an unknown
#: URL (and for an id the route's pattern refuses), DRF's 404, 405 and 406,
#: and Django's 500, each described or accepted.
UNDESCRIBED = [
    ("unknown URL 404", "GET", "/nope/", {}, "item"),
    ("unrouted id 404", "GET", "/items/abc/", {}, "item"),
    ("missing object 404", "GET", "/items/{missing}/", {}, "item"),
    ("format=api 404", "GET", "/items/{one}/?format=api", {}, "item"),
    ("TRACE 405", "TRACE", "/items/{one}/", {}, "item"),
    ("POST 405", "POST", "/readonly-items/", {}, "readonly-list"),
    ("HSR route 405", "POST", "/_hsr/item-detail", {}, "item"),
    ("500", "GET", "/x/boom/", {}, "boom"),
    ("browsable 404", "GET", "/x/browsable-items/{missing}/", {}, "browsable"),
    ("perm 401", "PUT", "/x/perm-items/{one}/", {}, "perm"),
]


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
    content = bool(response.content)
    assert link_problems(method, response.status_code, response_headers, content) == []


BROWSER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


@pytest.mark.parametrize(
    "url, kind",
    [
        ("/nope/", "item"),
        ("/x/browsable-items/{one}/", "browsable"),
    ],
)
def test_undescribed_html_only_when_accepted(client, world, url, kind):
    """A browser asking for HTML gets Django's page or the browsable API,
    with ``Vary: Accept``; a client accepting only JSON never does."""
    response = client.get(world.format(url), headers={"Accept": BROWSER})
    assert response["Content-Type"].startswith("text/html")
    assert "Accept" in response["Vary"]
    hsr = hsr_of(client, kind)
    assert undescribed_problems(BROWSER, headers_of(response), hsr) == []
    assert undescribed_problems("application/json", headers_of(response), hsr) != []
    json_response = client.get(world.format(url), headers=JSON)
    assert json_response["Content-Type"].startswith("application/json")
    assert (
        undescribed_problems("application/json", headers_of(json_response), hsr) == []
    )


def test_format_api_is_undescribed_html(client, world):
    """Excluded from the suite: DRF serves ``?format=api`` without ``Accept``
    as the browsable API's HTML, an undescribed response to a request whose
    ``Accept`` is absent. scrudful keeps the DRF feature and sends ``Vary:
    Accept``; with ``Accept: application/json`` the answer is DRF's 406,
    described. The example (JSON-only renderers) answers ``?format=api``
    with a described 404 (see ``UNDESCRIBED``)."""
    url = world.format("/x/browsable-items/{one}/?format=api")
    hsr = hsr_of(client, "browsable")
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert "Accept" in response["Vary"]
    assert undescribed_problems(None, headers_of(response), hsr) != []
    assert hsr_link_problems(headers_of(response), target("browsable")) == []
    response = client.get(url, headers=JSON)
    assert response.status_code == 406
    assert undescribed_problems("application/json", headers_of(response), hsr) == []
    assert link_problems("GET", 406, headers_of(response), True, hsr) == []


def test_vary_accept_only_with_a_renderer_that_isnt_json(client, world):
    """The browsable API's and the CSV view's responses send ``Vary:
    Accept``; the example's, JSON only, don't need it."""
    for url in ("/x/browsable-items/{one}/", "/x/csv-items/{one}/"):
        for headers in ({}, {"If-None-Match": '"v1"'}, {"If-Match": '"no"'}):
            response = client.get(world.format(url), headers=headers)
            assert "Accept" in response.get("Vary", ""), (url, headers)
    for url in ("/items/{one}/", "/stats/"):
        response = client.get(world.format(url))
        assert response.status_code == 200
        assert "Accept" not in response.get("Vary", "")


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


@pytest.mark.parametrize(
    "kind, url, headers", OPTIONS, ids=[f"{o[0]}-{bool(o[2])}" for o in OPTIONS]
)
def test_expose_headers_of_options(client, world, kind, url, headers):
    response = client.options(world.format(url), headers=headers)
    assert {"Link", "Allow"} <= exposed(response)


@pytest.mark.parametrize(
    "url, body",
    [("/x/perm-items/{one}/", {"name": ""}), ("/x/perm-items/", {})],
)
def test_the_serializer_400_is_problem_details(client, world, url, body):
    """With a problem-details error entity, DRF's validation 400 is problem
    details, as the HSR's entry says, its errors the ``errors`` member."""
    item = url.endswith("/{one}/")
    response = (client.put if item else client.post)(
        world.format(url),
        json.dumps(body),
        content_type="application/json",
        # A create checks no precondition without one.
        headers={"If-Match": '"v1"', **WRITER} if item else WRITER,
    )
    assert response.status_code == 400
    assert response["Content-Type"] == PROBLEM_JSON
    problem = json.loads(response.content)
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(problem)
    assert problem == problem_from_errors(
        400, problem["errors"], "Request validation failed"
    )
    assert problem["errors"]["name"]
    links = [link for link, _ in parse_links(response["Link"])]
    kind = "perm" if item else "perm-list"
    assert links == [
        PROBLEM.schema_link_or_func,
        PROBLEM.context_link_or_func,
        target(kind),
    ]
