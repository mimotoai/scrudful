"""Tests for ``tests/semantic_http.py``, the Semantic HTTP 0.1 wire checks."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from scrudful._adapter import CONTEXT_TYPE, describe_options, options_links
from tests.semantic_http import (
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
    return {
        "Content-Type": "application/json",
        "Link": options_links(),
        "Allow": allow,
    }


def test_link_problems_options_conformant():
    body = {"get": {"responses": {}}, "options": describe_options()}
    assert link_problems("OPTIONS", 200, _options_headers(), True, body) == []


def test_link_problems_options_allow_lists_non_body_method():
    body = {"get": {"responses": {}}, "options": describe_options()}
    headers = _options_headers("GET, PUT, OPTIONS")
    problems = link_problems("OPTIONS", 200, headers, True, body)
    assert any("Allow" in problem for problem in problems)


def test_link_problems_options_wrong_targets_and_status():
    body = {"options": describe_options()}
    headers = {
        "Content-Type": "application/json",
        "Link": f"{SCHEMA}, {CONTEXT}",
        "Allow": "OPTIONS",
    }
    assert link_problems("OPTIONS", 200, headers, True, body)
    assert link_problems("OPTIONS", 204, _options_headers("OPTIONS"), False, body)


def test_link_problems_options_head_with_content():
    body = {
        "head": {
            "responses": {
                "200": {"content": PERSON["get"]["responses"]["200"]["content"]}
            }
        },
        "options": describe_options(),
    }
    headers = _options_headers("HEAD, OPTIONS")
    assert link_problems("OPTIONS", 200, headers, True, body)


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
