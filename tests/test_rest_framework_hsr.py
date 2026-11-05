"""The HTTP Schema Resource of a DRF view, built without a request, and
served by ``hsr_urls()``.

The module is its own URLconf (``pytestmark`` below); the ``hsr_urls()``
tests use ``tests/drf_hsr_urls.py`` (the ``urls`` fixture) or a URLconf class.
"""

import json
import os
import pathlib
import subprocess
import sys
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import Group, User
from django.core import checks
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, RequestFactory
from django.urls import ResolverMatch, include, path, resolve, reverse
from rest_framework import viewsets
from rest_framework.authentication import BasicAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.metadata import BaseMetadata
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.views import APIView

from scrudful._hsr import (
    HsrConfigError,
    hsr_etag,
    hsr_link,
    hsr_response_links,
    serialize_hsr,
)
from scrudful.rest_framework import (
    Entity,
    ScrudfulMetadata,
    ScrudfulViewFunc,
    _hsr_target,
    hsr_urls,
    http_schema_resource,
    http_schema_resource_view,
    scrudful,
    scrudful_api_view,
    scrudful_viewset,
)
from tests import drf_hsr_urls
from tests.semantic_http import (
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    validate_http_schema_resource,
)

pytestmark = [pytest.mark.urls(__name__), pytest.mark.django_db]

BASE_URL = "https://api.example/"
OP_ERROR = Entity(
    schema_link_or_func="https://api.example/schemas/op-error",
    context_link_or_func="https://api.example/contexts/op-error",
)
API_ERROR = Entity(
    schema_link_or_func="https://api.example/schemas/api-error",
    context_link_or_func="https://api.example/contexts/api-error",
)
#: The errors DRF answers itself, which carry ``SCRUDFUL["ERROR"]``.
FRAMEWORK_ERRORS = ("401", "403", "406", "415")
OPTIONS_204 = {"responses": {"204": {"description": "No Content"}}}


def entity_content(entity, media_type="application/json"):
    """The ``content`` of a JSON ``entity`` with absolute links."""
    return {
        media_type: {
            "schema": {"$ref": entity.schema_link_or_func},
            "x-rdf-context": {
                "href": entity.context_link_or_func,
                "type": "application/ld+json",
            },
        }
    }


def item_etag(view, request, pk=None, **kwargs):
    return "v1"


@pytest.fixture(autouse=True)
def base_url(settings):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL}


@pytest.fixture
def client():
    return APIClient()


# --- Views --------------------------------------------------------------------


class ItemViewSetBase(viewsets.ViewSet):
    #: Set per route through ``as_view(..., resource_type_name=...)``, as
    #: scrud-django's views do.
    resource_type_name = None

    def list(self, request):
        return Response([])

    def create(self, request):
        return Response({"id": 1}, status=201)

    def retrieve(self, request, pk):
        if pk == "0":
            raise NotFound
        return Response({"id": pk})

    def update(self, request, pk):
        return Response({"id": pk})

    def destroy(self, request, pk):
        return Response(status=204)


@scrudful_viewset
class ItemViewSet(ItemViewSetBase):
    class Meta:
        etag_func = item_etag
        schema_link_or_func = "/schemas/item"
        context_link_or_func = "/contexts/item"
        error = OP_ERROR


@scrudful_viewset
class NoErrorViewSet(ItemViewSetBase):
    class Meta:
        etag_func = item_etag
        schema_link_or_func = "/schemas/item"
        context_link_or_func = "/contexts/item"


@scrudful_viewset
class SameErrorViewSet(ItemViewSetBase):
    class Meta:
        etag_func = item_etag
        error = API_ERROR


@scrudful_viewset
class LockedViewSet(ItemViewSet):
    """Authentication required: an anonymous request is a 401."""

    authentication_classes = [BasicAuthentication]
    permission_classes = [IsAuthenticated]


@scrudful_viewset
class MetaBaseViewSet(ItemViewSetBase):
    class Meta:
        schema_link_or_func = "/schemas/item"
        context_link_or_func = "/contexts/item"
        base_url = "https://meta.example/"


@scrudful_viewset
class ActionView(viewsets.ViewSet):
    class Meta:
        statuses = {"post": [200]}

    @action(detail=False, methods=["post"])
    @scrudful(
        schema_link_or_func="https://x/s",
        context_link_or_func="https://x/c",
    )
    def ping(self, request):
        return Response({"pong": True})


CSV = Entity(
    schema_link_or_func="https://x/table.csvm.json",
    schema_type_or_func="application/csvm+json",
    context_link_or_func="https://x/table.csvm.json",
    context_type_or_func="application/csvm+json",
    media_type="text/csv",
)
CSV_LINKS = (
    '<https://x/table.csvm.json>; rel="describedby"; '
    'type="application/csvm+json", '
    '<https://x/table.csvm.json>; rel="https://w3id.org/semantic-http/ns#'
    'rdf-context"; type="application/csvm+json"'
)


class CsvView(APIView):
    @scrudful(entity=CSV)
    def get(self, request):
        return Response("a,b\r\n1,2\r\n", content_type="text/csv")


@scrudful_viewset
class CsvViewSet(ItemViewSetBase):
    class Meta:
        entity = CSV


@scrudful_api_view(
    ["GET", "PUT"],
    etag_func=lambda view, request, **kwargs: "v1",
    schema_link_or_func="/schemas/stats",
    context_link_or_func="/contexts/stats",
    statuses={"put": [200, 202]},
)
def stats(request):
    return Response({"count": 0})


@scrudful_api_view(
    ["GET"],
    schema_link_or_func="/schemas/stats",
    context_link_or_func="/contexts/stats",
    base_url="https://decorator.example/",
)
def own_base(request):
    return Response({})


#: Two routes of identical views: each request builds its own document.
HOST_A = ItemViewSet.as_view({"get": "retrieve", "put": "update"})
HOST_B = ItemViewSet.as_view({"get": "retrieve", "put": "update"})

urlpatterns = [
    path(
        "items/<str:pk>/",
        ItemViewSet.as_view({"get": "retrieve", "put": "update"}),
        name="items",
    ),
    path(
        "no-error/<str:pk>/",
        NoErrorViewSet.as_view({"get": "retrieve"}),
        name="no-error",
    ),
    path("locked/<str:pk>/", LockedViewSet.as_view({"get": "retrieve"}), name="locked"),
    path("a/<str:pk>/", HOST_A, name="a"),
    path("b/<str:pk>/", HOST_B, name="b"),
    path("csv/", CsvView.as_view(), name="csv"),
    path(
        "csv-items/<str:pk>/",
        CsvViewSet.as_view({"get": "retrieve"}),
        name="csv-items",
    ),
    path("stats/", stats, name="stats"),
    path("own-base/", own_base, name="own-base"),
    # Every response of a scrudful view links its HTTP Schema Resource.
    path("", include(hsr_urls())),
]


# --- The document -------------------------------------------------------------


def test_hsr_independent_of_request(settings):
    settings.SCRUDFUL = {"BASE_URL": "https://api.example/"}
    func = ItemViewSet.as_view(
        {"get": "retrieve", "put": "update"}, resource_type_name="item"
    )
    body = http_schema_resource(func)
    validate_http_schema_resource(body)
    assert body["options"] == OPTIONS_204
    assert body == http_schema_resource(func)  # cached and stable
    assert list(body) == ["get", "put", "options", "head"]
    get = body["get"]["responses"]
    assert get["200"]["content"] == {
        "application/json": {
            "schema": {"$ref": "https://api.example/schemas/item"},
            "x-rdf-context": {
                "href": "https://api.example/contexts/item",
                "type": "application/ld+json",
            },
        }
    }


class HostsURLconf:
    urlpatterns = [
        path("a/<str:pk>/", HOST_A, name="a"),
        path("b/<str:pk>/", HOST_B, name="b"),
        path("", include(hsr_urls())),
    ]


