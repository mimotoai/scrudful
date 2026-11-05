import importlib
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.test import RequestFactory
from django.urls import path, reverse_lazy
from django.utils.functional import lazy
from django.utils.safestring import SafeString

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


def test_handler_logs_the_request_it_cant_link(rf, settings, caplog):
    """The log of a Link the handler can't build names the request's path,
    so one of many identical errors can be traced."""
    settings.SCRUDFUL = {"BASE_URL": "/v1/"}
    view = error_handler(404, Entity("/s", context_link_or_func="/c"))
    with caplog.at_level("ERROR", logger="scrudful"):
        view(rf.get("/some/where", HTTP_ACCEPT="application/json"), Http404())
    [record] = caplog.records
    assert "/some/where" in record.getMessage()


def test_handler_lets_an_unexpected_error_propagate(rf, settings, monkeypatch):
    """Only the documented causes (the setting, the host, the entity's
    configuration) are logged and served without Links: anything else
    raised building them is a bug, which takes Django's own 500 path."""
    settings.SCRUDFUL = None

    def broken(*args):
        raise KeyError("bug")

    view = error_handler(404, Entity("https://x/s", context_link_or_func="https://x/c"))
    monkeypatch.setattr(_django, "link_content", broken)
    with pytest.raises(KeyError, match="bug"):
        view(rf.get("/nowhere", HTTP_ACCEPT="application/json"), Http404())


@pytest.mark.parametrize("status", [403, 400, "404", 404.0, None])
def test_handler_is_a_404_or_500_handler(status):
    """Its HTML answer is Django's page of 404 or 500: another status would
    answer a browser with the 500 page."""
    with pytest.raises(ValueError, match="404 or 500"):
        error_handler(status, PROBLEM_ERROR)


@pytest.mark.parametrize("error", [None, "https://x/s", {"media_type": "x"}])
def test_handler_needs_an_entity(error):
    with pytest.raises(TypeError, match="must be an Entity, not"):
        error_handler(404, error)


@pytest.mark.parametrize("media_type", ["text/turtle", "text/html", "text/csv"])
def test_handler_needs_a_json_entity(media_type):
    """Its bodies are JSON: an entity of another media type would label them
    as content no entity describes (and ``text/html`` would never select
    Django's HTML page)."""
    with pytest.raises(ValueError, match="JSON error bodies"):
        error_handler(404, Entity(media_type=media_type))


@pytest.mark.parametrize("adapter_name", ["rest_framework", "ninja"])
def test_the_public_handlers_check_their_entity(adapter_name):
    adapter = importlib.import_module(f"scrudful.{adapter_name}")
    with pytest.raises(ValueError, match="JSON error bodies"):
        adapter.handler404(Entity(media_type="text/turtle"))
    with pytest.raises(TypeError, match="must be an Entity"):
        adapter.handler500(None)


class LazyErrorURLconf:
    urlpatterns = [
        path("errors/schema", lambda request: None, name="error-schema"),
        path("errors/context", lambda request: None, name="error-context"),
    ]


def test_handler_doesnt_resolve_lazy_links_when_made(rf, settings):
    """Made in ``urls.py`` above the ``urlpatterns`` its ``reverse_lazy``
    links name, the handler resolves them only when it answers."""
    settings.SCRUDFUL = {"BASE_URL": "https://api.example/"}
    settings.ROOT_URLCONF = "tests.no_such_urlconf"  # not importable yet
    view = error_handler(
        404,
        Entity(
            reverse_lazy("error-schema"),
            context_link_or_func=reverse_lazy("error-context"),
        ),
    )
    settings.ROOT_URLCONF = LazyErrorURLconf
    response = view(rf.get("/x", HTTP_ACCEPT="application/json"), Http404())
    assert response["Link"].startswith("<https://api.example/errors/schema>")
    assert "<https://api.example/errors/context>" in response["Link"]


def test_handler_serves_an_unparsable_base_url_without_links(rf, settings, caplog):
    """A ``SCRUDFUL["BASE_URL"]`` that can't even be parsed is an invalid
    setting like any other: logged, and the error served without Links."""
    settings.SCRUDFUL = {"BASE_URL": "http://[::1"}
    view = error_handler(404, Entity("/s", context_link_or_func="/c"))
    with caplog.at_level("ERROR", logger="scrudful"):
        response = view(rf.get("/x", HTTP_ACCEPT="application/json"), Http404())
    assert response.status_code == 404 and "Link" not in response
    [record] = caplog.records
    assert isinstance(record.exc_info[1], ImproperlyConfigured)


