"""Unit tests for ``scrudful._adapter``, the glue shared by both adapters."""

import subprocess
import sys

import pytest

from scrudful import _adapter
from scrudful._adapter import (
    check_validators,
    expose_headers,
    precondition_headers,
    set_validator_headers,
    validator_policy,
)

# --- framework independence ------------------------------------------------


def test_imports_no_framework():
    code = (
        "import sys, scrudful._adapter; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'django', 'rest_framework', 'starlette', 'fastapi', 'pydantic'}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


# --- link defaults -----------------------------------------------------------


@pytest.mark.parametrize("module", ["scrudful.fastapi", "scrudful.rest_framework"])
def test_adapters_expose_link_defaults(module):
    adapter = __import__(module, fromlist=["_"])
    for name, value in {
        "DEFAULT_SCHEMA_REL": "describedBy",
        "DEFAULT_SCHEMA_TYPE": "application/json",
        "DEFAULT_CONTEXT_REL": "http://www.w3.org/ns/json-ld#context",
        "DEFAULT_CONTEXT_TYPE": "application/ld+json",
    }.items():
        assert getattr(_adapter, name) == value
        assert getattr(adapter, name) == value


# --- check_validators --------------------------------------------------------


def test_check_validators_accepts_none_and_callables():
    check_validators(None, None)
    check_validators(lambda: "x", None)
    check_validators(None, print)


@pytest.mark.parametrize(
    "args, message",
    [
        (("abc", None), "etag_func must be callable, not str"),
        ((None, 3), "last_modified_func must be callable, not int"),
        (("abc", 3), "etag_func must be callable, not str"),
    ],
)
def test_check_validators_rejects_non_callables(args, message):
    with pytest.raises(TypeError) as excinfo:
        check_validators(*args)
    assert str(excinfo.value) == message


# --- precondition_headers ----------------------------------------------------


def test_precondition_headers_joins_repeated_lines():
    lines = {
        "if-match": ['"a"', '"b"'],
        "if-none-match": ["*"],
        "if-modified-since": [],
        "if-unmodified-since": ["Wed, 21 Oct 2015 07:28:00 GMT"],
        "content-type": ["application/json"],
    }
    assert precondition_headers(lambda name: lines.get(name, [])) == {
        "if-match": '"a", "b"',
        "if-none-match": "*",
        "if-unmodified-since": "Wed, 21 Oct 2015 07:28:00 GMT",
    }


def test_precondition_headers_drops_empty_field_lines():
    # An empty (or blank) field is the same as an absent one, as in the core.
    values = {"if-match": ["", " \t"], "if-none-match": ["", '"a"', ""]}
    assert precondition_headers(lambda name: values.get(name, [])) == {
        "if-none-match": '"a"'
    }


def test_precondition_headers_none_present():
    assert precondition_headers(lambda name: []) == {}


# --- validator_policy --------------------------------------------------------


@pytest.mark.parametrize(
    "method, status, expected",
    [
        ("GET", 200, (False, True)),
        ("HEAD", 204, (False, True)),
        ("PUT", 200, (True, True)),
        ("PATCH", 299, (True, True)),
        ("POST", 201, (False, False)),
        ("DELETE", 204, (False, False)),
        ("GET", 304, (False, False)),
        ("GET", 199, (False, False)),
        ("PUT", 300, (False, False)),
        ("PATCH", 404, (False, False)),
    ],
)
def test_validator_policy(method, status, expected):
    assert validator_policy(method, status) == expected


# --- set_validator_headers ---------------------------------------------------


def test_set_validator_headers_adds_missing():
    headers = {}
    set_validator_headers(headers, '"v1"', 0)
    assert headers == {"ETag": '"v1"', "Last-Modified": "Thu, 01 Jan 1970 00:00:00 GMT"}


def test_set_validator_headers_keeps_existing():
    headers = {"ETag": '"own"'}
    set_validator_headers(headers, '"v1"', None)
    assert headers == {"ETag": '"own"'}


def test_set_validator_headers_without_validators():
    headers = {}
    set_validator_headers(headers, None, None)
    assert headers == {}


# --- expose_headers ----------------------------------------------------------


def test_expose_headers_lists_present_headers():
    headers = {"ETag": '"v1"', "Location": "/x", "Content-Type": "text/plain"}
    expose_headers(headers)
    assert headers["Access-Control-Expose-Headers"] == "ETag, Location"


def test_expose_headers_merges_existing_value():
    headers = {"Link": "<x>", "Access-Control-Expose-Headers": "X-Custom, etag"}
    expose_headers(headers)
    assert headers["Access-Control-Expose-Headers"] == "X-Custom, etag, Link"


def test_expose_headers_nothing_to_expose():
    headers = {"Access-Control-Expose-Headers": "X-Custom"}
    expose_headers(headers)
    assert headers == {"Access-Control-Expose-Headers": "X-Custom"}
    headers = {}
    expose_headers(headers)
    assert headers == {}


# --- response descriptions ---------------------------------------------------


def test_response_descriptions():
    malformed = "Malformed If-Match or If-None-Match header."
    assert _adapter.READ_DESCRIPTIONS == {
        304: "Not Modified: the representation matches the request's "
        "If-None-Match or If-Modified-Since validators.",
        400: malformed,
        412: "Precondition Failed.",
    }
    assert _adapter.WRITE_DESCRIPTIONS == {
        400: malformed,
        412: "Precondition Failed.",
        428: "Precondition Required: this request must be conditional "
        "(send If-Match or If-Unmodified-Since).",
    }


def test_adapters_render_shared_descriptions():
    from scrudful import fastapi

    for shared, fastapi_responses, drf_responses in (
        (
            _adapter.READ_DESCRIPTIONS,
            fastapi.CONDITIONAL_READ_RESPONSES,
            _adapter.READ_RESPONSES,
        ),
        (
            _adapter.WRITE_DESCRIPTIONS,
            fastapi.CONDITIONAL_WRITE_RESPONSES,
            _adapter.WRITE_RESPONSES,
        ),
    ):
        expected = {status: {"description": text} for status, text in shared.items()}
        assert fastapi_responses == expected
        assert drf_responses == {str(k): v for k, v in expected.items()}


# --- OPTIONS descriptions ----------------------------------------------------

SCHEMA = "http://example.com/schema.json"
CONTEXT = "http://example.com/context.jsonld"
ITEM_CONTENT = {"application/json": {"schema": SCHEMA, "context": CONTEXT}}
ETAG_PARAMETER = {
    "in": "header",
    "name": "If-Match",
    "schema": {"type": "string"},
    "required": True,
    "description": "The entity-tag of the current representation.",
}
LAST_MODIFIED_PARAMETER = {
    "in": "header",
    "name": "If-Unmodified-Since",
    "schema": {"type": "string"},
    "required": True,
    "description": "The Last-Modified date of the current representation.",
}
EITHER = " Either If-Match or If-Unmodified-Since is required."
BOTH_PARAMETERS = [
    {**ETAG_PARAMETER, "required": False},
    {**LAST_MODIFIED_PARAMETER, "required": False},
]
for _parameter in BOTH_PARAMETERS:
    _parameter["description"] += EITHER
READ_EARLY = {
    "304": {
        "description": "Not Modified: the representation matches the request's "
        "If-None-Match or If-Modified-Since validators."
    },
    "400": {"description": "Malformed If-Match or If-None-Match header."},
    "412": {"description": "Precondition Failed."},
}
WRITE_EARLY = {
    "400": {"description": "Malformed If-Match or If-None-Match header."},
    "412": {"description": "Precondition Failed."},
    "428": {
        "description": "Precondition Required: this request must be conditional "
        "(send If-Match or If-Unmodified-Since)."
    },
}


def test_adapter_describe_matches_drf():
    """The ``ScrudfulMetadata`` expectations of ``tests/test_rest_framework.py``."""
    describe_read = _adapter.describe_read
    describe_write = _adapter.describe_write
    # test_viewset_metadata_detail_and_list, test_metadata_for_action_routes
    assert describe_read(ITEM_CONTENT, True) == {
        "responses": {"200": {"description": "OK", "content": ITEM_CONTENT}}
        | READ_EARLY
    }
    list_content = {
        "application/json": {"schema": "http://example.com/list-schema.json"}
    }
    assert describe_read(list_content, False) == {
        "responses": {"200": {"description": "OK", "content": list_content}}
    }
    assert describe_read(None, False) == {"responses": {"200": {"description": "OK"}}}
    assert describe_read({}, False) == {"responses": {"200": {"description": "OK"}}}
    # the list route's create, without and with validators
    # (test_explicit_post_validators_are_evaluated)
    post_body = {
        "description": "The content for the resource to be created.",
        "required": True,
    }
    assert describe_write("post", None, False, False, False) == {
        "requestBody": post_body,
        "responses": {"201": {"description": "Created"}},
    }
    assert describe_write("post", ITEM_CONTENT, True, True, True) == {
        "requestBody": {**post_body, "content": ITEM_CONTENT},
        "responses": {
            "201": {"description": "Created"},
            "400": WRITE_EARLY["400"],
            "412": WRITE_EARLY["412"],
        },
    }
    # test_metadata_describes_non_scrudful_methods
    put_body = {"description": "The new content of the resource.", "required": True}
    assert describe_write("put", None, False, False, False) == {
        "requestBody": put_body,
        "responses": {"200": {"description": "OK"}},
    }
    # test_api_view_options_metadata (etag only)
    assert describe_write("put", ITEM_CONTENT, True, True, False) == {
        "requestBody": {**put_body, "content": ITEM_CONTENT},
        "responses": {"200": {"description": "OK"}} | WRITE_EARLY,
        "parameters": [ETAG_PARAMETER],
    }
    # test_metadata_parameters_for_a_single_validator
    assert describe_write("patch", None, True, False, True) == {
        "requestBody": {
            "description": "The changes to apply to the resource.",
            "required": True,
        },
        "responses": {"200": {"description": "OK"}} | WRITE_EARLY,
        "parameters": [LAST_MODIFIED_PARAMETER],
    }
    assert describe_write("put", None, True, True, True)["parameters"] == (
        BOTH_PARAMETERS
    )
    assert describe_write("delete", ITEM_CONTENT, False, False, False) == {
        "responses": {"204": {"description": "No Content"}}
    }
    assert describe_write("delete", None, True, True, True) == {
        "responses": {"204": {"description": "No Content"}} | WRITE_EARLY,
        "parameters": BOTH_PARAMETERS,
    }


def test_conditional_header_parameters():
    parameters = _adapter.conditional_header_parameters
    assert parameters(False, False) is None
    assert parameters(True, False) == [ETAG_PARAMETER]
    assert parameters(False, True) == [LAST_MODIFIED_PARAMETER]
    assert parameters(True, True) == BOTH_PARAMETERS


def test_describe_rejects_unknown_methods():
    with pytest.raises(ValueError, match="'get'"):
        _adapter.describe_write("get", None, False, False, False)


def test_descriptions_do_not_share_the_module_dictionaries():
    for describe in (
        lambda: _adapter.describe_read(None, True),
        lambda: _adapter.describe_write("put", None, True, True, False),
    ):
        first = describe()
        for response in first["responses"].values():
            response["description"] = "mutated"
        first["parameters"] = None
        assert describe()["responses"]["412"] == {"description": "Precondition Failed."}
