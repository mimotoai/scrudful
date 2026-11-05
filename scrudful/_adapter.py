"""Framework-neutral glue shared by the FastAPI, DRF and django-ninja adapters.

Private, and stdlib-only like :mod:`scrudful.conditional`: the adapters keep
everything that touches their framework's request and response objects.
"""

from __future__ import annotations

import copy
import dataclasses
import http
import inspect
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol

from scrudful.conditional import SAFE_METHODS, ConditionalResult, validator_headers
from scrudful.utils import (
    EXPOSE_HEADERS,
    join_links,
    link_relations,
    merge_expose_headers,
)

# Semantic HTTP 0.1 vocabulary.
SCHEMA_REL = "describedby"
SCHEMA_TYPE = "application/schema+json"
JSON_LD_CONTEXT_REL = "http://www.w3.org/ns/json-ld#context"
CONTEXT_TYPE = "application/ld+json"
RDF_CONTEXT_REL = "https://w3id.org/semantic-http/ns#rdf-context"
HSR_SCHEMA_URL = "https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json"
HSR_CONTEXT_URL = (
    "https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld"
)
#: The relation and media type of the Link to an HTTP Schema Resource.
HSR_LINK_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"
HSR_LINK_TYPE = "application/json"
#: Media types that carry their RDF in-band, so need no context link.
IN_BAND_MEDIA_TYPES = frozenset(
    {
        "application/ld+json",
        "text/turtle",
        "application/n-triples",
        "application/n-quads",
        "application/trig",
        "application/rdf+xml",
    }
)
#: The keys of an HTTP Schema Resource that are methods (the others are ``x-``).
METHOD_KEYS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


class HsrConfigError(ValueError):
    """A configuration the HTTP Schema Resource cannot be built from.

    The adapters re-raise it as their framework's configuration error.
    """


#: The message of the schema-without-context error (:class:`HsrConfigError`).
SCHEMA_WITHOUT_CONTEXT = (
    "{media_type} content has a schema link but no RDF context link; the HTTP "
    "Schema Resource's x-rdf-context would be missing. Give the context link "
    "(context_link_or_func)"
)

DEFAULT_SCHEMA_REL = SCHEMA_REL
DEFAULT_SCHEMA_TYPE = SCHEMA_TYPE
DEFAULT_CONTEXT_REL = JSON_LD_CONTEXT_REL
DEFAULT_CONTEXT_TYPE = CONTEXT_TYPE

PRECONDITION_FIELDS = (
    "if-match",
    "if-none-match",
    "if-modified-since",
    "if-unmodified-since",
)
#: The preconditions a create (a POST) evaluates: If-Modified-Since applies
#: only to GET and HEAD (RFC 9110 §13.1.3).
CREATE_PRECONDITION_FIELDS = ("if-match", "if-none-match", "if-unmodified-since")
#: Methods whose 2xx responses get freshly computed validators (RFC 9110 §9.3.4).
FRESH_VALIDATOR_METHODS = frozenset({"PUT", "PATCH"})
#: Methods that neither select nor modify a representation of the target
#: (RFC 9110 §13.2.1): their If-* headers are ignored, and their responses
#: get no validators. A TRACE's content is the echoed request.
NO_PRECONDITION_METHODS = frozenset({"CONNECT", "OPTIONS", "TRACE"})

_MALFORMED = "Malformed If-Match or If-None-Match header."
_PRECONDITION_FAILED = "Precondition Failed."
#: Descriptions of the responses a conditional GET/HEAD can get early, by status.
READ_DESCRIPTIONS = {
    304: "Not Modified: the representation matches the request's "
    "If-None-Match or If-Modified-Since validators.",
    400: _MALFORMED,
    412: _PRECONDITION_FAILED,
}
#: Descriptions of the responses a conditional write can get early, by status.
WRITE_DESCRIPTIONS = {
    400: _MALFORMED,
    412: _PRECONDITION_FAILED,
    428: "Precondition Required: this request must be conditional "
    "(send If-Match or If-Unmodified-Since).",
}

#: :data:`READ_DESCRIPTIONS` as OpenAPI responses (int status keys), published
#: by the FastAPI and django-ninja adapters as ``CONDITIONAL_READ_RESPONSES``.
CONDITIONAL_READ_RESPONSES: dict[int, dict[str, str]] = {
    status: {"description": text} for status, text in READ_DESCRIPTIONS.items()
}
#: :data:`WRITE_DESCRIPTIONS` as OpenAPI responses (int status keys), published
#: as ``CONDITIONAL_WRITE_RESPONSES``.
CONDITIONAL_WRITE_RESPONSES: dict[int, dict[str, str]] = {
    status: {"description": text} for status, text in WRITE_DESCRIPTIONS.items()
}