@pytest.mark.parametrize("link", ["schema", "context"])
def test_handler_rejects_a_link_url_it_cant_parse_when_made(link):
    """A plain link URL that can't be parsed fails when the handler is made
    (in ``urls.py``), not on each error it answers."""
    urls = {"schema": "https://x/s", "context": "https://x/c"}
    urls[link] = "http://[::1/errors"
    with pytest.raises(ImproperlyConfigured, match=r"http://\[::1/errors"):
        error_handler(404, Entity(urls["schema"], context_link_or_func=urls["context"]))


def _unparsable_url() -> str:
    return "http://[::1/errors/schema"


@pytest.mark.parametrize(
    "url, error",
    [
        (reverse_lazy("no-such-url-name"), "NoReverseMatch"),
        (lazy(_unparsable_url, str)(), "ValueError"),
    ],
    ids=["doesnt-reverse", "unparsable"],
)
@pytest.mark.parametrize("status", [404, 500])
def test_handler_serves_its_error_without_a_lazy_link_it_cant_resolve(
    rf, settings, caplog, status, url, error
):
    """A lazy link URL is evaluated only when the handler answers: one whose
    name doesn't reverse, or whose URL can't be parsed, is logged with the
    request's path, and the error served without the entity's Links."""
    settings.SCRUDFUL = None
    view = error_handler(status, Entity(url, context_link_or_func="https://x/c"))
    request = rf.get("/some/where", HTTP_ACCEPT="application/json")
    with caplog.at_level("ERROR", logger="scrudful"):
        response = view(request, Http404()) if status == 404 else view(request)
    assert response.status_code == status
    assert "Link" not in response
    [record] = caplog.records
    assert record.name == "scrudful._django" and record.levelname == "ERROR"
    assert f"handler{status}" in record.getMessage()
    assert "/some/where" in record.getMessage()
    assert type(record.exc_info[1]).__name__ == error


def _original_bug(request):
    raise RuntimeError("original bug")


class BrokenErrorLinkURLconf:
    """A ``urls.py`` whose error handlers' schema link names no URL."""

    urlpatterns = [path("boom", _original_bug)]
    handler404 = error_handler(
        404,
        Entity(reverse_lazy("no-such-url-name"), context_link_or_func="https://x/c"),
    )
    handler500 = error_handler(
        500,
        Entity(reverse_lazy("no-such-url-name"), context_link_or_func="https://x/c"),
    )


@pytest.mark.parametrize("url, status", [("/boom", 500), ("/missing", 404)])
def test_django_answers_and_logs_an_error_whose_handler_cant_link(
    client, db, settings, caplog, url, status
):
    """Through Django's own error handling: the handler answers the error,
    so Django logs it (``django.request``, which it does only once the
    handler returned)."""
    settings.ROOT_URLCONF = BrokenErrorLinkURLconf
    settings.SCRUDFUL = None
    client.raise_request_exception = False
    with caplog.at_level("WARNING"):
        response = client.get(url, HTTP_ACCEPT="application/json")
    assert response.status_code == status and "Link" not in response
    [logged] = [r for r in caplog.records if r.name == "django.request"]
    assert logged.getMessage().endswith(url)
    if status == 500:
        assert isinstance(logged.exc_info[1], RuntimeError)
    [scrudful_log] = [r for r in caplog.records if r.name == "scrudful._django"]
    assert type(scrudful_log.exc_info[1]).__name__ == "NoReverseMatch"


class FailingHandler500URLconf:
    urlpatterns = [path("boom", _original_bug)]
    handler500 = error_handler(
        500, Entity("https://x/s", context_link_or_func="https://x/c")
    )


