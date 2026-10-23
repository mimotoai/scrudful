"""ASGI entry point of the scrudful django-ninja example (e.g. for uvicorn)."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "scrudful_ninja.settings")

application = get_asgi_application()
