"""Tests of the example FastAPI app (``examples/fastapi/api.py``)."""

import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import pytest
from fastapi.testclient import TestClient

from examples.fastapi import api
from scrudful._hsr import hsr_link, options_operation
from scrudful.fastapi import add_options
from tests import test_wire_fastapi as wire
from tests.semantic_http import (
    HSR_DOCUMENT_REL,
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    undescribed_problems,
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
#: The HSR links of /users/{user_id} and /users, on each of their responses.
USER_HSR_LINK = hsr_link(f"{BASE}/_hsr/users.user_id")
USERS_HSR_LINK = hsr_link(f"{BASE}/_hsr/users")
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


@pytest.fixture(autouse=True, scope="module")
def test_base_url():
    """The test client's URL as the base URL (the example's is localhost:8000).

    Calling ``add_options`` again replaces its options: give the example's
    ``load`` again."""
    add_options(api.app, base_url=f"{BASE}/", load=api.load_user)
    yield
    add_options(api.app, base_url=api.BASE_URL, load=api.load_user)


@pytest.fixture(autouse=True)
def fresh_data():
    api.reset_data()
    yield
    api.reset_data()


@pytest.fixture
def client():
    with TestClient(api.app) as client:
        yield client


def hsr_of(client, path):
    """The HTTP Schema Resource the OPTIONS of ``path`` links to: the 204's
    HSR link, followed by a GET of the HSR route."""
    response = client.options(path)
    headers = dict(response.headers)
    assert options_204_problems(response.status_code, headers, response.content) == []
    (target,) = [
        target
        for target, params in parse_links(headers["link"])
        if params.get("rel") == HSR_DOCUMENT_REL
    ]
    document = client.get(target)
    assert (
        hsr_get_problems(document.status_code, dict(document.headers), document.content)
        == []
    )
    return document.json()


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
    assert response.headers["link"] == f"{LINKS}, {USER_HSR_LINK}"
    assert response.headers["access-control-expose-headers"] == "ETag, Link, Allow"


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
    assert response.headers["link"] == f"{LINKS}, {USER_HSR_LINK}"
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
    assert response.headers["link"] == f"{LINKS}, {USERS_HSR_LINK}"
    assert response.headers["access-control-expose-headers"] == "Link, Location, Allow"
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
    assert response.headers["link"] == f"{LINKS}, {USER_HSR_LINK}"
    assert response.headers["access-control-expose-headers"] == "ETag, Link, Allow"
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
    # No content: only the HSR link.
    assert response.headers["link"] == USER_HSR_LINK
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
    headers = dict(response.headers)
    allow = {"GET", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"}
    assert (
        options_204_problems(response.status_code, headers, response.content, allow)
        == []
    )
    assert response.status_code == 204
    assert response.headers["allow"] == "GET, PUT, DELETE, OPTIONS, HEAD, PATCH"
    assert hsr_link_problems(headers, f"{BASE}/_hsr/users.user_id") == []
    body = hsr_of(client, "/users/1")
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "delete", "options", "head", "patch"]
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
    assert body["options"] == options_operation()
    users = client.options("/users")
    headers = dict(users.headers)
    assert options_204_problems(users.status_code, headers, users.content) == []
    assert users.headers["allow"] == "POST, OPTIONS"
    assert hsr_link_problems(headers, f"{BASE}/_hsr/users") == []
    validate_http_schema_resource(hsr_of(client, "/users"))


USER = "/users/2"
USERS = "/users"
#: The HSR route of the item (any /users/<id>) and of the collection.
HSR_ROUTES = {"/users/": "/_hsr/users.user_id", USERS: "/_hsr/users"}
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
    ("options", USER, {}, 204),
    ("get", "/users/99", {}, 404),
    ("head", "/users/99", {}, 404),
    ("get", "/users/x", {}, 422),
    ("put", "/users/99", {"json": BODY, "headers": IF_MATCH}, 412),
    ("delete", "/users/99", {"headers": {"If-Match": '"user-99-v1"'}}, 404),
    ("post", USER, {}, 405),
    ("post", USERS, {"json": BODY}, 201),
    ("post", USERS, {"json": INVALID}, 422),
    ("options", USERS, {}, 204),
    ("get", USERS, {}, 405),
    ("get", "/nowhere", {}, 404),
]
# Statuses with nothing in the OPTIONS bodies to agree with: the 405 (of a
# method the path doesn't serve) and a 304 (a HEAD's too), which has no
# content, hence no entry. Their Links are still checked by the rules that
# don't need the body. The 404 the endpoints raise and FastAPI's 422 agree with
# the ``default`` entry (Starlette's 404 for an unknown path has no body).
NO_ENTRY = {304, 405}