def test_hsr_is_identical_across_hosts_and_requesters(client, settings):
    """Relative links, two hosts and two requesters, each route building its
    own document on its first request (a GET of its HSR): a request-based
    build would differ."""
    settings.ALLOWED_HOSTS = ["a.example", "b.example"]
    settings.ROOT_URLCONF = HostsURLconf
    a = client.get("/_hsr/a", HTTP_HOST="a.example")
    other = APIClient()
    other.force_login(User.objects.create(username="bob"))
    b = other.get("/_hsr/b", HTTP_HOST="b.example")
    assert b.wsgi_request.user.username == "bob"
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == http_schema_resource(HOST_A)
    assert http_schema_resource(HOST_B) == http_schema_resource(HOST_A)
    assert "https://api.example/schemas/item" in json.dumps(a.json())
    assert "a.example" not in json.dumps(a.json())
    # The OPTIONS of either host links to the HSR at the base URL.
    a_options = client.options("/a/1/", HTTP_HOST="a.example")
    b_options = other.options("/a/1/", HTTP_HOST="b.example")
    for response in (a_options, b_options):
        headers = dict(response.items())
        assert hsr_link_problems(headers, "https://api.example/_hsr/a") == []


def test_determine_metadata_returns_the_resolved_views_document():
    request = APIRequestFactory().options("/items/1/")
    request.resolver_match = resolve("/items/1/")
    func = request.resolver_match.func
    body = ScrudfulMetadata().determine_metadata(Request(request), func.cls())
    assert body == http_schema_resource(func)
    assert body["options"] == OPTIONS_204


def test_http_schema_resource_returns_a_copy():
    func = ItemViewSet.as_view({"get": "retrieve"})
    body = http_schema_resource(func)
    body["get"]["responses"].clear()
    assert http_schema_resource(func)["get"]["responses"]


def test_hsr_is_built_once_per_view_func():
    calls = []

    @scrudful_viewset
    class Counting(ItemViewSetBase):
        class Meta:
            schema_link_or_func = staticmethod(
                lambda view, request, **kw: calls.append(1) or "https://x/s"
            )
            context_link_or_func = "https://x/c"

    func = Counting.as_view({"get": "retrieve"})
    http_schema_resource(func)
    built = len(calls)
    assert built
    http_schema_resource(func)
    assert len(calls) == built
    # Another view function of the class is another document.
    http_schema_resource(Counting.as_view({"get": "retrieve"}))
    assert len(calls) == 2 * built


def test_a_settings_change_rebuilds(settings):
    func = ItemViewSet.as_view({"get": "retrieve"})
    assert "https://api.example/schemas/item" in json.dumps(http_schema_resource(func))
    settings.SCRUDFUL = {"BASE_URL": "https://other.example/"}
    assert "https://other.example/schemas/item" in json.dumps(
        http_schema_resource(func)
    )


def test_describe_mode_view_has_initkwargs():
    seen = []

    class V(viewsets.ModelViewSet):
        resource_type_name = None

        class Meta:
            schema_link_or_func = staticmethod(
                lambda view, request, **kw: seen.append(
                    (view.resource_type_name, request)
                )
                or "https://x/s"
            )
            context_link_or_func = "https://x/c"

    scrudful_viewset(V)
    http_schema_resource(V.as_view({"get": "retrieve"}, resource_type_name="note"))
    assert seen and set(seen) == {("note", None)}


def test_describe_mode_view_is_a_requestless_view_instance():
    seen = []

    def schema(view, request, **kwargs):
        seen.append(
            (type(view), view.action, view.action_map, view.args, view.kwargs, request)
        )
        assert view.request is None
        return "https://x/s"

    @scrudful_viewset
    class V(ItemViewSetBase):
        class Meta:
            schema_link_or_func = schema
            context_link_or_func = "https://x/c"

    http_schema_resource(V.as_view({"get": "retrieve", "put": "update"}))
    actions = {"get": "retrieve", "put": "update", "head": "retrieve"}
    assert {entry[0] for entry in seen} == {V}
    assert {entry[1] for entry in seen} == {"retrieve", "update"}
    assert all(entry[2:] == (actions, (), {}, None) for entry in seen)


def test_api_views_and_function_views_are_described():
    # Django's View.setup serves HEAD by GET.
    body = http_schema_resource(CsvView.as_view())
    assert list(body) == ["get", "options", "head"]
    body = http_schema_resource(stats)
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "options"]
    assert body["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "https://api.example/schemas/stats"
    }


def test_not_a_drf_view_is_a_type_error():
    with pytest.raises(TypeError, match="as_view"):
        http_schema_resource(lambda request: None)


# --- Link callables in describe mode ------------------------------------------


def test_callable_needing_the_request_is_improperly_configured():
    def schema(view, request, **kwargs):
        return request.build_absolute_uri("/s")

    @scrudful_viewset
    class V(ItemViewSetBase):
        class Meta:
            schema_link_or_func = schema
            context_link_or_func = "https://x/c"

    with pytest.raises(ImproperlyConfigured) as info:
        http_schema_resource(V.as_view({"get": "retrieve"}))
    message = str(info.value)
    assert "func(view, None)" in message
    assert "schema" in message
    assert isinstance(info.value.__cause__, AttributeError)


def test_callable_error_message():
    def schema(view, request, **kwargs):
        raise LookupError("no request")

    @scrudful_viewset
    class V(ItemViewSetBase):
        class Meta:
            schema_link_or_func = schema
            context_link_or_func = "https://x/c"

    with pytest.raises(ImproperlyConfigured) as info:
        http_schema_resource(V.as_view({"get": "retrieve"}))
    assert str(info.value) == (
        "test_callable_error_message.<locals>.schema, a link callable of V GET, "
        "must work without a request when the HTTP Schema Resource is built "
        "(called as func(view, None)): LookupError('no request')"
    )
    assert isinstance(info.value.__cause__, LookupError)


def test_url_kwargs_are_not_passed_in_describe_mode():
    @scrudful_viewset
    class V(ItemViewSetBase):
        class Meta:
            schema_link_or_func = staticmethod(
                lambda view, request, pk: f"https://x/s?pk={pk}"
            )
            context_link_or_func = "https://x/c"

    with pytest.raises(ImproperlyConfigured, match=r"func\(view, None\)"):
        http_schema_resource(V.as_view({"get": "retrieve"}))


def test_error_entity_callables_are_called_in_describe_mode():
    seen = []
    relative = Entity(
        schema_link_or_func=lambda view, request: seen.append(request) or "/s/e",
        context_link_or_func="/c/e",
    )

    @scrudful_viewset
    class V(ItemViewSetBase):
        class Meta:
            etag_func = item_etag
            error = relative

    body = http_schema_resource(V.as_view({"get": "retrieve"}))
    assert seen and set(seen) == {None}
    assert body["get"]["responses"]["412"]["content"]["application/json"]["schema"] == {
        "$ref": "https://api.example/s/e"
    }


# --- Base URL -----------------------------------------------------------------


def test_relative_link_without_base_url_is_improperly_configured(settings):
    settings.SCRUDFUL = {}
    with pytest.raises(ImproperlyConfigured) as info:
        http_schema_resource(ItemViewSet.as_view({"get": "retrieve"}))
    # HsrConfigError's own message.
    assert "relative URL '/schemas/item'" in str(info.value)
    assert "SCRUDFUL['BASE_URL']" in str(info.value)
    assert isinstance(info.value.__cause__, HsrConfigError)


def test_relative_link_resolves_against_base_url_on_the_wire_too(client):
    response = client.get("/items/1/")
    assert response.status_code == 200
    assert "<https://api.example/schemas/item>" in response["Link"]
    assert "<https://api.example/contexts/item>" in response["Link"]
    body = json.dumps(http_schema_resource(response.wsgi_request.resolver_match.func))
    assert "https://api.example/schemas/item" in body
    assert "https://api.example/contexts/item" in body


def test_error_links_resolve_against_base_url_on_the_wire_too(client):
    relative = Entity(schema_link_or_func="/s/e", context_link_or_func="/c/e")

    @scrudful_viewset
    class V(ItemViewSetBase):
        class Meta:
            error = relative

    response = V.as_view({"get": "retrieve"})(APIRequestFactory().get("/x/0/"), pk="0")
    assert response.status_code == 404
    assert "<https://api.example/s/e>" in response["Link"]
    # A scrudful early response too.
    response = client.put("/items/1/")
    assert response.status_code == 428
    assert "<https://api.example/schemas/op-error>" in response["Link"]


