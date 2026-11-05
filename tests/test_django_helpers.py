import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.test import RequestFactory

from scrudful import _django
from scrudful._adapter import prefers_html
from scrudful._django import (
    aware,
    conditional_response,
    error_handler,
    precondition_headers,
)
from scrudful._hsr import PROBLEM_DETAILS_CONTEXT_URL, PROBLEM_DETAILS_SCHEMA_URL
from scrudful.conditional import ConditionalResult
from scrudful.entity import Entity
from scrudful.problem import PROBLEM_JSON
from tests.semantic_http import link_problems

PROBLEM_ERROR = Entity(
    media_type=PROBLEM_JSON,
    schema_link_or_func=PROBLEM_DETAILS_SCHEMA_URL,
    context_link_or_func=PROBLEM_DETAILS_CONTEXT_URL,
)


def test_precondition_headers_reads_django_request():
    request = RequestFactory().get("/", HTTP_IF_MATCH='"a"', HTTP_IF_NONE_MATCH=" ")
    assert precondition_headers(request) == {"if-match": '"a"'}


def test_conditional_response_304():
    response = conditional_response(ConditionalResult(304, {"ETag": '"a"'}, None))
    assert response.status_code == 304
    assert response["ETag"] == '"a"'
    assert "ETag" in response["Access-Control-Expose-Headers"]


def test_conditional_response_json():
    response = conditional_response(ConditionalResult(412, {}, {"detail": "x"}))
    assert response.status_code == 412
    assert json.loads(response.content) == {"detail": "x"}


def test_aware_uses_time_zone(settings):
    settings.TIME_ZONE = "Europe/Paris"
    assert aware(datetime(2026, 1, 1)).utcoffset() == timedelta(hours=1)
    value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert aware(value) is value
    assert aware(1.5) == 1.5


@pytest.mark.parametrize(
    "accept, html",
    [
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
        ("application/json, text/plain, */*", False),
        ("*/*", False),
        (None, False),
        ("", False),
        ("application/problem+json", False),
        ("text/*", True),
        ("text/html;q=0, */*", False),
        ("text/html;q=0.5, */*;q=0.5", True),
        ("text/html, application/problem+json", False),
    ],
)
def test_prefers_html(accept, html):
    assert prefers_html(accept, "application/problem+json") is html


def test_handler404_json_with_links(rf):
    view = error_handler(404, PROBLEM_ERROR)
    response = view(rf.get("/nowhere", HTTP_ACCEPT="application/json"), Http404())
    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/problem+json")
    assert response["Vary"] == "Accept"
    assert json.loads(response.content)["status"] == 404
    assert link_problems("GET", 404, dict(response.items()), True) == []


def test_handler500_plain_json_without_accept(rf):
    response = error_handler(500, Entity())(rf.get("/"))
    assert response.status_code == 500
    assert json.loads(response.content) == {"detail": "Internal Server Error"}
    assert response["Vary"] == "Accept"


def test_handler500_html_for_browsers(rf):
    response = error_handler(500, PROBLEM_ERROR)(
        rf.get("/", HTTP_ACCEPT="text/html,*/*;q=0.8")
    )
    assert response.status_code == 500
    assert response["Content-Type"].startswith("text/html")
    assert response["Vary"] == "Accept"


def test_handler404_html_for_browsers(rf):
    request = rf.get("/", HTTP_ACCEPT="text/html")
    response = error_handler(404, PROBLEM_ERROR)(request, Http404())
    assert response.status_code == 404
    assert response["Content-Type"].startswith("text/html")


@pytest.mark.parametrize("module", ["scrudful.ninja", "scrudful.rest_framework"])
def test_adapters_reexport_handlers(module, rf):
    adapter = importlib.import_module(module)
    assert {"handler404", "handler500"} <= set(adapter.__all__)
    assert adapter.handler404(PROBLEM_ERROR)(rf.get("/"), Http404()).status_code == 404
    assert adapter.handler500(PROBLEM_ERROR)(rf.get("/")).status_code == 500


@pytest.mark.parametrize("module", ["scrudful.ninja", "scrudful.rest_framework"])
def test_handler_links_resolve_against_the_base_url(module, rf, settings):
    """Relative links resolve against ``SCRUDFUL["BASE_URL"]`` when it is
    set, as the HTTP Schema Resource's do, else against the request."""
    adapter = importlib.import_module(module)
    error = Entity("/schemas/error", context_link_or_func="/contexts/error")
    request = rf.get("/nowhere", HTTP_ACCEPT="application/json")
    links = [
        '<{0}schemas/error>; rel="describedby"; type="application/schema+json"',
        '<{0}contexts/error>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"',
    ]
    for base, expected in (
        ("https://api.example/", "https://api.example/"),
        (None, "http://testserver/"),
    ):
        settings.SCRUDFUL = None if base is None else {"BASE_URL": base}
        for response in (
            adapter.handler404(error)(request, Http404()),
            adapter.handler500(error)(request),
        ):
            assert response["Link"] == ", ".join(links).format(expected)


