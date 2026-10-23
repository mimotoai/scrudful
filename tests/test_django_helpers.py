import json
from datetime import datetime, timedelta, timezone

from django.test import RequestFactory

from scrudful._django import aware, conditional_response, precondition_headers
from scrudful.conditional import ConditionalResult


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
