"""Tests for scrudful._hsr, the shared HTTP Schema Resource core."""

import json

import pytest

from scrudful._adapter import describe_content, merge_links, options_links
from scrudful._hsr import (
    HSR_LINK_REL,
    HsrConfigError,
    absolute_for_hsr,
    allow_value,
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
        ({"get"}, "GET, OPTIONS, HEAD"),
        ({"PUT", "patch"}, "PUT, OPTIONS, PATCH"),
        (set(), "OPTIONS"),
        ({"GET", "QUERY"}, "GET, OPTIONS, HEAD, QUERY"),
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


def test_hsr_response_links_matches_options_links():
    assert hsr_response_links() == options_links()


def test_serialize_and_etag_are_stable():
    body = {
        "get": {"responses": {"200": {"description": "OK"}}},
        "options": options_operation(),
    }
    assert serialize_hsr(body) == serialize_hsr(json.loads(serialize_hsr(body)))
    assert hsr_etag(serialize_hsr(body)).startswith('"') and len(hsr_etag(b"x")) == 34


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
