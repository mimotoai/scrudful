from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote, urljoin

# Response headers a browser client may need to read (CORS), in the order
# they are appended to Access-Control-Expose-Headers. ``Link`` and ``Allow``
# are always exposed; ``ETag`` and ``Location`` only when the response has them.
EXPOSE_HEADERS = ("ETag", "Link", "Location", "Allow")

# Deprecated alias of EXPOSE_HEADERS, removed with the old OPTIONS helpers.
EXPOSABLE_HEADERS = EXPOSE_HEADERS

# RFC 3986 reserved characters plus '%' (unreserved characters are always kept
# by ``urllib.parse.quote``).
_URI_SAFE = ":/?#[]@!$&'()*+,;=%"
_CTL_RE = re.compile("[\x00-\x1f\x7f]")
_NON_LATIN1_RE = re.compile("[^\x00-\xff]")


def _quoted_string(value: str) -> str:
    if _CTL_RE.search(value):
        raise ValueError(f"{value!r} contains control characters")
    if _NON_LATIN1_RE.search(value):
        raise ValueError(f"{value!r} contains characters outside Latin-1")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def link_content(url: object, rel: object, content_type: object | None = None) -> str:
    """Return one RFC 8288 link-value: ``<url>; rel="rel"; type="type"``.

    Characters outside the RFC 3986 reserved/unreserved set (and ``%``) are
    percent-encoded in ``url`` (non-ASCII as UTF-8). ``rel`` and
    ``content_type`` are emitted as quoted-strings; ``ValueError`` is raised if
    they contain control characters or characters outside Latin-1. The ``type``
    parameter is omitted when ``content_type`` is empty. Non-``str`` arguments
    (e.g. a Starlette ``URL`` or a lazy string) are converted with ``str()``.
    """
    value = f"<{quote(str(url), safe=_URI_SAFE)}>; rel={_quoted_string(str(rel))}"
    if content_type:
        value += f"; type={_quoted_string(str(content_type))}"
    return value


def join_links(*values: str | None) -> str | None:
    """Join link-values into one Link field value, skipping empty ones."""
    return ", ".join(value for value in values if value) or None


def merge_expose_headers(existing: str | None, names: Iterable[str]) -> str | None:
    """Merge header ``names`` into an Access-Control-Expose-Headers value.

    Names are compared case-insensitively; the existing order is kept and new
    names are appended in the given order. Return ``None`` if the result is
    empty.
    """
    merged: list[str] = []
    seen: set[str] = set()
    candidates = (existing or "").split(",")
    for name in (*candidates, *names):
        name = name.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            merged.append(name)
    return ", ".join(merged) or None


def resolve_uri(url: object, base: str) -> str:
    """Resolve ``url`` against the absolute URI ``base``, as a browser would.

    Absolute values are unchanged. Non-``str`` arguments (e.g. a Starlette
    ``URL`` or a lazy string) are converted with ``str()``.
    """
    return urljoin(base, str(url))


def _split_top_level(value: str, separator: str) -> list[str]:
    """Split ``value`` on ``separator`` outside ``<...>`` and quoted strings."""
    parts = []
    start = 0
    in_angle = in_quotes = escaped = False
    for index, char in enumerate(value):
        if in_quotes:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quotes = False
        elif in_angle:
            in_angle = char != ">"
        elif char == '"':
            in_quotes = True
        elif char == "<":
            in_angle = True
        elif char == separator:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _unquote(value: str) -> str | None:
    """The text of a token or quoted-string parameter value, else ``None``."""
    value = value.strip(" \t")
    if not value.startswith('"'):
        return value
    if len(value) < 2 or not value.endswith('"'):
        return None
    return re.sub(r"\\(.)", r"\1", value[1:-1])


def link_relations(field_value: str | None) -> set[str]:
    """The lower-cased relation types of the links in a Link field value.

    Every link-value of the RFC 8288 field value is read: its ``rel`` may hold
    several space-separated types, as a quoted-string or a token. Commas inside
    ``<...>`` or quoted strings do not separate link-values. Malformed
    link-values are skipped; this never raises.
    """
    relations: set[str] = set()
    for link in _split_top_level(field_value or "", ","):
        link = link.strip(" \t")
        if not link.startswith("<") or ">" not in link:
            continue
        params = _split_top_level(link[link.index(">") + 1 :], ";")[1:]
        for param in params:
            name, _, value = param.partition("=")
            if name.strip(" \t").lower() == "rel":
                # RFC 8288 §3.4.1: only the first rel parameter counts.
                relations.update((_unquote(value) or "").lower().split())
                break
    return relations
