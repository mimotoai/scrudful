"""A URLconf mounting scrudful DRF views every way, with ``hsr_urls()``.

Used by ``tests/test_rest_framework_hsr.py``: a ``DefaultRouter``, a
hand-mounted ``path()``, a :func:`scrudful_api_view`, a namespaced
``include()`` and the include of :func:`hsr_urls`; and, under ``x/``, the
views whose OPTIONS ``Allow`` depends on the requester (``perm-items``,
``admin-items``, ``hook-items``).
"""

from types import SimpleNamespace

from django.http import Http404, JsonResponse
from django.urls import include, path, reverse
from rest_framework import viewsets
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import (
    SAFE_METHODS,
    BasePermission,
    IsAdminUser,
    IsAuthenticatedOrReadOnly,
)
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter, SimpleRouter
from rest_framework.throttling import BaseThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from scrudful.entity import Entity
from scrudful.rest_framework import (
    hsr_urls,
    http_schema_resource_view,
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


# --- OPTIONS: Allow per requester (``x/``) --------------------------------------

#: The error entity of the ``x/`` views.
X_ERROR = Entity(
    schema_link_or_func="https://api.example/schemas/error",
    context_link_or_func="https://api.example/contexts/error",
)
#: The objects of the ``x/`` views, by ``pk``: ``"1"`` is the writer's.
X_OBJECTS = {
    "1": SimpleNamespace(pk="1", owner="writer"),
    "2": SimpleNamespace(pk="2", owner="other"),
}
#: What the ``x/`` views' permissions, throttles and hook were called for,
#: in order (the tests clear it).
CALLS = []


class IsWriterOrReadOnly(BasePermission):
    """Writes for the ``writers`` group only."""

    def has_permission(self, request, view):
        CALLS.append(("view", request.method, view.request.method, view.action))
        if request.method in SAFE_METHODS:
            return True
        return request.user.groups.filter(name="writers").exists()


class IsOwnerOrReadOnly(BasePermission):
    """Writes to an object for its owner only."""

    def has_object_permission(self, request, view, obj):
        CALLS.append(("object", request.method, obj.pk))
        return request.method in SAFE_METHODS or obj.owner == request.user.username


class CountingThrottle(BaseThrottle):
    """Allows every request, recording the check."""

    def allow_request(self, request, view):
        CALLS.append(("throttle", request.method))
        return True


class XViewSetBase(viewsets.GenericViewSet):
    """A generic viewset over :data:`X_OBJECTS`, without a model."""

    def get_object(self):
        CALLS.append(("load", self.kwargs["pk"]))
        obj = X_OBJECTS.get(self.kwargs["pk"])
        if obj is None:
            raise Http404
        self.check_object_permissions(self.request, obj)
        return obj

    def list(self, request):
        return Response([])

    def create(self, request):
        return Response({"pk": "3"}, status=201)

    def retrieve(self, request, pk):
        return Response({"pk": self.get_object().pk})

    def update(self, request, pk):
        return Response({"pk": self.get_object().pk})

    def partial_update(self, request, pk):
        return Response({"pk": self.get_object().pk})

    def destroy(self, request, pk):
        self.get_object()
        return Response(status=204)


@scrudful_viewset
class PermItemViewSet(XViewSetBase):
    permission_classes = [
        IsAuthenticatedOrReadOnly,
        IsWriterOrReadOnly,
        IsOwnerOrReadOnly,
    ]
    throttle_classes = [CountingThrottle]

    class Meta:
        error = X_ERROR


@scrudful_viewset
class AdminItemViewSet(XViewSetBase):
    """Admins only, and once a day; bad credentials are a 401."""

    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAdminUser]
    throttle_classes = [CountingThrottle, OncePerDay]

    class Meta:
        error = X_ERROR


class HiddenFromOthers(BasePermission):
    """An object is invisible (``Http404``) to all but its owner, as DRF's
    ``DjangoObjectPermissions`` hides an object the requester may not read."""

    def has_object_permission(self, request, view, obj):
        CALLS.append(("object", request.method, obj.pk))
        if obj.owner != request.user.username:
            raise Http404
        return True


@scrudful_viewset
class HiddenItemViewSet(XViewSetBase):
    permission_classes = [HiddenFromOthers]

    class Meta:
        error = X_ERROR


class DenyAll(BasePermission):
    def has_permission(self, request, view):
        CALLS.append(("view", request.method, view.request.method, view.action))
        return False


def hook(view, request, method, obj):
    """The ``allowed_func`` of ``hook-items``: reads for anyone, PUT for
    ``writer``, PATCH refused by raising, DELETE never."""
    CALLS.append(("hook", method, request.method, obj and obj.pk))
    if method == "PATCH":
        raise PermissionDenied
    if method == "PUT":
        return request.user.username == "writer"
    return method in ("GET", "HEAD")


@scrudful_viewset
class HookItemViewSet(XViewSetBase):
    """Every permission check denies: ``Meta.allowed_func`` alone decides."""

    permission_classes = [DenyAll]

    class Meta:
        allowed_func = hook
        error = X_ERROR


router = DefaultRouter()
router.register("items", ItemViewSet, basename="item")
# The same viewset again: a second view function, with a document of its own.
router.register("other-items", ItemViewSet, basename="other-item")
router.register("plain", PlainViewSet, basename="plain")
router.register("locked", LockedViewSet, basename="locked")
router.register("elsewhere", ElsewhereViewSet, basename="elsewhere")

x_router = SimpleRouter()
x_router.register("perm-items", PermItemViewSet, basename="perm-item")
x_router.register("admin-items", AdminItemViewSet, basename="admin-item")
x_router.register("hook-items", HookItemViewSet, basename="hook-item")
x_router.register("hidden-items", HiddenItemViewSet, basename="hidden-item")

thing_router = SimpleRouter()
thing_router.register("things", ThingViewSet, basename="thing")

#: ElsewhereViewSet's detail route, whose HSR is mounted by hand at its
#: Meta.hsr_url, ``https://schemas.example/hsr/elsewhere-detail.json``.
elsewhere_detail = next(p.callback for p in router.urls if p.name == "elsewhere-detail")

urlpatterns = [
    path("", include(router.urls)),
    path(
        "hsr/elsewhere-detail.json",
        http_schema_resource_view(elsewhere_detail),
        name="elsewhere-detail-hsr",
    ),
    path("notes/<int:pk>", NoteView.as_view(), name="notes"),
    path("stats", stats, name="stats"),
    path("report", report, name="report"),
    path("schemas/stats", schema, name="stats-schema"),
    path("api/", include((thing_router.urls, "api"))),
    path("x/", include(x_router.urls)),
    path("", include(hsr_urls())),
]