def test_a_failing_handler500_logs_the_error_it_was_answering(
    client, db, settings, caplog, monkeypatch
):
    """Django logs the error a ``handler500`` answers only once it returns,
    and nothing past it catches what it raises: when it fails for a reason
    it doesn't serve around (a bug), it logs the error it was answering
    first, which would otherwise reach no log but as the context of its own
    exception."""
    settings.ROOT_URLCONF = FailingHandler500URLconf
    settings.SCRUDFUL = None

    def broken(*args):
        raise KeyError("bug")

    monkeypatch.setattr(_django, "link_content", broken)
    client.raise_request_exception = False
    with caplog.at_level("ERROR"), pytest.raises(KeyError, match="bug"):
        client.get("/boom", HTTP_ACCEPT="application/json")
    # Django's own log of it never ran.
    assert not [r for r in caplog.records if r.name == "django.request"]
    [record] = [r for r in caplog.records if r.name == "scrudful._django"]
    assert record.levelname == "ERROR"
    assert "handler500" in record.getMessage() and "/boom" in record.getMessage()
    assert isinstance(record.exc_info[1], RuntimeError)
    assert str(record.exc_info[1]) == "original bug"


def test_a_failing_handler404_logs_nothing_of_its_own(
    rf, settings, caplog, monkeypatch
):
    """A ``handler404`` that fails is Django's to answer, with its
    ``handler500``: only that one logs the error it was answering."""
    settings.SCRUDFUL = None

    def broken(*args):
        raise KeyError("bug")

    view = error_handler(404, Entity("https://x/s", context_link_or_func="https://x/c"))
    monkeypatch.setattr(_django, "link_content", broken)
    with caplog.at_level("ERROR"), pytest.raises(KeyError):
        try:
            raise Http404("missing")
        except Http404 as exc:
            view(rf.get("/x", HTTP_ACCEPT="application/json"), exc)
    assert not caplog.records


CONTEXT_LINK = (
    '<https://x/c>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)


def test_handler_gives_a_link_by_one_rule_when_made_and_answering(rf, settings):
    """Whether a link is given is decided alike when the handler is made
    and when it answers: an empty ``str`` of any class (``SafeString``) is no
    link, so a rel of it isn't checked either."""
    settings.SCRUDFUL = None
    view = error_handler(
        404,
        Entity(
            SafeString(""),
            context_link_or_func="https://x/c",
            schema_rel_or_func="a\nb",
        ),
    )
    response = view(rf.get("/x", HTTP_ACCEPT="application/json"), Http404())
    assert response["Link"] == CONTEXT_LINK


def _no_url() -> str:
    return ""


def test_handler_gives_no_link_for_a_lazy_url_of_nothing(rf, settings):
    """A lazy URL counts as given when the handler is made, which doesn't
    evaluate it; one that evaluates to ``""`` gives no Link when it answers
    (not one to the request's own URL)."""
    settings.SCRUDFUL = None
    view = error_handler(
        404, Entity(lazy(_no_url, str)(), context_link_or_func="https://x/c")
    )
    response = view(rf.get("/x", HTTP_ACCEPT="application/json"), Http404())
    assert response["Link"] == CONTEXT_LINK


def test_handler_rejects_a_link_value_it_cant_write():
    """A rel or type a Link can't carry is refused when the handler is made,
    not on each error it answers."""
    with pytest.raises(ValueError, match="control"):
        error_handler(
            404,
            Entity(
                "https://x/s",
                context_link_or_func="https://x/c",
                schema_rel_or_func="a\nb",
            ),
        )


# --- The SCRUDFUL setting's keys --------------------------------------------------


def test_setting_keys_are_the_typed_dicts():
    keys = {"BASE_URL", "ERROR", "HSR_CACHE_CONTROL", "HSR_ROUTE", "HSR_URL"}
    assert _django.SETTING_KEYS == keys
    assert set(_django.ScrudfulSettings.__annotations__) == keys


def test_each_setting_key_has_one_accessor():
    """Every key the setting accepts has the accessor the system check runs,
    and the mapping can't be changed from outside."""
    assert _django.SETTING_ACCESSORS.keys() == _django.SETTING_KEYS
    with pytest.raises(TypeError):
        _django.SETTING_ACCESSORS["BASE_URL"] = lambda: None


def test_a_setting_key_without_an_accessor_fails_the_import():
    """A key added to ``ScrudfulSettings`` without an accessor is caught when
    the module is imported, not left unchecked by the system check."""
    source = pathlib.Path(_django.__file__).read_text()
    source = source.replace(
        "    HSR_URL: Callable[[str], str]\n",
        "    HSR_URL: Callable[[str], str]\n    NEW_KEY: str\n",
        1,
    )
    assert "NEW_KEY" in source
    with pytest.raises(AssertionError, match="NEW_KEY"):
        exec(compile(source, _django.__file__, "exec"), {"__name__": "copy_of"})


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