#: Representation metadata of the response an early one replaces: not copied
#: onto the body-less (or differently typed) early response (RFC 9110 §15.4.5).
BODY_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "content-encoding",
        "content-range",
        "content-language",
    }
)

#: :data:`READ_DESCRIPTIONS` as HTTP Schema Resource responses (string status keys).
READ_RESPONSES = {
    str(status): {"description": text} for status, text in READ_DESCRIPTIONS.items()
}
#: :data:`WRITE_DESCRIPTIONS` as HTTP Schema Resource responses (string status keys).
WRITE_RESPONSES = {
    str(status): {"description": text} for status, text in WRITE_DESCRIPTIONS.items()
}
#: The request body description and success status of each write method.
_WRITES = {
    "post": ("The content for the resource to be created.", "201"),
    "put": ("The new content of the resource.", "200"),
    "patch": ("The changes to apply to the resource.", "200"),
    "delete": (None, "204"),
}
_STATUS_TEXT = {"200": "OK", "201": "Created", "204": "No Content"}


class Headers(Protocol):
    """Case-insensitive response headers (Starlette's, or a Django response).

    ``get`` returns a single value and ``[]=`` replaces every existing line of
    that name, so a field repeated on several lines must be joined by the
    caller first.
    """

    def __contains__(self, name: str, /) -> bool: ...
    def __setitem__(self, name: str, value: str, /) -> None: ...
    def get(self, name: str, /) -> str | None: ...


def check_validators(etag_func: Any, last_modified_func: Any) -> None:
    """Raise ``TypeError`` unless each validator callback is ``None`` or callable."""
    check_callables(etag_func=etag_func, last_modified_func=last_modified_func)


def check_callables(**funcs: Any) -> None:
    """Raise ``TypeError`` unless each argument is ``None`` or callable."""
    for name, func in funcs.items():
        if func is not None and not callable(func):
            raise TypeError(f"{name} must be callable, not {type(func).__name__}")


def precondition_headers(getlist: Callable[[str], Sequence[str]]) -> dict[str, str]:
    """Collect the precondition fields, joining repeated lines (RFC 9110 §5.3).

    Empty (or blank) lines are dropped: an empty field is the same as an absent
    one, as in :func:`scrudful.conditional.evaluate_preconditions`.
    """
    headers = {}
    for name in PRECONDITION_FIELDS:
        values = [value for value in getlist(name) if value.strip(" \t")]
        if values:
            headers[name] = ", ".join(values)
    return headers


def evaluates_create_preconditions(method: str, headers: Mapping[str, str]) -> bool:
    """Whether a create's request has preconditions to evaluate.

    A POST carrying one of :data:`CREATE_PRECONDITION_FIELDS` (``headers``
    as :func:`precondition_headers` collects them, empty fields dropped).
    Without one there is nothing to evaluate, so the collection's validators
    are not computed.
    """
    return method == "POST" and any(
        name in headers for name in CREATE_PRECONDITION_FIELDS
    )


def evaluates_preconditions(method: str) -> bool:
    """Whether a request with ``method`` (upper-case) has its If-* evaluated.

    Not for CONNECT, OPTIONS and TRACE (:data:`NO_PRECONDITION_METHODS`),
    whose response is no representation of the target resource: the
    resource's validators don't describe it, so a server ignores the
    conditional headers (RFC 9110 §13.2.1). Their HTTP Schema Resource entries
    list neither scrudful's early responses nor the precondition headers.
    """
    return method not in NO_PRECONDITION_METHODS


def validator_policy(method: str, status: int) -> tuple[bool, bool]:
    """``(refresh, send)``: recompute the validators / send them after the view.

    Only for a 2xx to GET and HEAD (send) or PUT and PATCH (refresh and
    send): never for the :data:`NO_PRECONDITION_METHODS`, among others.
    """
    if not 200 <= status < 300:
        return False, False
    refresh = method in FRESH_VALIDATOR_METHODS
    return refresh, refresh or method in SAFE_METHODS


