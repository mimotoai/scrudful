"""Framework-agnostic HTTP conditional-request evaluation (RFC 9110 §13).

Portions adapted from Django 6.1.2 (django/utils/http.py, django/utils/cache.py),
BSD-3-Clause; see LICENSES/Django-BSD-3-Clause.txt.

Changes from Django:

- The HTTP-date regular expressions are ASCII-only and anchored with
  ``fullmatch`` (Django's ``$`` also accepts a trailing newline), and
  ``parse_http_date`` returns ``None`` for invalid input instead of raising.
- Entity-tag lists are parsed with a tokenizer, so commas inside quoted
  opaque-tags no longer split a tag, and a malformed field is reported
  (``None``) instead of silently dropping its invalid members.
- ``quote_etag`` validates the opaque-tag against RFC 9110 ``etagc``.
- The precondition evaluation (``django.utils.cache.get_conditional_response``)
  is rewritten as ``evaluate_preconditions``: it returns a framework-neutral
  ``ConditionalResult`` instead of building responses, adds 400 for malformed
  entity-tag lists and 428 (RFC 6585 §3) for unconditional state-changing
  requests, treats a missing representation per RFC 9110 §13.2.1, compares
  timestamps with ``is not None`` checks (epoch 0 is a valid timestamp) and
  ignores If-Unmodified-Since when there is no modification date.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate
from typing import Any

__all__ = [
    "PRECONDITION_REQUIRED_METHODS",
    "SAFE_METHODS",
    "ConditionalResult",
    "evaluate_preconditions",
    "format_http_date",
    "http_timestamp",
    "parse_etag_list",
    "parse_http_date",
    "quote_etag",
    "validator_headers",
]

PRECONDITION_REQUIRED_METHODS: frozenset[str] = frozenset({"PUT", "PATCH", "DELETE"})
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

# RFC 9110 §8.8.3: etagc = %x21 / %x23-7E / obs-text (obs-text = %x80-FF).
_ETAGC = "\x21\x23-\x7e\x80-\xff"
_OPAQUE_TAG = f'"[{_ETAGC}]*"'
_ENTITY_TAG_RE = re.compile(f"(?:W/)?{_OPAQUE_TAG}")
_ETAGC_RE = re.compile(f"[{_ETAGC}]*")
_OWS = " \t"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# RFC 9110 §5.6.7 HTTP-date formats (adapted from django/utils/http.py).
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_D = r"(?P<day>[0-9]{2})"
_D2 = r"(?P<day>[ 0-9][0-9])"
_M = r"(?P<mon>[A-Za-z]{3})"
_Y = r"(?P<year>[0-9]{4})"
_Y2 = r"(?P<year>[0-9]{2})"
_T = r"(?P<hour>[0-9]{2}):(?P<min>[0-9]{2}):(?P<sec>[0-9]{2})"
_IMF_FIXDATE = re.compile(rf"[A-Za-z]{{3}}, {_D} {_M} {_Y} {_T} GMT")
_RFC850_DATE = re.compile(rf"[A-Za-z]{{6,9}}, {_D}-{_M}-{_Y2} {_T} GMT")
_ASCTIME_DATE = re.compile(rf"[A-Za-z]{{3}} {_M} {_D2} {_T} {_Y}")


def quote_etag(value: object) -> str:
    """Return ``value`` as an RFC 9110 entity-tag.

    A value that already is a valid entity-tag (``"..."`` or ``W/"..."``) is
    returned unchanged; anything else is wrapped in double quotes, making it a
    strong entity-tag. Raise ``ValueError`` if the result would not be a valid
    entity-tag (the opaque part contains DQUOTE, SP, a control character or a
    character outside Latin-1).
    """
    tag = str(value)
    if _ENTITY_TAG_RE.fullmatch(tag):
        return tag
    if not _ETAGC_RE.fullmatch(tag):
        raise ValueError(f"{tag!r} cannot be used as an entity-tag")
    return f'"{tag}"'


def parse_etag_list(value: str | None) -> list[str] | None:
    """Parse an If-Match / If-None-Match field value.

    Return ``[]`` when the field is absent or empty, ``["*"]`` for ``*``, and
    otherwise the entity-tags as sent (including any ``W/`` prefix). Return
    ``None`` if the field is present but malformed.
    """
    if value is None:
        return []
    value = value.strip(_OWS)
    # Empty list elements don't count (RFC 9110 §5.6.1.2), so "*," is "*".
    if value.strip(_OWS + ",") == "*":
        return ["*"]
    tags: list[str] = []
    pos, end = 0, len(value)
    while pos < end:
        # Skip OWS and empty list elements (RFC 9110 §5.6.1.2).
        while pos < end and value[pos] in _OWS + ",":
            pos += 1
        if pos == end:
            break
        match = _ENTITY_TAG_RE.match(value, pos)
        if match is None:
            return None
        tags.append(match.group())
        pos = match.end()
        while pos < end and value[pos] in _OWS:
            pos += 1
        if pos < end and value[pos] != ",":
            return None
    return tags


def http_timestamp(value: datetime | int | float | None) -> int | None:
    """Convert a modification time to whole seconds since the epoch (UTC).

    Aware datetimes are converted to UTC, naive datetimes are assumed to be in
    UTC, and int/float epoch values are accepted. The result is floored to a
    whole second, matching the one-second resolution of HTTP-dates.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        delta = value - _EPOCH
        return delta.days * 86400 + delta.seconds
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.floor(value)
    raise TypeError(
        f"expected a datetime, int, float or None, not {type(value).__name__}"
    )


