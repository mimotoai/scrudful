"""Mechanics of ``scrudful.rest_framework``, tested with small ad-hoc views.

The module is its own URLconf (``pytestmark`` below). The views keep their
state in ``store``, which is reset for every test.
"""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from django.http import HttpResponse
from django.test import override_settings
from django.urls import include, path
from rest_framework import views as drf_views
from rest_framework import serializers, viewsets
from rest_framework.authentication import BasicAuthentication
from rest_framework.decorators import (
    action,
    api_view,
    authentication_classes,
    permission_classes,
    renderer_classes,
    throttle_classes,
)
from rest_framework.metadata import SimpleMetadata
from rest_framework.exceptions import NotFound
from rest_framework.mixins import UpdateModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.routers import SimpleRouter
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.throttling import BaseThrottle

from scrudful._adapter import allow_header, describe_options, options_links
from scrudful.rest_framework import (
    Entity,
    ScrudfulMetadata,
    ScrudfulViewFunc,
    exception_handler,
    scrudful,
    scrudful_api_view,
    scrudful_viewset,
)
from tests.semantic_http import (
    link_problems,
    parse_links,
    validate_http_schema_resource,
)

# The example settings enable ATOMIC_REQUESTS, so every request uses the DB.
pytestmark = [pytest.mark.urls(__name__), pytest.mark.django_db]

