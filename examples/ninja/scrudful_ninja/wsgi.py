"""WSGI entry point of the scrudful django-ninja example."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "scrudful_ninja.settings")

application = get_wsgi_application()