def set_validator_headers(
    headers: Headers, etag: str | None, last_modified: int | None
) -> None:
    """Set ETag / Last-Modified from the validators unless already set."""
    for name, value in validator_headers(etag, last_modified).items():
        if name not in headers:
            headers[name] = value


_ALWAYS_EXPOSED = frozenset({"Link", "Allow"})


def expose_headers(headers: Headers) -> None:
    """Merge the exposed headers into Access-Control-Expose-Headers.

    ``Link`` and ``Allow`` are always exposed; ``ETag`` and ``Location`` only
    when present.
    """
    names = [
        name for name in EXPOSE_HEADERS if name in _ALWAYS_EXPOSED or name in headers
    ]
    headers["Access-Control-Expose-Headers"] = merge_expose_headers(
        headers.get("Access-Control-Expose-Headers"), names
    )


def evaluated_signature(func: Callable[..., Any]) -> inspect.Signature:
    """``func``'s signature with string annotations evaluated in its module.

    For endpoints written with ``from __future__ import annotations``: a
    framework reading the strings off an adapter's wrapper would evaluate them
    in the wrapper's globals (the adapter module's), where the endpoint's names
    are undefined. If an annotation can't be evaluated, the unevaluated
    signature is returned.
    """
    try:
        return inspect.signature(func, eval_str=True)
    except NameError:
        pass
    try:
        return inspect.signature(func)
    except NameError:
        # Python 3.14+: annotations naming objects imported only under
        # TYPE_CHECKING can't be evaluated; FastAPI and ninja do the same.
        if sys.version_info < (3, 14):
            raise
        from annotationlib import Format

        return inspect.signature(func, annotation_format=Format.FORWARDREF)


def resolved_signature(func: Callable[..., Any]) -> inspect.Signature:
    """``func``'s signature with each string annotation evaluated in its module.

    For endpoints written with ``from __future__ import annotations``, as
    :func:`evaluated_signature`, but each annotation is evaluated on its own:
    one that can't be (e.g. a name imported only under ``TYPE_CHECKING``) is
    left as it is, and the others are still evaluated.
    """
    try:
        signature = inspect.signature(func)
    except NameError:
        # Python 3.14+, see evaluated_signature.
        if sys.version_info < (3, 14):
            raise
        from annotationlib import Format

        signature = inspect.signature(func, annotation_format=Format.FORWARDREF)
    globalns = getattr(inspect.unwrap(func), "__globals__", {})

    def resolve(annotation: Any) -> Any:
        if isinstance(annotation, str):
            try:
                return eval(annotation, globalns)  # same as typing/inspect
            except Exception:
                pass
        return annotation

    return signature.replace(
        parameters=[
            param.replace(annotation=resolve(param.annotation))
            for param in signature.parameters.values()
        ],
        return_annotation=resolve(signature.return_annotation),
    )


def resolve_annotation(annotation: Any, globalns: dict[str, Any]) -> Any:
    """Evaluate a string or forward-reference annotation in ``globalns``.

    Other annotations are returned unchanged; ``None`` if evaluation fails.
    """
    if not isinstance(annotation, (str, type)):
        annotation = getattr(annotation, "__forward_arg__", annotation)
    if isinstance(annotation, str):
        try:
            return eval(annotation, globalns)  # same as typing/inspect
        except Exception:
            return None
    return annotation


# --- Media types and links --------------------------------------------------


def essence(media_type: str) -> str:
    """The lowercase ``type/subtype`` of ``media_type``, without parameters."""
    return media_type.partition(";")[0].strip().lower()


def media_type_key(media_type: str) -> str:
    """``media_type`` as a ``content`` key: lowercase ``type/subtype``, then the
    parameters (after the first ``;``) as written.

    Raises
    ------
    ValueError
        If the ``type/subtype`` lacks a type or a subtype.
    """
    head, semicolon, parameters = media_type.strip().partition(";")
    main, slash, sub = head.strip().lower().partition("/")
    if not (main and slash and sub):
        raise ValueError(f"not a media type: {media_type!r}")
    return f"{main}/{sub}{semicolon}{parameters}"


# --- Integer query parameters --------------------------------------------------

