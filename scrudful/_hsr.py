"""The HTTP Schema Resource (HSR) vocabulary and helpers shared by the adapters.

Private, and stdlib-only like :mod:`scrudful._adapter`: the adapters serve the
HSR and answer OPTIONS (Semantic HTTP 0.1) with these helpers, and keep
everything that touches their framework.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from scrudful._adapter import (
    CONTEXT_TYPE,
    HSR_CONTEXT_URL,
    HSR_LINK_REL,
    HSR_LINK_TYPE,
    HSR_SCHEMA_URL,
    JSON_LD_CONTEXT_REL,
    METHOD_KEYS,
    SCHEMA_REL,
    SCHEMA_TYPE,
    HsrConfigError,
)
from scrudful.utils import _NON_LATIN1_RE, link_content, resolve_uri

__all__ = [
    "DEFAULT_HSR_CACHE_CONTROL",
    "HSR_LINK_REL",
    "HSR_LINK_TYPE",
    "HSR_ROUTE_SEGMENT",
    "PROBLEM_DETAILS_CONTEXT_URL",
    "PROBLEM_DETAILS_SCHEMA_URL",
    "HsrConfigError",
    "absolute_for_hsr",
    "allow_value",
    "check_base_url",
    "check_hsr_cache_control",
    "check_hsr_url_result",
    "hsr_etag",
    "hsr_link",
    "hsr_response_links",
    "hsr_route_name",
    "is_preflight",
    "options_operation",
    "serialize_hsr",
]

PROBLEM_DETAILS_SCHEMA_URL = (
    "https://w3id.org/semantic-http/0.1/problem-details.schema.json"
)
PROBLEM_DETAILS_CONTEXT_URL = (
    "https://w3id.org/semantic-http/0.1/problem-details.context.jsonld"
)
DEFAULT_HSR_CACHE_CONTROL = "public, max-age=300"
#: The control characters no field value may hold: all but HTAB (RFC 9110).
_FIELD_CTL_RE = re.compile("[\x00-\x08\x0a-\x1f\x7f]")
#: The path segment the HSR routes live under.
HSR_ROUTE_SEGMENT = "_hsr"

_PARAMETER = re.compile(r"\{(?:[^{}:]*:)?([^{}]*)\}")


def options_operation() -> dict[str, Any]:
    """The HSR's ``options`` operation: a 204 without content (a fresh dict)."""
    return {"responses": {"204": {"description": "No Content"}}}


def allow_value(methods: Iterable[str]) -> str:
    """The ``Allow`` value for ``methods``.

    ``OPTIONS`` is always allowed. ``HEAD`` is listed only when it is in
    ``methods``, never inferred from ``GET``: ``Allow`` lists exactly the
    methods available, and a resource may answer HEAD with 405 (ninja does,
    unless an operation lists HEAD). The methods of
    :data:`~scrudful._adapter.METHOD_KEYS` come first, in that order, then any
    others sorted.
    """
    allowed = {method.upper() for method in methods}
    allowed.add("OPTIONS")
    known = [key.upper() for key in METHOD_KEYS]
    ordered = [method for method in known if method in allowed]
    ordered += sorted(allowed - set(known))
    return ", ".join(ordered)


def hsr_link(url: str) -> str:
    """The Link value pointing at the HTTP Schema Resource at ``url``."""
    return link_content(url, HSR_LINK_REL, HSR_LINK_TYPE)


def hsr_response_links() -> str:
    """The Link field value of an HSR response: its own schema and context."""
    schema = link_content(HSR_SCHEMA_URL, SCHEMA_REL, SCHEMA_TYPE)
    context = link_content(HSR_CONTEXT_URL, JSON_LD_CONTEXT_REL, CONTEXT_TYPE)
    return f"{schema}, {context}"


