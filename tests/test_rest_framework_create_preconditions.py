"""A create evaluates its preconditions against the collection's validators."""

from datetime import datetime, timezone

import pytest
from django.http import Http404
from django.urls import include, path
from rest_framework import generics, serializers, viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.routers import SimpleRouter
from rest_framework.test import APIClient

from items.models import Item
from scrudful.problem import PROBLEM_JSON, problem_details
from scrudful.rest_framework import (
    _NEVER_CREATES,
    Entity,
    ScrudfulViewFunc,
    scrudful,
    scrudful_viewset,
)

pytestmark = [pytest.mark.urls(__name__), pytest.mark.django_db]

LIST_MODIFIED = datetime(2026, 1, 1, tzinfo=timezone.utc)
BEFORE = "Wed, 31 Dec 2025 00:00:00 GMT"
AFTER = "Fri, 02 Jan 2026 00:00:00 GMT"


class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ["id", "name"]


list_calls: list = []


@pytest.fixture(autouse=True)
def _reset():
    list_calls.clear()


def list_etag(view, request, **kwargs):
    list_calls.append("etag")
    return f"n{Item.objects.count()}"


def list_modified(view, request, **kwargs):
    return LIST_MODIFIED


def item_error_body(request, result):
    return {**problem_details(request, result), "from": "item"}


def list_error_body(request, result):
    return {**problem_details(request, result), "from": "list"}


@scrudful_viewset
class ObjectViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

    class Meta:
        object_validators = True
        etag_func = staticmethod(lambda obj: f"v{obj.version}")
        error = Entity(schema_link_or_func="/schemas/problem", media_type=PROBLEM_JSON)
        error_body_func = item_error_body
        list_etag_func = list_etag
        list_last_modified_func = list_modified
        list_error_body_func = list_error_body


@scrudful_viewset
class PlainViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

    class Meta:
        list_etag_func = list_etag


@scrudful_viewset
class ModifiedViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

    class Meta:
        list_last_modified_func = list_modified


@scrudful_viewset
class NoListValidatorsViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

    class Meta:
        object_validators = True
        etag_func = staticmethod(lambda obj: f"v{obj.version}")


