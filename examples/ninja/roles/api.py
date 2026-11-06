"""The SCRUDful role API: ``/roles``, ``/roles/{pk}`` and ``/roles/search``.

Authentication and authorization stand in for django-scoped-rbac: the
``X-Rbac-Context`` header names the caller's rbac_context (a real API would
derive what the caller may see from its credentials). Everything scrudful
computes, the validators and the precondition results, comes from what the
authorization already approved:

* the item operations get the role from ``load=authorized_role``, which only
  finds roles in the caller's context;
* the list returns the queryset scoped to the caller's context;
* the create and the search decide from the parsed body.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, List

from django.db.models import Max, QuerySet
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from ninja import Schema, Status
from ninja.errors import HttpError
from ninja.security import APIKeyHeader

from scrudful.ninja import (
    CONDITIONAL_READ_RESPONSES,
    CONDITIONAL_WRITE_RESPONSES,
    Entity,
    ScrudfulRouter,
    scrudful,
    scrudful_create,
    scrudful_list,
)

from .models import Role, new_etag


class RbacContextHeader(APIKeyHeader):
    """Authentication: the ``X-Rbac-Context`` header, else 401.

    ``request.auth`` is the caller's rbac_context.
    """

    param_name = "X-Rbac-Context"

    def authenticate(self, request: HttpRequest, key: str | None) -> str | None:
        return key or None


# --- Schemas ----------------------------------------------------------------------


class RoleIn(Schema):
    name: str
    rbac_context: str


class RolePatch(Schema):
    # Omitted: unchanged. An explicit null is rejected (422): pydantic doesn't
    # validate defaults, only the values sent.
    name: str = None  # type: ignore[assignment]


class RoleOut(Schema):
    id: int
    name: str
    rbac_context: str


class RoleSearch(Schema):
    name: str


class Message(Schema):
    detail: str


# --- Links ------------------------------------------------------------------------


def document_link(kind: str, name: str) -> Any:
    """A link callable: the URL of a document of ``documents.py``.

    Link callables get ``(request, obj)`` on a response, and ``(None, None)``
    when the HTTP Schema Resource is built, so they must return the same
    target without either. The URL is relative: scrudful resolves it against
    ``SCRUDFUL["BASE_URL"]`` (settings.py).
    """

    def link(request: HttpRequest | None, obj: Any) -> str:
        return reverse(f"api:{kind}", kwargs={"name": name})

    return link


def documents(name: str) -> dict[str, Any]:
    """The schema and context links of the documents ``name``."""
    return dict(
        schema_link_or_func=document_link("schema", name),
        context_link_or_func=document_link("context", name),
    )


def entity(name: str) -> Entity:
    return Entity(**documents(name))


#: scrudful's own 400, 412 and 428 bodies ({"detail": ...}) carry these Links.
ERROR = entity("error")


# --- Item callbacks, shared by every operation on roles -----------------------------


def authorized_role(request: HttpRequest, pk: int, **params: Any) -> Role:
    """Fetch-if-allowed: the role, if it is in the caller's rbac_context.

    The "No PUT-create" pattern: a missing role and a hidden one both raise
    404, so they are indistinguishable for every method and If-* header.
    Roles are created with POST /roles.
    """
    return get_object_or_404(Role, pk=pk, rbac_context=request.auth)


def role_etag(role: Role) -> str:
    return role.etag


def role_modified(role: Role) -> datetime:
    return role.modified_at


def role_url(request: HttpRequest, role: Role) -> str:
    return request.build_absolute_uri(reverse("api:role", kwargs={"pk": role.pk}))


#: The validators and Links of a role, for the item, create and list operations,
#: so the 201 and each list entry carry what a GET of the role sends.
ROLE = dict(etag_func=role_etag, last_modified_func=role_modified, **documents("role"))
#: The item operations: the validators are computed from the loaded role.
ITEM = dict(load=authorized_role, error=ERROR, **ROLE)


def save_role(role: Role, **changes: Any) -> Role:
    """Write ``changes`` to ``role`` if nobody changed it since it was loaded.

    The preconditions are evaluated before the handler runs, so two requests
    with the same If-Match can both pass them; the UPDATE re-checks the ETag
    atomically, and the loser gets a 412.
    """
    changes.update(etag=new_etag(), modified_at=timezone.now())
    if not Role.objects.filter(pk=role.pk, etag=role.etag).update(**changes):
        raise HttpError(412, "Precondition failed.")
    role.refresh_from_db()
    return role


def forbidden(request: HttpRequest, rbac_context: str) -> Status | None:
    """Authorization of a body: roles are written only in the caller's context."""
    if rbac_context != request.auth:
        return Status(403, {"detail": f"You may not write roles in {rbac_context!r}."})
    return None


