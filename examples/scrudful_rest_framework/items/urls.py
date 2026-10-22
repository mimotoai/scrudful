from django.urls import include, path
from rest_framework.routers import DefaultRouter

from items import views

router = DefaultRouter()
router.register("items", views.ItemViewSet, basename="item")
router.register("readonly-items", views.ReadOnlyItemViewSet, basename="readonly-item")

urlpatterns = [
    path("", include(router.urls)),
    path("stats/", views.item_stats, name="item-stats"),
    path("schemas/item.json", views.item_schema, name="item-schema"),
    path("contexts/item.jsonld", views.item_context, name="item-context"),
]
