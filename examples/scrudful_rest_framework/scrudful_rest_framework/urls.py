"""URL configuration of the scrudful Django REST Framework example."""

from django.contrib import admin
from django.urls import include, path

import scrudful.rest_framework

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("items.urls")),
    # Serves each scrudful view's HTTP Schema Resource, at /_hsr/<view name>.
    # Every scrudful view's responses link to it: without this include they
    # raise ImproperlyConfigured.
    path("", include(scrudful.rest_framework.hsr_urls())),
]

#: Django's 404 (a URL no pattern matches) and 500 pages, as JSON with the
#: Links of the error documents, unless the client prefers HTML (with DEBUG
#: off; DEBUG shows Django's debug pages). The links are plain values (no
#: callables), resolved against SCRUDFUL["BASE_URL"].
PAGE_ERROR = scrudful.rest_framework.Entity(
    "/schemas/error.json", context_link_or_func="/contexts/error.jsonld"
)
handler404 = scrudful.rest_framework.handler404(PAGE_ERROR)
handler500 = scrudful.rest_framework.handler500(PAGE_ERROR)