def test_without_base_url_the_wire_resolves_against_the_request(client, settings):
    settings.SCRUDFUL = {}
    response = client.get("/items/1/")
    assert "<http://testserver/schemas/item>" in response["Link"]


def test_meta_base_url_overrides_the_setting():
    func = MetaBaseViewSet.as_view({"get": "retrieve"})
    assert "https://meta.example/schemas/item" in json.dumps(http_schema_resource(func))
    response = func(APIRequestFactory().get("/x/1/"), pk="1")
    assert "<https://meta.example/schemas/item>" in response["Link"]


def test_decorator_base_url_overrides_the_setting(client):
    assert "https://decorator.example/schemas/stats" in json.dumps(
        http_schema_resource(own_base)
    )
    assert (
        "<https://decorator.example/schemas/stats>" in client.get("/own-base/")["Link"]
    )


@pytest.mark.parametrize("value", ["/relative/", "api.example", 3])
def test_base_url_must_be_absolute(settings, value):
    with pytest.raises(ImproperlyConfigured, match="absolute URL"):
        scrudful(base_url=value)
    settings.SCRUDFUL = {"BASE_URL": value}
    with pytest.raises(ImproperlyConfigured, match=r"SCRUDFUL\[.BASE_URL.\]"):
        http_schema_resource(NoErrorViewSet.as_view({"get": "retrieve"}))


# --- statuses= ----------------------------------------------------------------


def test_statuses_option():
    body = http_schema_resource(ActionView.as_view({"post": "ping"}))
    assert set(body["post"]["responses"]) >= {"200"}
    assert "201" not in body["post"]["responses"]
    assert body["post"]["responses"]["200"]["content"] == {
        "application/json": {
            "schema": {"$ref": "https://x/s"},
            "x-rdf-context": {"href": "https://x/c", "type": "application/ld+json"},
        }
    }


def test_statuses_default_to_drfs():
    body = http_schema_resource(
        ItemViewSet.as_view({"post": "create", "delete": "destroy"})
    )
    assert "201" in body["post"]["responses"]
    assert "204" in body["delete"]["responses"]
    body = http_schema_resource(ItemViewSet.as_view({"get": "retrieve"}))
    assert "200" in body["get"]["responses"]


def test_decorator_statuses_each_get_the_content():
    responses = http_schema_resource(stats)["put"]["responses"]
    assert {"200", "202"} <= set(responses)
    assert responses["202"]["description"] == "Accepted"
    assert responses["202"]["content"] == responses["200"]["content"]


@pytest.mark.parametrize(
    "statuses, error",
    [
        ([200], TypeError),
        ({"get": 200}, TypeError),
        ({"get": "200"}, TypeError),
        ({"get": [True]}, TypeError),
        ({"get": []}, ValueError),
        ({"get": [404]}, ValueError),
        ({"fetch": [200]}, ValueError),
        ({"options": [200]}, ValueError),
    ],
)
def test_statuses_are_checked(statuses, error):
    with pytest.raises(error):
        scrudful(statuses=statuses)


# --- entity= ------------------------------------------------------------------


def test_entity_option_describes_csv():
    body = http_schema_resource(CsvView.as_view())
    validate_http_schema_resource(body)
    assert body["get"]["responses"]["200"]["content"] == {
        "text/csv": {
            "x-schema": {
                "href": "https://x/table.csvm.json",
                "type": "application/csvm+json",
            },
            "x-rdf-context": {
                "href": "https://x/table.csvm.json",
                "type": "application/csvm+json",
            },
        }
    }


def test_entity_links_on_the_wire(client):
    for url, name in (("/csv/", "csv"), ("/csv-items/1/", "csv-items")):
        hsr = hsr_link(f"https://api.example/_hsr/{name}")
        assert client.get(url)["Link"] == f"{CSV_LINKS}, {hsr}"
    body = http_schema_resource(CsvViewSet.as_view({"get": "retrieve"}))
    assert "text/csv" in body["get"]["responses"]["200"]["content"]


def test_entity_defaults_to_json_from_the_link_options():
    handler = ItemViewSet.__dict__["retrieve"]
    assert handler.entity is None
    assert handler.media_type == "application/json"
    assert ItemViewSet.__dict__["retrieve"].schema_link_or_func == "/schemas/item"


def test_entity_and_link_options_are_exclusive():
    with pytest.raises(TypeError, match="entity"):
        scrudful(entity=CSV, schema_link_or_func="https://x/s")
    with pytest.raises(TypeError, match="Entity"):
        scrudful(entity="text/csv")
    with pytest.raises(TypeError, match="entity"):

        @scrudful_viewset
        class Both(ItemViewSetBase):
            class Meta:
                entity = CSV
                schema_link_or_func = "https://x/s"


# --- allowed_func= ------------------------------------------------------------


def test_allowed_func_is_stored():
    def allowed(view, request, method, obj):
        return True

    assert scrudful(allowed_func=allowed)(lambda request: None).allowed_func is allowed

    @scrudful_viewset
    class V(ItemViewSetBase):
        class Meta:
            allowed_func = allowed

    assert V.__dict__["retrieve"].allowed_func is allowed
    assert V.__dict__["list"].allowed_func is allowed
    assert ScrudfulViewFunc(lambda request: None).allowed_func is None
    with pytest.raises(TypeError, match="allowed_func"):
        scrudful(allowed_func="yes")


# --- Error entries ------------------------------------------------------------


def test_default_and_explicit_error_entries(settings):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "ERROR": API_ERROR}
    body = http_schema_resource(
        ItemViewSet.as_view({"get": "retrieve", "put": "update"})
    )
    validate_http_schema_resource(body)
    for method in ("get", "put"):
        responses = body[method]["responses"]
        assert responses["default"] == {
            "description": "Error.",
            "content": entity_content(OP_ERROR),
        }
        for key in FRAMEWORK_ERRORS:
            assert responses[key]["content"] == entity_content(API_ERROR), key
            assert responses[key]["description"]


def test_default_error_entry_is_the_api_wide_entity(settings):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "ERROR": API_ERROR}
    body = http_schema_resource(NoErrorViewSet.as_view({"get": "retrieve"}))
    responses = body["get"]["responses"]
    assert responses["default"]["content"] == entity_content(API_ERROR)
    assert not set(FRAMEWORK_ERRORS) & responses.keys()


def test_same_entity_needs_no_explicit_error_entries(settings):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "ERROR": API_ERROR}
    body = http_schema_resource(SameErrorViewSet.as_view({"get": "retrieve"}))
    responses = body["get"]["responses"]
    assert responses["default"]["content"] == entity_content(API_ERROR)
    assert not set(FRAMEWORK_ERRORS) & responses.keys()


def test_handler_error_without_api_wide_entity():
    """DRF's own 401/403/406/415 then carry the handler's entity (as in 0.9),
    which ``default`` describes: no explicit entries."""
    body = http_schema_resource(ItemViewSet.as_view({"get": "retrieve"}))
    responses = body["get"]["responses"]
    assert responses["default"]["content"] == entity_content(OP_ERROR)
    assert not set(FRAMEWORK_ERRORS) & responses.keys()


def test_no_error_entity_gives_a_default_entry_without_content():
    body = http_schema_resource(NoErrorViewSet.as_view({"get": "retrieve"}))
    assert body["get"]["responses"]["default"] == {"description": "Error."}


def test_head_error_entries_have_no_content(settings):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "ERROR": API_ERROR}
    body = http_schema_resource(ItemViewSet.as_view({"get": "retrieve"}))
    responses = body["head"]["responses"]
    assert set(responses) == set(body["get"]["responses"])
    assert responses["default"] == {"description": "Error."}
    assert all("content" not in entry for entry in responses.values())


def test_without_scrudfuls_exception_handler_errors_are_described_without_content(
    settings,
):
    """Raised errors then carry no Links: ``default`` has no content."""
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "ERROR": API_ERROR}
    settings.REST_FRAMEWORK = {}
    body = http_schema_resource(ItemViewSet.as_view({"get": "retrieve"}))
    responses = body["get"]["responses"]
    assert responses["default"] == {"description": "Error."}
    assert not set(FRAMEWORK_ERRORS) & responses.keys()
    # scrudful's own 412 still carries the handler's entity.
    assert responses["412"]["content"] == entity_content(OP_ERROR)


