"""Unit tests for ``scrudful._adapter``, the glue shared by both adapters."""

import subprocess
import sys

import pytest

from scrudful import _adapter
from scrudful._adapter import (
    CONTEXT_TYPE,
    HSR_CONTEXT_URL,
    HSR_SCHEMA_URL,
    JSON_LD_CONTEXT_REL,
    RDF_CONTEXT_REL,
    SCHEMA_TYPE,
    allow_header,
    check_validators,
    context_defaults,
    describe_content,
    describe_options,
    describe_read,
    describe_write,
    essence,
    expose_headers,
    is_in_band,
    is_json,
    media_type_key,
    merge_links,
    options_links,
    precondition_headers,
    set_validator_headers,
    validator_policy,
)
from scrudful.utils import link_content
from tests.semantic_http import validate_http_schema_resource

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
        "DEFAULT_SCHEMA_REL": "describedby",
        "DEFAULT_SCHEMA_TYPE": "application/schema+json",
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


# --- Semantic HTTP 0.1 vocabulary -------------------------------------------

SCHEMA_LINK = link_content("https://h/s", "describedby", SCHEMA_TYPE)
CONTEXT_LINK = link_content("https://h/c", JSON_LD_CONTEXT_REL, CONTEXT_TYPE)
IN_BAND = [
    "application/ld+json",
    "text/turtle",
    "application/n-triples",
    "application/n-quads",
    "application/trig",
    "application/rdf+xml",
]


def test_defaults_are_0_1():
    assert _adapter.DEFAULT_SCHEMA_REL == "describedby"
    assert _adapter.DEFAULT_SCHEMA_TYPE == "application/schema+json"


def test_merge_links_view_describedby_drops_schema_link():
    view = '<https://h/v>; rel="DescribedBy"'
    assert merge_links(view, SCHEMA_LINK, CONTEXT_LINK) == (f"{view}, {CONTEXT_LINK}")


def test_merge_links_other_relation_keeps_both():
    view = '<https://h/n>; rel="next"'
    assert merge_links(view, SCHEMA_LINK, CONTEXT_LINK) == (
        f"{view}, {SCHEMA_LINK}, {CONTEXT_LINK}"
    )


@pytest.mark.parametrize("rel", [JSON_LD_CONTEXT_REL, RDF_CONTEXT_REL])
def test_merge_links_view_context_drops_context_link(rel):
    view = link_content("https://h/v", rel, "text/turtle")
    assert merge_links(view, SCHEMA_LINK, CONTEXT_LINK) == f"{view}, {SCHEMA_LINK}"


def test_merge_links_nothing():
    assert merge_links(None, None, None) is None


def test_media_type_key():
    assert (
        media_type_key(" Application/JSON; charset=UTF-8 ")
        == "application/json; charset=UTF-8"
    )
    assert media_type_key("Text/CSV") == "text/csv"


@pytest.mark.parametrize("value", ["text", "/json", "text/", ""])
def test_media_type_key_rejects_malformed(value):
    with pytest.raises(ValueError):
        media_type_key(value)


def test_essence():
    assert essence("Application/JSON; charset=UTF-8") == "application/json"


@pytest.mark.parametrize(
    "media_type, expected",
    [
        ("application/json", True),
        ("application/problem+json", True),
        ("application/json; charset=utf-8", True),
        ("Application/JSON", True),
        ("application/ld+json", False),
        ("text/csv", False),
    ],
)
def test_is_json(media_type, expected):
    assert is_json(media_type) is expected


@pytest.mark.parametrize("media_type", IN_BAND)
def test_is_in_band(media_type):
    assert is_in_band(media_type)
    assert is_in_band(media_type.upper())
    assert is_in_band(f"{media_type}; charset=utf-8")


def test_is_not_in_band():
    assert not is_in_band("application/json")
    assert not is_in_band("text/csv")


def test_context_defaults():
    assert context_defaults("application/json") == (JSON_LD_CONTEXT_REL, CONTEXT_TYPE)
    assert context_defaults("application/problem+json; charset=utf-8") == (
        JSON_LD_CONTEXT_REL,
        CONTEXT_TYPE,
    )
    assert context_defaults("text/turtle") == (None, None)
    assert context_defaults("application/ld+json") == (None, None)
    assert context_defaults("text/csv") == (RDF_CONTEXT_REL, None)


