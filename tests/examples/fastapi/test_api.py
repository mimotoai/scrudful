"""Tests of the example FastAPI app (``examples/fastapi/api.py``)."""

import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import pytest
from fastapi.testclient import TestClient

from examples.fastapi import api
from scrudful._adapter import allow_header, options_links
from tests.semantic_http import (
    link_problems,
    parse_links,
    validate_http_schema_resource,
)

BASE = "http://testserver"
SCHEMA_LINK = (
    f'<{BASE}/schemas/user.json>; rel="describedby"; type="application/schema+json"'
)
CONTEXT_LINK = (
    f'<{BASE}/contexts/user.jsonld>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
LINKS = f"{SCHEMA_LINK}, {CONTEXT_LINK}"
ERROR_LINKS = (
    f'<{BASE}/schemas/error.json>; rel="describedby"; type="application/schema+json", '
    f'<{BASE}/contexts/error.jsonld>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
# The seed data has sub-second timestamps; the header is floored to seconds.
SEED_MODIFIED = "Tue, 25 May 2021 14:45:30 GMT"
BEFORE_SEED_MODIFIED = "Tue, 25 May 2021 14:45:29 GMT"
JOHN = {
    "id": 1,
    "name": "John",
    "age": 22,
    "created_at": "2021-05-24T09:30:15.250000Z",
    "updated_at": "2021-05-25T14:45:30.750000Z",
}


@pytest.fixture(autouse=True)
def fresh_data():
    api.reset_data()
    yield
    api.reset_data()


@pytest.fixture
def client():
    with TestClient(api.app) as client:
        yield client


def assert_unchanged(client, user_id=2):
    response = client.get(f"/users/{user_id}")
    assert response.status_code == 200
    assert response.headers["etag"] == f'"user-{user_id}-v1"'


# --- GET ---------------------------------------------------------------------------


def test_get_user(client):
    response = client.get("/users/1")
    assert response.status_code == 200
    assert response.json() == JOHN
    assert response.headers["etag"] == '"user-1-v1"'
    assert response.headers["last-modified"] == SEED_MODIFIED
    assert response.headers["link"] == LINKS
    assert response.headers["access-control-expose-headers"] == "ETag, Link"


def test_links_resolve(client):
    schema = client.get("/schemas/user.json").json()
    assert schema["title"] == "User"
    assert set(schema["properties"]) == set(JOHN)
    context = client.get("/contexts/user.jsonld").json()
    assert context["@context"]["updated_at"] == "dateModified"


@pytest.mark.parametrize("headers", [{}, {"If-None-Match": "*"}])
def test_get_missing_user(client, headers):
    response = client.get("/users/99", headers=headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}
    assert "etag" not in response.headers
    assert "last-modified" not in response.headers


@pytest.mark.parametrize(
    "if_none_match",
    ['"user-1-v1"', 'W/"user-1-v1"', "*", '"user-2-v1", "user-1-v1"'],
)
def test_if_none_match_gives_304(client, if_none_match):
    response = client.get("/users/1", headers={"If-None-Match": if_none_match})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == '"user-1-v1"'
    assert response.headers["last-modified"] == SEED_MODIFIED


def test_if_none_match_of_another_user_gives_200(client):
    response = client.get("/users/1", headers={"If-None-Match": '"user-2-v1"'})
    assert response.status_code == 200


def test_if_modified_since_echo_gives_304(client):
    last_modified = client.get("/users/1").headers["last-modified"]
    response = client.get("/users/1", headers={"If-Modified-Since": last_modified})
    assert response.status_code == 304
    response = client.get("/users/1", headers={"If-Modified-Since": "not a date"})
    assert response.status_code == 200
    earlier = {"If-Modified-Since": BEFORE_SEED_MODIFIED}
    assert client.get("/users/1", headers=earlier).status_code == 200


def test_head_user(client):
    response = client.head("/users/1")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["etag"] == '"user-1-v1"'
    assert response.headers["link"] == LINKS
    response = client.head("/users/1", headers={"If-None-Match": '"user-1-v1"'})
    assert response.status_code == 304


# --- POST --------------------------------------------------------------------------


def test_create_user(client):
    response = client.post("/users", json={"name": "Lisa", "age": 23})
    assert response.status_code == 201
    body = response.json()
    assert (body["id"], body["name"], body["age"]) == (4, "Lisa", 23)
    assert body["created_at"] == body["updated_at"]
    assert response.headers["location"] == f"{BASE}/users/4"
    assert response.headers["link"] == LINKS
    assert response.headers["access-control-expose-headers"] == "Link, Location"
    assert "etag" not in response.headers
    assert "last-modified" not in response.headers

    created = client.get(response.headers["location"])
    assert created.status_code == 200
    assert created.json() == body
    assert created.headers["etag"] == '"user-4-v1"'


def test_create_user_ignores_preconditions(client):
    """POST /users has no validators, so precondition headers don't apply."""
    headers = {
        "If-Match": '"nothing"',
        "If-Unmodified-Since": "Thu, 01 Jan 1970 00:00:00 GMT",
    }
    response = client.post("/users", json={"name": "Lisa", "age": 23}, headers=headers)
    assert response.status_code == 201


@pytest.mark.parametrize(
    "body", [{"name": "", "age": 1}, {"name": "Lisa", "age": -1}, {"name": "Lisa"}]
)
def test_create_user_validates_input(client, body):
    assert client.post("/users", json=body).status_code == 422
    assert client.get("/users/4").status_code == 404


def test_created_ids_never_collide(client):
    deleted = client.delete("/users/2", headers={"If-Match": '"user-2-v1"'})
    assert deleted.status_code == 204
    first = client.post("/users", json={"name": "Lisa", "age": 23}).json()
    second = client.post("/users", json={"name": "Mo", "age": 30}).json()
    assert (first["id"], second["id"]) == (4, 5)
    assert client.get("/users/3").json()["name"] == "Bob"


# --- PUT / PATCH -------------------------------------------------------------------


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
@pytest.mark.parametrize(
    "headers",
    [{}, {"If-Unmodified-Since": "yesterday"}, {"If-None-Match": '"user-2-v0"'}],
)
def test_write_without_precondition_gives_428(client, method, headers):
    response = client.request(
        method, "/users/2", json={"name": "Eve", "age": 99}, headers=headers
    )
    assert response.status_code == 428
    assert response.json() == {
        "detail": "This request must be conditional.",
        "accepted-precondition-headers": ["If-Match", "If-Unmodified-Since"],
    }
    assert_unchanged(client)


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
@pytest.mark.parametrize("if_match", ["user-2-v1", "not-the-etag", '"user-2-v1'])
def test_write_with_malformed_if_match_gives_400(client, method, if_match):
    response = client.request(
        method,
        "/users/2",
        json={"name": "Eve", "age": 99},
        headers={"If-Match": if_match, "If-Unmodified-Since": "yesterday"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Malformed If-Match header."}
    assert_unchanged(client)


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
@pytest.mark.parametrize(
    "headers",
    [
        {"If-Match": '"user-1-v1"'},
        {"If-Match": 'W/"user-2-v1"'},
        {"If-Unmodified-Since": BEFORE_SEED_MODIFIED},
        {"If-Match": '"user-2-v1"', "If-None-Match": '"user-2-v1"'},
    ],
)
def test_write_with_failed_precondition_gives_412(client, method, headers):
    response = client.request(
        method, "/users/2", json={"name": "Eve", "age": 99}, headers=headers
    )
    assert response.status_code == 412
    assert response.json() == {"detail": "Precondition failed."}
    assert_unchanged(client)


def test_put_replaces_user_and_returns_fresh_validators(client):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    response = client.put(
        "/users/2",
        json={"id": 1, "name": "Alicia", "age": 25},
        headers={"If-Match": '"user-2-v1"'},
    )
    after = datetime.now(timezone.utc)
    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["name"], body["age"]) == (2, "Alicia", 25)
    assert response.headers["etag"] == '"user-2-v2"'
    assert response.headers["link"] == LINKS
    assert response.headers["access-control-expose-headers"] == "ETag, Link"
    last_modified = parsedate_to_datetime(response.headers["last-modified"])
    assert before <= last_modified <= after
    assert client.get("/users/1").json() == JOHN

    current = client.get("/users/2")
    assert current.json() == body
    assert current.headers["etag"] == '"user-2-v2"'
    assert current.headers["last-modified"] == response.headers["last-modified"]


def test_validators_change_after_an_update(client):
    old = client.get("/users/2").headers
    put = client.put(
        "/users/2",
        json={"name": "Alicia", "age": 25},
        headers={"If-Match": old["etag"]},
    )
    new = put.headers

    assert client.get("/users/2", headers={"If-None-Match": old["etag"]}).json() == (
        put.json()
    )
    matching = {"If-None-Match": new["etag"]}
    assert client.get("/users/2", headers=matching).status_code == 304
    stale = {"If-Modified-Since": old["last-modified"]}
    assert client.get("/users/2", headers=stale).status_code == 200
    # Real (sub-second) timestamps still round-trip.
    fresh = {"If-Modified-Since": new["last-modified"]}
    assert client.get("/users/2", headers=fresh).status_code == 304
    lost_update = client.put(
        "/users/2", json={"name": "Eve", "age": 99}, headers={"If-Match": old["etag"]}
    )
    assert lost_update.status_code == 412
    assert client.get("/users/2").json()["name"] == "Alicia"


def test_put_with_if_unmodified_since(client):
    response = client.put(
        "/users/2",
        json={"name": "Alicia", "age": 25},
        headers={"If-Unmodified-Since": SEED_MODIFIED},
    )
    assert response.status_code == 200
    assert response.headers["etag"] == '"user-2-v2"'


def test_put_missing_user(client):
    body = {"name": "Eve", "age": 99}
    assert client.put("/users/99", json=body).status_code == 404
    response = client.put("/users/99", json=body, headers={"If-Match": '"user-99-v1"'})
    assert response.status_code == 412


def test_patch_updates_only_the_given_fields(client):
    response = client.patch(
        "/users/2", json={"age": 30}, headers={"If-Match": '"user-2-v1"'}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["age"]) == ("Alice", 30)
    assert response.headers["etag"] == '"user-2-v2"'
    assert response.headers["last-modified"] != SEED_MODIFIED
    assert (
        client.patch(
            "/users/2", json={"age": 31}, headers={"If-Match": '"user-2-v1"'}
        ).status_code
        == 412
    )


@pytest.mark.parametrize("body", [{"name": None}, {"age": None}, {"name": ""}])
def test_patch_rejects_invalid_values(client, body):
    response = client.patch("/users/2", json=body, headers={"If-Match": '"user-2-v1"'})
    assert response.status_code == 422
    assert_unchanged(client)
    assert client.get("/users/2").json()["name"] == "Alice"


def test_put_under_a_non_utc_local_timezone(client):
    """Last-Modified is real UTC whatever the server's local timezone."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is not available on this platform")
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    try:
        before = datetime.now(timezone.utc).replace(microsecond=0)
        response = client.put(
            "/users/2",
            json={"name": "Alicia", "age": 25},
            headers={"If-Match": '"user-2-v1"'},
        )
        after = datetime.now(timezone.utc)
    finally:
        if saved is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = saved
        time.tzset()
    last_modified = parsedate_to_datetime(response.headers["last-modified"])
    assert before <= last_modified <= after


# --- DELETE ------------------------------------------------------------------------


def test_delete_user(client):
    response = client.delete("/users/2", headers={"If-Match": '"user-2-v1"'})
    assert response.status_code == 204
    assert response.content == b""
    assert "etag" not in response.headers
    assert "link" not in response.headers
    assert client.get("/users/2").status_code == 404
    assert client.delete("/users/2").status_code == 404
    again = client.delete("/users/2", headers={"If-Match": '"user-2-v1"'})
    assert again.status_code == 404
    assert [client.get(f"/users/{i}").status_code for i in (1, 3)] == [200, 200]


@pytest.mark.parametrize(
    "headers, status",
    [
        ({}, 428),
        ({"If-Match": "user-2-v1"}, 400),
        ({"If-Match": '"user-2-v0"'}, 412),
        ({"If-Match": '"user-2-v1"', "If-None-Match": "*"}, 412),
        ({"If-Unmodified-Since": BEFORE_SEED_MODIFIED}, 412),
    ],
)
def test_delete_preconditions(client, headers, status):
    assert client.delete("/users/2", headers=headers).status_code == status
    assert_unchanged(client)


def test_delete_with_if_unmodified_since(client):
    response = client.delete("/users/2", headers={"If-Unmodified-Since": SEED_MODIFIED})
    assert response.status_code == 204


def test_delete_missing_user_with_precondition(client):
    response = client.delete("/users/99", headers={"If-Match": '"user-99-v1"'})
    assert response.status_code == 404


# --- OpenAPI -----------------------------------------------------------------------


def test_openapi_documents_conditional_responses(client):
    paths = client.get("/openapi.json").json()["paths"]
    get = paths["/users/{user_id}"]["get"]
    assert set(get["responses"]) == {"200", "304", "400", "404", "412", "422"}
    assert [p["name"] for p in get["parameters"]] == ["user_id"]
    for method, success in [("put", "200"), ("patch", "200"), ("delete", "204")]:
        operation = paths["/users/{user_id}"][method]
        expected = {success, "400", "404", "412", "422", "428"}
        assert set(operation["responses"]) == expected
        assert [p["name"] for p in operation["parameters"]] == ["user_id"]
    patch = client.get("/openapi.json").json()["components"]["schemas"]["UserPatch"]
    assert patch["properties"]["name"]["type"] == "string"  # not nullable
    post = paths["/users"]["post"]
    assert "parameters" not in post
    assert set(post["responses"]) == {"201", "422"}


# --- OPTIONS and Semantic HTTP 0.1 conformance ---------------------------------


def test_options_user(client):
    response = client.options("/users/1")
    assert response.status_code == 200
    body = response.json()
    validate_http_schema_resource(body)
    assert response.headers["allow"] == "GET, PUT, DELETE, OPTIONS, HEAD, PATCH"
    assert response.headers["allow"] == allow_header(body)
    assert response.headers["link"] == options_links()
    assert body["get"]["responses"]["200"]["content"] == {
        "application/json": {
            "schema": {"$ref": f"{BASE}/schemas/user.json"},
            "x-rdf-context": {
                "href": f"{BASE}/contexts/user.jsonld",
                "type": "application/ld+json",
            },
        }
    }
    assert body["put"]["responses"]["428"]["content"] == {
        "application/json": {
            "schema": {"$ref": f"{BASE}/schemas/error.json"},
            "x-rdf-context": {
                "href": f"{BASE}/contexts/error.jsonld",
                "type": "application/ld+json",
            },
        }
    }
    users = client.options("/users")
    assert users.headers["allow"] == "POST, OPTIONS"
    validate_http_schema_resource(users.json())


USER = "/users/2"
USERS = "/users"
IF_MATCH = {"If-Match": '"user-2-v1"'}
IF_MATCH_STALE = {"If-Match": '"user-2-v0"'}
NOT_MODIFIED = {"If-None-Match": "*"}
MALFORMED = {"If-Match": "user-2-v1"}
BODY = {"name": "Eve", "age": 99}
INVALID = {"name": "", "age": 99}

# (method, path, request arguments, status).
CONFORMANCE = [
    ("get", USER, {}, 200),
    ("get", USER, {"headers": NOT_MODIFIED}, 304),
    ("get", USER, {"headers": IF_MATCH_STALE}, 412),
    ("get", USER, {"headers": {"If-None-Match": "user-2-v1"}}, 400),
    ("head", USER, {}, 200),
    ("head", USER, {"headers": NOT_MODIFIED}, 304),
    ("head", USER, {"headers": IF_MATCH_STALE}, 412),
    ("put", USER, {"json": BODY, "headers": IF_MATCH}, 200),
    ("put", USER, {"json": BODY}, 428),
    ("put", USER, {"json": BODY, "headers": IF_MATCH_STALE}, 412),
    ("put", USER, {"json": BODY, "headers": MALFORMED}, 400),
    ("put", USER, {"json": INVALID, "headers": IF_MATCH}, 422),
    ("patch", USER, {"json": {"age": 30}, "headers": IF_MATCH}, 200),
    ("patch", USER, {"json": {"age": 30}}, 428),
    ("patch", USER, {"json": {"age": -1}, "headers": IF_MATCH}, 422),
    ("delete", USER, {"headers": IF_MATCH}, 204),
    ("delete", USER, {}, 428),
    ("delete", USER, {"headers": IF_MATCH_STALE}, 412),
    ("delete", USER, {"headers": MALFORMED}, 400),
    ("options", USER, {}, 200),
    ("get", "/users/99", {}, 404),
    ("head", "/users/99", {}, 404),
    ("get", "/users/x", {}, 422),
    ("put", "/users/99", {"json": BODY, "headers": IF_MATCH}, 412),
    ("delete", "/users/99", {"headers": {"If-Match": '"user-99-v1"'}}, 404),
    ("post", USER, {}, 405),
    ("post", USERS, {"json": BODY}, 201),
    ("post", USERS, {"json": INVALID}, 422),
    ("options", USERS, {}, 200),
    ("get", USERS, {}, 405),
    ("get", "/nowhere", {}, 404),
]
# Statuses with nothing in the OPTIONS bodies to agree with: the 404 the
# endpoints and Starlette raise, the 405 and FastAPI's 422 are not listed, and a
# 304 (a HEAD's too) has no content, hence no entry. Their Links are still
# checked by the rules that don't need the body.
NO_ENTRY = {304, 404, 405, 422}


@pytest.mark.parametrize(
    "method, path, arguments, status",
    CONFORMANCE,
    ids=[f"{m}-{p}-{s}-{i}" for i, (m, p, _, s) in enumerate(CONFORMANCE)],
)
def test_example_links_everywhere(client, method, path, arguments, status):
    """Every response, a HEAD included, follows the Semantic HTTP 0.1 Link rules
    and agrees with the OPTIONS body of its path."""
    options = client.options(path)
    hsr = options.json() if options.status_code == 200 else None
    response = client.request(method.upper(), path, **arguments)
    assert response.status_code == status
    content = method != "head" and bool(response.content)
    assert content == (status not in (204, 304) and method != "head")
    problems = link_problems(
        method.upper(),
        status,
        dict(response.headers),
        content,
        None if status in NO_ENTRY else hsr,
    )
    assert problems == []
    if status in (404, 405, 422):
        # The Links are still those of the error entity.
        assert response.headers["link"] == ERROR_LINKS
        assert response.headers["content-type"] == "application/json"


def test_example_links_are_served_with_the_documents(client):
    """Every document a Link points at is served."""
    links = parse_links(client.get(USER).headers["link"])
    links += parse_links(client.put(USER, json=BODY).headers["link"])
    links += parse_links(client.post(USERS, json=BODY).headers["link"])
    for target, _ in links:
        assert client.get(target.removeprefix(BASE)).status_code == 200