#: The characters pydantic strips around a string (Rust's ``str::trim``: the
#: Unicode ``White_Space`` property, which ``str.isspace`` widens with
#: U+001C..U+001F).
_WHITE_SPACE = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
#: A JSON integer: no sign but ``-``, no leading zero, ASCII digits only.
_JSON_INT = re.compile(r"-?(?:0|[1-9][0-9]*)", re.ASCII)
_DIGIT_RUN = re.compile(r"-?[0-9]+", re.ASCII)
#: The longest integer string pydantic parses (CPython's default
#: ``sys.int_info.default_max_str_digits``), sign included.
_MAX_INT_LENGTH = 4300


def parse_query_int(raw: str) -> int | None:
    """``raw`` as pydantic v2's lax ``int`` validates a string, or ``None`` where
    it fails (``int_parsing`` or ``int_parsing_size``).

    So DRF's ``ScrudfulPagination`` accepts exactly the ``page`` and
    ``page_size`` that django-ninja's query model does, without depending on
    pydantic. Unlike ``int()``: only ASCII digits, ``"1.0"`` (a fraction of
    zeros) is 1, and only the Unicode ``White_Space`` characters are stripped.
    It mirrors pydantic-core's steps, quirks included (``"0-5"`` is -5): a
    JSON integer as is; else, after dropping a fraction of zeros and a sign,
    underscores between digits and leading zeros (and underscores again)
    are removed before the JSON integer is read. Checked against pydantic
    by ``tests/test_page_parameters.py``.
    """
    text = raw.strip(_WHITE_SPACE)
    run = _DIGIT_RUN.match(text)
    if run is not None and run.end() > _MAX_INT_LENGTH:
        prefix = _JSON_INT.match(text)
        if prefix is not None and prefix.end() == run.end():
            return None  # int_parsing_size
    if _JSON_INT.fullmatch(text):
        return _int(text)
    point = text.find(".")
    if point >= 0 and text[point + 1 :] and not text[point + 1 :].strip("0"):
        text = text[:point]
    sign = ""
    if text[:1] in ("+", "-"):
        sign, text = text[0], text[1:]
        if text[:1] in ("+", "-"):
            return None
    text = _strip_underscores(text) or text
    unpadded = _strip_leading_zeros(text)
    if unpadded is not None:
        text = _strip_underscores(unpadded) or unpadded
    if sign == "-":
        text = "-" + text
    if len(text) > _MAX_INT_LENGTH or not _JSON_INT.fullmatch(text):
        return None
    return _int(text)


def _int(text: str) -> int | None:
    """``int(text)``, or ``None`` for more digits than the interpreter's limit
    (``sys.set_int_max_str_digits``), which may be below pydantic's."""
    try:
        return int(text)
    except ValueError:
        return None


def _strip_underscores(text: str) -> str | None:
    """``text`` without its underscores, when each is between other characters
    and none is doubled; else ``None``."""
    if "_" not in text or text[0] == "_" or text[-1] == "_" or "__" in text:
        return None
    return text.replace("_", "")


def _strip_leading_zeros(text: str) -> str | None:
    """``text`` from the first character after its leading run of zeros and
    underscores that is a digit or ``-``; its last character when the run is the
    whole of it; ``None`` when it doesn't start with ``0`` or the run ends on
    another character."""
    if not text.startswith("0"):
        return None
    for index in range(1, len(text)):
        char = text[index]
        if char in "0_":
            continue
        if char in "123456789-":
            return text[index:]
        return None
    return text[-1]


def is_json(media_type: str) -> bool:
    """Whether ``media_type`` is JSON content: ``application/json`` or ``+json``,
    other than JSON-LD, which carries its context in-band."""
    return is_json_syntax(media_type) and essence(media_type) != "application/ld+json"


def is_json_syntax(media_type: str) -> bool:
    """Whether ``media_type`` is written as JSON: ``application/json`` or
    ``+json``, JSON-LD included (unlike :func:`is_json`, which asks whether
    its context is out of band)."""
    name = essence(media_type)
    return name == "application/json" or name.endswith("+json")


def with_charset(media_type: str, charset: str) -> str:
    """``media_type`` with the ``charset`` parameter ``charset`` (the encoding
    of the bytes it labels), in place of any charset it names."""
    name, *parameters = split_parameters(media_type)
    kept = [
        parameter.strip()
        for parameter in parameters
        if parameter.strip()
        and parameter.partition("=")[0].strip().lower() != "charset"
    ]
    return "; ".join([name.strip(), *kept, f"charset={charset}"])


