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
from rest_framework import viewsets
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
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.routers import SimpleRouter
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.throttling import BaseThrottle

from scrudful.rest_framework import (
    ScrudfulMetadata,
    ScrudfulViewFunc,
    scrudful,
    scrudful_api_view,
    scrudful_viewset,
)

# The example settings enable ATOMIC_REQUESTS, so every request uses the DB.
pytestmark = [pytest.mark.urls(__name__), pytest.mark.django_db]

MODIFIED = datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
MODIFIED_HTTP = "Tue, 02 Jan 2024 03:04:05 GMT"
SCHEMA = "http://example.com/schema.json"
CONTEXT = "http://example.com/context.jsonld"
SCHEMA_LINK = f'<{SCHEMA}>; rel="describedBy"; type="application/json"'
CONTEXT_LINK = (
    f'<{CONTEXT}>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)


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
        if not store.exists:
            return not_found()
        return Response({"id": pk, "version": store.version})

    def update(self, request, pk, partial=False):
        if not store.exists:
            return not_found()
        if request.data.get("invalid"):
            return Response({"name": ["invalid"]}, status=400)
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


@scrudful_viewset
class ActionViewSet(ThingViewSetBase):
    class Meta:
        etag_func = etag_func

    @action(detail=True, methods=["get"])
    @scrudful(etag_func=etag_func, schema_link_or_func=SCHEMA)
    def below(self, request, pk):
        """Action decorated below @action."""
        return Response({"action": "below"})

    @below.mapping.put
    @scrudful(etag_func=etag_func)
    def below_put(self, request, pk):
        store.update()
        return Response({"action": "below_put"})

    @scrudful(etag_func=etag_func, schema_link_or_func=SCHEMA)
    @action(detail=True, methods=["get"])
    def above(self, request, pk):
        """Action decorated above @action."""
        return Response({"action": "above"})

    @action(detail=True, methods=["get"])
    def plain(self, request, pk):
        return Response({"action": "plain"})


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


@scrudful_viewset
class OwnMetadataViewSet(ThingViewSetBase):
    metadata_class = SimpleMetadata

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
        list_etag_func = nested_etag
        list_schema_link_or_func = staticmethod(
            lambda view, request, parent_pk: f"{SCHEMA}?list={parent_pk}"
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


own_metadata.metadata_class = SimpleMetadata
api_own_metadata = scrudful_api_view(["GET"], etag_func=lambda view, request: "x")(
    own_metadata
)


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
    ("actions", ActionViewSet),
    ("headers", HeaderViewSet),
    ("own-options", OwnOptionsViewSet),
    ("own-metadata", OwnMetadataViewSet),
    ("plain-metadata", PlainMetadataViewSet),
    ("injection", InjectionViewSet),
    ("etag-format", ETagFormatViewSet),
    ("lm-format", LastModifiedFormatViewSet),
    ("post", PostViewSet),
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


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_unconditional_write_gives_428(client, method):
    response = getattr(client, method)("/things/1/")
    assert response.status_code == 428
    assert response.json() == {
        "detail": "This request must be conditional.",
        "accepted-precondition-headers": ["If-Match", "If-Unmodified-Since"],
    }
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
    post = client.options("/post/").json()["post"]
    assert set(post["responses"]) == {"201", "400", "412"}
    assert "parameters" not in post


def test_list_uses_the_list_configuration(client):
    response = client.get("/things/")
    assert response["Link"] == (
        '<http://example.com/list-schema.json>; rel="describedBy"; '
        'type="application/json"'
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
        '%3Chttps://evil.example>; rel="describedBy"; type="application/json"'
    )


def test_link_url_is_percent_encoded(client):
    response = client.get("/injection/1/", {"v": "été"})
    assert response["Link"].startswith("<http://example.com/schema?v=%C3%A9t%C3%A9>")


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
        assert response["Link"] == SCHEMA_LINK
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
    assert (
        response["Link"]
        == f'<{SCHEMA}?pk=1>; rel="describedBy"; type="application/json"'
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
    assert set(metadata) == {"get", "put", "delete"}
    content = {"application/json": {"schema": f"{SCHEMA}?pk=7"}}
    assert metadata["get"]["responses"]["200"]["content"] == content
    assert metadata["put"]["requestBody"]["content"] == content
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
    assert api_own_metadata.cls.metadata_class is SimpleMetadata
    assert client.options("/api-own-metadata/").json()["name"] == "Own Metadata"


# --- OPTIONS metadata ---------------------------------------------------------


def test_viewset_metadata_detail_and_list(client):
    detail = client.options("/things/1/").json()
    assert set(detail) == {"get", "put", "patch", "delete"}
    assert detail["get"]["responses"]["200"]["content"] == {
        "application/json": {"schema": SCHEMA, "context": CONTEXT}
    }
    listing = client.options("/things/").json()
    assert set(listing) == {"get", "post"}
    assert listing["get"]["responses"] == {
        "200": {
            "description": "OK",
            "content": {
                "application/json": {"schema": "http://example.com/list-schema.json"}
            },
        }
    }
    assert listing["post"]["responses"] == {"201": {"description": "Created"}}


def test_metadata_parameters_for_a_single_validator(client):
    put = client.options("/lm-only/1/").json()["put"]
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
    assert set(metadata) == {"get", "post"}
    assert metadata["get"]["responses"]["200"]["content"] == {
        "application/json": {"schema": f"{SCHEMA}?list=5"}
    }
    assert metadata["post"]["requestBody"]["content"] == {
        "application/json": {"schema": f"{SCHEMA}?parent=5"}
    }
    response = client.get("/parents/5/children/")
    assert response["ETag"] == '"list-5"'
    assert store.calls == [("nested_etag", 5)]


def test_metadata_for_action_routes(client):
    metadata = client.options("/actions/1/below/").json()
    assert set(metadata) == {"get", "put"}
    assert metadata["get"]["responses"]["200"]["content"] == {
        "application/json": {"schema": SCHEMA}
    }
    plain = client.options("/actions/1/plain/").json()
    assert plain == {"get": {"responses": {"200": {"description": "OK"}}}}


def test_user_defined_options_is_kept(client):
    assert client.options("/own-options/1/").json() == {"own": True}


def test_user_defined_metadata_class_is_kept(client):
    assert OwnMetadataViewSet.metadata_class is SimpleMetadata
    assert ThingViewSet.metadata_class is ScrudfulMetadata
    assert client.options("/own-metadata/1/").json()["name"] == "Own Metadata Instance"


def test_metadata_describes_non_scrudful_methods(client):
    metadata = client.options("/plain-metadata/1/").json()
    assert set(metadata) == {"get", "put", "patch", "delete"}
    assert metadata["put"] == {
        "requestBody": {
            "description": "The new content of the resource.",
            "required": True,
        },
        "responses": {"200": {"description": "OK"}},
    }


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
    assert set(response.data) == {"get", "put"}
