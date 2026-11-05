"""URL configuration of the scrudful Django REST Framework example."""

from django.contrib import admin
from django.urls import include, path

import scrudful.rest_framework

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("items.urls")),
    # Serves each scrudful view's HTTP Schema Resource, at /_hsr/<view name>.
    path("", include(scrudful.rest_framework.hsr_urls())),
]
