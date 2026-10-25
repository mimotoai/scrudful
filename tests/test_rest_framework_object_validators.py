"""``object_validators``: validators computed from ``get_object()``."""

import pytest
from django.urls import include, path
from rest_framework import generics, serializers, viewsets
from rest_framework.permissions import BasePermission
from rest_framework.routers import SimpleRouter
from rest_framework.test import APIClient

from items.models import Item
from scrudful.rest_framework import (
    Entity,
    exception_handler,
    scrudful,
    scrudful_api_view,
    scrudful_viewset,
)

pytestmark = [pytest.mark.urls(__name__), pytest.mark.django_db]

calls: list = []
object_checks: list = []


@pytest.fixture(autouse=True)
def _reset():
    calls.clear()
    object_checks.clear()


class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ["id", "name"]


class HideHidden(BasePermission):
    def has_object_permission(self, request, view, obj):
        object_checks.append(obj)
        return obj.name != "hidden"


def object_etag(obj):
    calls.append(("etag", obj))
    return f"v{obj.version}"


def object_modified(obj):
    calls.append(("modified", obj))
    return obj.updated_at


@scrudful_viewset
class ObjectViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    permission_classes = [HideHidden]

    class Meta:
        object_validators = True
        etag_func = object_etag
        last_modified_func = object_modified


def legacy_etag(view, request, pk):
    calls.append(("legacy", pk))
    return f"pk{pk}"


@scrudful_viewset
class LegacyViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

    class Meta:
        etag_func = legacy_etag


class ExplicitView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    permission_classes = [HideHidden]
    seen: list = []

    @scrudful(etag_func=object_etag, object_validators=True)
    def put(self, request, *args, **kwargs):
        self.seen.append(self.get_object())
        return super().put(request, *args, **kwargs)


ERROR = Entity(schema_link_or_func="/schemas/error")


@scrudful_viewset
class ErrorViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

    def get_exception_handler(self):
        return exception_handler

    class Meta:
        object_validators = True
        etag_func = object_etag
        error = ERROR


router = SimpleRouter()
router.register("errors", ErrorViewSet, basename="error")
router.register("objects", ObjectViewSet, basename="object")
router.register("legacy", LegacyViewSet, basename="legacy")
urlpatterns = [
    path("", include(router.urls)),
    path("explicit/<int:pk>/", ExplicitView.as_view()),
]


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def item():
    return Item.objects.create(name="one")


def test_object_validators_receive_the_object(client, item):
    response = client.get(f"/objects/{item.pk}/")
    assert response.status_code == 200
    assert response["ETag"] == '"v1"'
    assert "Last-Modified" in response
    assert [name for name, _ in calls] == ["etag", "modified"]
    assert all(isinstance(obj, Item) and obj.pk == item.pk for _, obj in calls)


def test_object_validators_not_called_when_permission_denied(client):
    hidden = Item.objects.create(name="hidden")
    response = client.get(f"/objects/{hidden.pk}/")
    assert response.status_code == 403
    assert calls == []
    assert "ETag" not in response
    put = client.put(f"/objects/{hidden.pk}/", {"name": "x"}, format="json")
    assert put.status_code == 403
    assert calls == []
    assert "ETag" not in put


def test_object_validators_missing_object_is_404_not_428(client):
    response = client.put("/objects/999/", {"name": "x"}, format="json")
    assert response.status_code == 404
    assert calls == []
    assert "ETag" not in response


def test_handler_reuses_the_precondition_object(client, item):
    response = client.get(f"/objects/{item.pk}/")
    assert response.status_code == 200
    # One get_object() for the request: the object permission ran once and the
    # validators saw the very instance the handler serialized.
    assert len(object_checks) == 1
    assert {id(obj) for _, obj in calls} == {id(object_checks[0])}


def test_object_validators_412_on_stale_if_match(client, item):
    response = client.put(
        f"/objects/{item.pk}/",
        {"name": "x"},
        format="json",
        HTTP_IF_MATCH='"v0"',
    )
    assert response.status_code == 412
    item.refresh_from_db()
    assert item.name == "one"


def test_object_validators_requires_precondition(client, item):
    response = client.put(f"/objects/{item.pk}/", {"name": "x"}, format="json")
    assert response.status_code == 428


def test_object_validators_put_refreshes_validators(client, item):
    response = client.put(
        f"/objects/{item.pk}/",
        {"name": "two"},
        format="json",
        HTTP_IF_MATCH='"v1"',
    )
    assert response.status_code == 200
    assert response["ETag"] == '"v2"'
    assert response.json()["name"] == "two"
    # Loaded for the request, then again after the update.
    assert len(object_checks) == 2
    assert object_checks[0] is not object_checks[1]


def test_object_validators_put_to_hidden_name_sends_no_validators(client, item):
    response = client.put(
        f"/objects/{item.pk}/",
        {"name": "hidden"},
        format="json",
        HTTP_IF_MATCH='"v1"',
    )
    assert response.status_code == 200
    assert "ETag" not in response
    assert "Last-Modified" not in response


def test_object_validators_if_none_match_304(client, item):
    response = client.get(f"/objects/{item.pk}/", HTTP_IF_NONE_MATCH='"v1"')
    assert response.status_code == 304


def test_object_validators_off_keeps_legacy_callback_signature(client, item):
    response = client.get(f"/legacy/{item.pk}/")
    assert response.status_code == 200
    assert response["ETag"] == f'"pk{item.pk}"'
    assert calls == [("legacy", str(item.pk))]
    assert client.put("/legacy/999/", {"name": "x"}, format="json").status_code == 428


def test_object_validators_on_api_view_is_type_error():
    with pytest.raises(TypeError, match="needs a view with get_object"):
        scrudful_api_view(["GET"], etag_func=lambda obj: "x", object_validators=True)


def test_object_validators_needs_a_validator():
    with pytest.raises(TypeError, match="etag_func or last_modified_func"):

        @scrudful_viewset
        class NoValidators(viewsets.ModelViewSet):
            queryset = Item.objects.all()

            class Meta:
                object_validators = True


def test_object_validators_needs_get_object():
    with pytest.raises(TypeError, match="needs a view with get_object"):

        @scrudful_viewset
        class NoGetObject(viewsets.ViewSet):
            def retrieve(self, request, pk=None): ...

            class Meta:
                object_validators = True
                etag_func = object_etag


def test_explicit_decorator_handler_gets_the_same_instance(client, item):
    ExplicitView.seen.clear()
    response = client.put(
        f"/explicit/{item.pk}/",
        {"name": "two"},
        format="json",
        HTTP_IF_MATCH='"v1"',
    )
    assert response.status_code == 200
    assert response["ETag"] == '"v2"'
    # One load for the precondition (reused by the handler's calls), one fresh.
    assert len(object_checks) == 2
    assert ExplicitView.seen == [object_checks[0]]
    assert ExplicitView.seen[0] is object_checks[0]
    # The override does not leak to later requests.
    assert client.get(f"/explicit/{item.pk}/").status_code == 200
    assert len(object_checks) == 3


def test_object_validators_missing_object_404_carries_error_links(client):
    for response in (
        client.get("/errors/999/"),
        client.put("/errors/999/", {"name": "x"}, format="json"),
    ):
        assert response.status_code == 404
        assert "/schemas/error" in response["Link"]
