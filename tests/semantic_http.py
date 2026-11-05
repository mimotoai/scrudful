"""Helpers that check Semantic HTTP 0.1 conformance, shared by the adapter tests.

``validate_http_schema_resource`` checks an OPTIONS body against the vendored
HTTP Schema Resource schema, ``parse_links`` reads a Link field value and
``link_problems`` applies the per-response rules of the specification.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from scrudful._adapter import (
    CONTEXT_TYPE,
    HSR_CONTEXT_URL,
    HSR_SCHEMA_URL,
    JSON_LD_CONTEXT_REL,
    METHOD_KEYS,
    RDF_CONTEXT_REL,
    SCHEMA_REL,
    essence,
    is_in_band,
    is_json,
)
from scrudful.utils import _split_top_level, _unquote

FIXTURES = Path(__file__).parent / "fixtures" / "semantic-http-0.1"
HSR_SCHEMA = json.loads((FIXTURES / "http-schema-resource.schema.json").read_text())
PROBLEM_DETAILS_SCHEMA = json.loads(
    (FIXTURES / "problem-details.schema.json").read_text()
)
HSR_DOCUMENT_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"
HSR_OPTIONS_ENTRY = {"responses": {"204": {"description": "No Content"}}}
_ABSOLUTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CONTEXT_RELS = {JSON_LD_CONTEXT_REL, RDF_CONTEXT_REL}


# --- HTTP Schema Resource ----------------------------------------------------


def _content_maps(body: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Every ``content`` map of the operations of an HTTP Schema Resource."""
    for method in METHOD_KEYS:
        operation = body.get(method)
        if not isinstance(operation, Mapping):
            continue
        request_body = operation.get("requestBody")
        if isinstance(request_body, Mapping) and "content" in request_body:
            yield request_body["content"]
        for response in (operation.get("responses") or {}).values():
            if isinstance(response, Mapping) and "content" in response:
                yield response["content"]


