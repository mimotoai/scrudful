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
    allow_header,
    essence,
    is_in_band,
    is_json,
)
from scrudful.utils import _split_top_level, _unquote

FIXTURES = Path(__file__).parent / "fixtures" / "semantic-http-0.1"
HSR_SCHEMA = json.loads((FIXTURES / "http-schema-resource.schema.json").read_text())
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
    if method == "OPTIONS":
        problems += _options_problems(
            status, headers, content_type, schemas, contexts, hsr
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


def _options_problems(
    status: int,
    headers: Mapping[str, str],
    content_type: str | None,
    schemas: list[tuple[str, dict[str, str]]],
    contexts: list[tuple[str, dict[str, str]]],
    hsr: dict | None,
) -> list[str]:
    problems = []
    if status != 200:
        problems.append(f"OPTIONS answered {status}, need 200")
    if content_type is None or essence(content_type) != "application/json":
        problems.append(f"OPTIONS Content-Type {content_type!r}, need JSON")
    if [target for target, _ in schemas] != [HSR_SCHEMA_URL]:
        problems.append("the OPTIONS describedby link is not the HTTP Schema Resource")
    if [target for target, _ in contexts] != [HSR_CONTEXT_URL]:
        problems.append("the OPTIONS context link is not the HTTP Schema Resource's")
    allow = _header(headers, "allow")
    if allow is None:
        problems.append("OPTIONS without Allow")
    if hsr is not None:
        if allow is not None and allow != allow_header(hsr):
            problems.append(f"Allow {allow!r} is not the body's {allow_header(hsr)!r}")
        if "options" not in hsr:
            problems.append("the OPTIONS body has no options entry")
        for response in ((hsr.get("head") or {}).get("responses") or {}).values():
            if "content" in response:
                problems.append("a head response has content")
    return problems


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