@pytest.mark.parametrize(
    "method, path, arguments, status",
    CONFORMANCE,
    ids=[f"{m}-{p}-{s}-{i}" for i, (m, p, _, s) in enumerate(CONFORMANCE)],
)
def test_example_links_everywhere(client, method, path, arguments, status):
    """Every response, a HEAD included, follows the Semantic HTTP 0.1 Link rules
    and agrees with the OPTIONS body of its path."""
    route = HSR_ROUTES.get("/users/" if path.startswith("/users/") else path)
    hsr = None if route is None else client.get(route).json()
    response = client.request(method.upper(), path, **arguments)
    assert response.status_code == status
    content = method != "head" and bool(response.content)
    assert content == (status not in (204, 304) and method != "head")
    if method == "options":
        headers = dict(response.headers)
        assert options_204_problems(status, headers, response.content) == []
        return
    problems = link_problems(
        method.upper(),
        status,
        dict(response.headers),
        content,
        None if status in NO_ENTRY else hsr,
    )
    assert problems == []
    # The HSR link of the path, on every response of a described one.
    hsr_link_value = {USER: USER_HSR_LINK, USERS: USERS_HSR_LINK}.get(path)
    if path.startswith("/users/"):
        hsr_link_value = USER_HSR_LINK
    if hsr_link_value is None:
        assert HSR_DOCUMENT_REL not in response.headers.get("link", "")
    else:
        assert response.headers["link"].endswith(hsr_link_value)
    if status in (404, 405, 422):
        # The Links are still those of the error entity.
        expected = ERROR_LINKS
        if hsr_link_value is not None:
            expected = f"{ERROR_LINKS}, {hsr_link_value}"
        assert response.headers["link"] == expected
        assert response.headers["content-type"] == "application/json"


def test_example_links_are_served_with_the_documents(client):
    """Every document a Link points at is served."""
    links = parse_links(client.get(USER).headers["link"])
    links += parse_links(client.put(USER, json=BODY).headers["link"])
    links += parse_links(client.post(USERS, json=BODY).headers["link"])
    for target, _ in links:
        assert client.get(target.removeprefix(BASE)).status_code == 200


def test_options_of_a_missing_user_is_load_users_404(client):
    """``add_options(load=load_user)``: OPTIONS of a missing user is the 404
    a GET gets (a non-integer id too), with the error Links and the HSR link;
    a preflight loads nothing."""
    for path in ("/users/99", "/users/x"):
        response = client.options(path)
        headers = dict(response.headers)
        assert response.status_code == 404
        assert "allow" not in headers
        assert link_problems("OPTIONS", 404, headers, True) == []
        assert response.headers["link"] == f"{ERROR_LINKS}, {USER_HSR_LINK}"
    preflight = {"Origin": "https://a.example", "Access-Control-Request-Method": "PUT"}
    assert client.options("/users/99", headers=preflight).status_code == 204


# --- The wire-conformance suite, on the example --------------------------------
#
# The exchanges of tests/test_wire_fastapi.py with the example app, checked as
# that suite checks them (tests/semantic_http.py).


@pytest.mark.parametrize("exchange", wire.example_exchanges(), ids=lambda e: e.id)
def test_wire_exchanges(client, exchange):
    """Every response has its content Links, the HSR link of its resource
    (none for an unknown URL) and agrees with its HSR entry."""
    response = wire.send({"example": client}, exchange)
    headers = wire.headers_of(response)
    content = wire.has_content(exchange, response)
    hsr = None
    if exchange.kind is not None and exchange.status not in (304, 405):
        hsr = client.get(wire.KINDS[exchange.kind].hsr).json()
    problems = link_problems(exchange.method, exchange.status, headers, content, hsr)
    assert problems == []
    if exchange.kind is None:
        assert HSR_DOCUMENT_REL not in headers.get("link", "")
    else:
        target = BASE + wire.KINDS[exchange.kind].hsr
        assert hsr_link_problems(headers, target) == []


@pytest.mark.parametrize(
    "path, allow",
    [
        ("/users/1", "GET, PUT, DELETE, OPTIONS, HEAD, PATCH"),
        ("/users", "POST, OPTIONS"),
    ],
)
def test_wire_options_and_hsr(client, path, allow):
    """The OPTIONS 204 and its HSR, as the wire suite checks them."""
    response = client.options(path)
    headers = wire.headers_of(response)
    expected = {method.strip() for method in allow.split(",")}
    problems = options_204_problems(
        response.status_code, headers, response.content, expected
    )
    assert problems == []
    (target,) = [
        target
        for target, params in parse_links(headers["link"])
        if params.get("rel") == HSR_DOCUMENT_REL
    ]
    document = client.get(target)
    headers = wire.headers_of(document)
    assert hsr_get_problems(document.status_code, headers, document.content) == []


@pytest.mark.parametrize("accept", [None, "application/json"], ids=["absent", "json"])
@pytest.mark.parametrize(
    "method, path",
    [("GET", "/nope"), ("GET", "/users/99"), ("POST", "/users/1"), ("GET", "/users/x")],
)
def test_wire_no_undescribed_content(accept, method, path):
    """No error is content the HSR doesn't describe, for an absent ``Accept``
    or ``Accept: application/json``."""
    with TestClient(api.app) as client:
        del client.headers["accept"]
        hsr = client.get("/_hsr/users.user_id").json()
        headers = {} if accept is None else {"Accept": accept}
        response = client.request(method, path, headers=headers)
    assert response.status_code >= 400
    headers = wire.headers_of(response)
    assert undescribed_problems(accept, headers, hsr) == []