#: Answers OPTIONS on every path with a 204 whose Allow lists the methods
#: this requester may use, and links to the path's HTTP Schema Resource,
#: which it serves by GET under ``_hsr/``.
router = ScrudfulRouter(tags=["roles"])


# --- The collection: list and create ------------------------------------------------


def authorized_roles(request: HttpRequest, **params: Any) -> QuerySet[Role]:
    """The roles the caller may see: the list, and a create's target.

    scoped-rbac's list filter would go here; the model's Meta.ordering keeps
    the pages stable.
    """
    return Role.objects.filter(rbac_context=request.auth)


def roles_etag(roles: QuerySet[Role]) -> str:
    """The collection's ETag: changes when any listed role changes."""
    digest = hashlib.sha256()
    for pk, etag in roles.values_list("pk", "etag"):
        digest.update(f"{pk}:{etag};".encode())
    return digest.hexdigest()


def roles_modified(roles: QuerySet[Role]) -> datetime | None:
    return roles.aggregate(Max("modified_at"))["modified_at__max"]


@router.api_operation(
    ["GET", "HEAD"],
    "",
    response=List[RoleOut],
    openapi_extra={"responses": CONDITIONAL_READ_RESPONSES},
)
@scrudful_list(
    href_func=role_url,
    **ROLE,
    list_etag_func=roles_etag,
    list_last_modified_func=roles_modified,
    list_schema_link_or_func=document_link("schema", "roles"),
    list_context_link_or_func=document_link("context", "roles"),
    page_size=20,
    max_page_size=100,
    error=ERROR,
)
def list_roles(request: HttpRequest) -> QuerySet[Role]:
    return authorized_roles(request)


@router.post("", response={201: RoleOut, 403: Message})
@scrudful_create(
    location_func=role_url,
    **ROLE,
    # The target is the collection: If-Match takes the list's ETag (412 if
    # it changed), and a create without a precondition is never a 428.
    list_load=authorized_roles,
    list_etag_func=roles_etag,
    list_last_modified_func=roles_modified,
    error=ERROR,
    responses={403: ERROR},
)
def create_role(request: HttpRequest, payload: RoleIn) -> Any:
    # No load: authorization is decided from the body.
    denied = forbidden(request, payload.rbac_context)
    if denied is not None:
        return denied
    # 201 with Location, the role's Links and its ETag and Last-Modified.
    return Role.objects.create(**payload.dict())


# --- A general POST, async ---------------------------------------------------------


@router.post("/search", response=List[RoleOut])
@scrudful(request=entity("role-search"), **documents("role-search-result"))
async def search_roles(request: HttpRequest, search: RoleSearch) -> list[Role]:
    # An async operation: ninja serializes the result on the event loop, so
    # evaluate the queryset here (and select_related/prefetch_related what the
    # schema reads). 200 with the result's Links, no validators.
    roles = Role.objects.filter(rbac_context=request.auth, name__icontains=search.name)
    return [role async for role in roles]


# --- One role -----------------------------------------------------------------------


@router.api_operation(
    ["GET", "HEAD"],
    "/{int:pk}",
    response=RoleOut,
    url_name="role",
    openapi_extra={"responses": CONDITIONAL_READ_RESPONSES},
)
@scrudful(**ITEM, load_as="role")
def get_role(request: HttpRequest, pk: int, role: Role) -> Role:
    return role


@router.put(
    "/{int:pk}",
    response={200: RoleOut, 403: Message},
    openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES},
)
@scrudful(**ITEM, load_as="role", responses={403: ERROR})
def replace_role(request: HttpRequest, pk: int, payload: RoleIn, role: Role) -> Any:
    denied = forbidden(request, payload.rbac_context)
    if denied is not None:
        return denied
    # After a 2xx, scrudful calls load again for the new ETag and
    # Last-Modified; if it raised now, the client would see an error
    # although the write was committed.
    return save_role(role, **payload.dict())


@router.patch(
    "/{int:pk}",
    response=RoleOut,
    openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES},
)
@scrudful(**ITEM, load_as="role", request=entity("role-patch"))
def update_role(request: HttpRequest, pk: int, payload: RolePatch, role: Role) -> Role:
    return save_role(role, **payload.dict(exclude_unset=True))


@router.delete(
    "/{int:pk}",
    response={204: None},
    openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES},
)
@scrudful(**ITEM, load_as="role")
def delete_role(request: HttpRequest, pk: int, role: Role) -> None:
    # The 204 has no body, so it gets none of the role entity's Links.
    if not Role.objects.filter(pk=role.pk, etag=role.etag).delete()[0]:
        raise HttpError(412, "Precondition failed.")
