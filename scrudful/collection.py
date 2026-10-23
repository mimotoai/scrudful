"""JSON Schema and JSON-LD documents describing the scrud list envelope.

The envelope is a page of a collection::

    {"count", "page_count", "first", "previous", "next", "last",
     "content": [{"href", "etag", "last_modified", "link", "content": <item>}]}

This module is framework-neutral and depends only on the standard library.
"""

from __future__ import annotations

from typing import Any

# Keys of one entry of a page's ``content`` array.
ENVELOPE_FIELDS = ("href", "etag", "last_modified", "link", "content")
# Keys of a page.
PAGE_FIELDS = (
    "count",
    "page_count",
    "first",
    "previous",
    "next",
    "last",
    "content",
)

_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_URI = {"type": "string", "format": "uri"}
_NULLABLE_URI = {"type": ["string", "null"], "format": "uri"}
_COUNT = {"type": "integer", "minimum": 0}


def collection_schema(
    item_schema_uri: str,
    *,
    schema_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) of a list envelope.

    Parameters
    ----------
    item_schema_uri
        URI of the schema of one item; referenced with ``$ref`` from each
        entry's ``content``.
    schema_id
        Optional ``$id`` of the returned schema.
    title
        Optional ``title`` of the returned schema.

    Notes
    -----
    Every field is required. ``previous`` and ``next`` may be ``null``, as may
    an entry's ``etag``, ``last_modified`` and ``link``, which are optional in
    the adapter.
    """
    entry = {
        "type": "object",
        "properties": {
            "href": _URI,
            "etag": {"type": ["string", "null"]},
            "last_modified": {"type": ["string", "null"]},
            "link": {"type": ["string", "null"]},
            "content": {"$ref": item_schema_uri},
        },
        "required": list(ENVELOPE_FIELDS),
    }
    schema: dict[str, Any] = {"$schema": _DRAFT}
    if schema_id is not None:
        schema["$id"] = schema_id
    if title is not None:
        schema["title"] = title
    schema.update(
        {
            "type": "object",
            "properties": {
                "count": _COUNT,
                "page_count": _COUNT,
                "first": _URI,
                "previous": _NULLABLE_URI,
                "next": _NULLABLE_URI,
                "last": _URI,
                "content": {"type": "array", "items": entry},
            },
            "required": list(PAGE_FIELDS),
        }
    )
    return schema


def collection_context(
    item_type_iri: str,
    *,
    vocabulary: str,
    collection_iri: str | None = None,
) -> dict[str, Any]:
    """Return the JSON-LD context of a list envelope.

    Parameters
    ----------
    item_type_iri
        IRI of the type of the items; exposed as the ``item_type`` term.
    vocabulary
        Prefix prepended to each field name to form its IRI (for example
        ``"https://example.com/vocab#"``). Required: no default is assumed.
    collection_iri
        Optional IRI used as the ``@type`` of the collection.

    Notes
    -----
    ``content`` is mapped as an ``@list`` container, preserving item order.
    """
    context: dict[str, Any] = {}
    if collection_iri is not None:
        context["@type"] = collection_iri
    context["item_type"] = item_type_iri
    for name in dict.fromkeys(PAGE_FIELDS + ENVELOPE_FIELDS):
        context[name] = vocabulary + name
    context["content"] = {"@id": vocabulary + "content", "@container": "@list"}
    return {"@context": context}
