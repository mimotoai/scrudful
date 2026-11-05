"""Django settings of the scrudful Django REST Framework example.

Development settings only: the secret key is public and DEBUG is on.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-scrudful-example-not-for-production"

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

# The absolute URL the API is served at. scrudful resolves the relative link
# URLs against it, in the HTTP Schema Resources (which don't depend on the
# request) and in the Link headers.
SCRUDFUL = {"BASE_URL": os.environ.get("SCRUDFUL_BASE_URL", "http://localhost:8000/")}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "items",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "scrudful_rest_framework.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "scrudful_rest_framework.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # The validator callbacks and the write share one transaction; see
        # items/views.py.
        "ATOMIC_REQUESTS": True,
    }
}

REST_FRAMEWORK = {
    # Gives the errors DRF itself raises (a serializer's 400, a 404, a 405 ...)
    # the Links of the views' error entity.
    "EXCEPTION_HANDLER": "scrudful.rest_framework.exception_handler",
    # Semantic HTTP 0.1: the HTTP Schema Resource describes JSON content only.
    # The browsable API would be allowed for a client asking for text/html
    # (scrudful sends Vary: Accept), but DRF also serves its HTML for
    # ?format=api without Accept, which is undescribed: it is left out. Form
    # bodies would be allowed (a resource MAY accept undescribed request
    # media types), but have no lifting to RDF: only JSON is accepted.
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