def format_http_date(timestamp: int) -> str:
    """Format an epoch timestamp as an IMF-fixdate (RFC 9110 §5.6.7)."""
    return formatdate(timestamp, usegmt=True)


def parse_http_date(value: str | None) -> int | None:
    """Parse an HTTP-date (IMF-fixdate, RFC 850 or asctime format).

    Return seconds since the epoch, or ``None`` if ``value`` is absent or not a
    valid HTTP-date.
    """
    if value is None:
        return None
    value = value.strip(_OWS)
    for regex in (_IMF_FIXDATE, _RFC850_DATE, _ASCTIME_DATE):
        m = regex.fullmatch(value)
        if m is not None:
            break
    else:
        return None
    try:
        year = int(m["year"])
        if year < 100:
            # RFC 9110 §5.6.7: a two-digit year that appears to be more than
            # 50 years in the future represents the most recent past year with
            # the same last two digits.
            current_year = datetime.now(timezone.utc).year
            current_century = current_year - (current_year % 100)
            if year - (current_year % 100) > 50:
                year += current_century - 100
            else:
                year += current_century
        month = _MONTHS.index(m["mon"].lower()) + 1
        result = datetime(
            year,
            month,
            int(m["day"]),
            int(m["hour"]),
            int(m["min"]),
            int(m["sec"]),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    return http_timestamp(result)


def validator_headers(etag: str | None, last_modified: int | None) -> dict[str, str]:
    """Return the ETag / Last-Modified response headers for the given validators."""
    headers: dict[str, str] = {}
    if etag is not None:
        headers["ETag"] = etag
    if last_modified is not None:
        headers["Last-Modified"] = format_http_date(last_modified)
    return headers


@dataclass(frozen=True)
class ConditionalResult:
    """Outcome of a failed or short-circuited precondition evaluation."""

    status: int  # 304, 400, 412 or 428
    headers: dict[str, str]  # validator headers for 304; {} otherwise
    body: dict[str, Any] | None  # JSON error body for 400/412/428; None for 304


def _opaque(tag: str) -> str:
    return tag[2:] if tag.startswith("W/") else tag


def _strong_match(etag: str | None, tags: list[str]) -> bool:
    if etag is None or etag.startswith("W/"):
        return False
    return any(tag == etag for tag in tags if not tag.startswith("W/"))


def _weak_match(etag: str | None, tags: list[str]) -> bool:
    if etag is None:
        return False
    opaque = _opaque(etag)
    return any(_opaque(tag) == opaque for tag in tags)


def _precondition_failed() -> ConditionalResult:
    return ConditionalResult(
        status=412, headers={}, body={"detail": "Precondition failed."}
    )


def evaluate_preconditions(
    method: str,
    headers: Mapping[str, str],
    *,
    etag: str | None,
    last_modified: int | None,
    require_precondition: bool,
) -> ConditionalResult | None:
    """Evaluate the request preconditions of RFC 9110 §13.2.2.

    ``headers`` maps lower-case field names to field values (repeated field
    lines joined with ``", "``); empty values are treated as absent. ``etag``
    is an already-quoted entity-tag and ``last_modified`` an epoch timestamp;
    both ``None`` means the target resource has no current representation.
    When ``require_precondition`` is true, unconditional PUT/PATCH/DELETE
    requests on an existing resource are rejected with 428 (RFC 6585 §3); a
    request is conditional if it has an If-Match, an ``If-None-Match: *``
    (which then fails with 412, RFC 9110 §13.1.2) or a valid
    If-Unmodified-Since while ``last_modified`` is known.

    Preconditions are evaluated only for an existing resource and for PUT,
    which may create one. In particular a POST to a target whose validators
    are both ``None`` proceeds whatever its If-Match says (the handler answers,
    e.g. 201 or 404).

    Return ``None`` if the request should proceed to the handler, otherwise
    the ``ConditionalResult`` to respond with.
    """

    def header(name: str) -> str | None:
        return headers.get(name) or None

    if_match = parse_etag_list(header("if-match"))
    if if_match is None:
        return ConditionalResult(
            status=400, headers={}, body={"detail": "Malformed If-Match header."}
        )
    if_none_match = parse_etag_list(header("if-none-match"))
    if if_none_match is None:
        return ConditionalResult(
            status=400, headers={}, body={"detail": "Malformed If-None-Match header."}
        )
    # Invalid dates MUST be ignored (RFC 9110 §13.1.3, §13.1.4).
    if_unmodified_since = parse_http_date(header("if-unmodified-since"))
    if_modified_since = parse_http_date(header("if-modified-since"))

    exists = etag is not None or last_modified is not None
    if not exists and method != "PUT":
        # Let the handler respond (e.g. 404); RFC 9110 §13.2.1.
        return None

    if require_precondition and exists and method in PRECONDITION_REQUIRED_METHODS:
        # ``If-None-Match: *`` (create only if absent, RFC 9110 §13.1.2) is
        # conditional too; on an existing resource it fails below with 412.
        conditional = (
            bool(if_match)
            or if_none_match == ["*"]
            or (if_unmodified_since is not None and last_modified is not None)
        )
        if not conditional:
            accepted = []
            if etag is not None:
                accepted.append("If-Match")
            if last_modified is not None:
                accepted.append("If-Unmodified-Since")
            return ConditionalResult(
                status=428,
                headers={},
                body={
                    "detail": "This request must be conditional.",
                    "accepted-precondition-headers": accepted,
                },
            )

    # Step 1: If-Match (strong comparison).
    if if_match:
        passes = exists if if_match == ["*"] else _strong_match(etag, if_match)
        if not passes:
            return _precondition_failed()
    # Step 2: If-Unmodified-Since, only when If-Match is absent.
    elif if_unmodified_since is not None and last_modified is not None:
        if last_modified > if_unmodified_since:
            return _precondition_failed()

    # Step 3: If-None-Match (weak comparison).
    if if_none_match:
        if if_none_match == ["*"]:
            matches = exists
        else:
            matches = _weak_match(etag, if_none_match)
        if matches:
            if method in SAFE_METHODS:
                return ConditionalResult(
                    status=304,
                    headers=validator_headers(etag, last_modified),
                    body=None,
                )
            return _precondition_failed()
    # Step 4: If-Modified-Since, only for GET/HEAD without If-None-Match.
    elif (
        method in SAFE_METHODS
        and if_modified_since is not None
        and last_modified is not None
        and last_modified <= if_modified_since
    ):
        return ConditionalResult(
            status=304, headers=validator_headers(etag, last_modified), body=None
        )

    return None
