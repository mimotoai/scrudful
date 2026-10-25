"""JSON Schema and JSON-LD documents describing the scrud list envelope.

The envelope is a page of a collection::

    {"count", "page_count", "first", "previous", "next", "last",
     "content": [{"href", "etag", "last_modified", "link", "content": <item>}]}

This module is framework-neutral. Importing it needs only the standard library;
:func:`slice_page` loads Django by name, to recognise a ``QuerySet``.
"""

from __future__ import annotations

import importlib
import math
import warnings
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

# A Django ``QueryDict`` (not imported: this module stays stdlib-only).
QueryDict = Any

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
_URL_TERMS = ("href", "first", "previous", "next", "last")


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

    def uri(nullable: bool = False) -> dict[str, Any]:
        return {"type": ["string", "null"] if nullable else "string", "format": "uri"}

    def count() -> dict[str, Any]:
        return {"type": "integer", "minimum": 0}

    entry = {
        "type": "object",
        "properties": {
            "href": uri(),
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
                "count": count(),
                "page_count": count(),
                "first": uri(),
                "previous": uri(True),
                "next": uri(True),
                "last": uri(),
                "content": {"type": "array", "items": entry},
            },
            "required": list(PAGE_FIELDS),
        }
    )
    return schema


def collection_context(item_context_uri: str, *, vocabulary: str) -> dict[str, Any]:
    """Return the JSON-LD context of a list envelope.

    Parameters
    ----------
    item_context_uri
        URI of the JSON-LD context of one item. Each entry's ``content`` is
        expanded with it, so the item's fields survive expansion.
    vocabulary
        Prefix prepended to each field name to form its IRI (for example
        ``"https://example.com/vocab#"``). Required: no default is assumed.

    Notes
    -----
    The page's ``content`` is an ``@list`` of entries, preserving order. The
    URL-valued terms (``href``, ``first``, ``previous``, ``next``, ``last``)
    are typed ``@id``. The type of the collection and of its items belongs in
    the response body or the item context; a JSON-LD context cannot assert it.
    """
    context: dict[str, Any] = {"@version": 1.1}
    for name in dict.fromkeys(PAGE_FIELDS + ENVELOPE_FIELDS):
        if name == "content":
            continue
        if name in _URL_TERMS:
            context[name] = {"@id": vocabulary + name, "@type": "@id"}
        else:
            context[name] = vocabulary + name
    context["content"] = {
        "@id": vocabulary + "content",
        "@container": "@list",
        "@context": {
            "content": {"@id": vocabulary + "content", "@context": item_context_uri}
        },
    }
    return {"@context": context}


class InvalidPage(Exception):
    """A page number past the last page (page 1 always exists)."""


def slice_page(items: Any, number: int, size: int) -> tuple[int, list[Any]]:
    """The item count of ``items`` and the items of page ``number``.

    ``count()`` for a ``QuerySet``, else ``len()``. An unordered ``QuerySet``
    gets Django's ``UnorderedObjectListWarning``, as with its ``Paginator``.

    Raises
    ------
    InvalidPage
        For a page past the last one (page 1 always exists).
    """
    # Resolved by name, not imported: this module stays stdlib-only.
    paginator = importlib.import_module("django.core.paginator")
    queryset_type = importlib.import_module("django.db.models").QuerySet

    if isinstance(items, queryset_type):
        if not items.ordered:
            warnings.warn(
                "Pagination may yield inconsistent results with an unordered "
                f"object_list: {items.model} {items.__class__.__name__}.",
                paginator.UnorderedObjectListWarning,
                stacklevel=2,
            )
        count = items.count()
    else:
        count = len(items)
    if number > max(math.ceil(count / size), 1):
        raise InvalidPage("Invalid page.")
    start = (number - 1) * size
    return count, list(items[start : start + size])


def page_url(url: str, query: QueryDict, number: int) -> str:
    """``url`` with the query ``query`` (a ``QueryDict``) and ``page=number``.

    The parameters are sorted by name, as DRF's ``replace_query_param`` does.
    The query comes from ``request.GET``, not from ``url``: they are the same
    in Django, but ninja's ``TestClient`` builds URLs without a query.
    """
    query = query.copy()
    query.setlist("page", [str(number)])
    scheme, netloc, path, _, _ = urlsplit(url)
    encoded = urlencode(sorted(query.lists()), doseq=True)
    return urlunsplit((scheme, netloc, path, encoded, ""))


def envelope_entry(
    *,
    href: str,
    etag: str | None,
    last_modified: str | None,
    link: str | None,
    content: Any,
) -> dict[str, Any]:
    """One entry of a page's ``content`` array, keyed by ``ENVELOPE_FIELDS``."""
    values = (href, etag, last_modified, link, content)
    return dict(zip(ENVELOPE_FIELDS, values))


def page_envelope(
    *,
    count: int,
    number: int,
    size: int,
    url: str,
    query: QueryDict,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """The page ``number`` of ``count`` items, keyed by ``PAGE_FIELDS``."""
    page_count = math.ceil(count / size)

    def link(n: int) -> str:
        return page_url(url, query, n)

    values = (
        count,
        page_count,
        link(1),
        link(number - 1) if number > 1 else None,
        link(number + 1) if number < page_count else None,
        link(max(page_count, 1)),
        entries,
    )
    return dict(zip(PAGE_FIELDS, values))