def _uris(value: Any) -> Iterator[Any]:
    """Every ``$ref`` and ``href`` value under ``value``."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ("$ref", "href"):
                yield item
            else:
                yield from _uris(item)
    elif isinstance(value, list):
        for item in value:
            yield from _uris(item)


def validate_http_schema_resource(body: dict) -> None:
    """Check ``body`` is a Semantic HTTP 0.1 HTTP Schema Resource.

    Raises ``jsonschema.ValidationError`` when ``body`` does not match the
    vendored JSON Schema, and ``AssertionError`` for the rules the schema can't
    express: absolute ``$ref`` and ``href`` values under ``content``, lowercase
    type/subtype ``content`` keys, and no two keys of one ``content`` map with
    the same type/subtype.
    """
    Draft202012Validator(HSR_SCHEMA).validate(body)
    for content in _content_maps(body):
        for uri in _uris(content):
            assert isinstance(uri, str) and _ABSOLUTE.match(uri), f"relative: {uri!r}"
        names = [key.partition(";")[0].strip() for key in content]
        for name in names:
            assert name == name.lower(), f"type/subtype not lowercase: {name!r}"
        assert len(set(names)) == len(names), f"duplicate media types: {names}"


# --- Link header ---------------------------------------------------------------


def parse_links(value: str | None) -> list[tuple[str, dict[str, str]]]:
    """The ``(target, params)`` of each link of a Link field value.

    Parameter names are lower-cased and quoted values unquoted; for a repeated
    parameter the first counts (RFC 8288 section 3).
    """
    links = []
    for link in _split_top_level(value or "", ","):
        link = link.strip(" \t")
        if not link:
            continue
        end = link.index(">")
        params: dict[str, str] = {}
        for param in _split_top_level(link[end + 1 :], ";")[1:]:
            name, _, raw = param.partition("=")
            params.setdefault(name.strip(" \t").lower(), _unquote(raw) or "")
        links.append((link[1:end], params))
    return links


def _relations(params: Mapping[str, str]) -> list[str]:
    return params.get("rel", "").lower().split()


def _schema_links(links: list[tuple[str, dict[str, str]]]) -> list[tuple[str, dict]]:
    return [link for link in links if SCHEMA_REL in _relations(link[1])]


def _context_links(links: list[tuple[str, dict[str, str]]]) -> list[tuple[str, dict]]:
    return [link for link in links if _CONTEXT_RELS & set(_relations(link[1]))]


# --- Per-response rules --------------------------------------------------------


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def link_problems(
    method: str,
    status: int,
    headers: Mapping[str, str],
    has_content: bool,
    hsr: dict | None = None,
) -> list[str]:
    """How a response breaks the Semantic HTTP 0.1 rules; empty if it conforms.

    ``headers`` holds the response's header fields (a repeated Link field
    joined with ``", "``), ``has_content`` whether it has content. With ``hsr``,
    the HTTP Schema Resource that the resource's OPTIONS response returned, the
    Links must also agree with it: they are the ``$ref`` / ``href`` of the entry
    chosen by method, status and media type. A response with an
    ``Access-Control-Allow-Methods`` field is a CORS preflight answer, which the
    rules don't cover.
    """
    if _header(headers, "access-control-allow-methods") is not None:
        return []
    links = parse_links(_header(headers, "link"))
    schemas, contexts = _schema_links(links), _context_links(links)
    content_type = _header(headers, "content-type")
    problems = []
    if has_content:
        if len(schemas) != 1:
            problems.append(f"{len(schemas)} describedby links, need exactly 1")
        elif "type" not in schemas[0][1]:
            problems.append("the describedby link has no type")
        if content_type is None:
            problems.append("content without a Content-Type")
        else:
            problems += _context_problems(content_type, contexts)
    # An error answering OPTIONS is an error response, not the description.
    if method == "OPTIONS" and status < 400:
        # options_204_problems only tests whether the body is empty: a
        # stand-in body says whether the response had content.
        problems += options_204_problems(
            status, headers, b"x" if has_content else b""
        )
    if hsr is not None and (has_content or method == "HEAD"):
        problems += _agreement_problems(
            method, status, content_type, schemas, contexts, hsr
        )
    return problems


def _context_problems(
    content_type: str, contexts: list[tuple[str, dict[str, str]]]
) -> list[str]:
    if is_in_band(content_type):
        if contexts:
            return [f"{len(contexts)} context links for in-band RDF, need none"]
        return []
    if len(contexts) != 1:
        return [f"{len(contexts)} context links, need exactly 1"]
    _, params = contexts[0]
    if is_json(content_type):
        if JSON_LD_CONTEXT_REL not in _relations(params):
            return ["the context link of JSON content needs the json-ld context rel"]
        if params.get("type") != CONTEXT_TYPE:
            return [f"the context link needs type={CONTEXT_TYPE!r}"]
    else:
        if RDF_CONTEXT_REL not in _relations(params):
            return ["the context link of non-JSON content needs the rdf-context rel"]
        if not params.get("type"):
            return ["the context link has no type"]
    return []


def _select_entry(
    operation: Any, status: int, content_type: str
) -> dict[str, Any] | None:
    """The Media Type Object for ``status`` and ``content_type``, if listed."""
    responses = (operation or {}).get("responses") or {}
    response = (
        responses.get(str(status))
        or responses.get(f"{status // 100}XX")
        or responses.get("default")
    )
    for key, entry in ((response or {}).get("content") or {}).items():
        if essence(key) == essence(content_type):
            return entry
    return None


def _agreement_problems(
    method: str,
    status: int,
    content_type: str | None,
    schemas: list[tuple[str, dict[str, str]]],
    contexts: list[tuple[str, dict[str, str]]],
    hsr: dict,
) -> list[str]:
    if content_type is None:
        return ["no Content-Type to choose the HTTP Schema Resource entry by"]
    key = "get" if method == "HEAD" else method.lower()
    entry = _select_entry(hsr.get(key), status, content_type)
    if entry is None:
        return [f"no {key} {status} {essence(content_type)} entry to agree with"]
    schema = entry.get("schema", {}).get("$ref") or entry.get("x-schema", {}).get(
        "href"
    )
    context = entry.get("x-rdf-context", {}).get("href")
    links = ([target for target, _ in schemas], [target for target, _ in contexts])
    if links != ([schema], [context] * (context is not None)):
        return [f"the Links {links} don't agree with the entry's {schema}, {context}"]
    return []


# --- Rules of the 37ec1ce revision -------------------------------------------


def hsr_link_problems(
    headers: Mapping[str, str], expected_target: str | None = None
) -> list[str]:
    """How the HTTP Schema Resource link of ``headers`` breaks the rules.

    There must be exactly one, with ``type="application/json"`` and no
    ``anchor``; with ``expected_target`` its target must equal it.
    """
    links = [
        link
        for link in parse_links(_header(headers, "link"))
        if HSR_DOCUMENT_REL in _relations(link[1])
    ]
    if len(links) != 1:
        return [f"{len(links)} HTTP Schema Resource links, need exactly 1"]
    target, params = links[0]
    problems = []
    if params.get("type") != "application/json":
        problems.append(
            f"the HTTP Schema Resource link has type {params.get('type')!r}"
        )
    if "anchor" in params:
        problems.append("the HTTP Schema Resource link has an anchor")
    if expected_target is not None and target != expected_target:
        problems.append(f"the HTTP Schema Resource link targets {target!r}")
    return problems


def options_204_problems(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    expected_allow: set[str] | None = None,
) -> list[str]:
    """How a non-preflight OPTIONS response breaks the rules.

    It must be a 204 without content, with ``Allow`` (equal to
    ``expected_allow`` when given) and one HTTP Schema Resource link.
    """
    problems = []
    if status != 204:
        problems.append(f"OPTIONS answered {status}, need 204")
    if body:
        problems.append("OPTIONS 204 has content")
    if _header(headers, "content-type") is not None:
        problems.append("OPTIONS 204 has a Content-Type")
    allow = _header(headers, "allow")
    if allow is None:
        problems.append("OPTIONS without Allow")
    elif expected_allow is not None:
        methods = {m.strip() for m in allow.split(",") if m.strip()}
        if methods != expected_allow:
            problems.append(f"Allow {sorted(methods)} is not {sorted(expected_allow)}")
    return problems + hsr_link_problems(headers)


def hsr_get_problems(
    status: int, headers: Mapping[str, str], body: bytes
) -> list[str]:
    """How a GET of the HTTP Schema Resource breaks the rules."""
    problems = []
    if status != 200:
        problems.append(f"HSR GET answered {status}, need 200")
    content_type = _header(headers, "content-type")
    if content_type is None or essence(content_type) != "application/json":
        problems.append(f"HSR Content-Type {content_type!r}, need application/json")
    links = parse_links(_header(headers, "link"))
    if [t for t, _ in _schema_links(links)] != [HSR_SCHEMA_URL]:
        problems.append("the HSR describedby link is not the HTTP Schema Resource's")
    if [t for t, _ in _context_links(links)] != [HSR_CONTEXT_URL]:
        problems.append("the HSR context link is not the HTTP Schema Resource's")
    if _header(headers, "etag") is None:
        problems.append("HSR without ETag")
    cache_control = _header(headers, "cache-control") or ""
    if "public" not in [d.strip().lower() for d in cache_control.split(",")]:
        problems.append(f"HSR Cache-Control {cache_control!r} lacks public")
    try:
        document = json.loads(body)
        validate_http_schema_resource(document)
    except Exception as exc:  # JSON, schema or AssertionError: all are problems
        problems.append(f"the HSR body is invalid: {exc}")
    else:
        if document.get("options") != HSR_OPTIONS_ENTRY:
            problems.append(f"the HSR options entry is {document.get('options')!r}")
    return problems


def _accepts(accept: str, media_type: str) -> bool:
    """Whether ``accept`` accepts ``media_type`` (RFC 9110 section 12.5.1).

    The most specific matching range decides; its ``q=0`` means not acceptable.
    """
    type_, _, subtype = media_type.partition("/")
    best: tuple[int, float] | None = None
    for part in _split_top_level(accept, ","):
        range_, *params = [p.strip(" \t") for p in _split_top_level(part, ";")]
        range_ = range_.lower()
        if range_ == "*/*":
            rank = 0
        elif range_ == f"{type_}/*":
            rank = 1
        elif range_ == media_type:
            rank = 2
        else:
            continue
        q = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if best is None or rank > best[0]:
            best = (rank, q)
    return best is not None and best[1] > 0


def undescribed_problems(
    accept: str | None, headers: Mapping[str, str], hsr: dict
) -> list[str]:
    """How a response of a media type the HSR doesn't describe breaks the rules.

    Content whose media type is no ``content`` key of ``hsr`` is allowed only
    when the request's ``accept`` accepts it.
    """
    content_type = _header(headers, "content-type")
    if content_type is None:
        return []
    media_type = essence(content_type)
    described = {essence(key) for content in _content_maps(hsr) for key in content}
    if media_type in described:
        return []
    if accept is not None and _accepts(accept, media_type):
        return []
    return [f"{media_type} is undescribed and not accepted by {accept!r}"]
