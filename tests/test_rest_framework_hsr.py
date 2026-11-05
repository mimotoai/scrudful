"""The HTTP Schema Resource of a DRF view, built without a request.

The module is its own URLconf (``pytestmark`` below).
"""

import json

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.urls import path
from rest_framework import viewsets
from rest_framework.authentication import BasicAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.views import APIView

from scrudful._hsr import HsrConfigError
from scrudful.rest_framework import (
    Entity,
    ScrudfulMetadata,
    ScrudfulViewFunc,
    http_schema_resource,
    scrudful,
    scrudful_api_view,
    scrudful_viewset,
)
from tests.semantic_http import validate_http_schema_resource

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
    path("items/<str:pk>/", ItemViewSet.as_view({"get": "retrieve", "put": "update"})),
    path("no-error/<str:pk>/", NoErrorViewSet.as_view({"get": "retrieve"})),
    path("locked/<str:pk>/", LockedViewSet.as_view({"get": "retrieve"})),
    path("a/<str:pk>/", HOST_A),
    path("b/<str:pk>/", HOST_B),
    path("csv/", CsvView.as_view()),
    path("csv-items/<str:pk>/", CsvViewSet.as_view({"get": "retrieve"})),
    path("stats/", stats),
    path("own-base/", own_base),
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


def test_hsr_is_identical_across_hosts_and_requesters(client, settings):
    """Relative links, two hosts and two requesters, each route building its
    own document on its first request: a request-based build would differ."""
    settings.ALLOWED_HOSTS = ["a.example", "b.example"]
    a = client.options("/a/1/", HTTP_HOST="a.example")
    other = APIClient()
    other.force_authenticate(User.objects.create(username="bob"))
    b = other.options("/b/1/", HTTP_HOST="b.example")
    assert b.wsgi_request.user.username == "bob"
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == http_schema_resource(HOST_A)
    assert http_schema_resource(HOST_B) == http_schema_resource(HOST_A)
    assert "https://api.example/schemas/item" in json.dumps(a.json())
    assert "a.example" not in json.dumps(a.json())


def test_determine_metadata_returns_the_resolved_views_document(client):
    response = client.options("/items/1/")
    assert response.status_code == 200
    func = response.wsgi_request.resolver_match.func
    assert response.json() == http_schema_resource(func)
    assert response.json()["options"] == OPTIONS_204


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
    assert client.get("/csv/")["Link"] == CSV_LINKS
    assert client.get("/csv-items/1/")["Link"] == CSV_LINKS
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


def test_a_subclass_without_removed_hooks_still_works():
    class Extended(ScrudfulMetadata):
        def determine_metadata(self, request, view):
            return {**super().determine_metadata(request, view), "x-extra": True}

    @scrudful_viewset
    class V(ItemViewSet):
        metadata_class = Extended

    func = V.as_view({"get": "retrieve"})
    response = func(APIRequestFactory().options("/"), pk="1")
    assert response.status_code == 200
    assert response.data["x-extra"] is True
    assert response.data["options"] == OPTIONS_204