def split_parameters(media_type: str) -> list[str]:
    """``media_type`` split at each ``;`` outside a quoted string (whose text,
    a ``;`` included, is kept as written)."""
    parts = []
    start = 0
    quoted = escaped = False
    for index, char in enumerate(media_type):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            parts.append(media_type[start:index])
            start = index + 1
    parts.append(media_type[start:])
    return parts


def is_in_band(media_type: str) -> bool:
    """Whether ``media_type`` carries RDF in-band (no context link needed)."""
    return essence(media_type) in IN_BAND_MEDIA_TYPES


def context_defaults(media_type: str) -> tuple[str | None, str | None]:
    """The default ``(rel, type)`` of the context link for ``media_type``.

    ``(None, None)`` when the content has no context link (in-band RDF); a
    ``None`` type when the caller has to give one (non-JSON content).
    """
    if is_in_band(media_type):
        return None, None
    if is_json(media_type):
        return JSON_LD_CONTEXT_REL, CONTEXT_TYPE
    return RDF_CONTEXT_REL, None


def context_link_type(media_type: str, value: Any) -> str:
    """The ``type`` of the context link of ``media_type`` content.

    ``value``, the evaluated ``context_type_or_func``, unless it is empty;
    else the default of :func:`context_defaults` (JSON-LD, for JSON content).
    The Link header and the HTTP Schema Resource both take the type from here, so
    they agree, and fail alike.

    Raises
    ------
    ValueError
        If neither gives one: a ``context_type_or_func`` callable returned
        nothing for content that is not JSON. A context Link without a type
        would not say how to lift the content.
    """
    if value:
        return str(value)
    default = context_defaults(media_type)[1]
    if default is None:
        raise ValueError(f"the context of {media_type!r} needs a type")
    return default


def check_error_media_type(media_type: str) -> None:
    """Raise ``ValueError`` unless an ``error`` entity's ``media_type`` is JSON.

    The ``error`` entity describes scrudful's own 400, 412 and 428 bodies,
    which are JSON objects served as ``media_type``.
    """
    if not is_json(media_type):
        raise ValueError(
            f"error has the media type {media_type!r}, but the error entity "
            "describes scrudful's JSON error bodies (400, 412 and 428): use "
            "application/json or a +json media type"
        )


def has_default_entity(method: str, status: int) -> bool:
    """Whether a ``method`` response with ``status`` gets the endpoint's entity.

    A 2xx but 204, which has no body for the entity to describe, to any
    method but TRACE, whose content is the echoed request (``message/http``),
    not the endpoint's. Another response the endpoint returns gets no Links
    from scrudful (it keeps those it set itself); scrudful's own 400, 412
    and 428 and the errors raised get the error entity's instead.
    """
    return 200 <= status < 300 and status != 204 and method.upper() != "TRACE"


#: The relations of an RDF Context Link Header: one slot (see :func:`merge_links`).
_CONTEXT_RELS = frozenset({JSON_LD_CONTEXT_REL, RDF_CONTEXT_REL})


def merge_links(
    existing: str | None,
    schema: str | None,
    context: str | None,
    hsr: str | None = None,
) -> str | None:
    """Join the view's Link ``existing`` with the ``schema``, ``context`` and
    ``hsr`` links.

    A link of ``existing`` with a relation that scrudful's ``schema``
    (``context``) link-value has replaces that link: with the defaults, a
    ``describedby`` link replaces the schema link; with
    ``schema_rel_or_func="profile"``, a ``profile`` link replaces it, and a
    ``describedby`` one doesn't. The JSON-LD context and the ``rdf-context``
    relations are one slot, since a response has at most one RDF Context
    Link Header: when scrudful's context link has either, a link of either
    replaces it. An overridden context relation is matched exactly.
    Relation types are compared case-insensitively, every one of a ``rel``
    holding several (RFC 8288 §2.1.1, §3.3). A link of another relation
    replaces nothing. A link of ``existing`` with the HTTP Schema Resource
    relation replaces scrudful's ``hsr`` link.
    """
    relations = link_relations(existing)
    if HSR_LINK_REL.lower() in relations:
        hsr = None
    if relations & link_relations(schema):
        schema = None
    context_rels = link_relations(context)
    if context_rels & _CONTEXT_RELS:
        context_rels |= _CONTEXT_RELS
    if relations & context_rels:
        context = None
    return join_links(existing, schema, context, hsr)