def test_the_wire_agrees_with_the_error_entries(client, settings):
    """401/403 carry SCRUDFUL["ERROR"]; other raised errors the handler's."""
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "ERROR": API_ERROR}
    refused = client.get("/locked/1/")
    assert refused.status_code == 401
    assert refused["Link"].startswith("<https://api.example/schemas/api-error>")
    missing = client.get("/items/0/")
    assert missing.status_code == 404
    assert missing["Link"].startswith("<https://api.example/schemas/op-error>")
    # A handler without an error entity: the API-wide one.
    missing = client.get("/no-error/0/")
    assert missing.status_code == 404
    assert missing["Link"].startswith("<https://api.example/schemas/api-error>")


def test_error_setting_must_be_an_entity(settings):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "ERROR": "problem"}
    with pytest.raises(ImproperlyConfigured, match="Entity"):
        http_schema_resource(ItemViewSet.as_view({"get": "retrieve"}))
    settings.SCRUDFUL = {
        "BASE_URL": BASE_URL,
        "ERROR": Entity(media_type="text/plain"),
    }
    with pytest.raises(ImproperlyConfigured, match="JSON"):
        http_schema_resource(ItemViewSet.as_view({"get": "retrieve"}))


# --- Removed ScrudfulMetadata hooks --------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "determine_metadata_for_get",
        "determine_metadata_for_head",
        "determine_metadata_for_options",
        "determine_metadata_for_post",
        "determine_metadata_for_put",
        "determine_metadata_for_patch",
        "determine_metadata_for_delete",
        "_write",
        "required_conditional_headers",
        "determine_metadata",
    ],
)
def test_a_subclass_defining_a_removed_hook_is_a_type_error(name):
    """0.9's hooks took the request: overriding one would silently do nothing."""
    with pytest.raises(TypeError) as info:
        type("Custom", (ScrudfulMetadata,), {name: lambda self, *args: {}})
    message = str(info.value)
    assert f"Custom.{name}" in message
    assert "without a request" in message
    assert "statuses=" in message and "entity=" in message
    assert "0.10.0 changelog" in message


def test_determine_metadata_served_nowhere_is_a_type_error():
    """R18: OPTIONS has no body and the HSR route builds the document
    itself, so an override would silently do nothing, mixed in too."""

    class Own(BaseMetadata):
        def determine_metadata(self, request, view):
            return {}

    def own_hook(self, request, view):
        return {}

    cases = [
        ((ScrudfulMetadata,), {"determine_metadata": own_hook}),
        ((Own, ScrudfulMetadata), {}),
    ]
    for bases, namespace in cases:
        with pytest.raises(TypeError) as info:
            type("Custom", bases, namespace)
        message = str(info.value)
        assert "Custom.determine_metadata" in message
        assert "served nowhere" in message
        assert "204 without a body" in message
    # Mixed in after ScrudfulMetadata, its determine_metadata never runs:
    # nothing to drop.
    combined = type("Combined", (ScrudfulMetadata, Own), {})
    assert combined.determine_metadata is ScrudfulMetadata.determine_metadata


def test_a_subclass_without_removed_hooks_still_works(settings, client):
    class Extended(ScrudfulMetadata):
        label = "extended"

    @scrudful_viewset
    class V(ItemViewSet):
        metadata_class = Extended

    func = V.as_view({"get": "retrieve"})
    request = APIRequestFactory().options("/v/1/")
    request.resolver_match = ResolverMatch(func, (), {"pk": "1"})
    body = Extended().determine_metadata(Request(request), func.cls())
    assert body == http_schema_resource(func)
    assert body["options"] == OPTIONS_204

    # A ScrudfulMetadata subclass keeps scrudful's OPTIONS.
    class URLconf:
        urlpatterns = [
            path("v/<str:pk>/", func, name="v"),
            path("", include(hsr_urls())),
        ]

    settings.ROOT_URLCONF = URLconf
    r = client.options("/v/1/")
    assert options_204_problems(r.status_code, dict(r.items()), r.content) == []


# --- hsr_urls() ----------------------------------------------------------------

NO_INCLUDE = (
    "add path('', include(scrudful.rest_framework.hsr_urls())) to your root "
    "URLconf, or set SCRUDFUL['HSR_URL']"
)


@pytest.fixture
def urls(settings):
    """A URLconf of a DefaultRouter, a hand-mounted path(), scrudful_api_views,
    a namespaced include and hsr_urls() (``tests/drf_hsr_urls.py``)."""
    settings.ROOT_URLCONF = "tests.drf_hsr_urls"


def target(url, **extra):
    """``_hsr_target`` of a GET of ``url``, resolved by the current URLconf."""
    request = RequestFactory().get(url, **extra)
    request.resolver_match = resolve(url)
    return _hsr_target(request)


def schema_ref(response, method="get", status="200"):
    content = json.loads(response.content)[method]["responses"][status]["content"]
    return content["application/json"]["schema"]["$ref"]


def get_problems(response):
    return hsr_get_problems(
        response.status_code, dict(response.items()), response.content
    )


@pytest.mark.parametrize(
    "view_name, url",
    [
        ("item-detail", "/items/1/"),
        ("item-list", "/items/"),
        ("notes", "/notes/1"),
        ("api:thing-detail", "/api/things/1/"),
        ("api:thing-list", "/api/things/"),
        ("stats", "/stats"),
        ("report", "/report"),
    ],
)
def test_hsr_urls_serves_every_scrudful_view(urls, client, view_name, url):
    response = client.get(f"/_hsr/{view_name}")
    assert get_problems(response) == []
    assert json.loads(response.content) == http_schema_resource(resolve(url).func)
    assert response.content == serialize_hsr(json.loads(response.content))
    assert response["ETag"] == hsr_etag(response.content)
    assert response["Link"] == hsr_response_links()
    # ThingViewSet's Meta.hsr_cache_control, else the default.
    cache_control = (
        "public, max-age=3600" if "thing" in view_name else "public, max-age=300"
    )
    assert response["Cache-Control"] == cache_control
    assert "Link" in response["Access-Control-Expose-Headers"]


def test_hsr_route_takes_a_namespaced_view_name(urls, client):
    # Django's str converter excludes only "/": the ":" of a namespace is kept.
    assert (
        reverse("scrudful_hsr", kwargs={"view_name": "api:thing-detail"})
        == "/_hsr/api:thing-detail"
    )
    response = client.get("/_hsr/api:thing-detail")
    assert response.status_code == 200
    assert schema_ref(response) == "https://things.example/schemas/thing"
    # Not the un-namespaced name.
    assert client.get("/_hsr/thing-detail").status_code == 404


@pytest.mark.parametrize(
    "view_name", ["plain-detail", "api-root", "scrudful_hsr", "nope", "api:nope"]
)
def test_hsr_of_no_scrudful_view_is_a_json_404(urls, client, view_name):
    response = client.get(f"/_hsr/{view_name}")
    assert response.status_code == 404
    assert response["Content-Type"] == "application/json"
    assert json.loads(response.content) == {"detail": "Not found."}


def test_hsr_identical_across_hosts_and_requesters(urls, settings):
    settings.ALLOWED_HOSTS = ["a.example", "b.example"]
    anonymous = Client()
    alice = Client()
    alice.force_login(User.objects.create_user("alice"))
    # Two view functions of one viewset: each request builds its own
    # document (the first to need it), so a request-dependent build differs.
    a = anonymous.get("/_hsr/item-detail", HTTP_HOST="a.example")
    b = alice.get("/_hsr/other-item-detail", HTTP_HOST="b.example")
    assert a.status_code == b.status_code == 200
    assert a.content == b.content
    assert a["ETag"] == b["ETag"]
    # Every method, whatever the requester (allowed_func is OPTIONS' Allow).
    assert list(json.loads(a.content)) == ["get", "put", "delete", "options", "head"]
    assert schema_ref(a) == "https://api.example/schemas/item"
    # The same document again, the other way round.
    again = alice.get("/_hsr/item-detail", HTTP_HOST="b.example")
    assert again.content == a.content