@pytest.mark.parametrize("status", [404, 500])
def test_handler_serves_its_error_without_links_it_cant_build(
    rf, settings, caplog, status
):
    """A handler404/handler500 never raises: a Link it can't build (an
    invalid ``SCRUDFUL["BASE_URL"]``) is logged, and the error is served
    without it, so the original error is still answered (and logged)."""
    settings.SCRUDFUL = {"BASE_URL": "/v1/"}
    view = error_handler(status, Entity("/s", context_link_or_func="/c"))
    request = rf.get("/nowhere", HTTP_ACCEPT="application/json")
    with caplog.at_level("ERROR", logger="scrudful"):
        response = view(request, Http404()) if status == 404 else view(request)
    assert response.status_code == status
    assert response["Content-Type"] == "application/json"
    assert "Link" not in response
    [record] = caplog.records
    assert record.name == "scrudful._django" and record.levelname == "ERROR"
    assert f"handler{status}" in record.getMessage()
    assert isinstance(record.exc_info[1], ImproperlyConfigured)


def test_handler_needs_the_request_only_for_a_relative_link(rf, settings, caplog):
    """Absolute links don't read the request's host (which may be one
    ALLOWED_HOSTS refuses); relative ones without a base URL do, and a host
    refused is logged and the links left out."""
    settings.SCRUDFUL = None
    settings.ALLOWED_HOSTS = ["testserver"]
    request = rf.get("/nowhere", HTTP_HOST="evil.example")
    absolute = Entity("https://x.example/s", context_link_or_func="https://x/c")
    response = error_handler(500, absolute)(request)
    assert response["Link"].startswith("<https://x.example/s>")
    with caplog.at_level("ERROR", logger="scrudful"):
        response = error_handler(500, Entity("/s", context_link_or_func="/c"))(request)
    assert response.status_code == 500 and "Link" not in response
    [record] = caplog.records
    assert type(record.exc_info[1]).__name__ == "DisallowedHost"


def test_handler_rejects_callable_links():
    with pytest.raises(TypeError, match="plain link values"):
        error_handler(
            404, Entity(schema_link_or_func=lambda *a: "s", context_link_or_func="c")
        )


# --- The SCRUDFUL setting's keys --------------------------------------------------


def test_setting_keys_are_the_typed_dicts():
    keys = {"BASE_URL", "ERROR", "HSR_CACHE_CONTROL", "HSR_ROUTE", "HSR_URL"}
    assert _django.SETTING_KEYS == keys
    assert set(_django.ScrudfulSettings.__annotations__) == keys


def test_setting_accessors_give_the_defaults(settings):
    settings.SCRUDFUL = None
    assert _django.setting_base_url() is None
    assert _django.setting_hsr_route() is True
    assert _django.setting_hsr_url() is None
    assert _django.setting_hsr_cache_control() == "public, max-age=300"
    assert _django.setting_error() is None


def test_setting_accessors_give_the_values(settings):
    def hsr_url(name):
        return f"https://x.example/{name}"

    error = Entity("https://x.example/e", context_link_or_func="https://x.example/c")
    settings.SCRUDFUL = {
        "BASE_URL": "https://api.example/",
        "HSR_ROUTE": False,
        "HSR_URL": hsr_url,
        "HSR_CACHE_CONTROL": "no-cache",
        "ERROR": error,
    }
    assert _django.setting_base_url() == "https://api.example/"
    assert _django.setting_hsr_route() is False
    assert _django.setting_hsr_url() is hsr_url
    assert _django.setting_hsr_cache_control() == "no-cache"
    assert _django.setting_error() is error


@pytest.mark.parametrize(
    "accessor, key, value, match",
    [
        ("setting_base_url", "BASE_URL", "/v1/", "BASE_URL.*absolute URL"),
        ("setting_base_url", "BASE_URL", 3, "BASE_URL"),
        ("setting_hsr_route", "HSR_ROUTE", "no", "HSR_ROUTE.*bool, not str"),
        ("setting_hsr_url", "HSR_URL", "https://x/", "HSR_URL.*callable.*not str"),
        ("setting_hsr_cache_control", "HSR_CACHE_CONTROL", 300, "HSR_CACHE_CONTROL"),
        ("setting_hsr_cache_control", "HSR_CACHE_CONTROL", " ", "HSR_CACHE_CONTROL"),
        ("setting_error", "ERROR", "oops", "ERROR.*Entity, not str"),
        ("setting_error", "ERROR", Entity(media_type="text/csv"), "ERROR.*JSON"),
    ],
)
def test_setting_accessors_raise_improperly_configured(
    settings, accessor, key, value, match
):
    settings.SCRUDFUL = {key: value}
    with pytest.raises(ImproperlyConfigured, match=match):
        getattr(_django, accessor)()
