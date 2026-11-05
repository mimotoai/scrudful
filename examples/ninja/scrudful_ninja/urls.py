"""URL configuration of the scrudful django-ninja example."""

from django.urls import path

import scrudful.ninja
from scrudful_ninja.api import api

urlpatterns = [
    path("api/", api.urls),
]

#: Django's 404 (a URL no pattern matches) and 500 pages, as JSON with the
#: Links of the error documents, unless the client prefers HTML (with DEBUG
#: off; DEBUG shows Django's debug pages). The links are plain values (no
#: callables), resolved against SCRUDFUL["BASE_URL"].
PAGE_ERROR = scrudful.ninja.Entity(
    "/api/schemas/error", context_link_or_func="/api/contexts/error"
)
handler404 = scrudful.ninja.handler404(PAGE_ERROR)
handler500 = scrudful.ninja.handler500(PAGE_ERROR)