def test_hsr_get_answers_if_none_match_with_304_and_serves_head(urls, client):
    etag = client.get("/_hsr/item-detail")["ETag"]
    assert etag.startswith('"')  # strong
    for if_none_match in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        response = client.get("/_hsr/item-detail", HTTP_IF_NONE_MATCH=if_none_match)
        assert response.status_code == 304
        assert response.content == b""
        assert response["ETag"] == etag
        assert response["Cache-Control"] == "public, max-age=300"
    other = client.get("/_hsr/item-detail", HTTP_IF_NONE_MATCH='"other"')
    assert other.status_code == 200
    head = client.head("/_hsr/item-detail")
    assert head.status_code == 200
    assert head.content == b""
    assert head["ETag"] == etag
    assert head["Link"] == hsr_response_links()


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "options"])
def test_hsr_route_answers_other_methods_405(urls, method):
    # CSRF-exempt: an unsafe method gets the 405, not CSRF's 403.
    client = Client(enforce_csrf_checks=True)
    response = getattr(client, method)("/_hsr/item-detail")
    assert response.status_code == 405
    assert response["Allow"] == "GET, HEAD"
    # No content, so no media type to describe.
    assert response.content == b""
    assert "Content-Type" not in response


@pytest.fixture
def throttle_cache():
    cache.clear()
    yield
    cache.clear()


def test_hsr_route_runs_no_permission_check_or_throttle(urls, client, throttle_cache):
    # The resource: admins only, once a day.
    assert client.get("/locked/1/").status_code in (401, 403)
    admin = Client()
    admin.force_login(User.objects.create_user("admin", is_staff=True))
    assert admin.get("/locked/1/").status_code == 200
    assert admin.get("/locked/1/").status_code == 429
    # Its HTTP Schema Resource: anyone, any number of times.
    for requester in (client, admin, client):
        assert get_problems(requester.get("/_hsr/locked-detail")) == []


def test_hsr_cache_control_from_the_setting_and_meta(urls, client, settings):
    settings.SCRUDFUL = {
        "BASE_URL": BASE_URL,
        "HSR_CACHE_CONTROL": "public, max-age=60",
    }
    assert client.get("/_hsr/item-detail")["Cache-Control"] == "public, max-age=60"
    # Meta.hsr_cache_control, on the 200 and the 304.
    thing = client.get("/_hsr/api:thing-detail")
    assert thing["Cache-Control"] == "public, max-age=3600"
    not_modified = client.get(
        "/_hsr/api:thing-detail", HTTP_IF_NONE_MATCH=thing["ETag"]
    )
    assert not_modified.status_code == 304
    assert not_modified["Cache-Control"] == "public, max-age=3600"


class UnnamedURLconf:
    urlpatterns = [
        path("plain/", drf_hsr_urls.schema),  # not scrudful: needs no name
        path(
            "v1/",
            include([path("notes/<int:pk>", drf_hsr_urls.NoteView.as_view())]),
        ),
        path("", include(hsr_urls())),
    ]


class UnnamedAPIViewURLconf:
    urlpatterns = [path("stats/", drf_hsr_urls.stats), path("", include(hsr_urls()))]


def test_unnamed_scrudful_view_is_improperly_configured(settings, client):
    """A request of the unnamed route raises; the HSR route has no name of it
    to serve, so any name it is asked for is a 404 (R28)."""
    settings.ROOT_URLCONF = UnnamedURLconf
    with pytest.raises(ImproperlyConfigured, match="'v1/notes/<int:pk>'") as info:
        target("/v1/notes/1")
    assert "name=" in str(info.value)
    assert client.get("/_hsr/notes").status_code == 404
    settings.ROOT_URLCONF = UnnamedAPIViewURLconf
    with pytest.raises(ImproperlyConfigured, match="'stats/'"):
        target("/stats/")
    assert client.get("/_hsr/stats").status_code == 404


class ClashURLconf:
    urlpatterns = [
        path("notes/<int:pk>", drf_hsr_urls.NoteView.as_view(), name="clash"),
        path("stats", drf_hsr_urls.stats, name="clash"),
        path("report", drf_hsr_urls.report, name="report"),
        path("", include(hsr_urls())),
    ]


def test_one_name_for_two_scrudful_views_is_improperly_configured(settings, client):
    settings.ROOT_URLCONF = ClashURLconf
    with pytest.raises(ImproperlyConfigured, match="'clash'") as info:
        client.get("/_hsr/clash")
    assert "'notes/<int:pk>'" in str(info.value)
    assert "'stats'" in str(info.value)
    # The other names are served (R28).
    response = client.get("/_hsr/report")
    assert response.status_code == 200
    assert get_problems(response) == []


class OneBadRouteURLconf:
    """A named scrudful route beside an unnamed one (the final review's probe)."""

    thing = drf_hsr_urls.ItemViewSet.as_view({"get": "retrieve", "put": "update"})
    urlpatterns = [
        path("things/<str:pk>/", thing, name="thing"),
        path("other/<str:pk>/", thing),
        path("", include(hsr_urls())),
    ]


def test_one_bad_route_leaves_the_others_hsrs_served(settings, client):
    """An unnamed scrudful route breaks only its own requests (R28)."""
    settings.ROOT_URLCONF = OneBadRouteURLconf
    response = client.get("/_hsr/thing")
    assert response.status_code == 200
    assert get_problems(response) == []
    assert client.get("/things/1/").status_code == 200
    with pytest.raises(ImproperlyConfigured, match="'other/<str:pk>/'"):
        client.get("/other/1/")


class NoIncludeURLconf:
    urlpatterns = [
        path(
            "items/<str:pk>/",
            drf_hsr_urls.ItemViewSet.as_view({"get": "retrieve"}),
            name="item-detail",
        )
    ]


def test_missing_include_is_improperly_configured(settings):
    # Task 12 makes OPTIONS (and Task 13 every response) link to this target.
    settings.ROOT_URLCONF = NoIncludeURLconf
    with pytest.raises(ImproperlyConfigured) as info:
        target("/items/1/")
    assert str(info.value) == NO_INCLUDE
    settings.SCRUDFUL = {
        "BASE_URL": BASE_URL,
        "HSR_URL": lambda view_name: f"https://schemas.example/{view_name}",
    }
    assert target("/items/1/") == "https://schemas.example/item-detail"


def scrudful_check_errors():
    """The ``scrudful.*`` messages of Django's URL system checks."""
    return [
        message
        for message in checks.run_checks(tags=[checks.Tags.urls])
        if message.id.startswith("scrudful.")
    ]


def test_system_check_passes_a_correct_urlconf(urls):
    assert scrudful_check_errors() == []


def test_system_check_reports_an_unnamed_route(settings):
    settings.ROOT_URLCONF = UnnamedURLconf
    [error] = scrudful_check_errors()
    assert isinstance(error, checks.Error)
    assert error.id == "scrudful.E001"
    assert "'v1/notes/<int:pk>'" in error.msg and "name=" in error.msg
    # With hsr_url, the link of an unnamed route needs no name of it.
    settings.SCRUDFUL = {
        "BASE_URL": BASE_URL,
        "HSR_URL": lambda view_name: f"https://schemas.example/{view_name}",
    }
    assert scrudful_check_errors() == []


def test_system_check_reports_a_name_clash(settings):
    settings.ROOT_URLCONF = ClashURLconf
    [error] = scrudful_check_errors()
    assert error.id == "scrudful.E002"
    assert "'clash'" in error.msg
    assert "'notes/<int:pk>'" in error.msg and "'stats'" in error.msg


def test_system_check_reports_a_missing_include(settings):
    settings.ROOT_URLCONF = NoIncludeURLconf
    [error] = scrudful_check_errors()
    assert error.id == "scrudful.E003"
    assert error.msg == NO_INCLUDE
    settings.SCRUDFUL = {
        "BASE_URL": BASE_URL,
        "HSR_URL": lambda view_name: f"https://schemas.example/{view_name}",
    }
    assert scrudful_check_errors() == []


def test_system_check_reports_a_route_off_without_hsr_url(settings):
    settings.ROOT_URLCONF = NoIncludeURLconf
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "HSR_ROUTE": False}
    [error] = scrudful_check_errors()
    assert error.id == "scrudful.E004"
    assert "hsr_url" in error.msg