# --- HTTP Schema Resource descriptions ---------------------------------------
#
# The operations of an HTTP Schema Resource (an OpenAPI Path Item), shared by
# the DRF and django-ninja adapters. ``content`` is a Media
# Type map as built by :func:`describe_content`; falsy means none.
# ``conditional``: whether the method's preconditions are evaluated.


def describe_content(
    media_type: str,
    schema: str | None,
    schema_type: str,
    context: str | None,
    context_type: str | None,
) -> dict[str, Any] | None:
    """A ``content`` map with the Media Type Object of ``media_type``.

    ``schema`` and ``context`` are absolute URIs. The schema is a JSON Schema
    (``schema``) when ``schema_type`` is ``application/schema+json``, else an
    ``x-schema``. The ``x-rdf-context`` is left out of in-band media types; its
    type defaults to JSON-LD for JSON content. ``None`` without a ``schema``.

    Raises
    ------
    ValueError
        If ``media_type`` is malformed, or a context is given for non-JSON
        content without ``context_type``.
    HsrConfigError
        If ``media_type`` is not in-band and a ``schema`` is given without a
        ``context``.
    """
    key = media_type_key(media_type)
    if not schema:
        return None
    if not context and not is_in_band(media_type):
        raise HsrConfigError(SCHEMA_WITHOUT_CONTEXT.format(media_type=key))
    if essence(schema_type) == SCHEMA_TYPE:
        entry: dict[str, Any] = {"schema": {"$ref": schema}}
    else:
        entry = {"x-schema": {"href": schema, "type": schema_type}}
    if context and not is_in_band(media_type):
        context_type = context_link_type(media_type, context_type)
        entry["x-rdf-context"] = {"href": context, "type": context_type}
    return {key: entry}


def check_error_body_func(func: Any) -> None:
    """Raise ``TypeError`` unless ``func`` is ``None`` or a plain callable."""
    if func is None:
        return
    if not callable(func):
        raise TypeError(f"error_body_func must be callable, not {type(func).__name__}")
    if inspect.iscoroutinefunction(func):
        raise TypeError("error_body_func must be a plain function, not async")


def with_error_body(
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None,
    request: Any,
    result: ConditionalResult,
) -> ConditionalResult:
    """``result`` with the body ``error_body_func(request, result)`` returns.

    A 304 (no body) and a missing function leave ``result`` as it is. The
    status, headers, Links and Content-Type are never changed.
    """
    if error_body_func is None or result.body is None:
        return result
    body = error_body_func(request, result)
    if not isinstance(body, Mapping):
        raise TypeError(
            f"error_body_func must return a dict, not {type(body).__name__}"
        )
    return dataclasses.replace(result, body=body)


def _accept_ranges(accept: str) -> list[tuple[str, float]]:
    """The ``(type/subtype range, q)`` pairs of an Accept field value."""
    ranges = []
    for item in accept.split(","):
        pieces = [piece.strip() for piece in item.split(";")]
        media_range = pieces[0].lower()
        if "/" not in media_range:
            continue
        weight = 1.0
        for parameter in pieces[1:]:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                try:
                    weight = min(max(float(value.strip()), 0.0), 1.0)
                except ValueError:
                    weight = 0.0
                break
        ranges.append((media_range, weight))
    return ranges


def _accept_rank(ranges: list[tuple[str, float]], media_type: str) -> tuple[float, int]:
    """``(q, specificity)`` of ``media_type`` for the Accept ``ranges``.

    The most specific matching range decides (RFC 9110 section 12.5.1);
    ``(0.0, -1)`` when none matches.
    """
    kind = media_type.partition("/")[0]
    best = -1
    weight = 0.0
    for media_range, q in ranges:
        if media_range == media_type:
            specificity = 2
        elif media_range == f"{kind}/*":
            specificity = 1
        elif media_range == "*/*":
            specificity = 0
        else:
            continue
        if specificity > best:
            best, weight = specificity, q
    return weight, best


def prefers_html(accept: str | None, media_type: str) -> bool:
    """Whether an Accept field value prefers ``text/html`` to ``media_type``.

    True iff ``text/html`` is acceptable (a quality above 0) and ranks above
    ``media_type``'s essence: by quality first, then, at equal quality, by the
    specificity of the range that matches it (RFC 9110 section 12.5.1). So
    ``text/html,*/*;q=0.8`` and ``text/html, application/*`` prefer HTML, and
    ``*/*`` does not. An absent or empty ``accept`` prefers nothing.
    """
    if not accept or not accept.strip():
        return False
    ranges = _accept_ranges(accept)
    html = _accept_rank(ranges, "text/html")
    other = _accept_rank(ranges, essence(media_type))
    return html[0] > 0 and html > other


