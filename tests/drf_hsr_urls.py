"""A URLconf mounting scrudful DRF views every way, with ``hsr_urls()``.

Used by ``tests/test_rest_framework_hsr.py``: a ``DefaultRouter``, a
hand-mounted ``path()``, a :func:`scrudful_api_view`, a namespaced
``include()`` and the include of :func:`hsr_urls`.
"""

from django.http import JsonResponse
from django.urls import include, path, reverse
from rest_framework import viewsets
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter, SimpleRouter
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from scrudful.rest_framework import (
    hsr_urls,
    scrudful,
    scrudful_api_view,
    scrudful_viewset,
)


def item_etag(view, request, pk=None, **kwargs):
    return "v1"


def anonymous_reads_only(view, request, method, obj):
    """An ``allowed_func`` that depends on the requester (for OPTIONS only)."""
    return method in ("GET", "HEAD") or request.user.is_authenticated


class ItemViewSetBase(viewsets.ViewSet):
    def list(self, request):
        return Response([])

    def create(self, request):
        return Response({"id": 1}, status=201)

    def retrieve(self, request, pk):
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
        allowed_func = anonymous_reads_only


@scrudful_viewset
class ThingViewSet(ItemViewSetBase):
    class Meta:
        etag_func = item_etag
        schema_link_or_func = "https://things.example/schemas/thing"
        context_link_or_func = "https://things.example/contexts/thing"
        base_url = "https://things.example/"
        # Served by hsr_urls() even with SCRUDFUL["HSR_ROUTE"] = False.
        hsr_route = True
        hsr_cache_control = "public, max-age=3600"


class OncePerDay(SimpleRateThrottle):
    """One request a day, for any requester."""

    scope = "scrudful-hsr-test"
    rate = "1/day"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


@scrudful_viewset
class LockedViewSet(ItemViewSetBase):
    """Admins only, and throttled: its HSR is neither."""

    permission_classes = [IsAdminUser]
    throttle_classes = [OncePerDay]

    class Meta:
        etag_func = item_etag
        schema_link_or_func = "/schemas/item"
        context_link_or_func = "/contexts/item"


@scrudful_viewset
class ElsewhereViewSet(ItemViewSetBase):
    """Its HSR is served elsewhere, not by hsr_urls()."""

    class Meta:
        schema_link_or_func = "/schemas/item"
        context_link_or_func = "/contexts/item"
        hsr_route = False

        def hsr_url(view_name):
            return f"https://schemas.example/hsr/{view_name}.json"


class PlainViewSet(ItemViewSetBase):
    """Not scrudful: it has no HTTP Schema Resource."""


class NoteView(APIView):
    @scrudful(
        etag_func=item_etag,
        schema_link_or_func="/schemas/note",
        context_link_or_func="/contexts/note",
    )
    def get(self, request, pk):
        return Response({"id": pk})


@scrudful_api_view(
    ["GET"],
    schema_link_or_func=lambda view, request, **kwargs: reverse("stats-schema"),
    context_link_or_func="/contexts/stats",
)
def stats(request):
    return Response({"count": 0})


@scrudful_api_view(
    ["GET"],
    schema_link_or_func="/schemas/report",
    context_link_or_func="/contexts/report",
    base_url="https://reports.example/",
)
def report(request):
    return Response({})


def schema(request):
    return JsonResponse({})


router = DefaultRouter()
router.register("items", ItemViewSet, basename="item")
# The same viewset again: a second view function, with a document of its own.
router.register("other-items", ItemViewSet, basename="other-item")
router.register("plain", PlainViewSet, basename="plain")
router.register("locked", LockedViewSet, basename="locked")
router.register("elsewhere", ElsewhereViewSet, basename="elsewhere")

thing_router = SimpleRouter()
thing_router.register("things", ThingViewSet, basename="thing")

urlpatterns = [
    path("", include(router.urls)),
    path("notes/<int:pk>", NoteView.as_view(), name="notes"),
    path("stats", stats, name="stats"),
    path("report", report, name="report"),
    path("schemas/stats", schema, name="stats-schema"),
    path("api/", include((thing_router.urls, "api"))),
    path("", include(hsr_urls())),
]