def test_system_check_reports_an_invalid_setting(urls, settings):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "HSR_ROUTE": "yes"}
    [error] = scrudful_check_errors()
    assert error.id == "scrudful.E006"
    assert "HSR_ROUTE" in error.msg


def test_system_check_reports_an_hsr_that_cannot_be_built(urls, settings):
    # The views' relative links need a base URL.
    settings.SCRUDFUL = None
    errors = scrudful_check_errors()
    assert errors
    assert {error.id for error in errors} == {"scrudful.E005"}
    assert any("item-detail" in error.msg for error in errors)
    assert all("relative" in error.msg for error in errors)
    # Reported once: by the hsr_urls() pattern, not again by the registered
    # check.
    assert len({error.msg for error in errors}) == len(errors)


#: Runs the system checks in a fresh process, where nothing imported
#: scrudful.rest_framework before they start (as ``manage.py check``).
FRESH_CHECK = """
import sys, django
django.setup()
from django.conf import settings
from django.core import checks
settings.ROOT_URLCONF = "tests.drf_hsr_urls"
assert "scrudful.rest_framework" not in sys.modules
print(sorted({m.id for m in checks.run_checks() if m.id.startswith("scrudful.")}))
"""


def test_system_check_runs_in_a_fresh_process():
    """Django's URL check loads the URLconf, so the hsr_urls() pattern's check
    runs though this module wasn't imported when the checks started."""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.settings",  # SCRUDFUL = None
        "PYTHONPATH": os.pathsep.join([".", "examples/scrudful_rest_framework"]),
    }
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", FRESH_CHECK],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        cwd=pathlib.Path(__file__).resolve().parent.parent,
    )
    assert result.stdout.strip() == "['scrudful.E005']"


def test_hsr_target_is_the_route_resolved_against_the_base_url(urls):
    assert target("/items/1/") == "https://api.example/_hsr/item-detail"
    assert target("/notes/1") == "https://api.example/_hsr/notes"
    # Meta.base_url, and a scrudful_api_view's base_url=.
    assert target("/api/things/1/") == "https://things.example/_hsr/api:thing-detail"
    assert target("/report") == "https://reports.example/_hsr/report"
    # A DRF Request, as the views have.
    request = RequestFactory().get("/items/")
    request.resolver_match = resolve("/items/")
    assert _hsr_target(Request(request)) == "https://api.example/_hsr/item-list"


def test_hsr_target_without_a_base_url_is_the_requests(urls, settings):
    settings.SCRUDFUL = None
    settings.ALLOWED_HOSTS = ["testserver", "a.example"]
    assert target("/items/1/") == "http://testserver/_hsr/item-detail"
    assert (
        target("/items/1/", HTTP_HOST="a.example")
        == "http://a.example/_hsr/item-detail"
    )


def test_hsr_target_without_resolver_match_is_improperly_configured(urls):
    with pytest.raises(ImproperlyConfigured, match="resolver_match"):
        _hsr_target(RequestFactory().get("/items/1/"))


def test_hsr_url_setting_and_meta(urls, settings):
    settings.SCRUDFUL = {
        "BASE_URL": BASE_URL,
        "HSR_URL": lambda view_name: f"https://schemas.example/{view_name}",
    }
    assert target("/items/1/") == "https://schemas.example/item-detail"
    assert target("/api/things/1/") == "https://schemas.example/api:thing-detail"
    # Meta.hsr_url wins over the setting.
    assert (
        target("/elsewhere/1/") == "https://schemas.example/hsr/elsewhere-detail.json"
    )


def test_hsr_route_false_with_hsr_url(urls, settings, client):
    # Meta.hsr_route = False: out of hsr_urls(), linked to its Meta.hsr_url.
    assert client.get("/_hsr/elsewhere-detail").status_code == 404
    assert client.get("/_hsr/elsewhere-list").status_code == 404
    assert (
        target("/elsewhere/1/") == "https://schemas.example/hsr/elsewhere-detail.json"
    )
    # SCRUDFUL["HSR_ROUTE"] = False: needs an HSR_URL.
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "HSR_ROUTE": False}
    assert client.get("/_hsr/item-detail").status_code == 404
    with pytest.raises(ImproperlyConfigured, match="HSR_URL"):
        target("/items/1/")
    settings.SCRUDFUL = {
        "BASE_URL": BASE_URL,
        "HSR_ROUTE": False,
        "HSR_URL": lambda view_name: f"https://schemas.example/{view_name}",
    }
    assert client.get("/_hsr/item-detail").status_code == 404
    assert target("/items/1/") == "https://schemas.example/item-detail"
    # Meta.hsr_route = True overrides the setting.
    assert client.get("/_hsr/api:thing-detail").status_code == 200
    assert target("/api/things/1/") == "https://schemas.example/api:thing-detail"


def test_meta_hsr_route_false_without_hsr_url_is_improperly_configured(settings):
    @scrudful_viewset
    class Off(drf_hsr_urls.ItemViewSetBase):
        class Meta:
            hsr_route = False

    class OffURLconf:
        urlpatterns = [
            path("off/<str:pk>/", Off.as_view({"get": "retrieve"}), name="off"),
            path("", include(hsr_urls())),
        ]

    settings.ROOT_URLCONF = OffURLconf
    with pytest.raises(ImproperlyConfigured, match="hsr_route"):
        target("/off/1/")


@pytest.mark.parametrize(
    "key, value, match, route, link",
    [
        # Read by the route, the link, or both.
        ("HSR_ROUTE", "no", "HSR_ROUTE.*bool", True, True),
        ("HSR_URL", "https://x/", "HSR_URL.*callable", False, True),
        ("HSR_CACHE_CONTROL", 300, "HSR_CACHE_CONTROL.*str", True, False),
    ],
)
def test_hsr_settings_are_checked(
    urls, settings, client, key, value, match, route, link
):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, key: value}
    if route:
        with pytest.raises(ImproperlyConfigured, match=match):
            client.get("/_hsr/item-detail")
    if link:
        with pytest.raises(ImproperlyConfigured, match=match):
            target("/items/1/")


@pytest.mark.parametrize(
    "name, value, match",
    [
        ("hsr_route", "no", "hsr_route.*bool"),
        ("hsr_url", "https://x/", "hsr_url.*callable"),
        ("hsr_cache_control", 300, "hsr_cache_control.*str"),
    ],
)
def test_hsr_meta_options_are_checked(name, value, match):
    meta = type("Meta", (), {name: value})
    with pytest.raises(TypeError, match=match):
        scrudful_viewset(type("V", (drf_hsr_urls.ItemViewSetBase,), {"Meta": meta}))


class MovedURLconf:
    urlpatterns = [
        path("stats", drf_hsr_urls.stats, name="stats"),
        path("v2/schemas/stats", drf_hsr_urls.schema, name="stats-schema"),
        path("", include(hsr_urls())),
    ]


def test_a_root_urlconf_change_rebuilds_the_documents_and_the_scan(
    urls, settings, client
):
    assert schema_ref(client.get("/_hsr/stats")) == "https://api.example/schemas/stats"
    settings.ROOT_URLCONF = MovedURLconf
    # The same view function: its link callable reverses in the new URLconf.
    assert (
        schema_ref(client.get("/_hsr/stats")) == "https://api.example/v2/schemas/stats"
    )
    assert client.get("/_hsr/item-detail").status_code == 404


# --- http_schema_resource_view() ------------------------------------------------


def test_http_schema_resource_view_serves_a_view_mounted_elsewhere(urls, client):
    """Meta.hsr_route = False + Meta.hsr_url, the document mounted at that URL
    with http_schema_resource_view (``tests/drf_hsr_urls.py``)."""
    hsr_target = target("/elsewhere/1/")
    assert hsr_target == "https://schemas.example/hsr/elsewhere-detail.json"
    mounted = urlsplit(hsr_target).path
    response = client.get(mounted)
    assert get_problems(response) == []
    view_func = resolve("/elsewhere/1/").func
    assert view_func is drf_hsr_urls.elsewhere_detail
    assert json.loads(response.content) == http_schema_resource(view_func)
    assert response["ETag"] == hsr_etag(response.content)
    assert response["Link"] == hsr_response_links()
    assert response["Cache-Control"] == "public, max-age=300"
    # The route doesn't serve it.
    assert client.get("/_hsr/elsewhere-detail").status_code == 404
    not_modified = client.get(mounted, HTTP_IF_NONE_MATCH=f"W/{response['ETag']}")
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified["ETag"] == response["ETag"]
    assert not_modified["Cache-Control"] == "public, max-age=300"
    head = client.head(mounted)
    assert head.status_code == 200 and head.content == b""