def reason_phrase(status: int) -> str:
    """The reason phrase of ``status`` (django-ninja's OpenAPI text when unknown)."""
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "Unknown Status Code"


def _success_responses(
    statuses: Iterable[int] | None, default: str
) -> dict[str, dict[str, Any]]:
    """The responses of ``statuses``, else of the ``default`` status (DRF's)."""
    if statuses is None:
        return {default: {"description": _STATUS_TEXT[default]}}
    return {str(status): {"description": reason_phrase(status)} for status in statuses}


def describe_read(
    content: dict[str, Any] | None,
    conditional: bool,
    statuses: Iterable[int] | None = None,
    error_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe a GET: its 200 and, when conditional, the 304, 400 and 412.

    ``statuses`` replaces the 200 by the statuses the operation answers
    (``content`` then goes on the 200, if listed). ``error_content`` goes on the
    400 and 412.
    """
    responses = _success_responses(statuses, "200")
    if content and "200" in responses:
        responses["200"]["content"] = content
    if conditional:
        responses.update(copy.deepcopy(READ_RESPONSES))
        _add_content(responses, ("400", "412"), error_content)
    return {"responses": responses}


def _add_content(
    responses: dict[str, dict[str, Any]],
    statuses: Iterable[str],
    content: dict[str, Any] | None,
) -> None:
    """Put a copy of ``content`` on each of ``statuses`` among ``responses``."""
    if content:
        for status in statuses:
            responses[status]["content"] = copy.deepcopy(content)


def describe_write(
    method: str,
    content: dict[str, Any] | None,
    conditional: bool,
    has_etag: bool,
    has_last_modified: bool,
    statuses: Iterable[int] | None = None,
    response_content: dict[str, Any] | None = None,
    error_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe a ``post``, ``put``, ``patch`` or ``delete``.

    ``content`` describes the request body (DELETE has none, and the body is
    left out when ``content`` is falsy). ``response_content`` goes on every
    success status but 204, ``error_content`` on the 400, 412 and 428. The success
    status is DRF's (201 for POST, 204 for DELETE, else 200), unless
    ``statuses`` lists the statuses the operation answers. A conditional
    POST gets the 400 and 412, since it is evaluated but never required to be
    conditional; the other methods also get the 428 and the precondition
    header ``parameters`` (see :func:`conditional_header_parameters`).

    Raises
    ------
    ValueError
        If ``method`` is not one of the four.
    """
    if method not in _WRITES:
        raise ValueError(f"not a write method: {method!r}")
    body_description, status = _WRITES[method]
    metadata: dict[str, Any] = {}
    if body_description is not None and content:
        metadata["requestBody"] = {
            "description": body_description,
            "required": True,
            "content": content,
        }
    responses = metadata["responses"] = _success_responses(statuses, status)
    if response_content:
        for code, response in responses.items():
            if code != "204":
                response["content"] = copy.deepcopy(response_content)
    if conditional:
        if method == "post":
            responses.update(
                {code: copy.deepcopy(WRITE_RESPONSES[code]) for code in ("400", "412")}
            )
        else:
            responses.update(copy.deepcopy(WRITE_RESPONSES))
            metadata["parameters"] = conditional_header_parameters(
                has_etag, has_last_modified
            )
        _add_content(
            responses,
            [code for code in ("400", "412", "428") if code in responses],
            error_content,
        )
    return metadata


def conditional_header_parameters(
    has_etag: bool, has_last_modified: bool
) -> list[dict[str, Any]] | None:
    """Describe the precondition headers a conditional write accepts.

    One of them is required; a header is marked ``required`` when it is the
    only one accepted. ``None`` without validators.
    """
    headers = []
    if has_etag:
        headers.append(("If-Match", "The entity-tag of the current representation."))
    if has_last_modified:
        headers.append(
            (
                "If-Unmodified-Since",
                "The Last-Modified date of the current representation.",
            )
        )
    if not headers:
        return None
    either = " Either If-Match or If-Unmodified-Since is required."
    return [
        {
            "in": "header",
            "name": name,
            "schema": {"type": "string"},
            "required": len(headers) == 1,
            "description": description if len(headers) == 1 else description + either,
        }
        for name, description in headers
    ]
