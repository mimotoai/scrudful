"""The JSON Schema and JSON-LD context documents the role API's Links point to.

scrudful builds the collection documents (``scrudful.collection``); serving
them, like the item documents, is the application's job. These operations are
public (``auth=None``): the documents describe the API, not its data.
"""

from __future__ import annotations

from typing import Any, Callable, List

from django.http import Http404, HttpRequest, JsonResponse
from django.urls import reverse
from ninja import Router
from pydantic import TypeAdapter

from scrudful.collection import collection_context, collection_schema

from .api import Message, RoleOut, RolePatch, RoleSearch

#: The vocabulary of the role API's terms (an example IRI).
VOCABULARY = "https://example.com/vocab/roles#"
DRAFT = "https://json-schema.org/draft/2020-12/schema"

router = Router(auth=None, tags=["documents"])


def url(request: HttpRequest, kind: str, name: str) -> str:
    return request.build_absolute_uri(reverse(f"api:{kind}", kwargs={"name": name}))


def model_schema(model: Any) -> Callable[[HttpRequest], dict[str, Any]]:
    def build(request: HttpRequest) -> dict[str, Any]:
        return {"$schema": DRAFT, **TypeAdapter(model).json_schema()}

    return build


def vocabulary_context(request: HttpRequest) -> dict[str, Any]:
    return {"@context": {"@version": 1.1, "@vocab": VOCABULARY}}


SCHEMAS: dict[str, Callable[[HttpRequest], dict[str, Any]]] = {
    "role": model_schema(RoleOut),
    "roles": lambda request: collection_schema(
        url(request, "schema", "role"),
        schema_id=url(request, "schema", "roles"),
        title="Roles",
    ),
    "role-patch": model_schema(RolePatch),
    "role-search": model_schema(RoleSearch),
    "role-search-result": model_schema(List[RoleOut]),
    "error": model_schema(Message),
}
CONTEXTS: dict[str, Callable[[HttpRequest], dict[str, Any]]] = {
    **{name: vocabulary_context for name in SCHEMAS},
    "roles": lambda request: collection_context(
        url(request, "context", "role"), vocabulary=VOCABULARY
    ),
}


def serve(
    documents: dict[str, Callable[[HttpRequest], Any]],
    request: HttpRequest,
    name: str,
    content_type: str,
) -> JsonResponse:
    if name not in documents:
        raise Http404(name)
    return JsonResponse(documents[name](request), content_type=content_type)


@router.get("/schemas/{name}", url_name="schema")
def schema(request: HttpRequest, name: str) -> JsonResponse:
    return serve(SCHEMAS, request, name, "application/schema+json")


@router.get("/contexts/{name}", url_name="context")
def context(request: HttpRequest, name: str) -> JsonResponse:
    return serve(CONTEXTS, request, name, "application/ld+json")
