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


def test_precondition_headers_keeps_empty_field_lines():
    # A present but empty field is not the same as a missing one.
    assert precondition_headers(lambda name: [""] if name == "if-match" else []) == {
        "if-match": ""
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
    from scrudful import fastapi, rest_framework

    for shared, fastapi_responses, drf_responses in (
        (
            _adapter.READ_DESCRIPTIONS,
            fastapi.CONDITIONAL_READ_RESPONSES,
            rest_framework._READ_RESPONSES,
        ),
        (
            _adapter.WRITE_DESCRIPTIONS,
            fastapi.CONDITIONAL_WRITE_RESPONSES,
            rest_framework._WRITE_RESPONSES,
        ),
    ):
        expected = {status: {"description": text} for status, text in shared.items()}
        assert fastapi_responses == expected
        assert drf_responses == {str(k): v for k, v in expected.items()}