MODIFIED = datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
MODIFIED_HTTP = "Tue, 02 Jan 2024 03:04:05 GMT"
SCHEMA = "http://example.com/schema.json"
CONTEXT = "http://example.com/context.jsonld"
SCHEMA_LINK = f'<{SCHEMA}>; rel="describedby"; type="application/schema+json"'
CONTEXT_LINK = (
    f'<{CONTEXT}>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
LIST_CONTEXT = "http://example.com/list-context.jsonld"
# APIClient builds absolute URIs against this base.
BASE = "http://testserver"
ERROR = Entity(
    schema_link_or_func="/schemas/error", context_link_or_func="/contexts/error"
)
ERROR_LINKS = (
    f'<{BASE}/schemas/error>; rel="describedby"; type="application/schema+json", '
    f'<{BASE}/contexts/error>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)


def content(schema, context=None, context_type="application/ld+json"):
    """The ``content`` of a JSON entity with these (absolute) links."""
    entry = {"schema": {"$ref": schema}}
    if context is not None:
        entry["x-rdf-context"] = {"href": context, "type": context_type}
    return {"application/json": entry}


THING_CONTENT = content(SCHEMA, CONTEXT, "application/x-context")


class Store:
    """One resource whose validators the callbacks read."""

    def reset(self):
        self.exists = True
        self.version = 1
        self.modified = MODIFIED
        self.etag_value = None
        self.calls = []

    def etag(self, view, request, pk):
        """Strict signature: an extra keyword argument raises TypeError."""
        self.calls.append(("etag", pk))
        if not self.exists:
            return None
        return self.etag_value if self.etag_value is not None else f"v{self.version}"

    def last_modified(self, view, request, pk):
        self.calls.append(("last_modified", pk))
        return self.modified if self.exists else None

    def update(self):
        self.version += 1
        self.modified = MODIFIED + timedelta(hours=1)


store = Store()
store.reset()


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield
    store.reset()


@pytest.fixture
def client():
    return APIClient()


def etag_func(view, request, pk):
    return store.etag(view, request, pk)


def last_modified_func(view, request, pk):
    return store.last_modified(view, request, pk)


def not_found():
    return Response({"detail": "Not found."}, status=404)


# --- Views --------------------------------------------------------------------


class ThingViewSetBase(viewsets.ViewSet):
    """Like ModelViewSet, without a model."""

    def list(self, request):
        return Response([{"id": 1}])

    def create(self, request):
        return Response({"id": 2}, status=201, headers={"Location": "/things/2/"})

    def retrieve(self, request, pk):
        """Return the thing."""
        if pk == "0":
            raise NotFound
        if not store.exists:
            return not_found()
        return Response({"id": pk, "version": store.version})

    def update(self, request, pk, partial=False):
        if not store.exists:
            return not_found()
        if request.data.get("invalid"):
            return Response({"name": ["invalid"]}, status=400)
        if request.data.get("raise"):
            raise serializers.ValidationError({"name": ["This field is required."]})
        store.update()
        return Response({"id": pk, "version": store.version, "partial": partial})

    def partial_update(self, request, *args, **kwargs):
        # Same as DRF's UpdateModelMixin.
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, pk):
        if not store.exists:
            return not_found()
        store.exists = False
        return Response(status=204)


@scrudful_viewset
class ThingViewSet(ThingViewSetBase):
    class Meta:
        etag_func = etag_func
        last_modified_func = last_modified_func
        schema_link_or_func = SCHEMA
        context_link_or_func = CONTEXT
        context_rel_or_func = "my-context-rel"
        context_type_or_func = "application/x-context"
        list_schema_link_or_func = "http://example.com/list-schema.json"
        list_context_link_or_func = LIST_CONTEXT


@scrudful_viewset
class ETagOnlyViewSet(ThingViewSetBase):
    class Meta:
        etag_func = etag_func


@scrudful_viewset
class LastModifiedOnlyViewSet(ThingViewSetBase):
    class Meta:
        last_modified_func = last_modified_func


@scrudful_viewset
class LinksOnlyViewSet(ThingViewSetBase):
    class Meta:
        schema_link_or_func = SCHEMA


@scrudful_viewset
class ChildViewSet(ThingViewSet):
    """A re-decorated subclass with its own Meta."""

    class Meta:
        etag_func = staticmethod(lambda view, request, pk: f"child-{store.version}")


@scrudful_viewset
class InheritingChildViewSet(ThingViewSet):
    """A re-decorated subclass that inherits its parent's Meta."""


def explicit_etag(view, request, pk):
    return "explicit"


@scrudful_viewset
class ExplicitViewSet(ThingViewSetBase):
    class Meta:
        etag_func = etag_func

    @scrudful(etag_func=explicit_etag)
    def retrieve(self, request, pk):
        return Response({"id": pk})


def other_etag(view, request, pk):
    return f"other{store.version}"


class StockPartialUpdate(ThingViewSetBase):
    """PATCH is DRF's stock partial_update, which delegates to update."""

    partial_update = UpdateModelMixin.partial_update


@scrudful_viewset
class StockPatchViewSet(StockPartialUpdate):
    class Meta:
        etag_func = etag_func


@scrudful_viewset
class StockExplicitUpdateViewSet(StockPartialUpdate):
    class Meta:
        etag_func = etag_func

    @scrudful(etag_func=other_etag)
    def update(self, request, pk, partial=False):
        return super().update(request, pk, partial=partial)


@scrudful_viewset
class SubclassedStockViewSet(StockPatchViewSet):
    """Decorates a decorated viewset again, now with an explicit update."""

    @scrudful(etag_func=other_etag)
    def update(self, request, pk, partial=False):
        return super(StockPatchViewSet, self).update(request, pk, partial=partial)


@scrudful_viewset
class UserPartialUpdateViewSet(ThingViewSetBase):
    """A user-defined partial_update keeps the Meta configuration."""

    class Meta:
        etag_func = etag_func

    @scrudful(etag_func=other_etag)
    def update(self, request, pk, partial=False):
        return super().update(request, pk, partial=partial)


@scrudful_viewset
class ActionViewSet(ThingViewSetBase):
    class Meta:
        etag_func = etag_func

    @action(detail=True, methods=["get"])
    @scrudful(
        etag_func=etag_func, schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT
    )
    def below(self, request, pk):
        """Action decorated below @action."""
        return Response({"action": "below"})

    @below.mapping.put
    @scrudful(etag_func=etag_func)
    def below_put(self, request, pk):
        store.update()
        return Response({"action": "below_put"})

    @scrudful(
        etag_func=etag_func, schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT
    )
    @action(detail=True, methods=["get"])
    def above(self, request, pk):
        """Action decorated above @action."""
        return Response({"action": "above"})

    @action(detail=True, methods=["get"])
    def plain(self, request, pk):
        return Response({"action": "plain"})


@scrudful_viewset
class ErrorViewSet(ThingViewSetBase):
    """Relative links, and an error entity for scrudful's 400, 412 and 428."""

    class Meta:
        etag_func = etag_func
        last_modified_func = last_modified_func
        schema_link_or_func = "/schemas/thing"
        context_link_or_func = "/contexts/thing"
        error = ERROR
        list_etag_func = staticmethod(lambda view, request: f"n{store.version}")
        list_schema_link_or_func = "/schemas/things"
        list_context_link_or_func = "/contexts/things"
        list_error = Entity(
            schema_link_or_func=lambda view, request: "/schemas/problem",
            context_link_or_func=lambda view, request: "/contexts/problem",
            media_type="application/problem+json",
        )


IF_MATCH = {"If-Match": '"v1"'}
PROBLEM = Entity(
    "/schemas/problem",
    context_link_or_func="/contexts/problem",
    media_type="application/problem+json",
)
PROBLEM_LINKS = (
    f'<{BASE}/schemas/problem>; rel="describedby"; type="application/schema+json", '
    f'<{BASE}/contexts/problem>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)


@scrudful_viewset
class ErrorOnlyViewSet(ThingViewSetBase):
    """An error entity and no validators."""

    class Meta:
        error = ERROR


@scrudful_viewset
class ProblemViewSet(ThingViewSetBase):
    """Errors described as problem details."""

    class Meta:
        etag_func = etag_func
        schema_link_or_func = "/schemas/thing"
        context_link_or_func = "/contexts/thing"
        error = PROBLEM


@scrudful_viewset
class MixedActionViewSet(viewsets.ViewSet):
    """An action route whose PUT is not scrudful."""

    @action(detail=True, methods=["get"])
    @scrudful(
        etag_func=etag_func, schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT
    )
    def mixed(self, request, pk):
        return Response({"action": "mixed"})

    @mixed.mapping.put
    def mixed_put(self, request, pk):
        return Response({"action": "mixed_put"})


class OwnDescribedByViewSet(viewsets.ViewSet):
    """A view that sets its own describedby link."""

    @scrudful(
        schema_link_or_func="/schemas/thing", context_link_or_func="/contexts/thing"
    )
    def retrieve(self, request, pk):
        return Response({}, headers={"Link": '<https://o/s>; rel="describedby"'})


class HeaderViewSet(viewsets.ViewSet):
    """Handlers that set the headers scrudful also sets."""

    @scrudful(etag_func=etag_func, schema_link_or_func=SCHEMA)
    def retrieve(self, request, pk):
        return Response(
            {},
            headers={
                "ETag": '"handler"',
                "Link": '<http://example.com/other>; rel="other"',
                "Access-Control-Expose-Headers": "X-Custom, link",
            },
        )


@scrudful_viewset
class OwnOptionsViewSet(ThingViewSetBase):
    class Meta:
        etag_func = etag_func

    def options(self, request, *args, **kwargs):
        return Response({"own": True})


class CustomMetadata(SimpleMetadata):
    """A project's own metadata class."""


@scrudful_viewset
class OwnMetadataViewSet(ThingViewSetBase):
    metadata_class = CustomMetadata

    class Meta:
        etag_func = etag_func


class PlainMetadataViewSet(ThingViewSetBase):
    """Not scrudful, but described by ScrudfulMetadata."""

    metadata_class = ScrudfulMetadata


def nested_etag(view, request, parent_pk):
    store.calls.append(("nested_etag", parent_pk))
    return f"list-{parent_pk}"


@scrudful_viewset
class NestedViewSet(viewsets.ViewSet):
    class Meta:
        schema_link_or_func = staticmethod(
            lambda view, request, parent_pk, **kwargs: f"{SCHEMA}?parent={parent_pk}"
        )
        context_link_or_func = staticmethod(
            lambda view, request, parent_pk, **kwargs: f"{CONTEXT}?parent={parent_pk}"
        )
        list_etag_func = nested_etag
        list_schema_link_or_func = staticmethod(
            lambda view, request, parent_pk: f"{SCHEMA}?list={parent_pk}"
        )
        list_context_link_or_func = staticmethod(
            lambda view, request, parent_pk: f"{CONTEXT}?list={parent_pk}"
        )

    def list(self, request, parent_pk):
        return Response([])

    def create(self, request, parent_pk):
        return Response({}, status=201)


def injected_link(view, request, pk):
    return f"http://example.com/schema?v={request.query_params.get('v', '')}"


@scrudful_viewset
class InjectionViewSet(ThingViewSetBase):
    class Meta:
        schema_link_or_func = injected_link


ETAG_VALUES = {
    "weak": 'W/"abc"',
    "quoted": '"abc"',
    "int": 123,
    "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "invalid": 'a"b',
}


@scrudful_viewset
class ETagFormatViewSet(ThingViewSetBase):
    class Meta:
        etag_func = staticmethod(lambda view, request, pk: ETAG_VALUES[pk])


LAST_MODIFIED_VALUES = {
    "aware-ny": datetime(2024, 1, 1, 12, 0, 0, tzinfo=ZoneInfo("America/New_York")),
    "naive": datetime(2024, 1, 1, 12, 0, 0, 999999),
    "epoch": 1704110400.9,
}


@scrudful_viewset
class LastModifiedFormatViewSet(ThingViewSetBase):
    class Meta:
        last_modified_func = staticmethod(
            lambda view, request, pk: LAST_MODIFIED_VALUES[pk]
        )


class PostViewSet(viewsets.ViewSet):
    metadata_class = ScrudfulMetadata

    @scrudful(etag_func=lambda view, request: f"v{store.version}")
    def create(self, request):
        return Response({}, status=201)


@scrudful_api_view(
    ["GET", "PUT", "DELETE"],
    etag_func=etag_func,
    schema_link_or_func=lambda view, request, pk: f"{SCHEMA}?pk={pk}",
    context_link_or_func=CONTEXT,
)
def api_thing(request, pk):
    """The api_thing docstring."""
    if request.method == "PUT":
        store.update()
    return Response({"id": pk, "version": store.version})


@scrudful_api_view()
def api_default(request):
    return Response({"default": True})


class Deny(BaseThrottle):
    def allow_request(self, request, view):
        return False


@scrudful_api_view()
@throttle_classes([Deny])
def api_throttled(request):
    return Response({})


@scrudful_api_view()
@authentication_classes([BasicAuthentication])
@permission_classes([IsAuthenticated])
def api_authenticated(request):
    return Response({})


@scrudful_api_view()
@renderer_classes([JSONRenderer])
def api_json_only(request):
    return Response({})


def own_metadata(request):
    return Response({})


own_metadata.metadata_class = CustomMetadata
api_own_metadata = scrudful_api_view(["GET"], etag_func=lambda view, request: "x")(
    own_metadata
)


@scrudful_api_view(
    ["GET", "PUT"],
    etag_func=etag_func,
    error=Entity(
        schema_link_or_func=lambda view, request, pk: f"/schemas/error?pk={pk}",
        context_link_or_func="/contexts/error",
    ),
)
def api_error(request, pk):
    return Response({"id": pk})


@scrudful(
    etag_func=lambda request: f"v{store.version}",
    error=Entity(
        lambda request: "/schemas/error", media_type="application/problem+json"
    ),
)
def django_error_view(request):
    return HttpResponse("plain django")


@api_view(["GET", "PUT"])
@scrudful(etag_func=lambda request, pk: f"v{store.version}", schema_link_or_func=SCHEMA)
def function_view(request, pk):
    if request.method == "PUT":
        store.update()
    return Response({"id": pk})


@scrudful(etag_func=lambda request: f"v{store.version}", schema_link_or_func=SCHEMA)
def django_view(request):
    return HttpResponse("plain django")


router = SimpleRouter()
for prefix, viewset in [
    ("things", ThingViewSet),
    ("etag-only", ETagOnlyViewSet),
    ("lm-only", LastModifiedOnlyViewSet),
    ("links-only", LinksOnlyViewSet),
    ("child", ChildViewSet),
    ("inheriting-child", InheritingChildViewSet),
    ("explicit", ExplicitViewSet),
    ("stock-patch", StockPatchViewSet),
    ("stock-explicit", StockExplicitUpdateViewSet),
    ("stock-subclass", SubclassedStockViewSet),
    ("user-partial", UserPartialUpdateViewSet),
    ("actions", ActionViewSet),
    ("headers", HeaderViewSet),
    ("own-options", OwnOptionsViewSet),
    ("own-metadata", OwnMetadataViewSet),
    ("plain-metadata", PlainMetadataViewSet),
    ("injection", InjectionViewSet),
    ("etag-format", ETagFormatViewSet),
    ("lm-format", LastModifiedFormatViewSet),
    ("post", PostViewSet),
    ("errors", ErrorViewSet),
    ("problems", ProblemViewSet),
    ("error-only", ErrorOnlyViewSet),
    ("mixed-actions", MixedActionViewSet),
    ("own-describedby", OwnDescribedByViewSet),
]:
    router.register(prefix, viewset, basename=prefix)

urlpatterns = [
    path("", include(router.urls)),
    path(
        "parents/<int:parent_pk>/children/",
        NestedViewSet.as_view({"get": "list", "post": "create"}),
    ),
    path("api/<int:pk>/", api_thing),
    path("api-default/", api_default),
    path("api-throttled/", api_throttled),
    path("api-authenticated/", api_authenticated),
    path("api-json-only/", api_json_only),
    path("api-own-metadata/", api_own_metadata),
    path("function/<int:pk>/", function_view),
    path("django/", django_view),
    path("api-error/<int:pk>/", api_error),
    path("django-error/", django_error_view),
]


# --- Conditional GET ----------------------------------------------------------


def test_get_sends_validators_and_links(client):
    response = client.get("/things/1/")
    assert response.status_code == 200
    assert response["ETag"] == '"v1"'
    assert response["Last-Modified"] == MODIFIED_HTTP
    assert response["Access-Control-Expose-Headers"] == "ETag, Link"


def test_context_rel_and_type_are_honoured(client):
    """D-I1: the context link used the schema rel/type."""
    response = client.get("/things/1/")
    assert response["Link"] == (
        f'{SCHEMA_LINK}, <{CONTEXT}>; rel="my-context-rel"; '
        'type="application/x-context"'
    )


def test_304_carries_validators_and_no_body(client):
    response = client.get("/things/1/", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.content == b""
    assert response["ETag"] == '"v1"'
    assert response["Last-Modified"] == MODIFIED_HTTP
    assert "Content-Type" not in response
    assert "Link" not in response


def test_echoed_last_modified_gives_304(client):
    response = client.get("/lm-only/1/", headers={"If-Modified-Since": MODIFIED_HTTP})
    assert response.status_code == 304
    assert response["Last-Modified"] == MODIFIED_HTTP
    assert "ETag" not in response


def test_missing_resource_is_left_to_the_view(client):
    store.exists = False
    for headers in ({}, {"If-None-Match": "*"}, {"If-Match": '"v1"'}):
        response = client.get("/things/1/", headers=headers)
        assert response.status_code == 404
        assert "ETag" not in response
        # The Link header is added to every response of the view.
        assert response["Link"].startswith(SCHEMA_LINK)
    assert client.delete("/things/1/", headers={"If-Match": '"v1"'}).status_code == 404
    assert client.patch("/things/1/").status_code == 404
    assert client.put("/things/1/").status_code == 404
    assert client.put("/things/1/", headers={"If-Match": '"v1"'}).status_code == 412


# --- Conditional writes -------------------------------------------------------


def test_empty_if_match_is_absent(client):
    assert client.get("/things/1/", headers={"If-Match": ""}).status_code == 200
    assert client.put("/things/1/", headers={"If-Match": ""}).status_code == 428


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_unconditional_write_gives_428(client, method):
    response = getattr(client, method)("/things/1/")
    assert response.status_code == 428
    assert response.json() == {
        "detail": "This request must be conditional.",
        "accepted-precondition-headers": ["If-Match", "If-Unmodified-Since"],
    }
    assert store.version == 1


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_if_none_match_star_on_an_existing_resource_gives_412(client, method):
    """Create-only (RFC 9110 §13.1.2): conditional, so 412 rather than 428."""
    response = getattr(client, method)("/things/1/", headers={"If-None-Match": "*"})
    assert response.status_code == 412
    assert response.json() == {"detail": "Precondition failed."}
    assert store.version == 1


@pytest.mark.parametrize(
    "prefix, accepted",
    [("etag-only", ["If-Match"]), ("lm-only", ["If-Unmodified-Since"])],
)
def test_428_lists_the_configured_validators(client, prefix, accepted):
    response = client.put(f"/{prefix}/1/")
    assert response.status_code == 428
    assert response.json()["accepted-precondition-headers"] == accepted


def test_last_modified_only_write_with_if_unmodified_since(client):
    """D-C1: If-Unmodified-Since compared a string with an int (500)."""
    response = client.put("/lm-only/1/", headers={"If-Unmodified-Since": MODIFIED_HTTP})
    assert response.status_code == 200
    response = client.delete(
        "/lm-only/1/", headers={"If-Unmodified-Since": "Tue, 02 Jan 2024 03:04:04 GMT"}
    )
    assert response.status_code == 412


def test_if_match_alone_is_enough(client):
    """D-I6: both If-Match and If-Unmodified-Since were demanded."""
    response = client.put("/things/1/", headers={"If-Match": '"v1"'})
    assert response.status_code == 200


def test_patch_requires_a_precondition_and_strips_partial(client):
    """D-C2: PATCH crashed (partial kwarg) or skipped the If-Match check."""
    assert client.patch("/things/1/").status_code == 428
    assert client.patch("/things/1/", headers={"If-Match": '"v0"'}).status_code == 412
    store.calls.clear()
    response = client.patch("/things/1/", headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response.json()["partial"] is True
    assert response["ETag"] == '"v2"'
    # partial_update -> update is handled once: validators before and after.
    assert store.calls == [
        ("etag", "1"),
        ("last_modified", "1"),
        ("etag", "1"),
        ("last_modified", "1"),
    ]


def test_write_response_has_fresh_validators(client):
    """D-C3: the write response carried the pre-modification validators."""
    response = client.put("/things/1/", headers={"If-Match": '"v1"'})
    assert response["ETag"] == '"v2"'
    assert response["Last-Modified"] == "Tue, 02 Jan 2024 04:04:05 GMT"
    again = client.put("/things/1/", headers={"If-Match": response["ETag"]})
    assert again.status_code == 200


def test_failed_write_gets_no_validators(client):
    response = client.put("/things/1/", {"invalid": True}, headers={"If-Match": '"v1"'})
    assert response.status_code == 400
    assert "ETag" not in response
    assert "Last-Modified" not in response
    assert response["Link"].startswith(SCHEMA_LINK)


def test_delete_response_has_no_validators(client):
    response = client.delete("/things/1/", headers={"If-Match": '"v1"'})
    assert response.status_code == 204
    assert "ETag" not in response
    assert "Last-Modified" not in response


def test_views_without_validators_ignore_preconditions(client):
    for method in ("get", "put", "patch", "delete"):
        store.reset()
        response = getattr(client, method)(
            "/links-only/1/", headers={"If-Match": '"nope"'}
        )
        assert response.status_code in (200, 204)
        assert response["Link"] == SCHEMA_LINK
        assert "ETag" not in response


def test_handler_headers_are_kept_and_merged(client):
    response = client.get("/headers/1/")
    assert response["ETag"] == '"handler"'
    assert response["Link"] == f'<http://example.com/other>; rel="other", {SCHEMA_LINK}'
    assert response["Access-Control-Expose-Headers"] == "X-Custom, link, ETag"


def test_create_gets_links_but_no_validators(client):
    response = client.post("/things/", headers={"If-Match": '"stale"'})
    assert response.status_code == 201
    assert "ETag" not in response
    assert response["Link"] == f"{SCHEMA_LINK}, " + (
        f'<{CONTEXT}>; rel="my-context-rel"; type="application/x-context"'
    )
    assert response["Access-Control-Expose-Headers"] == "Link, Location"
    assert store.calls == []


def test_explicit_post_validators_are_evaluated(client):
    assert client.post("/post/", headers={"If-Match": '"v0"'}).status_code == 412
    response = client.post("/post/", headers={"If-Match": '"v1"'})
    assert response.status_code == 201
    assert "ETag" not in response
    body = client.options("/post/").json()
    validate_http_schema_resource(body)
    post = body["post"]
    assert set(post["responses"]) == {"201", "400", "412"}
    assert "parameters" not in post


def test_list_uses_the_list_configuration(client):
    response = client.get("/things/")
    assert response["Link"] == (
        '<http://example.com/list-schema.json>; rel="describedby"; '
        f'type="application/schema+json", <{LIST_CONTEXT}>; '
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )
    assert "ETag" not in response
    assert store.calls == []


# --- Entity-tag and date inputs -----------------------------------------------


@pytest.mark.parametrize(
    "key, expected",
    [
        ("weak", 'W/"abc"'),
        ("quoted", '"abc"'),
        ("int", '"123"'),
        ("uuid", '"12345678-1234-5678-1234-567812345678"'),
    ],
)
def test_etag_values(client, key, expected):
    """D-I4: valid tags were mangled and non-strings crashed."""
    response = client.get(f"/etag-format/{key}/")
    assert response["ETag"] == expected
    revalidated = client.get(
        f"/etag-format/{key}/", headers={"If-None-Match": expected}
    )
    assert revalidated.status_code == 304


def test_weak_etag_fails_if_match(client):
    response = client.put("/etag-format/weak/", headers={"If-Match": 'W/"abc"'})
    assert response.status_code == 412


def test_invalid_etag_raises(client):
    with pytest.raises(ValueError):
        client.get("/etag-format/invalid/")


@pytest.mark.parametrize(
    "key, expected",
    [
        ("aware-ny", "Mon, 01 Jan 2024 17:00:00 GMT"),
        ("naive", "Mon, 01 Jan 2024 12:00:00 GMT"),
        ("epoch", "Mon, 01 Jan 2024 12:00:00 GMT"),
    ],
)
def test_last_modified_values(client, key, expected):
    """D-I13: an aware non-UTC datetime was relabelled as UTC."""
    response = client.get(f"/lm-format/{key}/")
    assert response["Last-Modified"] == expected


@override_settings(TIME_ZONE="America/New_York")
def test_naive_last_modified_is_in_the_default_time_zone(client):
    response = client.get("/lm-format/naive/")
    assert response["Last-Modified"] == "Mon, 01 Jan 2024 17:00:00 GMT"


# --- Link header escaping -----------------------------------------------------


def test_link_url_cannot_inject_links(client):
    """D-I10: a URL built from request data could add links."""
    response = client.get(
        "/injection/1/", {"v": 'a>; rel="next", <https://evil.example'}
    )
    assert response["Link"] == (
        "<http://example.com/schema?v=a%3E;%20rel=%22next%22,%20"
        '%3Chttps://evil.example>; rel="describedby"; type="application/schema+json"'
    )


def test_link_url_is_percent_encoded(client):
    response = client.get("/injection/1/", {"v": "été"})
    assert response["Link"].startswith("<http://example.com/schema?v=%C3%A9t%C3%A9>")


# --- Error links and absolute links ------------------------------------------


def test_error_links_on_412_428_400(client):
    responses = {
        412: client.put("/errors/1/", headers={"If-Match": '"v0"'}),
        428: client.put("/errors/1/"),
        400: client.put("/errors/1/", headers={"If-Match": "v1"}),
    }
    for status, response in responses.items():
        assert response.status_code == status
        assert response["Link"] == ERROR_LINKS
        assert response["Access-Control-Expose-Headers"] == "Link"
    get_412 = client.get("/errors/1/", headers={"If-Match": '"v0"'})
    assert get_412.status_code == 412
    assert get_412["Link"] == ERROR_LINKS
    not_modified = client.get("/errors/1/", headers={"If-None-Match": '"v1"'})
    assert not_modified.status_code == 304
    assert "Link" not in not_modified
    assert "Content-Type" not in not_modified
    assert store.version == 1


def test_error_bodies_are_served_as_the_error_media_type(client):
    hsr = client.options("/problems/1/").json()
    validate_http_schema_resource(hsr)
    write = dict(data={"name": "x"}, format="json")
    for accept in ("application/json", "text/html"):
        headers = {"Accept": accept}
        early = [
            ("GET", client.get("/problems/1/", headers={**headers, "If-Match": '"x"'})),
            ("PUT", client.put("/problems/1/", **write, headers=headers)),
            (
                "PUT",
                client.put(
                    "/problems/1/", **write, headers={**headers, "If-Match": "x"}
                ),
            ),
        ]
        assert [response.status_code for _, response in early] == [412, 428, 400]
        for method, response in early:
            assert response["Content-Type"] == "application/problem+json"
            assert response["Link"] == PROBLEM_LINKS
            assert "detail" in response.json()
            problems = link_problems(
                method, response.status_code, dict(response.items()), True, hsr
            )
            assert problems == []
    # application/json errors bypass the browsable API too.
    response = client.put("/errors/1/", headers={"Accept": "text/html"})
    assert response.status_code == 428
    assert response["Content-Type"] == "application/json"
    # Without an error entity, DRF's negotiation is unchanged.
    response = client.put("/things/1/", headers={"Accept": "text/html"})
    assert response.status_code == 428
    assert response["Content-Type"].startswith("text/html")


@pytest.mark.parametrize("media_type", ["text/turtle", "text/csv", "application/xml"])
def test_error_media_type_must_be_json(media_type):
    error = Entity("/schemas/error", media_type=media_type)
    with pytest.raises(ValueError, match="scrudful's JSON error bodies"):
        scrudful(error=error)
    with pytest.raises(ValueError, match=media_type):
        scrudful_api_view(["GET"], error=error)
    with pytest.raises(ValueError, match=media_type):
        ScrudfulViewFunc(lambda request: None, error=error)
    with pytest.raises(ValueError, match=media_type):

        @scrudful_viewset
        class Invalid(ThingViewSetBase):
            class Meta:
                list_error = error


def test_error_links_of_the_list_and_with_callables(client):
    response = client.get("/errors/", headers={"If-None-Match": "n1"})
    assert response.status_code == 400
    assert response["Content-Type"] == "application/problem+json"
    assert response["Link"] == (
        f'<{BASE}/schemas/problem>; rel="describedby"; '
        'type="application/schema+json", '
        f'<{BASE}/contexts/problem>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"'
    )
    response = client.put("/api-error/7/", headers={"If-Match": '"v0"'})
    assert response.status_code == 412
    assert response["Link"] == (
        f'<{BASE}/schemas/error?pk=7>; rel="describedby"; '
        'type="application/schema+json", '
        f'<{BASE}/contexts/error>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"'
    )
    # Without an error entity, scrudful's own errors carry no Link.
    assert "Link" not in client.put("/things/1/")


def test_error_links_on_a_plain_django_view(client):
    response = client.get("/django-error/", headers={"If-Match": '"v0"'})
    assert response.status_code == 412
    assert response["Content-Type"] == "application/problem+json"
    assert response.json() == {"detail": "Precondition failed."}
    assert response["Link"] == (
        f'<{BASE}/schemas/error>; rel="describedby"; type="application/schema+json"'
    )
    assert response["Access-Control-Expose-Headers"] == "Link"
    response = client.get("/django-error/", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert "Link" not in response


def test_error_must_be_an_entity():
    with pytest.raises(TypeError, match="error"):
        scrudful(error="/schemas/error")
    with pytest.raises(TypeError, match="error"):
        ScrudfulViewFunc(lambda request: None, error={"schema": "/s"})


def test_links_are_absolute(client):
    response = client.get("/errors/1/")
    assert response["Link"] == (
        f'<{BASE}/schemas/thing>; rel="describedby"; type="application/schema+json", '
        f'<{BASE}/contexts/thing>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"'
    )


def test_view_link_not_duplicated(client):
    response = client.get("/own-describedby/1/")
    links = parse_links(response["Link"])
    described = [target for target, params in links if params["rel"] == "describedby"]
    assert described == ["https://o/s"]
    # The context link is still added.
    assert [target for target, _ in links] == [
        "https://o/s",
        f"{BASE}/contexts/thing",
    ]


# --- Semantic HTTP 0.1 OPTIONS ------------------------------------------------


def test_options_json_for_browsable_accept(client):
    """Review Focus 4: the browsable API does not take over OPTIONS."""
    for path_ in ("/things/1/", "/errors/", "/api-error/7/"):
        response = client.options(path_, headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert response["Content-Type"] == "application/json"
        assert response["Link"] == options_links()
        body = response.json()
        validate_http_schema_resource(body)
        assert response["Allow"] == allow_header(body)
        assert response["Access-Control-Expose-Headers"] == "Link"


def test_options_headers_and_method_order(client):
    response = client.options("/things/1/")
    body = response.json()
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "delete", "options", "head", "patch"]
    assert response["Allow"] == "GET, PUT, DELETE, OPTIONS, HEAD, PATCH"
    assert response["Link"] == options_links()
    assert body["options"] == describe_options()
    assert body["head"] == {
        "responses": {
            key: value
            for key, value in body["get"]["responses"].items()
            if key != "200"
        }
        | {"200": {"description": "OK"}}
    }
    assert link_problems("OPTIONS", 200, dict(response.items()), True, body) == []


def test_options_write_response_content(client):
    detail = client.options("/things/1/").json()
    validate_http_schema_resource(detail)
    for method in ("put", "patch"):
        assert detail[method]["requestBody"]["content"] == THING_CONTENT
        assert detail[method]["responses"]["200"]["content"] == THING_CONTENT
    assert "content" not in detail["delete"]["responses"]["204"]
    listing = client.options("/things/").json()
    validate_http_schema_resource(listing)
    assert listing["post"]["responses"]["201"]["content"] == THING_CONTENT


def test_options_error_content(client):
    error = content(f"{BASE}/schemas/error", f"{BASE}/contexts/error")
    detail = client.options("/errors/1/").json()
    validate_http_schema_resource(detail)
    for status in ("400", "412"):
        assert detail["get"]["responses"][status]["content"] == error
    assert "content" not in detail["get"]["responses"]["304"]
    for method in ("put", "patch", "delete"):
        for status in ("400", "412", "428"):
            assert detail[method]["responses"][status]["content"] == error
    for response in detail["head"]["responses"].values():
        assert "content" not in response
    listing = client.options("/errors/").json()
    validate_http_schema_resource(listing)
    assert listing["get"]["responses"]["400"]["content"] == {
        "application/problem+json": content(
            f"{BASE}/schemas/problem", f"{BASE}/contexts/problem"
        )["application/json"]
    }
    # The create is not conditional: no early responses to describe, but the
    # serializer's 400 carries the error entity's Links.
    assert set(listing["post"]["responses"]) == {"201", "400"}
    assert listing["post"]["responses"]["400"] == {
        "description": "Bad Request",
        "content": {
            "application/json": content(
                f"{BASE}/schemas/error", f"{BASE}/contexts/error"
            )["application/json"]
        },
    }


def test_options_action_without_links(client):
    """Review Focus 5: a route mixing scrudful and plain methods."""
    response = client.options("/mixed-actions/1/mixed/")
    body = response.json()
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "options", "head"]
    assert response["Allow"] == allow_header(body) == "GET, PUT, OPTIONS, HEAD"
    assert body["get"]["responses"]["200"]["content"] == content(SCHEMA, CONTEXT)
    assert body["put"] == {"responses": {"200": {"description": "OK"}}}
    plain = client.options("/actions/1/plain/")
    validate_http_schema_resource(plain.json())
    assert plain.json() == {
        "get": {"responses": {"200": {"description": "OK"}}},
        "options": describe_options(),
        "head": {"responses": {"200": {"description": "OK"}}},
    }
    assert plain["Allow"] == "GET, OPTIONS, HEAD"


def test_agreement_drf_item(client):
    options = client.options("/errors/1/")
    hsr = options.json()
    listing = client.options("/errors/").json()
    validate_http_schema_resource(hsr)
    validate_http_schema_resource(listing)
    write = dict(data={"name": "x"}, format="json")
    checks = [
        ("OPTIONS", options, hsr),
        ("GET", client.get("/errors/1/"), hsr),
        ("HEAD", client.head("/errors/1/"), hsr),
        ("PUT", client.put("/errors/1/", **write, headers={"If-Match": '"v1"'}), hsr),
        (
            "PATCH",
            client.patch("/errors/1/", **write, headers={"If-Match": '"v2"'}),
            hsr,
        ),
        ("PUT", client.put("/errors/1/", **write, headers={"If-Match": '"x"'}), hsr),
        ("PUT", client.put("/errors/1/", **write), hsr),
        ("PUT", client.put("/errors/1/", **write, headers={"If-Match": "x"}), hsr),
        ("POST", client.post("/errors/", **write), listing),
    ]
    statuses = [response.status_code for _, response, _ in checks]
    assert statuses == [200, 200, 200, 200, 200, 412, 428, 400, 201]
    for method, response, body in checks:
        has_content = bool(response.content)
        assert has_content == (method != "HEAD")
        headers = dict(response.items())
        assert (
            link_problems(method, response.status_code, headers, has_content, body)
            == []
        )


# --- Decoration mechanics -----------------------------------------------------


def test_class_attribute_is_the_wrapper_with_the_view_metadata():
    """D-I3: the descriptor returned a functools.partial."""
    wrapper = ThingViewSet.__dict__["retrieve"]
    assert isinstance(wrapper, ScrudfulViewFunc)
    assert ThingViewSet.retrieve is wrapper
    assert ThingViewSet.retrieve.__name__ == "retrieve"
    assert ThingViewSet.retrieve.__doc__ == "Return the thing."
    assert ThingViewSet.retrieve.__wrapped__ is ThingViewSetBase.retrieve
    bound = ThingViewSet().retrieve
    assert bound.__func__ is wrapper
    assert bound.__name__ == "retrieve"


def test_only_existing_methods_are_wrapped():
    """D-I2: missing methods were wrapped as ScrudfulViewFunc(None)."""

    @scrudful_viewset
    class ReadOnly(viewsets.ViewSet):
        def retrieve(self, request, pk):
            return Response({})

    assert isinstance(ReadOnly.__dict__["retrieve"], ScrudfulViewFunc)
    for name in ("list", "create", "update", "partial_update", "destroy"):
        assert not hasattr(ReadOnly, name)


def test_actions_are_routable_in_either_decorator_order(client):
    names = {extra.__name__ for extra in ActionViewSet.get_extra_actions()}
    assert names == {"below", "above", "plain"}
    for name in ("below", "above"):
        response = client.get(f"/actions/1/{name}/")
        assert response.status_code == 200
        assert response.json() == {"action": name}
        assert response["ETag"] == '"v1"'
        assert response["Link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
        response = client.get(f"/actions/1/{name}/", headers={"If-None-Match": '"v1"'})
        assert response.status_code == 304
    assert ActionViewSet.above.__doc__ == "Action decorated above @action."
    assert "ETag" not in client.get("/actions/1/plain/")


def test_action_mapping_is_scrudful(client):
    assert client.put("/actions/1/below/").status_code == 428
    response = client.put("/actions/1/below/", headers={"If-Match": '"v1"'})
    assert response.json() == {"action": "below_put"}
    assert response["ETag"] == '"v2"'


def test_redecorated_subclass_uses_its_own_meta(client):
    """D-I3: re-decorating a subclass raised AttributeError on every request."""
    response = client.get("/child/1/")
    assert response.status_code == 200
    assert response["ETag"] == '"child-1"'
    assert "Last-Modified" not in response
    assert "Link" not in response
    assert ChildViewSet.__dict__["retrieve"].view_method is ThingViewSetBase.retrieve
    # The parent keeps its configuration.
    assert client.get("/things/1/")["ETag"] == '"v1"'


def test_redecorated_subclass_inherits_the_parent_meta(client):
    response = client.get("/inheriting-child/1/")
    assert response["ETag"] == '"v1"'
    assert response["Last-Modified"] == MODIFIED_HTTP


def test_explicit_scrudful_method_is_kept_by_the_viewset_decorator(client):
    assert client.get("/explicit/1/")["ETag"] == '"explicit"'
    assert client.put("/explicit/1/", headers={"If-Match": '"v1"'}).status_code == 200


def test_non_callable_validator_is_rejected():
    with pytest.raises(TypeError, match="etag_func"):
        scrudful(etag_func="not callable")
    with pytest.raises(TypeError):
        scrudful(etag_func)  # keyword-only


def test_view_without_a_request_is_rejected():
    wrapper = scrudful(etag_func=lambda *args: "x")(lambda *args: None)
    with pytest.raises(TypeError, match="request"):
        wrapper("not a request")


# --- Function views -----------------------------------------------------------


def test_api_view_function_with_scrudful(client):
    """D-I12: @scrudful on a function view raised IndexError."""
    response = client.get("/function/1/")
    assert response.status_code == 200
    assert response["ETag"] == '"v1"'
    assert response["Link"] == SCHEMA_LINK
    assert client.get(
        "/function/1/", headers={"If-None-Match": '"v1"'}
    ).status_code == (304)
    assert client.put("/function/1/").status_code == 428
    response = client.put("/function/1/", headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response["ETag"] == '"v2"'


def test_plain_django_view_with_scrudful(client):
    response = client.get("/django/")
    assert response.content == b"plain django"
    assert response["ETag"] == '"v1"'
    assert response["Link"] == SCHEMA_LINK
    response = client.get("/django/", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response["ETag"] == '"v1"'
    response = client.get("/django/", headers={"If-Match": '"v0"'})
    assert response.status_code == 412
    assert response.json() == {"detail": "Precondition failed."}
    response = client.get("/django/", headers={"If-Match": "v0"})
    assert response.status_code == 400


# --- scrudful_api_view --------------------------------------------------------


def test_api_view_method_list(client):
    """D-I7: the method list was bound to etag_func."""
    response = client.get("/api/1/")
    assert response.status_code == 200
    assert response["ETag"] == '"v1"'
    assert response["Link"] == (
        f'<{SCHEMA}?pk=1>; rel="describedby"; type="application/schema+json", '
        f"{CONTEXT_LINK}"
    )
    assert client.put("/api/1/").status_code == 428
    response = client.put("/api/1/", headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response["ETag"] == '"v2"'
    assert client.post("/api/1/").status_code == 405


def test_api_view_defaults_to_get(client):
    assert client.get("/api-default/").status_code == 200
    assert client.post("/api-default/").status_code == 405


def test_api_view_method_list_must_be_a_list():
    with pytest.raises(TypeError, match="list"):
        scrudful_api_view("GET")
    view = scrudful_api_view(("GET", "POST"))(lambda request: Response({}))
    assert set(view.cls.http_method_names) == {"get", "post", "options"}


def test_api_view_without_parentheses_is_rejected():
    with pytest.raises(TypeError, match="parentheses"):

        @scrudful_api_view
        def view(request):
            return Response({})


def test_api_view_keeps_name_and_docstring():
    assert api_thing.cls.__name__ == "api_thing"
    assert api_thing.cls.__doc__ == "The api_thing docstring."
    assert api_thing.cls.get.__doc__ == "The api_thing docstring."
    assert api_thing.cls.get.__name__ == "api_thing"


def test_api_view_honours_policy_decorators(client):
    """D-I8: throttle/authentication/renderer decorators were ignored."""
    assert client.get("/api-throttled/").status_code == 429
    response = client.get("/api-authenticated/")
    assert response.status_code == 401
    assert response["WWW-Authenticate"].startswith("Basic")
    response = client.get("/api-json-only/", headers={"Accept": "text/html"})
    assert response.status_code == 406


def test_api_view_options_metadata(client):
    """D-I9: the metadata called link callbacks without the URL kwargs."""
    response = client.options("/api/7/")
    assert response.status_code == 200
    metadata = response.json()
    # api_view serves only the listed methods (and OPTIONS): no HEAD.
    assert list(metadata) == ["get", "put", "delete", "options"]
    assert response["Allow"] == "GET, PUT, DELETE, OPTIONS"
    assert response["Link"] == options_links()
    validate_http_schema_resource(metadata)
    item = content(f"{SCHEMA}?pk=7", CONTEXT)
    assert metadata["get"]["responses"]["200"]["content"] == item
    assert metadata["put"]["requestBody"]["content"] == item
    assert metadata["put"]["parameters"] == [
        {
            "in": "header",
            "name": "If-Match",
            "schema": {"type": "string"},
            "required": True,
            "description": "The entity-tag of the current representation.",
        }
    ]
    assert metadata["delete"]["responses"]["204"] == {"description": "No Content"}


def test_api_view_metadata_class_can_be_overridden(client):
    assert api_own_metadata.cls.metadata_class is CustomMetadata
    assert client.options("/api-own-metadata/").json()["name"] == "Own Metadata"


# --- OPTIONS metadata ---------------------------------------------------------


def test_viewset_metadata_detail_and_list(client):
    detail = client.options("/things/1/").json()
    validate_http_schema_resource(detail)
    assert list(detail) == ["get", "put", "delete", "options", "head", "patch"]
    assert detail["get"]["responses"]["200"]["content"] == THING_CONTENT
    listing = client.options("/things/").json()
    validate_http_schema_resource(listing)
    assert list(listing) == ["get", "post", "options", "head"]
    assert listing["get"]["responses"] == {
        "200": {
            "description": "OK",
            "content": content("http://example.com/list-schema.json", LIST_CONTEXT),
        }
    }
    assert listing["post"]["responses"] == {
        "201": {"description": "Created", "content": THING_CONTENT}
    }


def test_metadata_parameters_for_a_single_validator(client):
    body = client.options("/lm-only/1/").json()
    validate_http_schema_resource(body)
    put = body["put"]
    assert put["parameters"] == [
        {
            "in": "header",
            "name": "If-Unmodified-Since",
            "schema": {"type": "string"},
            "required": True,
            "description": "The Last-Modified date of the current representation.",
        }
    ]
    both = client.options("/things/1/").json()["put"]["parameters"]
    assert [p["description"] for p in both] == [
        "The entity-tag of the current representation. "
        "Either If-Match or If-Unmodified-Since is required.",
        "The Last-Modified date of the current representation. "
        "Either If-Match or If-Unmodified-Since is required.",
    ]


def test_metadata_for_nested_list_routes(client):
    """D-Minor: any URL kwarg used to mean "detail"."""
    metadata = client.options("/parents/5/children/").json()
    validate_http_schema_resource(metadata)
    assert list(metadata) == ["get", "post", "options", "head"]
    assert metadata["get"]["responses"]["200"]["content"] == content(
        f"{SCHEMA}?list=5", f"{CONTEXT}?list=5"
    )
    assert metadata["post"]["requestBody"]["content"] == content(
        f"{SCHEMA}?parent=5", f"{CONTEXT}?parent=5"
    )
    response = client.get("/parents/5/children/")
    assert response["ETag"] == '"list-5"'
    assert store.calls == [("nested_etag", 5)]


def test_metadata_for_action_routes(client):
    metadata = client.options("/actions/1/below/").json()
    validate_http_schema_resource(metadata)
    assert list(metadata) == ["get", "put", "options", "head"]
    assert metadata["get"]["responses"]["200"]["content"] == content(SCHEMA, CONTEXT)
    # The plain action: see test_options_action_without_links.


def test_user_defined_options_is_kept(client):
    assert client.options("/own-options/1/").json() == {"own": True}


def test_user_defined_metadata_class_is_kept(client):
    assert OwnMetadataViewSet.metadata_class is CustomMetadata
    assert ThingViewSet.metadata_class is ScrudfulMetadata
    response = client.options("/own-metadata/1/")
    assert response.json()["name"] == "Own Metadata Instance"
    # Not an HTTP Schema Resource: no HSR Links.
    assert "Link" not in response


def test_scrudful_metadata_subclass_gets_the_options_response():
    class Extended(ScrudfulMetadata):
        def determine_metadata(self, request, view):
            return {**super().determine_metadata(request, view), "x-extra": True}

    @scrudful_viewset
    class Sub(DefaultMetadataViewSet):
        metadata_class = Extended

    response = _options(Sub)
    response.render()
    assert response["Content-Type"] == "application/json"
    assert response["Link"] == options_links()
    assert response["Allow"] == "GET, OPTIONS, HEAD"
    assert response.data["x-extra"] is True


def test_metadata_describes_non_scrudful_methods(client):
    response = client.options("/plain-metadata/1/")
    metadata = response.json()
    validate_http_schema_resource(metadata)
    assert list(metadata) == ["get", "put", "delete", "options", "head", "patch"]
    assert metadata["put"] == {"responses": {"200": {"description": "OK"}}}
    # The Links come with the options that scrudful_viewset installs.
    assert "Link" not in response


def test_get_method_finds_only_scrudful_handlers():
    metadata = ScrudfulMetadata()
    assert metadata.get_method(PlainMetadataViewSet(), "retrieve") is None
    assert metadata.get_method(PlainMetadataViewSet(), "missing") is None
    wrapper = metadata.get_method(ThingViewSet(), "retrieve")
    assert wrapper is ThingViewSet.__dict__["retrieve"]


def test_metadata_can_be_called_directly():
    view = ThingViewSet.as_view({"get": "retrieve", "put": "update"})
    request = APIRequestFactory().options("/things/3/")
    response = view(request, pk="3")
    validate_http_schema_resource(response.data)
    assert list(response.data) == ["get", "put", "options", "head"]
    assert response["Allow"] == "GET, PUT, OPTIONS, HEAD"


def test_metadata_output_does_not_share_the_module_dictionaries():
    def describe():
        view = ThingViewSet.as_view({"get": "retrieve", "put": "update"})
        return view(APIRequestFactory().options("/things/3/"), pk="3").data

    first = describe()
    for operation in first.values():
        for response in operation["responses"].values():
            response["description"] = "mutated"
    second = describe()
    assert second["get"]["responses"]["304"]["description"].startswith("Not Modified")
    assert second["put"]["responses"]["412"]["description"] == "Precondition Failed."
    assert second["put"]["responses"]["428"]["description"].startswith(
        "Precondition Required"
    )


def test_async_views_are_rejected_at_decoration_time():
    async def view(request):
        pass

    with pytest.raises(TypeError, match="async views"):
        scrudful()(view)
    with pytest.raises(TypeError, match="async views"):
        ScrudfulViewFunc(view)
    with pytest.raises(TypeError, match="async views"):
        scrudful_api_view(["GET"])(view)

    with pytest.raises(TypeError, match="async views"):

        @scrudful_viewset
        class AsyncViewSet(viewsets.ViewSet):
            async def retrieve(self, request, pk):
                pass


# --- PATCH with an explicit update --------------------------------------------


def test_stock_partial_update_reaches_an_explicit_update(client):
    """An explicit @scrudful on update applies to PATCH (stock partial_update)."""
    stock = UpdateModelMixin.partial_update
    assert StockExplicitUpdateViewSet.partial_update is stock
    # Meta's validator would accept "v1"; the explicit one is "other1".
    assert (
        client.patch("/stock-explicit/1/", headers={"If-Match": '"v1"'}).status_code
        == 412
    )
    response = client.patch("/stock-explicit/1/", headers={"If-Match": '"other1"'})
    assert response.status_code == 200
    assert response.json()["partial"] is True
    assert response["ETag"] == '"other2"'


def test_patch_without_an_explicit_update_uses_meta(client):
    assert isinstance(StockPatchViewSet.__dict__["partial_update"], ScrudfulViewFunc)
    assert (
        client.patch("/stock-patch/1/", headers={"If-Match": '"other1"'}).status_code
        == 412
    )
    response = client.patch("/stock-patch/1/", headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response["ETag"] == '"v2"'


def test_redecorated_subclass_with_an_explicit_update(client):
    """The inherited partial_update wrapper is dropped for the stock method."""
    assert SubclassedStockViewSet.partial_update is UpdateModelMixin.partial_update
    assert (
        client.patch("/stock-subclass/1/", headers={"If-Match": '"v1"'}).status_code
        == 412
    )
    response = client.patch("/stock-subclass/1/", headers={"If-Match": '"other1"'})
    assert response.status_code == 200
    assert response["ETag"] == '"other2"'
    # The parent is unchanged.
    assert (
        client.patch("/stock-patch/1/", headers={"If-Match": '"v2"'}).status_code == 200
    )


def test_user_defined_partial_update_keeps_the_meta_configuration(client):
    assert (
        client.patch("/user-partial/1/", headers={"If-Match": '"other1"'}).status_code
        == 412
    )
    response = client.patch("/user-partial/1/", headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert response["ETag"] == '"v2"'


# --- metadata_class -----------------------------------------------------------


def _options(view_or_cls):
    factory = APIRequestFactory()
    return view_or_cls.as_view({"get": "retrieve"})(factory.options("/"), pk="1")


class DefaultMetadataViewSet(ThingViewSetBase):
    class Meta:
        etag_func = etag_func


def test_default_metadata_class_is_replaced():
    assert drf_views.APIView.metadata_class is SimpleMetadata

    @scrudful_viewset
    class Default(DefaultMetadataViewSet):
        pass

    assert Default.metadata_class is ScrudfulMetadata
    assert _options(Default).status_code == 200


def test_metadata_class_none_is_kept():
    @scrudful_viewset
    class NoMetadata(DefaultMetadataViewSet):
        metadata_class = None

    assert NoMetadata.metadata_class is None
    # DRF answers OPTIONS with 405 when metadata is disabled.
    response = _options(NoMetadata)
    assert response.status_code == 405
    assert "Link" not in response


def test_project_wide_metadata_class_is_kept(monkeypatch):
    monkeypatch.setattr(drf_views.APIView, "metadata_class", CustomMetadata)

    @scrudful_viewset
    class Project(DefaultMetadataViewSet):
        pass

    assert Project.metadata_class is CustomMetadata


def test_project_wide_none_is_kept(monkeypatch):
    monkeypatch.setattr(drf_views.APIView, "metadata_class", None)

    @scrudful_viewset
    class Project(DefaultMetadataViewSet):
        pass

    assert Project.metadata_class is None


def test_api_view_default_metadata_class_is_replaced():
    assert api_default.cls.metadata_class is ScrudfulMetadata


def test_api_view_metadata_class_none_is_kept():
    def view(request):
        return Response({})

    view.metadata_class = None
    built = scrudful_api_view(["GET"], etag_func=lambda view, request: "x")(view)
    assert built.cls.metadata_class is None
    assert built(APIRequestFactory().options("/")).status_code == 405


@pytest.mark.parametrize("project_wide", [CustomMetadata, None])
def test_api_view_project_wide_metadata_class_is_kept(monkeypatch, project_wide):
    monkeypatch.setattr(drf_views.APIView, "metadata_class", project_wide)

    def view(request):
        return Response({})

    built = scrudful_api_view(["GET"], etag_func=lambda view, request: "x")(view)
    assert built.cls.metadata_class is project_wide


# --- Exception handler --------------------------------------------------------


def test_exception_handler_adds_error_links(client):
    """Errors DRF itself raises carry the error entity's Links."""
    responses = {
        400: client.put(
            "/errors/1/", {"raise": True}, format="json", headers={"If-Match": '"v1"'}
        ),
        404: client.get("/errors/0/"),
        405: client.post("/errors/1/"),
    }
    for status, response in responses.items():
        assert response.status_code == status
        assert response["Link"] == ERROR_LINKS
        assert response["Content-Type"] == "application/json"
        assert response["Access-Control-Expose-Headers"] == "Link"
        assert "detail" in response.json() or status == 400
    assert responses[400].json() == {"name": ["This field is required."]}
    assert responses[405]["Allow"] == "GET, PUT, PATCH, DELETE, HEAD, OPTIONS"


def test_exception_handler_405_uses_the_first_allowed_handler_with_an_error(client):
    """No handler runs for an unrouted method: the first allowed one supplies
    the Links (here the list's, whose error entity differs from create's)."""
    response = client.put("/errors/")
    assert response.status_code == 405
    assert response["Link"] == PROBLEM_LINKS
    assert response["Content-Type"] == "application/problem+json"


def test_exception_handler_without_error_unchanged(client):
    for response in (
        client.put("/things/1/", {"raise": True}, format="json", headers=IF_MATCH),
        client.get("/things/0/"),
        client.post("/things/1/"),
        client.post("/links-only/1/"),
    ):
        assert "Link" not in response
        assert "Access-Control-Expose-Headers" not in response
        assert response["Content-Type"] == "application/json"
    # Not served as an error media type: DRF's negotiation still applies.
    response = client.get("/things/0/", headers={"Accept": "text/html"})
    assert response["Content-Type"].startswith("text/html")


def test_exception_handler_leaves_other_exceptions_alone():
    assert exception_handler(RuntimeError("boom"), {}) is None


def test_exception_handler_serves_the_error_media_type(client):
    """The HSR lists the statuses under the error entity's media type."""
    hsr = client.options("/problems/1/").json()
    validate_http_schema_resource(hsr)
    for accept in ("application/json", "text/html"):
        response = client.put(
            "/problems/1/",
            {"raise": True},
            format="json",
            headers={"Accept": accept, **IF_MATCH},
        )
        assert response.status_code == 400
        assert response["Content-Type"] == "application/problem+json"
        assert response["Link"] == PROBLEM_LINKS
        assert response.json() == {"name": ["This field is required."]}
        problems = link_problems("PUT", 400, dict(response.items()), True, hsr)
        assert problems == []
    for response in (client.get("/problems/0/"), client.post("/problems/1/")):
        assert response["Content-Type"] == "application/problem+json"
        assert response["Link"] == PROBLEM_LINKS


def test_exception_handler_keeps_the_headers_of_the_response(client):
    """A 401 without an error entity is DRF's, WWW-Authenticate included."""
    response = client.get("/api-authenticated/")
    assert response.status_code == 401
    assert "WWW-Authenticate" in response


def test_without_the_exception_handler_nothing_is_added_or_described(client):
    """Without the setting, a serializer 400 has no Link and is not described."""
    with override_settings(REST_FRAMEWORK={}):
        response = client.put(
            "/errors/1/", {"raise": True}, format="json", headers=IF_MATCH
        )
        assert response.status_code == 400
        assert "Link" not in response
        listing = client.options("/errors/").json()
        detail = client.options("/errors/1/").json()
    validate_http_schema_resource(listing)
    assert set(listing["post"]["responses"]) == {"201"}
    # Scrudful's own 400 of the conditional PUT is still there.
    assert "400" in detail["put"]["responses"]


def test_a_non_conditional_delete_has_no_400_entry(client):
    body = client.options("/errors/1/").json()
    assert "400" in body["put"]["responses"]
    body = client.options("/error-only/1/").json()
    assert "400" in body["put"]["responses"]
    assert set(body["delete"]["responses"]) == {"204"}
    plain = client.options("/problems/1/").json()
    assert "400" in plain["delete"]["responses"]  # conditional: scrudful's own


def test_a_serializer_400_is_described_with_the_exception_handler(client):
    listing = client.options("/errors/").json()
    assert set(listing["post"]["responses"]) == {"201", "400"}
