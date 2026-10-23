"""Django settings of the scrudful django-ninja example.

Development settings only: the secret key is public and DEBUG is on.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-scrudful-ninja-example-not-for-production"

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",  # ninja's interactive docs (/api/docs)
    "ninja",
    "roles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "scrudful_ninja.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {},
    },
]

WSGI_APPLICATION = "scrudful_ninja.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # No ATOMIC_REQUESTS: Django refuses it for async views, and
        # /api/roles/search is async. The writes re-check the ETag in their
        # UPDATE instead (roles/api.py, save_role).
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
