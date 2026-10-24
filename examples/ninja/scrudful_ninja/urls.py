"""URL configuration of the scrudful django-ninja example."""

from django.urls import path

from scrudful_ninja.api import api

urlpatterns = [
    path("api/", api.urls),
]
