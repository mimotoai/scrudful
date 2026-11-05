"""The HTTP Schema Resource is public under Django's ``LoginRequiredMiddleware``
(Django >= 5.1): every scrudful HSR view is ``login_required = False``.

The module is its own URLconf: DRF's ``hsr_urls()`` route and
``http_schema_resource_view``, and ninja's automatic HSR route and
``http_schema_resource_view``.
"""

from typing import Any

import django
import pytest
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.urls import include, path
from ninja import NinjaAPI, Schema
from rest_framework import viewsets
from rest_framework.response import Response

from scrudful import ninja as scrudful_ninja
from scrudful import rest_framework as scrudful_drf
from scrudful._django import error_handler
from scrudful.entity import Entity
from tests.semantic_http import hsr_get_problems

pytestmark = [
    pytest.mark.skipif(
        django.VERSION < (5, 1), reason="LoginRequiredMiddleware is Django 5.1's"
    ),
    pytest.mark.urls(__name__),
    pytest.mark.django_db,
]

LINKS = dict(
    schema_link_or_func="https://api.example/schemas/thing",
    context_link_or_func="https://api.example/contexts/thing",
)


@scrudful_drf.scrudful_viewset
class ThingViewSet(viewsets.ViewSet):
    class Meta:
        etag_func = staticmethod(lambda view, request, pk=None, **kw: "v1")
        schema_link_or_func = LINKS["schema_link_or_func"]
        context_link_or_func = LINKS["context_link_or_func"]

    def retrieve(self, request, pk):
        return Response({"id": pk})


thing = ThingViewSet.as_view({"get": "retrieve"})


class Thing(Schema):
    id: int


router = scrudful_ninja.ScrudfulRouter(base_url="https://api.example/")


@router.get("/{pk}", response=Thing)
@scrudful_ninja.scrudful(load=lambda request, pk, **kw: {"id": pk}, **LINKS)
def get_thing(request: Any, pk: int, obj: Any) -> Any:
    return obj


api = NinjaAPI(urls_namespace="login-required")
api.add_router("/things", router)

urlpatterns = [
    path("things/<str:pk>/", thing, name="thing"),
    path("thing-hsr/", scrudful_drf.http_schema_resource_view(thing)),
    path("n/", api.urls),
    path("n-hsr/", scrudful_ninja.http_schema_resource_view(router, "/{pk}")),
    path("", include(scrudful_drf.hsr_urls())),
]


@pytest.fixture
def login_required(settings):
    settings.SCRUDFUL = {"BASE_URL": "https://api.example/"}
    settings.MIDDLEWARE = [
        *settings.MIDDLEWARE,
        "django.contrib.auth.middleware.LoginRequiredMiddleware",
    ]


@pytest.mark.parametrize(
    "url", ["/_hsr/thing", "/thing-hsr/", "/n/things/_hsr/pk", "/n-hsr/"]
)
def test_the_hsr_is_public_under_login_required(login_required, url):
    response = Client().get(url)
    assert response.status_code == 200, response.get("Location")
    assert hsr_get_problems(200, dict(response.items()), response.content) == []


def test_the_resources_stay_behind_login(login_required):
    """Only the HSR views are exempt: DRF's views are exempt already (DRF
    authenticates them itself); ninja's operations are not."""
    assert Client().get("/things/1/").status_code == 200
    response = Client().get("/n/things/1")
    assert response.status_code == 302


def test_the_error_handler_views_are_exempt():
    view = error_handler(404, Entity(**LINKS))
    assert view.login_required is False
    assert isinstance(view(RequestFactory().get("/missing")), HttpResponse)