def test_describe_content_json_schema():
    assert describe_content(
        "application/json", "https://h/s", SCHEMA_TYPE, "https://h/c", None
    ) == {
        "application/json": {
            "schema": {"$ref": "https://h/s"},
            "x-rdf-context": {"href": "https://h/c", "type": "application/ld+json"},
        }
    }


def test_describe_content_x_schema():
    content = describe_content(
        "application/xml", "https://h/s", "application/xml", "https://h/c", "text/x"
    )
    assert content == {
        "application/xml": {
            "x-schema": {"href": "https://h/s", "type": "application/xml"},
            "x-rdf-context": {"href": "https://h/c", "type": "text/x"},
        }
    }


def test_describe_content_keeps_parameters():
    content = describe_content(
        "Application/JSON; charset=UTF-8", "https://h/s", SCHEMA_TYPE, None, None
    )
    assert content == {
        "application/json; charset=UTF-8": {"schema": {"$ref": "https://h/s"}}
    }


def test_describe_content_in_band():
    content = describe_content(
        "application/ld+json", "https://h/s", SCHEMA_TYPE, "https://h/c", None
    )
    assert content == {"application/ld+json": {"schema": {"$ref": "https://h/s"}}}


def test_describe_content_no_schema():
    assert describe_content("application/json", None, SCHEMA_TYPE, "x", None) is None
    assert describe_content("application/json", "", SCHEMA_TYPE, None, None) is None


CONTENT = {"application/json": {"schema": {"$ref": "https://h/s"}}}
ERRORS = {"application/problem+json": {"schema": {"$ref": "https://h/e"}}}


def test_describe_write_omits_request_body_without_content():
    metadata = describe_write("put", None, False, False, False)
    assert "requestBody" not in metadata
    with_content = describe_write("put", CONTENT, False, False, False)
    assert with_content["requestBody"]["content"] == CONTENT


def test_describe_write_response_content():
    put = describe_write("put", CONTENT, False, False, False, response_content=CONTENT)
    assert put["responses"]["200"]["content"] == CONTENT
    post = describe_write("post", None, False, False, False, response_content=CONTENT)
    assert post["responses"]["201"]["content"] == CONTENT
    delete = describe_write("delete", None, False, False, False, None, CONTENT)
    assert "content" not in delete["responses"]["204"]
    multiple = describe_write(
        "post", None, False, False, False, [200, 202, 204], CONTENT
    )
    assert "content" in multiple["responses"]["200"]
    assert "content" in multiple["responses"]["202"]
    assert "content" not in multiple["responses"]["204"]


def test_describe_error_content():
    read = describe_read(None, True, error_content=ERRORS)
    assert read["responses"]["400"]["content"] == ERRORS
    assert read["responses"]["412"]["content"] == ERRORS
    assert "content" not in read["responses"]["304"]
    put = describe_write("put", None, True, True, False, error_content=ERRORS)
    for code in ("400", "412", "428"):
        assert put["responses"][code]["content"] == ERRORS
    post = describe_write("post", None, True, True, False, error_content=ERRORS)
    assert post["responses"]["412"]["content"] == ERRORS
    assert "428" not in post["responses"]


def test_describe_error_content_not_shared_between_calls():
    first = describe_read(None, True, error_content=ERRORS)
    first["responses"]["400"]["content"].clear()
    assert ERRORS


def test_describe_options_valid():
    body = {"options": describe_options()}
    validate_http_schema_resource(body)
    assert body["options"]["responses"]["200"]["content"] == {
        "application/json": {
            "schema": {"$ref": HSR_SCHEMA_URL},
            "x-rdf-context": {"href": HSR_CONTEXT_URL, "type": CONTEXT_TYPE},
        }
    }


def test_describe_options_is_fresh():
    describe_options()["responses"].clear()
    assert describe_options()["responses"]


def test_allow_header():
    body = {"get": {}, "head": {}, "options": {}, "x-a": 1}
    assert allow_header(body) == "GET, HEAD, OPTIONS"


def test_options_links():
    assert options_links() == (
        "<https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json>;"
        ' rel="describedby"; type="application/schema+json", '
        "<https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld>;"
        ' rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )
