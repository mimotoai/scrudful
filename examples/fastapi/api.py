"""A SCRUDful user API built with FastAPI and ``scrudful.fastapi``.

Run it with ``python examples/fastapi/api.py`` and try, for example::

    curl -i http://localhost:8000/users/1
    curl -i http://localhost:8000/users/1 -H 'If-None-Match: "user-1-v1"'
    curl -i -X PUT http://localhost:8000/users/1 -H 'If-Match: "user-1-v1"' \\
        -H 'Content-Type: application/json' -d '{"name": "John", "age": 23}'
    curl -i -X OPTIONS http://localhost:8000/users/1

Every response with content carries the ``Link`` headers of its schema and
JSON-LD context (Semantic HTTP 0.1): the user's on success, the error's on
scrudful's own 400, 412 and 428 and on the errors FastAPI answers (404, 405,
422, and a JSON 500). OPTIONS answers with a 204 linking to the path's HTTP
Schema Resource (served at ``/_hsr/users.user_id`` and ``/_hsr/users``), and
every response of ``/users`` and ``/users/{user_id}`` carries that link too. Set
``SCRUDFUL_BASE_URL`` to the URL the app is served at (default
``http://localhost:8000/``): the relative link URLs resolve against it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from scrudful.fastapi import (
    CONDITIONAL_READ_RESPONSES,
    CONDITIONAL_WRITE_RESPONSES,
    Entity,
    add_error_links,
    add_options,
    scrudful,
)


class UserIn(BaseModel):
    """The writable fields of a user."""

    name: str = Field(min_length=1)
    age: int = Field(ge=0)


class UserPatch(BaseModel):
    """A partial update of a user."""

    # Omitted fields are left unchanged; an explicit null is rejected (422),
    # because defaults are not validated but supplied values are.
    name: str = Field(default=None, min_length=1)
    age: int = Field(default=None, ge=0)


class User(UserIn):
    id: int
    created_at: datetime
    updated_at: datetime


@dataclass
class _Record:
    user: User
    version: int = 1  # bumped on every update; part of the ETag


_records: dict[int, _Record] = {}
_next_id = 1


def _now() -> datetime:
    # Timezone-aware UTC: naive datetimes are taken to be UTC by scrudful.
    return datetime.now(timezone.utc)


def reset_data() -> None:
    """Restore the seed users (used by the tests)."""
    global _next_id
    seeded = datetime(2021, 5, 24, 9, 30, 15, 250000, tzinfo=timezone.utc)
    updated = datetime(2021, 5, 25, 14, 45, 30, 750000, tzinfo=timezone.utc)
    _records.clear()
    for user_id, (name, age) in enumerate(
        [("John", 22), ("Alice", 24), ("Bob", 25)], start=1
    ):
        user = User(
            id=user_id, name=name, age=age, created_at=seeded, updated_at=updated
        )
        _records[user_id] = _Record(user)
    _next_id = len(_records) + 1


reset_data()


def _get_record(user_id: int) -> _Record:
    try:
        return _records[user_id]
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found") from None


# Callbacks receive the endpoint's keyword arguments (path, query, body and
# dependency values, plus a declared ``request``). Returning ``None`` means
# "this user doesn't exist", so the endpoint can answer 404.
#
# The link callbacks are also called with no arguments, ``func()``, when the
# HTTP Schema Resource is built (it is the same for every requester), so they
# take ``**kwargs`` only and return a relative URL, which resolves against
# ``base_url`` both there and on the Link headers.


def user_etag(user_id: int, **kwargs: Any) -> str | None:
    record = _records.get(user_id)
    return None if record is None else f"user-{user_id}-v{record.version}"


def user_last_modified(user_id: int, **kwargs: Any) -> datetime | None:
    record = _records.get(user_id)
    return None if record is None else record.user.updated_at


def user_schema_link(**kwargs: Any) -> str:
    return app.url_path_for("user_schema")


def user_context_link(**kwargs: Any) -> str:
    return app.url_path_for("user_context")


def error_schema_link(**kwargs: Any) -> str:
    return app.url_path_for("error_schema")


def error_context_link(**kwargs: Any) -> str:
    return app.url_path_for("error_context")


# The body of every error: scrudful's own 400/412/428 (``{"detail": "..."}``,
# with the accepted headers on a 428) and, through ``add_error_links`` below,
# FastAPI's (``{"detail": "..."}``, or a list of validation errors on a 422).
ERROR = Entity(
    schema_link_or_func=error_schema_link, context_link_or_func=error_context_link
)
ERROR_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Error",
    "type": "object",
    "properties": {
        "detail": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "object"}},
            ]
        },
        "accepted-precondition-headers": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}
ERROR_CONTEXT = {"@context": {"@vocab": "https://schema.org/", "detail": "description"}}
USER_LINKS: dict[str, Any] = dict(
    schema_link_or_func=user_schema_link,
    context_link_or_func=user_context_link,
    error=ERROR,
)
USER_VALIDATORS: dict[str, Any] = dict(
    etag_func=user_etag, last_modified_func=user_last_modified
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "User not found"}}
READ_RESPONSES = {**CONDITIONAL_READ_RESPONSES, **NOT_FOUND}
WRITE_RESPONSES = {**CONDITIONAL_WRITE_RESPONSES, **NOT_FOUND}

BASE_URL = os.environ.get("SCRUDFUL_BASE_URL", "http://localhost:8000/")

app = FastAPI(title="SCRUDful users")


@app.get("/schemas/user.json", name="user_schema")
async def get_user_schema() -> dict[str, Any]:
    return User.model_json_schema()


@app.get("/contexts/user.jsonld", name="user_context")
async def get_user_context() -> dict[str, Any]:
    return {
        "@context": {
            "@vocab": "https://schema.org/",
            "name": "name",
            "age": "age",
            "created_at": "dateCreated",
            "updated_at": "dateModified",
        }
    }


@app.get("/schemas/error.json", name="error_schema")
async def get_error_schema() -> dict[str, Any]:
    return ERROR_SCHEMA


@app.get("/contexts/error.jsonld", name="error_context")
async def get_error_context() -> dict[str, Any]:
    return ERROR_CONTEXT


@app.post("/users", status_code=status.HTTP_201_CREATED, response_model=User)
@scrudful(**USER_LINKS)
async def create_user(request: Request, response: Response, user: UserIn) -> User:
    global _next_id
    now = _now()
    created = User(id=_next_id, **user.model_dump(), created_at=now, updated_at=now)
    _records[created.id] = _Record(created)
    _next_id += 1
    response.headers["Location"] = str(request.url_for("get_user", user_id=created.id))
    return created


# ``@scrudful`` goes below the route decorators. FastAPI routes HEAD only to a
# route that lists it: ``@app.head`` serves it with the GET endpoint
# (an ``api_route(..., methods=["GET", "HEAD"])`` would too, but FastAPI would
# give both methods the same OpenAPI operation id, and warn).
@app.get("/users/{user_id}", response_model=User, responses=READ_RESPONSES)
@app.head("/users/{user_id}", response_model=User, responses=READ_RESPONSES)
@scrudful(**USER_VALIDATORS, **USER_LINKS)
async def get_user(user_id: int) -> User:
    return _get_record(user_id).user


@app.put("/users/{user_id}", response_model=User, responses=WRITE_RESPONSES)
@scrudful(**USER_VALIDATORS, **USER_LINKS)
async def replace_user(user_id: int, user: UserIn) -> User:
    record = _get_record(user_id)
    record.user = record.user.model_copy(
        update={**user.model_dump(), "updated_at": _now()}
    )
    record.version += 1
    return record.user


@app.patch("/users/{user_id}", response_model=User, responses=WRITE_RESPONSES)
@scrudful(**USER_VALIDATORS, **USER_LINKS)
async def update_user(user_id: int, patch: UserPatch) -> User:
    record = _get_record(user_id)
    changes = patch.model_dump(exclude_unset=True)
    record.user = record.user.model_copy(update={**changes, "updated_at": _now()})
    record.version += 1
    return record.user


@app.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=WRITE_RESPONSES,
)
@scrudful(**USER_VALIDATORS, error=ERROR)
async def delete_user(user_id: int) -> None:
    _get_record(user_id)
    del _records[user_id]


# Before the app serves requests: the errors FastAPI answers get ERROR's Links,
# and every path with a scrudful endpoint answers OPTIONS.
add_error_links(app, ERROR)
add_options(app, base_url=BASE_URL)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