def test_http_schema_resource_view_has_the_views_cache_control(urls, rf, settings):
    # Called directly, without URL resolution (stats's link callable reverses).
    settings.SCRUDFUL = {"BASE_URL": BASE_URL, "HSR_CACHE_CONTROL": "public"}
    thing = http_schema_resource_view(
        drf_hsr_urls.ThingViewSet.as_view({"get": "retrieve"})
    )
    assert thing(rf.get("/anywhere"))["Cache-Control"] == "public, max-age=3600"
    stats = http_schema_resource_view(drf_hsr_urls.stats)
    response = stats(rf.get("/anywhere"))
    assert get_problems(response) == []
    assert response["Cache-Control"] == "public"
    assert json.loads(response.content) == http_schema_resource(drf_hsr_urls.stats)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "options"])
def test_http_schema_resource_view_answers_other_methods_405(urls, method):
    # CSRF-exempt: an unsafe method gets the 405, not CSRF's 403.
    client = Client(enforce_csrf_checks=True)
    response = getattr(client, method)("/hsr/elsewhere-detail.json")
    assert response.status_code == 405
    assert response["Allow"] == "GET, HEAD"
    assert "Allow" in response["Access-Control-Expose-Headers"]


@pytest.mark.parametrize(
    "view_func",
    [
        drf_hsr_urls.PlainViewSet.as_view({"get": "retrieve"}),
        APIView.as_view(),
        drf_hsr_urls.schema,
        None,
    ],
)
def test_http_schema_resource_view_of_no_scrudful_view_is_a_type_error(view_func):
    with pytest.raises(TypeError, match="scrudful view"):
        http_schema_resource_view(view_func)


# --- OPTIONS: 204, Allow per requester, the HSR link -----------------------------


@pytest.fixture
def options_urls(settings, throttle_cache):
    """``tests/drf_hsr_urls.py``, without a base URL (links resolve against
    ``http://testserver``), its users, and its call log cleared."""
    settings.ROOT_URLCONF = "tests.drf_hsr_urls"
    settings.SCRUDFUL = None
    writers = Group.objects.create(name="writers")
    User.objects.create_user("reader")
    User.objects.create_user("writer").groups.add(writers)
    drf_hsr_urls.CALLS.clear()
    yield
    drf_hsr_urls.CALLS.clear()


@pytest.fixture
def api_client(options_urls):
    return APIClient()


def calls(kind):
    """The logged calls of ``kind`` (see ``drf_hsr_urls.CALLS``)."""
    return [call[1:] for call in drf_hsr_urls.CALLS if call[0] == kind]


def as_user(api_client, username):
    if username is not None:
        api_client.force_authenticate(User.objects.get(username=username))
    return api_client


PREFLIGHT = {
    "HTTP_ORIGIN": "https://app.example",
    "HTTP_ACCESS_CONTROL_REQUEST_METHOD": "PUT",
}


def test_options_204(api_client):
    r = api_client.options("/items/1/")
    assert options_204_problems(r.status_code, dict(r.items()), r.content) == []
    target = "http://testserver/_hsr/item-detail"
    assert hsr_link_problems(dict(r.items()), target) == []
    exposed = {name.strip() for name in r["Access-Control-Expose-Headers"].split(",")}
    assert {"Allow", "Link"} <= exposed


def test_options_204_links_against_the_base_url(api_client, settings):
    settings.SCRUDFUL = {"BASE_URL": BASE_URL}
    r = api_client.options("/x/perm-items/1/")
    assert options_204_problems(r.status_code, dict(r.items()), r.content) == []
    target = f"{BASE_URL}_hsr/perm-item-detail"
    assert hsr_link_problems(dict(r.items()), target) == []


@pytest.mark.parametrize(
    "user, allow",
    [
        (None, "GET, OPTIONS, HEAD"),
        ("reader", "GET, OPTIONS, HEAD"),
        ("writer", "GET, PUT, DELETE, OPTIONS, HEAD, PATCH"),
    ],
)
def test_allow_per_requester(api_client, user, allow):
    # /x/perm-items/1/: IsAuthenticatedOrReadOnly, writes for the writers
    # group, and object writes for the owner (writer).
    r = as_user(api_client, user).options("/x/perm-items/1/")
    assert r.status_code == 204
    assert r.headers["Allow"] == allow


@pytest.mark.parametrize(
    "user, allow",
    [
        (None, "GET, OPTIONS, HEAD"),
        ("reader", "GET, OPTIONS, HEAD"),
        ("writer", "GET, POST, OPTIONS, HEAD"),
    ],
)
def test_allow_of_a_collection_loads_nothing(api_client, user, allow):
    r = as_user(api_client, user).options("/x/perm-items/")
    assert r.status_code == 204
    assert r.headers["Allow"] == allow
    assert calls("load") == []
    assert calls("object") == []


def test_object_level_denial_excluded(api_client):
    # The writer may write, but object 2 is someone else's.
    r = as_user(api_client, "writer").options("/x/perm-items/2/")
    assert r.status_code == 204
    assert r.headers["Allow"] == "GET, OPTIONS, HEAD"


def test_options_itself_is_not_gated(api_client):
    """Authenticate, don't gate: neither check_permissions nor (in loading)
    check_object_permissions runs for OPTIONS; each method is checked once,
    HEAD by GET's check."""
    as_user(api_client, "writer").options("/x/perm-items/2/")
    assert calls("load") == [("2",)]
    methods = ["GET", "PUT", "PATCH", "DELETE"]
    assert [call[0] for call in calls("view")] == methods
    assert calls("object") == [(method, "2") for method in methods]


def test_per_method_checks_see_the_method_and_leak_nothing(api_client):
    r = as_user(api_client, "writer").options("/x/perm-items/1/")
    # The permission gets a request of the method, and the view has it as
    # its request and the method's action, as DRF dispatches that method.
    assert calls("view") == [
        ("GET", "GET", "retrieve"),
        ("PUT", "PUT", "update"),
        ("PATCH", "PATCH", "partial_update"),
        ("DELETE", "DELETE", "destroy"),
    ]
    # The real request, and the view, are as DRF left them for OPTIONS.
    request, view = r.renderer_context["request"], r.renderer_context["view"]
    assert request.method == "OPTIONS"
    assert request._request.method == "OPTIONS"
    assert view.request is request
    assert view.action == "metadata"
    assert request.parser_context["request"] is request
    assert request.user.username == "writer"


@pytest.mark.parametrize("pk", ["1", "99"])
def test_anonymous_on_locked_resource_gets_204_allow_options(api_client, pk):
    # IsAdminUser view. Refused every method, the requester can't tell an
    # existing object (1) from a missing one (99), as with a GET (R17): the
    # object is not loaded.
    r = api_client.options(f"/x/admin-items/{pk}/")
    assert r.status_code == 204 and r.headers["Allow"] == "OPTIONS"
    assert options_204_problems(r.status_code, dict(r.items()), r.content) == []
    target = "http://testserver/_hsr/admin-item-detail"
    assert hsr_link_problems(dict(r.items()), target) == []
    assert calls("load") == []


def test_options_is_no_existence_oracle(api_client):
    """R17: the same answer for an existing and a missing object when every
    method is refused, as a GET gives the same refusal for both."""
    answers = []
    for pk in ("1", "99"):
        cache.clear()  # OncePerDay
        options = api_client.options(f"/x/admin-items/{pk}/")
        cache.clear()
        get = api_client.get(f"/x/admin-items/{pk}/")
        answers.append((options.status_code, options["Allow"], get.status_code))
    assert answers[0] == answers[1] == (204, "OPTIONS", 401)
    assert calls("load") == []


