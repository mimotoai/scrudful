import ast
import copy
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from scrudful import collection
from scrudful.collection import collection_context, collection_schema

ITEM_URI = "https://x/item.json"


def _validator():
    registry = Registry().with_resource(
        ITEM_URI,
        Resource.from_contents(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
            }
        ),
    )
    return Draft202012Validator(
        collection_schema(ITEM_URI),
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _envelope():
    return {
        "count": 1,
        "page_count": 1,
        "first": "https://x/items?page=1",
        "previous": None,
        "next": None,
        "last": "https://x/items?page=1",
        "content": [
            {
                "href": "https://x/items/1",
                "etag": '"abc"',
                "last_modified": "Thu, 01 Jan 1970 00:00:00 GMT",
                "link": '<https://x/schema>; rel="describedby"',
                "content": {},
            }
        ],
    }


def test_field_constants():
    assert collection.ENVELOPE_FIELDS == (
        "href",
        "etag",
        "last_modified",
        "link",
        "content",
    )
    assert collection.PAGE_FIELDS == (
        "count",
        "page_count",
        "first",
        "previous",
        "next",
        "last",
        "content",
    )


def test_schema_is_valid_draft_2020_12():
    schema = collection_schema(ITEM_URI)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_schema_id_and_title():
    schema = collection_schema(ITEM_URI, schema_id="https://x/c.json", title="Roles")
    assert schema["$id"] == "https://x/c.json"
    assert schema["title"] == "Roles"
    assert "$id" not in collection_schema(ITEM_URI)
    assert "title" not in collection_schema(ITEM_URI)


def test_schema_validates_envelope():
    validator = _validator()
    validator.validate(_envelope())
    broken = _envelope()
    del broken["content"][0]["href"]
    assert not validator.is_valid(broken)


def test_schema_item_content_refs_item_schema():
    item = collection_schema(ITEM_URI)["properties"]["content"]["items"]
    assert item["properties"]["content"] == {"$ref": ITEM_URI}


def test_schema_requires_all_fields():
    schema = collection_schema(ITEM_URI)
    assert set(schema["required"]) == set(collection.PAGE_FIELDS)
    item = schema["properties"]["content"]["items"]
    assert set(item["required"]) == set(collection.ENVELOPE_FIELDS)


def test_schema_previous_next_nullable():
    validator = _validator()
    page = _envelope()
    page["previous"] = None
    page["next"] = None
    assert validator.is_valid(page)
    page["first"] = None
    assert not validator.is_valid(page)


def test_schema_item_etag_last_modified_link_nullable():
    validator = _validator()
    page = _envelope()
    for key in ("etag", "last_modified", "link"):
        page["content"][0][key] = None
    assert validator.is_valid(page)
    page["content"][0]["href"] = None
    assert not validator.is_valid(page)


def test_schema_counts_non_negative_integers():
    validator = _validator()
    for key in ("count", "page_count"):
        page = copy.deepcopy(_envelope())
        page[key] = -1
        assert not validator.is_valid(page)
        page[key] = 1.5
        assert not validator.is_valid(page)


def test_context_maps_fields():
    context = collection_context("https://x/Role", vocabulary="https://v/#")["@context"]
    for name in set(collection.PAGE_FIELDS) | set(collection.ENVELOPE_FIELDS):
        if name != "content":
            assert context[name] == "https://v/#" + name
    assert context["content"]["@id"] == "https://v/#content"
    assert context["content"]["@container"] == "@list"
    assert context["item_type"] == "https://x/Role"


def test_context_collection_iri():
    plain = collection_context("https://x/Role", vocabulary="https://v/#")
    assert "@type" not in plain["@context"]
    with_iri = collection_context(
        "https://x/Role", vocabulary="https://v/#", collection_iri="https://x/Roles"
    )
    assert with_iri["@context"]["@type"] == "https://x/Roles"


def test_context_requires_vocabulary():
    with pytest.raises(TypeError):
        collection_context("i")  # type: ignore[call-arg]


def test_stdlib_only():
    tree = ast.parse(Path(collection.__file__).read_text())
    allowed = set(sys.stdlib_module_names) | {"scrudful"}
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add((node.module or "").split(".")[0])
    assert roots <= allowed
