"""Tests for scrudful._hsr, the shared HTTP Schema Resource core."""

import hashlib
import json

import pytest

from scrudful._adapter import describe_content, merge_links
from scrudful._hsr import (
    HSR_LINK_REL,
    HsrConfigError,
    absolute_for_hsr,
    allow_value,
    check_base_url,
    check_hsr_cache_control,
    check_hsr_url_result,
    hsr_etag,
    hsr_link,
    hsr_response_links,
    hsr_route_name,
    is_preflight,
    options_operation,
    serialize_hsr,
)


def test_options_operation_is_204_without_content():
    assert options_operation() == {"responses": {"204": {"description": "No Content"}}}
    assert options_operation() is not options_operation()


@pytest.mark.parametrize(
    "given, expected",
    [
        # HEAD only when given: never inferred from GET.
        ({"get"}, "GET, OPTIONS"),
        ({"get", "head"}, "GET, OPTIONS, HEAD"),
        ({"head"}, "OPTIONS, HEAD"),
        ({"PUT", "patch"}, "PUT, OPTIONS, PATCH"),
        (set(), "OPTIONS"),
        ({"GET", "QUERY"}, "GET, OPTIONS, QUERY"),
    ],
)
def test_allow_value(given, expected):
    assert allow_value(given) == expected


def test_hsr_link():
    assert hsr_link("https://h.example/_hsr/pk") == (
        '<https://h.example/_hsr/pk>; rel="https://w3id.org/semantic-http/ns#'
        'http-schema-resource-document"; type="application/json"'
    )
    assert HSR_LINK_REL.endswith("#http-schema-resource-document")


def test_hsr_response_links():
    assert hsr_response_links() == (
        "<https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json>;"
        ' rel="describedby"; type="application/schema+json", '
        "<https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld>;"
        ' rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )


def test_serialize_and_etag_are_stable():
    body = {
        "get": {"responses": {"200": {"description": "OK"}}},
        "options": options_operation(),
    }
    assert serialize_hsr(body) == serialize_hsr(json.loads(serialize_hsr(body)))
    assert hsr_etag(serialize_hsr(body)).startswith('"') and len(hsr_etag(b"x")) == 34


def test_hsr_etag_identifies_the_bytes():
    """The ETag is a function of the serialized bytes alone: a changed
    document gets a new one, the same bytes the same one."""
    data = serialize_hsr({"get": {"responses": {"200": {"description": "OK"}}}})
    other = serialize_hsr({"get": {"responses": {"201": {"description": "OK"}}}})
    assert hsr_etag(data) == '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    assert hsr_etag(data) == hsr_etag(bytes(data))
    assert hsr_etag(data) != hsr_etag(other)
    assert hsr_etag(b"a") != hsr_etag(b"b")


def test_is_preflight():
    h = {"origin": "https://a.example", "access-control-request-method": "PUT"}
    assert is_preflight("OPTIONS", h.get)
    assert not is_preflight("OPTIONS", {"origin": "https://a.example"}.get)
    assert not is_preflight("GET", h.get)


def test_absolute_for_hsr():
    assert absolute_for_hsr("https://x/s", None, "schema") == "https://x/s"
    assert (
        absolute_for_hsr("/s", "https://api.example/", "schema")
        == "https://api.example/s"
    )
    with pytest.raises(HsrConfigError, match="relative URL '/s'"):
        absolute_for_hsr("/s", None, "schema")


def test_absolute_for_hsr_verifies_its_result():
    """A base URL that is itself relative can't make a URL absolute."""
    with pytest.raises(HsrConfigError, match="not absolute"):
        absolute_for_hsr("/s", "api.example/", "schema")


@pytest.mark.parametrize("url", ["https://api.example", "https://api.example/v1/"])
def test_check_base_url_accepts_absolute_urls(url):
    check_base_url(url, "base_url")


@pytest.mark.parametrize(
    "url", ["/relative/", "api.example", "", "https://", 3, b"https://x/", ["x"]]
)
def test_check_base_url_rejects_the_rest_alike(url):
    """A non-str is refused as an absolute-URL error, like a relative str."""
    with pytest.raises(HsrConfigError, match=r"thing must be an absolute URL, not"):
        check_base_url(url, "thing")


@pytest.mark.parametrize("url", ["https://docs.example/_hsr/x", "urn:example:hsr"])
def test_check_hsr_url_result_returns_an_absolute_url(url):
    assert check_hsr_url_result(url, "hsr_url('/x')") == url


@pytest.mark.parametrize("value", [None, "", "/_hsr/x", "docs.example/x", 3, b"x"])
def test_check_hsr_url_result_rejects_the_rest(value):
    with pytest.raises(HsrConfigError, match=r"hsr_url\('/x'\) must return"):
        check_hsr_url_result(value, "hsr_url('/x')")


@pytest.mark.parametrize("value", ["no-cache", "public, max-age=60"])
def test_check_hsr_cache_control_accepts_a_directive_list(value):
    assert check_hsr_cache_control(value, "hsr_cache_control") == value


@pytest.mark.parametrize("value", ["", "  ", 300, None])
def test_check_hsr_cache_control_rejects_empty_and_non_str(value):
    with pytest.raises(HsrConfigError, match="hsr_cache_control must be"):
        check_hsr_cache_control(value, "hsr_cache_control")


def test_hsr_config_error_is_public():
    from scrudful import entity

    assert "HsrConfigError" in entity.__all__
    assert entity.HsrConfigError is HsrConfigError
    assert issubclass(HsrConfigError, ValueError)


def test_absolute_for_hsr_takes_any_uri_with_a_scheme():
    """An absolute URI has a scheme (RFC 3986 section 4.3), an authority or
    not: ``urn:`` and ``tag:`` URIs are absolute (the final review's M2)."""
    for url in ("urn:example:schema", "tag:example.com,2026:schema", "file:///s"):
        assert absolute_for_hsr(url, None, "schema") == url
        assert absolute_for_hsr(url, "https://api.example/", "schema") == url


@pytest.mark.parametrize(
    "path, name",
    [
        ("/", "_root"),
        ("", "_root"),
        ("/{pk}", "pk"),
        ("/{int:pk}/notes/{note_id}", "pk.notes.note_id"),
        ("/stats", "stats"),
    ],
)
def test_hsr_route_name(path, name):
    assert hsr_route_name(path) == name


def test_describe_content_rejects_schema_without_context():
    with pytest.raises(HsrConfigError, match="x-rdf-context"):
        describe_content(
            "application/json", "https://x/s", "application/schema+json", None, None
        )
    describe_content(
        "text/turtle", "https://x/s", "application/schema+json", None, None
    )


def test_merge_links_view_hsr_link_wins():
    view = hsr_link("https://other/_hsr/x")
    assert merge_links(view, None, None, hsr_link("https://h/_hsr/x")) == view
