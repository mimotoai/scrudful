"""A SCRUDful user API built with FastAPI and ``scrudful.fastapi``.

Run it with ``python examples/fastapi/api.py`` and try, for example::

    curl -i http://localhost:8000/users/1
    curl -i http://localhost:8000/users/1 -H 'If-None-Match: "user-1-v1"'
    curl -i -X PUT http://localhost:8000/users/1 -H 'If-Match: "user-1-v1"' \\
        -H 'Content-Type: application/json' -d '{"name": "John", "age": 23}'
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from scrudful.fastapi import CONDITIONAL_RESPONSES, scrudful


class UserIn(BaseModel):
    """The writable fields of a user."""

    name: str = Field(min_length=1)
    age: int = Field(ge=0)


class UserPatch(BaseModel):
    """A partial update of a user."""

    name: str | None = Field(default=None, min_length=1)
    age: int | None = Field(default=None, ge=0)


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


def user_etag(user_id: int, **kwargs: Any) -> str | None:
    record = _records.get(user_id)
    return None if record is None else f"user-{user_id}-v{record.version}"


def user_last_modified(user_id: int, **kwargs: Any) -> datetime | None:
    record = _records.get(user_id)
    return None if record is None else record.user.updated_at


def user_schema_link(request: Request, **kwargs: Any) -> Any:
    return request.url_for("user_schema")


def user_context_link(request: Request, **kwargs: Any) -> Any:
    return request.url_for("user_context")


USER_RESPONSES: dict[int | str, dict[str, Any]] = {
    **CONDITIONAL_RESPONSES,
    404: {"description": "User not found"},
}

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


@app.post("/users", status_code=status.HTTP_201_CREATED, response_model=User)
@scrudful(
    schema_link_or_func=user_schema_link,
    schema_type_or_func="application/schema+json",
    context_link_or_func=user_context_link,
)
async def create_user(request: Request, response: Response, user: UserIn) -> User:
    global _next_id
    now = _now()
    created = User(id=_next_id, **user.model_dump(), created_at=now, updated_at=now)
    _records[created.id] = _Record(created)
    _next_id += 1
    response.headers["Location"] = str(request.url_for("get_user", user_id=created.id))
    return created


# ``@scrudful`` goes below the route decorator. The endpoints declare ``request``
# only so that the link callbacks receive it.
@app.get("/users/{user_id}", response_model=User, responses=USER_RESPONSES)
@scrudful(
    etag_func=user_etag,
    last_modified_func=user_last_modified,
    schema_link_or_func=user_schema_link,
    schema_type_or_func="application/schema+json",
    context_link_or_func=user_context_link,
)
async def get_user(request: Request, user_id: int) -> User:
    return _get_record(user_id).user


@app.put("/users/{user_id}", response_model=User, responses=USER_RESPONSES)
@scrudful(
    etag_func=user_etag,
    last_modified_func=user_last_modified,
    schema_link_or_func=user_schema_link,
    schema_type_or_func="application/schema+json",
    context_link_or_func=user_context_link,
)
async def replace_user(request: Request, user_id: int, user: UserIn) -> User:
    record = _get_record(user_id)
    record.user = record.user.model_copy(
        update={**user.model_dump(), "updated_at": _now()}
    )
    record.version += 1
    return record.user


@app.patch("/users/{user_id}", response_model=User, responses=USER_RESPONSES)
@scrudful(
    etag_func=user_etag,
    last_modified_func=user_last_modified,
    schema_link_or_func=user_schema_link,
    schema_type_or_func="application/schema+json",
    context_link_or_func=user_context_link,
)
async def update_user(request: Request, user_id: int, patch: UserPatch) -> User:
    record = _get_record(user_id)
    changes = patch.model_dump(exclude_unset=True)
    record.user = record.user.model_copy(update={**changes, "updated_at": _now()})
    record.version += 1
    return record.user


@app.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=USER_RESPONSES,
)
@scrudful(
    etag_func=user_etag,
    last_modified_func=user_last_modified,
)
async def delete_user(user_id: int) -> None:
    _get_record(user_id)
    del _records[user_id]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