@pytest.mark.parametrize("url", ["/x/admin-items/1/", "/x/perm-items/1/"])
def test_bad_credentials_are_answered_as_drf_does(api_client, url):
    bad = {"HTTP_AUTHORIZATION": "Basic Ym9ndXM6Ym9ndXM="}  # bogus:bogus
    r = api_client.options(url, **bad)
    # AuthenticationFailed: a 401 with BasicAuthentication first
    # (admin-items), a 403 with DRF's default SessionAuthentication first,
    # which has no WWW-Authenticate (perm-items), as for a GET.
    get = api_client.get(url, **bad)
    assert r.status_code == get.status_code
    assert r.json() == get.json() == {"detail": "Invalid username/password."}
    assert r.get("WWW-Authenticate") == get.get("WWW-Authenticate")
    if "admin" in url:
        assert r.status_code == 401
        assert r["WWW-Authenticate"].startswith("Basic")
    else:
        assert r.status_code == 403
    assert calls("load") == []


def test_options_is_throttled_once_as_drf_throttles_the_view(api_client):
    # CountingThrottle records each check; OncePerDay allows one a day.
    assert api_client.options("/x/admin-items/1/").status_code == 204
    assert calls("throttle") == [("OPTIONS",)]
    assert api_client.options("/x/admin-items/1/").status_code == 429


@pytest.mark.parametrize("user", [None, "reader", "writer"])
def test_missing_object_is_404_with_error_links(api_client, user):
    # perm-items is readable by anyone: a GET would be a 404 too.
    r = as_user(api_client, user).options("/x/perm-items/99/")
    assert r.status_code == 404
    headers = dict(r.items())
    assert link_problems("OPTIONS", 404, headers, True) == []
    assert "<https://api.example/schemas/error>" in r["Link"]
    target = "http://testserver/_hsr/perm-item-detail"
    assert hsr_link_problems(headers, target) == []
    # The view-level checks came first (one passed), then the load, and no
    # object check; Allow is DRF's (every served method).
    # (Anonymous: IsAuthenticatedOrReadOnly refuses the writes first.)
    assert calls("view")[0] == ("GET", "GET", "retrieve")
    kinds = [call[0] for call in drf_hsr_urls.CALLS]
    assert kinds.index("load") > max(i for i, k in enumerate(kinds) if k == "view")
    assert calls("load") == [("99",)]
    assert calls("object") == []
    assert r["Allow"] == "GET, PUT, PATCH, DELETE, HEAD, OPTIONS"
    assert as_user(api_client, user).get("/x/perm-items/99/").status_code == 404


@pytest.mark.parametrize("user", [None, "reader"])
def test_an_object_check_raising_http404_is_that_404(api_client, user):
    """R16: an object permission that hides the object (``Http404``, as
    DjangoObjectPermissions for an unreadable object) answers the OPTIONS
    with that 404, as a GET, not with a per-method denial."""
    r = as_user(api_client, user).options("/x/hidden-items/1/")
    assert r.status_code == 404
    headers = dict(r.items())
    assert link_problems("OPTIONS", 404, headers, True) == []
    assert "<https://api.example/schemas/error>" in r["Link"]
    target = "http://testserver/_hsr/hidden-item-detail"
    assert hsr_link_problems(headers, target) == []
    assert calls("object") == [("GET", "1")]
    assert as_user(api_client, user).get("/x/hidden-items/1/").status_code == 404
    # Its owner sees it.
    drf_hsr_urls.CALLS.clear()
    r = as_user(api_client, "writer").options("/x/hidden-items/1/")
    assert r.status_code == 204
    assert r["Allow"] == "GET, PUT, DELETE, OPTIONS, HEAD, PATCH"


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"HTTP_COOKIE": "sessionid=x"},
        {"HTTP_AUTHORIZATION": "Basic Ym9ndXM6Ym9ndXM="},
        {"HTTP_ACCEPT": "application/xml"},
    ],
)
def test_preflight_runs_nothing(api_client, extra):
    # Admins only, once a day: three preflights, before authentication,
    # negotiation, permissions, throttles and loading.
    for _ in range(3):
        r = api_client.options("/x/admin-items/1/", **PREFLIGHT, **extra)
        assert r.status_code == 204
        assert r.content == b""
        assert "Content-Type" not in r
        assert "Link" not in r
    assert drf_hsr_urls.CALLS == []
    # The day's one request is still there.
    assert api_client.options("/x/admin-items/1/").status_code == 204


def test_preflight_needs_both_headers(api_client):
    r = api_client.options("/x/admin-items/1/", HTTP_ORIGIN="https://app.example")
    assert r.status_code == 204 and r["Allow"] == "OPTIONS"
    assert hsr_link_problems(dict(r.items())) == []


def test_preflight_with_a_session_and_csrf_checks(options_urls):
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(User.objects.get(username="writer"))
    r = client.options("/x/perm-items/1/", **PREFLIGHT)
    assert r.status_code == 204 and drf_hsr_urls.CALLS == []


@pytest.mark.parametrize(
    "user, url, allow",
    [
        (None, "/x/hook-items/1/", "GET, OPTIONS, HEAD"),
        ("writer", "/x/hook-items/1/", "GET, PUT, OPTIONS, HEAD"),
        ("writer", "/x/hook-items/", "GET, OPTIONS, HEAD"),
    ],
)
def test_allowed_func_overrides(api_client, user, url, allow):
    # Every permission denies; allowed_func alone decides (PATCH raises
    # PermissionDenied: not allowed).
    r = as_user(api_client, user).options(url)
    assert r.status_code == 204
    assert r["Allow"] == allow
    assert calls("view") == []
    hooked = calls("hook")
    assert hooked
    # The hook gets the method, a request of it, and the object (None for
    # the collection); HEAD is GET's answer.
    assert all(method == request_method for method, request_method, _ in hooked)
    assert "HEAD" not in [method for method, _, _ in hooked]
    pk = "1" if url.endswith("1/") else None
    assert {obj for _, _, obj in hooked} == {pk}


def test_head_only_when_served(api_client):
    # scrudful_api_view(["GET"]) doesn't serve HEAD.
    assert api_client.options("/stats").headers["Allow"] == "GET, OPTIONS"
    assert api_client.head("/stats").status_code == 405


def test_405_keeps_drfs_allow(api_client):
    r = as_user(api_client, "writer").post("/x/perm-items/1/")
    assert r.status_code == 405
    assert r["Allow"] == "GET, PUT, PATCH, DELETE, HEAD, OPTIONS"


def test_options_without_the_hsr_urls_include_is_improperly_configured(settings):
    settings.ROOT_URLCONF = NoIncludeURLconf
    client = APIClient()
    with pytest.raises(ImproperlyConfigured) as info:
        client.options("/items/1/")
    assert str(info.value) == NO_INCLUDE
    settings.SCRUDFUL = {
        "BASE_URL": BASE_URL,
        "HSR_URL": lambda view_name: f"https://schemas.example/{view_name}",
    }
    r = client.options("/items/1/")
    target = "https://schemas.example/item-detail"
    assert hsr_link_problems(dict(r.items()), target) == []


def not_a_bool(view, request, method, obj):
    return "yes"


class Broken(BasePermission):
    def has_permission(self, request, view):
        if request.method == "PUT":
            raise ValueError("broken")
        return True


@scrudful_viewset
class NotABoolViewSet(drf_hsr_urls.ItemViewSetBase):
    class Meta:
        allowed_func = not_a_bool


@scrudful_viewset
class BrokenViewSet(drf_hsr_urls.ItemViewSetBase):
    permission_classes = [Broken]


class CheckURLconf:
    urlpatterns = [
        path(
            "not-a-bool/<str:pk>/",
            NotABoolViewSet.as_view({"get": "retrieve"}),
            name="not-a-bool",
        ),
        path(
            "broken/<str:pk>/",
            BrokenViewSet.as_view({"get": "retrieve", "put": "update"}),
            name="broken",
        ),
        path("", include(hsr_urls())),
    ]


def test_allowed_func_must_return_a_bool(settings, client):
    settings.ROOT_URLCONF = CheckURLconf
    with pytest.raises(TypeError, match="allowed_func.*bool.*str"):
        client.options("/not-a-bool/1/")


def test_another_exception_of_a_check_propagates(settings, client):
    settings.ROOT_URLCONF = CheckURLconf
    with pytest.raises(ValueError, match="broken"):
        client.options("/broken/1/")