def serialize_hsr(body: Mapping[str, Any]) -> bytes:
    """The compact UTF-8 JSON of an HSR ``body``."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def hsr_etag(data: bytes) -> str:
    """A strong ETag for the serialized HSR ``data``."""
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'


def is_preflight(method: str, get_header: Callable[[str], str | None]) -> bool:
    """Whether the request is a CORS preflight: an OPTIONS with ``Origin`` and
    ``Access-Control-Request-Method``. ``get_header`` takes lowercase names."""
    return (
        method.upper() == "OPTIONS"
        and get_header("origin") is not None
        and get_header("access-control-request-method") is not None
    )


def absolute_for_hsr(url: str, base_url: str | None, what: str) -> str:
    """``url`` as an absolute URI for the HSR, which never depends on the request.

    A URL with a scheme is absolute (RFC 3986 section 4.3), with an
    authority or without one (``urn:example:schema``, ``tag:...``), and kept
    as is; any other is resolved against ``base_url``.

    Raises
    ------
    HsrConfigError
        If ``url`` is relative and there is no ``base_url``, or the result is
        not absolute (a ``base_url`` that is itself relative).
    """
    if urlsplit(url).scheme:
        return url
    if base_url:
        resolved = resolve_uri(url, base_url)
        if not urlsplit(resolved).scheme:
            raise HsrConfigError(
                f"{what} is the relative URL {url!r} and the base URL "
                f"{base_url!r} does not make it absolute: the result "
                f"{resolved!r} is not absolute"
            )
        return resolved
    raise HsrConfigError(
        f"{what} is the relative URL {url!r}; set a base URL "
        "(base_url=, or SCRUDFUL['BASE_URL'] for Django): the HTTP Schema "
        "Resource must not depend on the request"
    )


def hsr_route_name(path_template: str) -> str:
    """A route name for ``path_template``: its segments joined with ``.``.

    ``{name}`` and ``{converter:name}`` become ``name``; the empty path is
    ``"_root"``. It reads ninja's ``{converter:name}`` order only, in
    braces (Django's ``<converter:name>`` is left as it is; DRF names its
    HSR routes by URL name instead): Starlette's ``{name:converter}`` would
    give ``converter`` (the FastAPI adapter rewrites its templates first).
    """
    segments = [
        _PARAMETER.sub(r"\1", segment)
        for segment in path_template.split("/")
        if segment
    ]
    return ".".join(segments) or "_root"


def check_base_url(url: Any, what: str) -> None:
    """Check that ``url`` is an absolute URL with a host, as a base URL must be.

    The one check behind every base URL (``base_url=`` and
    ``SCRUDFUL["BASE_URL"]``). A value that is not a ``str`` is refused like a
    relative one. Each adapter calls this and re-raises the error as its own
    configuration error.

    Raises
    ------
    HsrConfigError
        If ``url`` is not a ``str`` with a scheme and a host, or can't be
        parsed as a URL at all (``urlsplit``'s ``ValueError``, as for
        ``"http://[::1"``).
    """
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
    except ValueError as exc:
        raise HsrConfigError(
            f"{what} must be an absolute URL, not {url!r} ({exc})"
        ) from exc
    if parts is None or not (parts.scheme and parts.netloc):
        raise HsrConfigError(f"{what} must be an absolute URL, not {url!r}")


def check_hsr_url_result(value: Any, what: str) -> str:
    """``value``, what an ``hsr_url(...)`` callable returned, if it is usable.

    The HTTP Schema Resource link must point at an absolute URL: a non-empty
    ``str`` with a scheme. Anything else would be sent as a broken link.

    Raises
    ------
    HsrConfigError
        If ``value`` is not a non-empty absolute ``str``.
    """
    if not (isinstance(value, str) and value and urlsplit(value).scheme):
        raise HsrConfigError(
            f"{what} must return an absolute URL (a str with a scheme), "
            f"not {value!r}"
        )
    return value


def check_hsr_cache_control(value: Any, what: str) -> str:
    """``value``, a ``Cache-Control`` value for the HTTP Schema Resource.

    Checked once, when given, as the Link rels and types are
    (:func:`scrudful.utils.link_content`): a value no field can carry would
    fail, or split the headers (a CR or LF), on every HSR response.

    Raises
    ------
    HsrConfigError
        If ``value`` is not a ``str``, is empty (an empty ``Cache-Control``
        field says nothing), or has characters a field can't carry: control
        characters (CR, LF, NUL ...; a tab is allowed, as RFC 9110's
        field values allow it), or characters outside Latin-1.
    """
    if not isinstance(value, str) or not value.strip():
        raise HsrConfigError(
            f"{what} must be a non-empty str of Cache-Control directives, "
            f"not {value!r}"
        )
    if _FIELD_CTL_RE.search(value):
        raise HsrConfigError(f"{what} {value!r} contains control characters")
    if _NON_LATIN1_RE.search(value):
        raise HsrConfigError(f"{what} {value!r} contains characters outside Latin-1")
    return value
