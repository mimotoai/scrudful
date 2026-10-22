from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote

# Response headers a browser client may need to read (CORS).
EXPOSABLE_HEADERS = ("ETag", "Link", "Location")

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


def get_string_or_evaluate(string_or_func, *args, **kwargs):
    if not string_or_func:
        return None
    if isinstance(string_or_func, str):
        return string_or_func
    return string_or_func(*args, **kwargs)
