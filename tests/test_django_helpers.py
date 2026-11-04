import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from django.http import Http404
from django.test import RequestFactory

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


def test_handler_rejects_callable_links():
    with pytest.raises(TypeError, match="plain link values"):
        error_handler(
            404, Entity(schema_link_or_func=lambda *a: "s", context_link_or_func="c")
        )
