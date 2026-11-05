"""Tests for ``tests/semantic_http.py``, the Semantic HTTP 0.1 wire checks."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from scrudful._adapter import CONTEXT_TYPE
from tests.semantic_http import (
    hsr_get_problems,
    hsr_link_problems,
    options_204_problems,
    undescribed_problems,
    link_problems,
    parse_links,
    validate_http_schema_resource,
)

FIXTURES = Path(__file__).parent / "fixtures" / "semantic-http-0.1"
PERSON = json.loads((FIXTURES / "person.http-schema-resource.json").read_text())

# The spec's "HTTP Examples -> GET" response.
PERSON_GET_LINKS = (
    '<https://example.com/schemas/person.schema.json>; rel="describedby";'
    ' type="application/schema+json", '
    "<https://example.com/contexts/person.context.jsonld>;"
    ' rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)
PERSON_GET_HEADERS = {"Content-Type": "application/json", "Link": PERSON_GET_LINKS}
SCHEMA = '<https://h/s>; rel="describedby"; type="application/schema+json"'
CONTEXT = (
    '<https://h/c>; rel="http://www.w3.org/ns/json-ld#context";'
    ' type="application/ld+json"'
)


# --- validate_http_schema_resource -------------------------------------------


def test_validate_accepts_person():
    validate_http_schema_resource(PERSON)


def test_validate_rejects_0_0_shape():
    body = {
        "get": {
            "responses": {
                "200": {
                    "description": "OK",
                    "content": {
                        "application/json": {
                            "schema": "https://h/s",
                            "context": "https://h/c",
                        }
                    },
                }
            }
        }
    }
    with pytest.raises(ValidationError):
        validate_http_schema_resource(body)


def test_validate_rejects_relative_ref():
    body = copy.deepcopy(PERSON)
    content = body["get"]["responses"]["200"]["content"]["application/json"]
    content["schema"]["$ref"] = "/schemas/person"
    with pytest.raises(ValidationError):
        validate_http_schema_resource(body)


def test_validate_rejects_relative_href():
    body = copy.deepcopy(PERSON)
    content = body["get"]["responses"]["200"]["content"]["application/json"]
    content["x-rdf-context"]["href"] = "contexts/person"
    with pytest.raises(ValidationError):
        validate_http_schema_resource(body)


def test_validate_rejects_uppercase_content_key():
    body = copy.deepcopy(PERSON)
    content = body["get"]["responses"]["200"]["content"]
    content["Application/JSON"] = content.pop("application/json")
    with pytest.raises(ValidationError):
        validate_http_schema_resource(body)


def test_validate_rejects_duplicate_media_types():
    body = copy.deepcopy(PERSON)
    content = body["get"]["responses"]["200"]["content"]
    content["application/json; charset=utf-8"] = content["application/json"]
    with pytest.raises(AssertionError, match="duplicate"):
        validate_http_schema_resource(body)


# --- parse_links -------------------------------------------------------------


def test_parse_links():
    assert parse_links(PERSON_GET_LINKS) == [
        (
            "https://example.com/schemas/person.schema.json",
            {"rel": "describedby", "type": "application/schema+json"},
        ),
        (
            "https://example.com/contexts/person.context.jsonld",
            {
                "rel": "http://www.w3.org/ns/json-ld#context",
                "type": "application/ld+json",
            },
        ),
    ]


def test_parse_links_handles_commas_and_case():
    assert parse_links('<https://x/a,b>; REL=next; Title="a, b"') == [
        ("https://x/a,b", {"rel": "next", "title": "a, b"})
    ]


def test_parse_links_empty():
    assert parse_links(None) == []
    assert parse_links("") == []


# --- link_problems -----------------------------------------------------------


def test_link_problems_conformant():
    headers = {"Content-Type": "application/json", "Link": f"{SCHEMA}, {CONTEXT}"}
    assert link_problems("GET", 200, headers, True) == []


def test_link_problems_no_content_needs_no_links():
    assert link_problems("DELETE", 204, {}, False) == []


def test_link_problems_missing_context():
    headers = {
        "Content-Type": "application/json",
        "Link": '<https://h/s>; rel="describedby"',
    }
    problems = link_problems("GET", 200, headers, True)
    assert any("context" in problem for problem in problems)
    assert any("type" in problem for problem in problems)


def test_link_problems_missing_schema():
    headers = {"Content-Type": "application/json", "Link": CONTEXT}
    problems = link_problems("GET", 200, headers, True)
    assert any("describedby" in problem for problem in problems)


def test_link_problems_duplicate_schema():
    headers = {
        "Content-Type": "application/json",
        "Link": f"{SCHEMA}, {SCHEMA}, {CONTEXT}",
    }
    assert link_problems("GET", 200, headers, True)


def test_link_problems_in_band_has_no_context():
    headers = {"Content-Type": "application/ld+json", "Link": SCHEMA}
    assert link_problems("GET", 200, headers, True) == []
    headers["Link"] = f"{SCHEMA}, {CONTEXT}"
    assert link_problems("GET", 200, headers, True)


def test_link_problems_non_json_needs_rdf_context():
    rdf = (
        '<https://h/c>; rel="https://w3id.org/semantic-http/ns#rdf-context";'
        ' type="application/ld+json"'
    )
    headers = {"Content-Type": "text/csv", "Link": f"{SCHEMA}, {rdf}"}
    assert link_problems("GET", 200, headers, True) == []
    headers["Link"] = f"{SCHEMA}, {CONTEXT}"
    assert link_problems("GET", 200, headers, True)


def test_link_problems_preflight_options_is_exempt():
    headers = {"Access-Control-Allow-Methods": "GET, OPTIONS"}
    assert link_problems("OPTIONS", 204, headers, False) == []


def _options_headers(allow="GET, OPTIONS"):
    return {"Link": HSR_LINK, "Allow": allow}


def test_link_problems_options_conformant():
    assert link_problems("OPTIONS", 204, _options_headers(), False) == []


def test_link_problems_options_wrong_status_content_and_links():
    """The old 200 answer with the HSR as content is no longer conformant."""
    assert link_problems("OPTIONS", 200, _options_headers(), False)
    assert link_problems("OPTIONS", 204, _options_headers(), True)
    headers = {"Link": f"{SCHEMA}, {CONTEXT}", "Allow": "OPTIONS"}
    assert link_problems("OPTIONS", 204, headers, False)
    assert link_problems("OPTIONS", 204, {"Link": HSR_LINK}, False)


def test_link_problems_options_error_is_an_error_response():
    """An error answering OPTIONS (a loader's 404) is checked as any error
    response, not as the OPTIONS description."""
    headers = {"Content-Type": "application/json", "Link": f"{SCHEMA}, {CONTEXT}"}
    assert link_problems("OPTIONS", 404, headers, True) == []
    assert link_problems("OPTIONS", 404, {"Content-Type": "application/json"}, True)


def test_link_problems_person_agreement():
    assert link_problems("GET", 200, PERSON_GET_HEADERS, True, PERSON) == []


def test_link_problems_head_uses_get_entry():
    body = {"get": PERSON["get"]}
    assert link_problems("HEAD", 200, PERSON_GET_HEADERS, False, body) == []


def test_link_problems_agreement_mismatch():
    headers = {**PERSON_GET_HEADERS, "Link": f"{SCHEMA}, {CONTEXT}"}
    problems = link_problems("GET", 200, headers, True, PERSON)
    assert any("agree" in problem.lower() for problem in problems)


def test_link_problems_agreement_selects_status_then_class_then_default():
    def entry(ref):
        return {
            "application/json": {
                "schema": {"$ref": ref},
                "x-rdf-context": {"href": "https://h/c", "type": CONTEXT_TYPE},
            }
        }

    body = {
        "get": {
            "responses": {
                "404": {"content": entry("https://h/exact")},
                "4XX": {"content": entry("https://h/class")},
                "default": {"content": entry("https://h/default")},
            }
        }
    }
    for status, ref in [(404, "exact"), (400, "class"), (500, "default")]:
        link = f'<https://h/{ref}>; rel="describedby"; type="application/schema+json"'
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Link": f"{link}, {CONTEXT}",
        }
        problems = link_problems("GET", status, headers, True, body)
        assert not any("agree" in problem.lower() for problem in problems)
    wrong = {"Content-Type": "application/json", "Link": f"{SCHEMA}, {CONTEXT}"}
    assert link_problems("GET", 404, wrong, True, body)


def test_link_problems_agreement_without_entry():
    body = {"get": {"responses": {"200": {"description": "OK"}}}}
    assert link_problems("GET", 200, PERSON_GET_HEADERS, True, body)


# --- 37ec1ce rules ----------------------------------------------------------

HSR_LINK = (
    "<https://h.example/_hsr/item>;"
    ' rel="https://w3id.org/semantic-http/ns#http-schema-resource-document";'
    ' type="application/json"'
)
HSR_RESPONSE_LINKS = (
    "<https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json>;"
    ' rel="describedby"; type="application/schema+json", '
    "<https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld>;"
    ' rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)


def test_options_204_conformant():
    h = {"Allow": "GET, HEAD, OPTIONS", "Link": HSR_LINK}
    assert options_204_problems(204, h, b"", {"GET", "HEAD", "OPTIONS"}) == []


def test_options_204_rejects_body_and_200():
    h = {"Allow": "GET, OPTIONS", "Link": HSR_LINK, "Content-Type": "application/json"}
    problems = options_204_problems(200, h, b"{}")
    assert any("content" in p for p in problems) and any("200" in p for p in problems)


def test_options_204_allow_must_equal_expected():
    h = {"Allow": "GET, PUT, OPTIONS", "Link": HSR_LINK}
    assert options_204_problems(204, h, b"", {"GET", "HEAD", "OPTIONS"})


def test_hsr_link_problems_anchor_type_count():
    assert hsr_link_problems({"Link": HSR_LINK}) == []
    assert hsr_link_problems({"Link": HSR_LINK + '; anchor="#x"'})
    assert hsr_link_problems(
        {"Link": HSR_LINK.replace("application/json", "text/plain")}
    )
    assert hsr_link_problems({"Link": f"{HSR_LINK}, {HSR_LINK}"})
    assert hsr_link_problems({"Link": HSR_LINK}, "https://h.example/other")


def test_hsr_get_problems_person_fixture():
    body = (FIXTURES / "person.http-schema-resource.json").read_bytes()
    h = {
        "Content-Type": "application/json",
        "Link": HSR_RESPONSE_LINKS,
        "ETag": '"x"',
        "Cache-Control": "public, max-age=300",
    }
    assert hsr_get_problems(200, h, body) == []


def test_hsr_get_problems_wants_a_strong_etag():
    """The HSR's ETag is strong (Semantic HTTP 0.1): a weak one is a problem
    (the final review's M3)."""
    body = (FIXTURES / "person.http-schema-resource.json").read_bytes()
    h = {
        "Content-Type": "application/json",
        "Link": HSR_RESPONSE_LINKS,
        "ETag": 'W/"x"',
        "Cache-Control": "public, max-age=300",
    }
    assert hsr_get_problems(200, h, body) == ["HSR with a weak ETag 'W/\"x\"'"]
    assert hsr_get_problems(200, {**h, "ETag": 'w/"x"'}, body)
    assert hsr_get_problems(200, {**h, "ETag": '"x"'}, body) == []


def test_undescribed_problems():
    hsr = json.loads((FIXTURES / "person.http-schema-resource.json").read_text())
    html = {"Content-Type": "text/html; charset=utf-8"}
    assert undescribed_problems(None, html, hsr)
    assert undescribed_problems("application/json", html, hsr)
    assert undescribed_problems("text/html,*/*;q=0.8", html, hsr) == []
    assert undescribed_problems(None, {"Content-Type": "application/json"}, hsr) == []