class ExplicitCreateView(generics.CreateAPIView):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

    @scrudful(
        etag_func=lambda obj: f"v{obj.version}",
        object_validators=True,
        creates=True,
        list_etag_func=list_etag,
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


@api_view(["GET", "POST"])
@scrudful(list_etag_func=lambda request: "n0")
def collection_view(request):
    return Response([], status=201 if request.method == "POST" else 200)


def parent_etag(request, parent_pk):
    if parent_pk != 1:
        raise Http404
    return f"n{parent_pk}"


@api_view(["POST"])
@scrudful(list_etag_func=parent_etag)
def children_view(request, parent_pk):
    return Response({}, status=201)


@api_view(["POST"])
@scrudful(list_etag_func=lambda request: None)
def no_validators_view(request):
    return Response({}, status=201)


router = SimpleRouter()
router.register("objects", ObjectViewSet, basename="object")
router.register("plains", PlainViewSet, basename="plain")
router.register("modifieds", ModifiedViewSet, basename="modified")
router.register("nolists", NoListValidatorsViewSet, basename="nolist")
urlpatterns = [
    path("", include(router.urls)),
    path("explicit/", ExplicitCreateView.as_view()),
    path("function/", collection_view),
    path("parents/<int:parent_pk>/children/", children_view),
    path("none/", no_validators_view),
]


@pytest.fixture
def client():
    return APIClient()


def post(client, url, **headers):
    return client.post(url, {"name": "new"}, format="json", headers=headers)


@pytest.mark.parametrize("url", ["/objects/", "/plains/", "/explicit/"])
def test_unknown_if_match_is_412_and_creates_nothing(client, url):
    response = post(client, url, **{"If-Match": '"nope"'})
    assert response.status_code == 412
    assert not Item.objects.exists()


@pytest.mark.parametrize("url", ["/objects/", "/plains/", "/explicit/"])
def test_current_if_match_creates(client, url):
    Item.objects.create(name="one")
    response = post(client, url, **{"If-Match": '"n1"'})
    assert response.status_code == 201
    assert Item.objects.count() == 2


def test_if_match_is_a_strong_comparison(client):
    response = post(client, "/objects/", **{"If-Match": 'W/"n0"'})
    assert response.status_code == 412


def test_if_match_star_creates(client):
    assert post(client, "/objects/", **{"If-Match": "*"}).status_code == 201


def test_if_none_match_star_is_412(client):
    response = post(client, "/objects/", **{"If-None-Match": "*"})
    assert response.status_code == 412
    assert not Item.objects.exists()


def test_if_none_match_is_a_weak_comparison(client):
    response = post(client, "/objects/", **{"If-None-Match": 'W/"n0"'})
    assert response.status_code == 412
    other = post(client, "/objects/", **{"If-None-Match": '"n9"'})
    assert other.status_code == 201


def test_past_if_unmodified_since_is_412(client):
    response = post(client, "/objects/", **{"If-Unmodified-Since": BEFORE})
    assert response.status_code == 412
    assert not Item.objects.exists()


def test_later_if_unmodified_since_creates(client):
    response = post(client, "/objects/", **{"If-Unmodified-Since": AFTER})
    assert response.status_code == 201


def test_if_unmodified_since_is_ignored_with_if_match(client):
    response = post(
        client, "/objects/", **{"If-Match": '"n0"', "If-Unmodified-Since": BEFORE}
    )
    assert response.status_code == 201


def test_if_modified_since_is_ignored(client):
    response = post(client, "/objects/", **{"If-Modified-Since": AFTER})
    assert response.status_code == 201


def test_unconditional_create_is_not_428(client):
    response = post(client, "/objects/")
    assert response.status_code == 201
    created = Item.objects.get()
    # The 201 still describes the created object, not the collection.
    assert response["ETag"] == f'"v{created.version}"'


def test_malformed_if_match_is_400(client):
    response = post(client, "/objects/", **{"If-Match": "nope"})
    assert response.status_code == 400
    assert not Item.objects.exists()


def test_412_has_the_item_error_entity_and_body(client):
    """The create's error entity is the item one (as for its serializer
    400): its 400/412 body comes from the matching error_body_func."""
    response = post(client, "/objects/", **{"If-Match": '"nope"'})
    assert response.status_code == 412
    body = response.json()
    assert body["from"] == "item"
    assert body["status"] == 412
    assert response["Content-Type"] == PROBLEM_JSON
    assert "/schemas/problem" in response["Link"]


@pytest.mark.parametrize(
    "headers", [{}, {"If-Modified-Since": AFTER}], ids=["none", "ims-only"]
)
def test_without_a_precondition_the_list_validators_are_not_called(client, headers):
    assert post(client, "/objects/", **headers).status_code == 201
    assert list_calls == []


def test_list_validators_apply_to_post_only(client):
    response = client.get("/function/", headers={"If-None-Match": '"n0"'})
    assert response.status_code == 200
    assert post(client, "/function/", **{"If-Match": '"nope"'}).status_code == 412


def test_without_list_validators_preconditions_are_ignored(client):
    response = post(client, "/nolists/", **{"If-Match": '"nope"'})
    assert response.status_code == 201


def test_options_describes_the_412(client):
    described = client.options("/objects/").json()["post"]
    assert {"201", "400", "412"} <= set(described["responses"])
    assert "428" not in described["responses"]
    assert "parameters" not in described


def test_options_without_list_validators_describes_201_only(client):
    described = client.options("/nolists/").json()["post"]
    assert list(described["responses"]) == ["201"]


def test_list_validators_must_be_callable():
    with pytest.raises(TypeError, match="list_etag_func"):
        ScrudfulViewFunc(lambda view, request: None, list_etag_func="n1")


def test_list_validators_with_target_validators_need_creates():
    with pytest.raises(TypeError, match="creates"):
        ScrudfulViewFunc(
            lambda view, request: None,
            etag_func=lambda view, request: "v1",
            list_etag_func=list_etag,
        )


def class_body_error(build):
    """The exception ``build`` raises from a class body's ``__set_name__``
    (wrapped in a ``RuntimeError`` before Python 3.12)."""
    with pytest.raises((TypeError, RuntimeError)) as info:
        build()
    error = info.value
    return error.__cause__ if isinstance(error, RuntimeError) else error


@pytest.mark.parametrize("name", sorted(_NEVER_CREATES))
def test_list_validators_on_a_handler_that_never_creates_are_rejected(name):
    handler = scrudful(list_etag_func=list_etag)(lambda self, request: None)
    error = class_body_error(
        lambda: type("V", (generics.GenericAPIView,), {name: handler})
    )
    assert isinstance(error, TypeError)
    assert f"V.{name}" in str(error)
    assert "POST" in str(error)


@pytest.mark.parametrize(
    "name, func",
    [("list_etag_func", list_etag), ("list_last_modified_func", list_modified)],
)
def test_explicit_create_without_the_list_validators_of_meta_is_rejected(name, func):
    meta = type("Meta", (), {name: staticmethod(func)})
    with pytest.raises(TypeError, match=f"Meta.{name}"):

        @scrudful_viewset
        class V(viewsets.ModelViewSet):
            queryset = Item.objects.all()
            serializer_class = ItemSerializer
            Meta = meta

            @scrudful()
            def create(self, request, *args, **kwargs):
                return super().create(request, *args, **kwargs)


def test_explicit_create_with_its_own_list_validators_is_kept():
    @scrudful_viewset
    class V(viewsets.ModelViewSet):
        queryset = Item.objects.all()
        serializer_class = ItemSerializer

        class Meta:
            list_etag_func = list_etag

        @scrudful(list_last_modified_func=list_modified)
        def create(self, request, *args, **kwargs):
            return super().create(request, *args, **kwargs)

    assert V.create.list_etag_func is None
    assert V.create.list_last_modified_func is list_modified


def test_list_last_modified_func_alone(client):
    assert (
        post(client, "/modifieds/", **{"If-Unmodified-Since": BEFORE}).status_code
        == 412
    )
    assert (
        post(client, "/modifieds/", **{"If-Unmodified-Since": AFTER}).status_code == 201
    )
    # No ETag: no If-Match but * can match it.
    assert post(client, "/modifieds/", **{"If-Match": '"n1"'}).status_code == 412
    assert post(client, "/modifieds/", **{"If-Match": "*"}).status_code == 201


def test_list_validators_get_the_url_kwargs(client):
    url = "/parents/1/children/"
    assert post(client, url, **{"If-Match": '"n1"'}).status_code == 201
    assert post(client, url, **{"If-Match": '"n2"'}).status_code == 412


def test_a_list_validator_raising_http404_is_a_404(client):
    """The way to answer a POST to a missing collection (e.g. its parent)."""
    response = post(client, "/parents/2/children/", **{"If-Match": "*"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "headers, status",
    [
        ({"If-None-Match": "*"}, 412),
        ({"If-Match": "*"}, 201),
        ({"If-Match": '"n0"'}, 412),
        ({"If-Unmodified-Since": BEFORE}, 201),
    ],
)
def test_list_validators_returning_none_still_mean_a_collection(
    client, headers, status
):
    """Unlike a GET of the list: the collection always has a representation
    (a missing one is the callback's Http404), so If-None-Match: * fails and
    If-Match: * passes."""
    assert post(client, "/none/", **headers).status_code == status
