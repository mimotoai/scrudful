"""Semantic HTTP 0.1 (spec commit ``37ec1ce``) wire conformance of ``scrudful.fastapi``.

Every exchange goes through Starlette's ``TestClient`` (with
``raise_server_exceptions=False``, so a 500 is answered as a server answers
it), and each check is one of ``tests/semantic_http.py``'s; every case
asserts that its problem list is empty. One test per rule family:

- ``test_described_content_links``: one ``describedby`` and one context link on
  content, and exactly one HTTP Schema Resource link (no ``anchor``, the path's
  HSR route) on every response of a described path, HEAD, 204, 304, the
  early 400/412/428, the raised 401/403/404/405/422/429 and the 500 included;
  none on an unknown URL's 404;
- ``test_agreement``: the Links agree with the HSR entry of the method, status
  and media type;
- ``test_options``: a non-preflight OPTIONS is a 204 without content, with
  ``Allow`` and the HSR link;
- ``test_hsr_get``: the HSR route's response and document;
- ``test_hsr_invariance``: the HSR's bytes are the same for two ``Host``s and
  two requesters, each request building the document afresh;
- ``test_allow_per_requester``: ``Allow`` lists what this requester may use
  (anonymous, reader, writer, admin, an object-level denial through
  ``allowed``), a requester refused every method can't tell a missing object
  from an existing one (ruling R17), and a throttle in ``dependencies=`` uses
  no quota (ruling R25);
- ``test_preflight``: a CORS preflight is a 2xx without Links, running no
  auth, throttle, hook or load, answered by scrudful (204, no content) or by
  Starlette's ``CORSMiddleware`` in front;
- ``test_undescribed``: no undescribed content for an absent ``Accept`` or
  ``Accept: application/json``, across 404, 405, 422 and 500;
- ``test_expose_headers``: ``Link`` and ``Allow`` exposed, ``ETag`` and
  ``Location`` when present, same-origin and (through ``CORSMiddleware``)
  cross-origin.

The scenarios are those of the 0.9.0 audit's FastAPI harness (its
``harness.py``, ``check_example.py``, ``check_minimal.py`` and
``check_accept.py``), on three apps:

- ``examples/fastapi/api.py``, with the test client's URL as its base URL;
- ``WIRE``, a minimal app for what the example doesn't show: security
  dependencies (an ``HTTPBearer`` user, writers, an admin-only path) deciding
  ``Allow``, a ``load`` (a missing note is OPTIONS's 404), a throttle in
  ``dependencies=``, problem details (the early errors, the 422 and the
  500), non-JSON and in-band entities, an endpoint setting its own Links, a
  dependency setting a Link, a 500, and Starlette's ``CORSMiddleware`` with
  ``allow_methods=["*"]`` and ``expose_headers=list(EXPOSE_HEADERS)``;
- ``HOOKED``, a router given ``add_options(router, allowed=..., load=...)``
  and included under ``/h``: router mode, an object-level denial, and
  scrudful's own preflight answer (no CORS middleware).

Excluded, and documented by ``test_a_returned_error_has_only_the_hsr_link``:
an error an endpoint returns rather than raises gets none of the error
entity's Links (scrudful can't tell its content from a success's), only the
HSR link; raise it to give it Links.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    Security,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from examples.fastapi import api as example
from scrudful import fastapi as scrudful_fastapi
from scrudful._hsr import hsr_link
from scrudful.fastapi import (
    Entity,
    add_error_links,
    add_options,
    http_schema_resource,
    scrudful,
)
from scrudful.problem import PROBLEM_JSON, problem_details, problem_from_errors
from scrudful.utils import EXPOSE_HEADERS
from tests.semantic_http import (
    HSR_DOCUMENT_REL,
    PROBLEM_DETAILS_SCHEMA,
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    undescribed_problems,
)

BASE = "http://testserver"
W3ID = "https://w3id.org/semantic-http/0.1/"
ORIGIN = "https://app.example.org"


# --- The wire app (WIRE) ----------------------------------------------------------

PROBLEM = Entity(
    f"{W3ID}problem-details.schema.json",
    context_link_or_func=f"{W3ID}problem-details.context.jsonld",
    media_type=PROBLEM_JSON,
)
NOTE = Entity("/schemas/note.json", context_link_or_func="/contexts/note.jsonld")
NOTES = Entity("/schemas/notes.json", context_link_or_func="/contexts/note.jsonld")
ROWS = Entity(
    "/schemas/rows.json",
    schema_type_or_func="application/csvm+json",
    context_link_or_func="/schemas/rows.json",
    context_type_or_func="application/csvm+json",
    media_type="text/csv",
)
#: In-band RDF: no context link.
LD = Entity("/schemas/note.json", media_type="application/ld+json")
AUTHOR_LINK = '<https://people.example/ada>; rel="author"'

#: The notes, by id; reset for each test (see ``state``).
STORE: dict[int, dict[str, Any]] = {}
#: Who OPTIONS loaded objects for, and how often each check ran.
LOADS: list[str] = []
CALLS: dict[str, int] = {"throttle": 0, "user": 0, "allowed": 0}


def reset_store() -> None:
    STORE.clear()
    STORE.update({1: {"name": "One", "version": 1}, 2: {"name": "Two", "version": 1}})


def note_etag(nid: Any = None, iid: Any = None, **kwargs: Any) -> str | None:
    note = STORE.get(int(nid if nid is not None else iid))
    return None if note is None else f"v{note['version']}"


def find(nid: Any) -> dict[str, Any]:
    note = STORE.get(int(nid))
    if note is None:
        raise HTTPException(404, "No such note")
    return note


bearer = HTTPBearer(auto_error=False)
TOKENS = {"reader", "writer", "admin"}


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str | None:
    """The requester: anonymous without credentials, a 401 for a bad token."""
    CALLS["user"] += 1
    if credentials is None:
        return None
    if credentials.credentials not in TOKENS:
        raise HTTPException(401, "Bad token", headers={"WWW-Authenticate": "Bearer"})
    return credentials.credentials


def requiring(*names: str) -> Callable[..., str]:
    def require(user: str | None = Depends(current_user)) -> str:
        if user is None:
            raise HTTPException(
                401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"}
            )
        if user not in names:
            raise HTTPException(403, "Not allowed")
        return user

    return require


writer = requiring("writer", "admin")
admin = requiring("admin")


def throttle() -> None:
    """Refuses every request, and counts them: a plain dependency."""
    CALLS["throttle"] += 1
    raise HTTPException(429, "Slow down", headers={"Retry-After": "60"})


def author(response: Response) -> None:
    response.headers.append("Link", AUTHOR_LINK)


#: The success representation's schema and context links, as a dependency
#: might set them.
SUCCESS_LINKS = (
    f'<{BASE}/schemas/note.json>; rel="describedby"; '
    f'type="application/schema+json", <{BASE}/contexts/note.jsonld>; '
    'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)


def success_links(response: Response) -> None:
    response.headers.append("Link", f"{SUCCESS_LINKS}, {AUTHOR_LINK}")


def load(request: Request, **params: Any) -> Any:
    """The object of OPTIONS: a missing note is a 404."""
    LOADS.append("wire")
    return find(next(iter(params.values())))


class NoteIn(BaseModel):
    name: str = Field(min_length=1)


CONDITIONAL = dict(etag_func=note_etag, error=PROBLEM, error_body_func=problem_details)


def wire_app() -> FastAPI:
    app = FastAPI()

    @app.get("/notes/{nid}")
    @app.head("/notes/{nid}")
    @scrudful(**CONDITIONAL, entity=NOTE)
    async def get_note(nid: int, user: str | None = Depends(current_user)):
        return {"id": nid, "name": find(nid)["name"]}

    @app.put("/notes/{nid}")
    @scrudful(**CONDITIONAL, entity=NOTE)
    async def put_note(nid: int, body: NoteIn, user: str = Security(writer)):
        note = find(nid)
        note.update(name=body.name, version=note["version"] + 1)
        return {"id": nid, "name": note["name"]}

    @app.delete("/notes/{nid}", status_code=204)
    @scrudful(**CONDITIONAL)
    async def delete_note(nid: int, user: str = Security(writer)):
        find(nid)
        del STORE[nid]

    @app.get("/notes")
    @scrudful(entity=NOTES, error=PROBLEM)
    async def list_notes():
        return [{"id": nid, "name": note["name"]} for nid, note in STORE.items()]

    @app.post("/notes", status_code=201)
    @scrudful(entity=NOTE, error=PROBLEM)
    async def create_note(
        body: NoteIn, request: Request, response: Response, user: str = Depends(writer)
    ):
        nid = max(STORE, default=0) + 1
        STORE[nid] = {"name": body.name, "version": 1}
        response.headers["Location"] = str(request.url_for("get_note", nid=nid))
        return {"id": nid, "name": body.name}

    @app.get("/admin/{nid}", dependencies=[Security(admin)])
    @app.put("/admin/{nid}", dependencies=[Security(admin)])
    @scrudful(**CONDITIONAL, entity=NOTE)
    async def admin_note(nid: int):
        return {"id": nid, "name": find(nid)["name"]}

    @app.get("/throttled/{nid}", dependencies=[Depends(throttle)])
    @scrudful(**CONDITIONAL, entity=NOTE)
    async def throttled_note(nid: int):
        return {"id": nid}

    @app.get("/csv/{nid}")
    @scrudful(**CONDITIONAL, entity=ROWS)
    async def csv_note(nid: int):
        return Response(f"id,name\n{nid},{find(nid)['name']}\n", media_type="text/csv")

    @app.get("/ld/{nid}")
    @scrudful(entity=LD, error=PROBLEM)
    async def ld_note(nid: int):
        body = {
            "@context": {"@vocab": "https://schema.org/"},
            "name": find(nid)["name"],
        }
        return JSONResponse(body, media_type="application/ld+json")

    @app.get("/own/{nid}")
    @scrudful(entity=NOTE, error=PROBLEM)
    async def own_links(nid: int, response: Response):
        # A schema link (another case of the relation) replaces scrudful's,
        # one of another relation replaces none.
        response.headers["Link"] = (
            f'<{BASE}/schemas/note.json>; rel="DescribedBy"; '
            f'type="application/schema+json", <{BASE}/notes>; rel="up"'
        )
        return {"id": nid}

    @app.get("/linked/{nid}", dependencies=[Depends(author)])
    @app.put("/linked/{nid}", dependencies=[Depends(author)])
    @scrudful(**CONDITIONAL, entity=NOTE)
    async def linked(nid: int):
        return {"id": nid, "name": find(nid)["name"]}

    @app.get("/described/{nid}", dependencies=[Depends(success_links)])
    @app.put("/described/{nid}", dependencies=[Depends(success_links)])
    @scrudful(**CONDITIONAL, entity=NOTE)
    async def described(nid: int):
        return {"id": nid, "name": find(nid)["name"]}

    @app.get("/boom/{nid}")
    @scrudful(entity=NOTE, error=PROBLEM)
    async def boom(nid: int):
        raise RuntimeError("boom")

    @app.get("/returned/{nid}")
    @scrudful(entity=NOTE, error=PROBLEM)
    async def returned(nid: int):
        return JSONResponse({"detail": "Gone"}, status_code=410)

    add_error_links(app, PROBLEM)
    add_options(app, base_url=f"{BASE}/", load=load)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ORIGIN],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
        expose_headers=list(EXPOSE_HEADERS),
    )
    return app


# --- The hooked router (HOOKED) ---------------------------------------------------


def allowed(request: Request, method: str, obj: Any) -> bool:
    """Reads for anyone, writes for the ``writer`` key; "Two" can't be deleted."""
    CALLS["allowed"] += 1
    if method in ("GET", "HEAD"):
        return True
    if request.headers.get("x-key") != "writer":
        return False
    return not (method == "DELETE" and obj is not None and obj["name"] == "Two")


def hooked_load(request: Request, iid: str) -> Any:
    LOADS.append("hooked")
    return find(iid)


def hooked_router() -> APIRouter:
    router = APIRouter()

    @router.get("/items/{iid}")
    @router.head("/items/{iid}")
    @router.put("/items/{iid}")
    @router.delete("/items/{iid}", status_code=204)
    @scrudful(**CONDITIONAL, entity=NOTE)
    async def item(iid: int, request: Request, x_key: str | None = Header(None)):
        note = find(iid)
        if request.method in ("GET", "HEAD"):
            return {"id": iid, "name": note["name"]}
        if x_key != "writer" or (request.method == "DELETE" and note["name"] == "Two"):
            raise HTTPException(403, "Not allowed")
        if request.method == "DELETE":
            del STORE[iid]
            return Response(status_code=204)
        note["version"] += 1
        return {"id": iid, "name": note["name"]}

    add_options(router, base_url=f"{BASE}/", allowed=allowed, load=hooked_load)
    return router


def hooked_app(router: APIRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/h")
    add_error_links(app, PROBLEM)
    return app


WIRE = wire_app()
HOOKED_ROUTER = hooked_router()
HOOKED = hooked_app(HOOKED_ROUTER)
APPS = {"example": example.app, "wire": WIRE, "hooked": HOOKED}


# --- The HSR of each kind of resource ---------------------------------------------


@dataclass(frozen=True)
class Kind:
    """A resource: its app, its template (in its owner's frame), a URL of it,
    and its HSR route."""

    app: str
    template: str
    url: str
    hsr: str

    @property
    def owner(self) -> FastAPI | APIRouter:
        return HOOKED_ROUTER if self.app == "hooked" else APPS[self.app]


KINDS = {
    "user": Kind("example", "/users/{user_id}", "/users/1", "/_hsr/users.user_id"),
    "users": Kind("example", "/users", "/users", "/_hsr/users"),
    "note": Kind("wire", "/notes/{nid}", "/notes/1", "/_hsr/notes.nid"),
    "notes": Kind("wire", "/notes", "/notes", "/_hsr/notes"),
    "admin": Kind("wire", "/admin/{nid}", "/admin/1", "/_hsr/admin.nid"),
    "throttled": Kind(
        "wire", "/throttled/{nid}", "/throttled/1", "/_hsr/throttled.nid"
    ),
    "csv": Kind("wire", "/csv/{nid}", "/csv/1", "/_hsr/csv.nid"),
    "ld": Kind("wire", "/ld/{nid}", "/ld/1", "/_hsr/ld.nid"),
    "own": Kind("wire", "/own/{nid}", "/own/1", "/_hsr/own.nid"),
    "linked": Kind("wire", "/linked/{nid}", "/linked/1", "/_hsr/linked.nid"),
    "described": Kind(
        "wire", "/described/{nid}", "/described/1", "/_hsr/described.nid"
    ),
    "boom": Kind("wire", "/boom/{nid}", "/boom/1", "/_hsr/boom.nid"),
    "returned": Kind("wire", "/returned/{nid}", "/returned/1", "/_hsr/returned.nid"),
    "hooked": Kind("hooked", "/items/{iid}", "/h/items/1", "/h/_hsr/items.iid"),
}


@dataclass(frozen=True)
class Exchange:
    """A request and the status it gets; ``kind`` is the resource whose HSR
    link it carries (``None``: an unknown URL, which carries none)."""

    label: str
    kind: str | None
    method: str
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    app: str = ""

    @property
    def id(self) -> str:
        return f"{self.kind or self.app}-{self.label}"


def on(kind: str, default_url: str | None = None) -> Callable[..., Exchange]:
    """An :class:`Exchange` factory for the ``kind`` resource, at its URL
    unless ``url=`` is given."""

    def exchange(
        label: str,
        method: str,
        status: int,
        headers: dict[str, str] | None = None,
        body: Any = None,
        url: str | None = None,
    ) -> Exchange:
        return Exchange(
            label,
            kind,
            method,
            url or default_url or KINDS[kind].url,
            status,
            headers or {},
            body,
            KINDS[kind].app,
        )

    return exchange


def unknown(app: str, label: str, url: str) -> Exchange:
    return Exchange(label, None, "GET", url, 404, app=app)


READER = {"Authorization": "Bearer reader"}
WRITER = {"Authorization": "Bearer writer"}
ADMIN = {"Authorization": "Bearer admin"}
JSON = {"Accept": "application/json"}
FRESH = {"If-None-Match": '"v1"'}
STALE = {"If-Match": '"v0"'}
CURRENT = {"If-Match": '"v1"'}
MALFORMED = {"If-Match": "v1"}


def example_exchanges() -> list[Exchange]:
    """The audit's exchanges with the example app (``check_example.py``)."""
    user = on("user", "/users/2")
    users = on("users")
    body = {"name": "Eve", "age": 99}
    current = {"If-Match": '"user-2-v1"'}
    stale = {"If-Match": '"user-2-v0"'}
    return [
        user("GET 200", "GET", 200, JSON),
        user("GET 200 no Accept", "GET", 200),
        user("HEAD 200", "HEAD", 200),
        user("GET 304", "GET", 304, {"If-None-Match": '"user-2-v1"'}),
        user("HEAD 304", "HEAD", 304, {"If-None-Match": "*"}),
        user("GET 400 malformed", "GET", 400, {"If-None-Match": "user-2-v1"}),
        user("GET 412", "GET", 412, stale),
        user("HEAD 412", "HEAD", 412, stale),
        user("PUT 428", "PUT", 428, {}, body),
        user("PUT 412", "PUT", 412, stale, body),
        user("PUT 400 malformed", "PUT", 400, {"If-Match": "user-2-v1"}, body),
        user("PUT 422", "PUT", 422, current, {"name": ""}),
        user("PUT 422 no precondition", "PUT", 422, {}, {"name": ""}),
        user("PUT 200", "PUT", 200, current, body),
        user("PATCH 200", "PATCH", 200, current, {"age": 30}),
        user("PATCH 428", "PATCH", 428, {}, {"age": 30}),
        user("DELETE 428", "DELETE", 428),
        user("DELETE 204", "DELETE", 204, current),
        user("GET 404", "GET", 404, url="/users/99"),
        user("HEAD 404", "HEAD", 404, url="/users/99"),
        user("GET 422 path", "GET", 422, url="/users/abc"),
        user("POST 405", "POST", 405),
        user("TRACE 405", "TRACE", 405),
        user("GET 200 Accept HTML", "GET", 200, {"Accept": "text/html"}),
        user(
            "GET 404 Accept HTML", "GET", 404, {"Accept": "text/html"}, url="/users/99"
        ),
        users("POST 201", "POST", 201, {}, body),
        users("POST 422", "POST", 422, {}, {}),
        users("GET 405", "GET", 405),
        unknown("example", "unknown URL 404", "/nope"),
    ]


def wire_exchanges() -> list[Exchange]:
    """The audit's exchanges with its minimal apps (``check_minimal.py``,
    ``check_accept.py``)."""
    note = on("note")
    notes = on("notes")
    admin = on("admin")
    linked = on("linked")
    hooked = on("hooked")
    hooked_writer = {"X-Key": "writer", **CURRENT}
    name = {"name": "x"}
    return [
        note("GET anonymous", "GET", 200),
        note("GET reader", "GET", 200, READER),
        note("GET bad token 401", "GET", 401, {"Authorization": "Bearer nobody"}),
        note("HEAD", "HEAD", 200),
        note("GET 304", "GET", 304, FRESH),
        note("GET 400 problem", "GET", 400, MALFORMED),
        note("GET 412 problem", "GET", 412, STALE),
        note("GET 404", "GET", 404, url="/notes/999"),
        note("GET 422 path", "GET", 422, url="/notes/x"),
        note("PUT anonymous 401", "PUT", 401, CURRENT, name),
        note("PUT reader 403", "PUT", 403, {**READER, **CURRENT}, name),
        note("PUT writer 428", "PUT", 428, WRITER, name),
        note("PUT writer 412", "PUT", 412, {**WRITER, **STALE}, name),
        note("PUT writer 422", "PUT", 422, {**WRITER, **CURRENT}, {"name": ""}),
        note("PUT writer 200", "PUT", 200, {**WRITER, **CURRENT}, name),
        note("DELETE writer 204", "DELETE", 204, {**WRITER, **CURRENT}),
        note("POST 405", "POST", 405, {}, name),
        notes("GET list", "GET", 200),
        notes("POST anonymous 401", "POST", 401, {}, name),
        notes("POST writer 201", "POST", 201, WRITER, name),
        notes("POST writer 422", "POST", 422, WRITER, {}),
        notes("PUT 405", "PUT", 405, {}, name),
        admin("GET anonymous 401", "GET", 401),
        admin("GET reader 403", "GET", 403, READER),
        admin("GET admin", "GET", 200, ADMIN),
        on("throttled")("GET 429", "GET", 429),
        on("csv")("GET text/csv", "GET", 200),
        on("csv")("GET text/csv 304", "GET", 304, FRESH),
        on("csv")("GET 404", "GET", 404, url="/csv/999"),
        on("ld")("GET in-band", "GET", 200),
        on("own")("GET own Links", "GET", 200),
        linked("GET 200", "GET", 200),
        linked("GET 304", "GET", 304, FRESH),
        linked("GET 400", "GET", 400, MALFORMED),
        linked("GET 412", "GET", 412, STALE),
        linked("PUT 428", "PUT", 428),
        linked("GET 404", "GET", 404, url="/linked/999"),
        on("described")("GET 200", "GET", 200),
        on("described")("GET 304", "GET", 304, FRESH),
        on("described")("GET 400", "GET", 400, MALFORMED),
        on("described")("GET 412", "GET", 412, STALE),
        on("described")("PUT 428", "PUT", 428),
        on("boom")("GET 500", "GET", 500),
        on("boom")("GET 500 JSON", "GET", 500, JSON),
        unknown("wire", "unknown URL 404", "/nope"),
        hooked("GET", "GET", 200),
        hooked("HEAD", "HEAD", 200),
        hooked("GET 304", "GET", 304, FRESH),
        hooked("GET 404", "GET", 404, url="/h/items/999"),
        hooked("PUT 428", "PUT", 428, {"X-Key": "writer"}),
        hooked("PUT anonymous 403", "PUT", 403, CURRENT),
        hooked("PUT writer 200", "PUT", 200, hooked_writer),
        hooked("DELETE Two 403", "DELETE", 403, hooked_writer, url="/h/items/2"),
        hooked("DELETE writer 204", "DELETE", 204, hooked_writer),
        hooked("POST 405", "POST", 405),
        unknown("hooked", "unknown URL 404", "/h/nope"),
    ]


EXCHANGES = example_exchanges() + wire_exchanges()
#: The exchanges whose Links agree with an HSR entry: all but an unknown
#: URL's 404, which has no HSR.
AGREEING = [exchange for exchange in EXCHANGES if exchange.kind is not None]


# --- Fixtures and helpers ------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def example_base_url():
    """The test client's URL as the example's base URL (its own is
    localhost:8000), as in the example's tests, with its ``load``
    (``add_options`` replaces the options)."""
    add_options(example.app, base_url=f"{BASE}/", load=example.load_user)
    yield
    add_options(example.app, base_url=example.BASE_URL, load=example.load_user)


@pytest.fixture(autouse=True)
def state():
    example.reset_data()
    reset_store()
    LOADS.clear()
    for name in CALLS:
        CALLS[name] = 0
    yield
    example.reset_data()


@pytest.fixture
def clients():
    made = {
        name: TestClient(app, raise_server_exceptions=False)
        for name, app in APPS.items()
    }
    yield made
    for client in made.values():
        client.close()


def send(clients: dict[str, TestClient], exchange: Exchange, **extra: Any) -> Any:
    """``exchange``'s request; its response, once it has the status."""
    kwargs: dict[str, Any] = {"headers": {**exchange.headers, **extra}}
    if exchange.body is not None:
        kwargs["json"] = exchange.body
    response = clients[exchange.app].request(exchange.method, exchange.url, **kwargs)
    assert response.status_code == exchange.status, (
        exchange.label,
        response.content[:300],
    )
    return response


def headers_of(response: Any) -> dict[str, str]:
    """The response's header fields, repeated ones joined (Link's too)."""
    return {
        name: ", ".join(response.headers.get_list(name))
        for name in response.headers.keys()
    }


def hsr_of(clients: dict[str, TestClient], kind: str) -> dict[str, Any]:
    """The HTTP Schema Resource of ``kind``, by a GET of its HSR route."""
    response = clients[KINDS[kind].app].get(KINDS[kind].hsr)
    assert response.status_code == 200, response.content
    return json.loads(response.content)


def target(kind: str) -> str:
    return BASE + KINDS[kind].hsr


def has_content(exchange: Exchange, response: Any) -> bool:
    return exchange.method != "HEAD" and bool(response.content)


def exposed(response: Any) -> set[str]:
    value = ", ".join(response.headers.get_list("access-control-expose-headers"))
    return {name.strip() for name in value.split(",") if name.strip()}


# --- The rule families -----------------------------------------------------------


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda e: e.id)
def test_described_content_links(clients, exchange):
    response = send(clients, exchange)
    headers = headers_of(response)
    content = has_content(exchange, response)
    assert content == (exchange.method != "HEAD" and exchange.status not in (204, 304))
    assert link_problems(exchange.method, exchange.status, headers, content) == []
    if exchange.kind is None:
        assert HSR_DOCUMENT_REL not in headers.get("link", "")
    else:
        assert hsr_link_problems(headers, target(exchange.kind)) == []


@pytest.mark.parametrize("exchange", AGREEING, ids=lambda e: e.id)
def test_agreement(clients, exchange):
    hsr = hsr_of(clients, exchange.kind)
    response = send(clients, exchange)
    content = has_content(exchange, response)
    problems = link_problems(
        exchange.method, exchange.status, headers_of(response), content, hsr
    )
    assert problems == []


#: ``(kind, headers)`` of an OPTIONS each resource answers with a 204.
OPTIONS = [
    ("user", {}),
    ("user", JSON),
    ("users", {}),
    ("note", {}),
    ("note", WRITER),
    ("notes", {}),
    ("admin", ADMIN),
    ("admin", {}),
    ("throttled", {}),
    ("csv", {}),
    ("ld", {}),
    ("own", {}),
    ("linked", {}),
    ("boom", {}),
    ("hooked", {}),
    ("hooked", {"X-Key": "writer"}),
]
OPTION_IDS = [f"{kind}-{bool(headers)}" for kind, headers in OPTIONS]


@pytest.mark.parametrize("kind, headers", OPTIONS, ids=OPTION_IDS)
def test_options(clients, kind, headers):
    response = clients[KINDS[kind].app].options(KINDS[kind].url, headers=headers)
    headers_ = headers_of(response)
    assert options_204_problems(response.status_code, headers_, response.content) == []
    assert hsr_link_problems(headers_, target(kind)) == []


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_hsr_get(clients, kind):
    response = clients[KINDS[kind].app].get(KINDS[kind].hsr)
    problems = hsr_get_problems(
        response.status_code, headers_of(response), response.content
    )
    assert problems == []
    assert json.loads(response.content) == http_schema_resource(
        KINDS[kind].owner, KINDS[kind].template
    )
    # A conditional GET with its ETag: 304.
    again = clients[KINDS[kind].app].get(
        KINDS[kind].hsr, headers={"If-None-Match": response.headers["etag"]}
    )
    assert again.status_code == 304
    # HEAD: the same headers, no content.
    head = clients[KINDS[kind].app].head(KINDS[kind].hsr)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["etag"] == response.headers["etag"]


#: The credentials of two requesters: anonymous, and authenticated (the
#: wire app's token, the hooked router's key, and a cookie).
REQUESTERS = ({}, {**ADMIN, "X-Key": "writer", "Cookie": "session=a"})


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_hsr_invariance(kind):
    """Two Hosts and two requesters: one HSR, as bytes and ETag. The built
    documents are discarded before each request, so each builds its own:
    a document that depended on the requester or its Host would differ."""
    responses = []
    options = scrudful_fastapi._hsr_options(KINDS[kind].owner)
    for host in ("a.example", "b.example"):
        with TestClient(APPS[KINDS[kind].app], base_url=f"http://{host}") as client:
            for credentials in REQUESTERS:
                options.built.clear()
                options.serialized.clear()
                responses.append(client.get(KINDS[kind].hsr, headers=credentials))
    assert {response.status_code for response in responses} == {200}
    assert len({response.content for response in responses}) == 1
    assert len({response.headers["etag"] for response in responses}) == 1
    # Built without a request: no Host leaks into it.
    assert b"a.example" not in responses[0].content
    assert b"b.example" not in responses[0].content


ITEM = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
READ = {"GET", "HEAD", "OPTIONS"}
#: ``(label, kind, url, headers, Allow)``.
ALLOW = [
    ("example user", "user", "/users/1", {}, ITEM | {"PATCH"}),
    ("example users", "users", "/users", {}, {"POST", "OPTIONS"}),
    ("anonymous", "note", "/notes/1", {}, READ),
    ("reader", "note", "/notes/1", READER, READ),
    ("writer", "note", "/notes/1", WRITER, ITEM),
    ("bad token", "note", "/notes/1", {"Authorization": "Bearer nobody"}, {"OPTIONS"}),
    ("anonymous list", "notes", "/notes", {}, {"GET", "OPTIONS"}),
    ("writer list", "notes", "/notes", WRITER, {"GET", "POST", "OPTIONS"}),
    ("anonymous admin", "admin", "/admin/1", {}, {"OPTIONS"}),
    ("reader admin", "admin", "/admin/1", READER, {"OPTIONS"}),
    ("admin admin", "admin", "/admin/1", ADMIN, {"GET", "PUT", "OPTIONS"}),
    ("throttled", "throttled", "/throttled/1", {}, {"GET", "OPTIONS"}),
    ("anonymous hooked", "hooked", "/h/items/1", {}, READ),
    ("writer hooked", "hooked", "/h/items/1", {"X-Key": "writer"}, ITEM),
    (
        "writer hooked, Two",
        "hooked",
        "/h/items/2",
        {"X-Key": "writer"},
        ITEM - {"DELETE"},
    ),
]


@pytest.mark.parametrize(
    "label, kind, url, headers, allow", ALLOW, ids=[a[0] for a in ALLOW]
)
def test_allow_per_requester(clients, label, kind, url, headers, allow):
    response = clients[KINDS[kind].app].options(url, headers=headers)
    problems = options_204_problems(
        response.status_code, headers_of(response), response.content, allow
    )
    assert problems == []
    assert hsr_link_problems(headers_of(response), target(kind)) == []


@pytest.mark.parametrize("headers", [{}, READER], ids=["anonymous", "reader"])
def test_allow_is_no_existence_oracle_for_a_requester_refused_everything(
    clients, headers
):
    """Ruling R17: the admin-only path refuses this requester every method,
    so OPTIONS loads nothing, and a missing note answers as an existing one
    does."""
    answers = []
    for url in ("/admin/1", "/admin/999"):
        response = clients["wire"].options(url, headers=headers)
        problems = options_204_problems(
            response.status_code, headers_of(response), response.content, {"OPTIONS"}
        )
        assert problems == []
        assert hsr_link_problems(headers_of(response), target("admin")) == []
        answers.append((response.status_code, response.headers["allow"]))
        answers[-1] += (response.headers["link"],)
    assert answers[0] == answers[1]
    assert LOADS == []


def test_allow_of_a_missing_object_is_its_404(clients):
    """Ruling R11: a requester who may use a method gets the 404 of a
    missing note (``load``'s), with the error Links and the HSR link."""
    response = clients["wire"].options("/notes/999", headers=WRITER)
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    assert link_problems("OPTIONS", 404, headers_of(response), True) == []
    assert hsr_link_problems(headers_of(response), target("note")) == []
    assert LOADS == ["wire"]


def test_allow_with_an_allowed_hook_loads_the_object_for_anyone(clients):
    """Ruling R17a, documented: with ``allowed`` every method needs the
    object, for every requester, so OPTIONS of a missing item is its 404
    whoever asks. Only a ``load`` hiding the objects keeps them secret."""
    response = clients["hooked"].options("/h/items/999")
    assert response.status_code == 404
    assert LOADS == ["hooked"]
    assert link_problems("OPTIONS", 404, headers_of(response), True) == []
    assert hsr_link_problems(headers_of(response), target("hooked")) == []


def test_a_throttle_in_dependencies_uses_no_quota(clients):
    """Ruling R25: OPTIONS runs only the security dependencies, so a throttle
    in ``dependencies=`` isn't run (``Allow`` lists the GET it would refuse),
    and its quota is untouched; the GET itself is throttled, with Links."""
    for _ in range(3):
        response = clients["wire"].options("/throttled/1")
        assert response.status_code == 204
        assert response.headers["allow"] == "GET, OPTIONS"
    assert CALLS["throttle"] == 0
    response = clients["wire"].get("/throttled/1")
    assert response.status_code == 429
    assert CALLS["throttle"] == 1
    assert link_problems("GET", 429, headers_of(response), True) == []
    assert hsr_link_problems(headers_of(response), target("throttled")) == []


PREFLIGHT = {"Origin": ORIGIN, "Access-Control-Request-Method": "PUT"}
#: ``(app, url, headers)`` of preflights: open, locked, throttled and hooked
#: resources, missing objects, a preflight of OPTIONS itself, with and
#: without credentials (a browser sends none; a bad token or a cookie are
#: ignored).
PREFLIGHTS = [
    ("wire", "/notes/1", {}),
    ("wire", "/notes/999", {}),
    ("wire", "/notes/1", {"Authorization": "Bearer nobody", "Cookie": "s=x"}),
    ("wire", "/notes/1", {"Access-Control-Request-Method": "OPTIONS"}),
    ("wire", "/notes", {"Access-Control-Request-Headers": "content-type"}),
    ("wire", "/admin/1", {}),
    ("wire", "/admin/999", {}),
    ("wire", "/throttled/1", {"Access-Control-Request-Method": "GET"}),
    ("hooked", "/h/items/1", {}),
    ("hooked", "/h/items/999", {"X-Key": "writer"}),
    ("hooked", "/h/items/1", {"Access-Control-Request-Method": "OPTIONS"}),
    ("example", "/users/1", {}),
    ("example", "/users/99", {}),
]


@pytest.mark.parametrize(
    "app, url, credentials",
    PREFLIGHTS,
    ids=[f"{a}{u}-{sorted(c)}" for a, u, c in PREFLIGHTS],
)
def test_preflight(clients, app, url, credentials):
    """A preflight is a 2xx without Links, and runs no security dependency,
    throttle, hook or load: on the wire app, Starlette's ``CORSMiddleware``
    answers it (allowing OPTIONS, the origin and its credentials); without
    one, scrudful does (a 204 without content)."""
    for _ in range(3):  # never throttled
        response = clients[app].options(url, headers={**PREFLIGHT, **credentials})
        problems = []
        if not 200 <= response.status_code < 300:
            problems.append(f"preflight answered {response.status_code}")
        if "link" in response.headers:
            problems.append("preflight has Links")
        if app == "wire":
            allow_methods = response.headers.get("access-control-allow-methods", "")
            methods = {m.strip() for m in allow_methods.split(",")}
            if not {"OPTIONS", "PUT", "GET"} <= methods:
                problems.append(f"Access-Control-Allow-Methods {allow_methods!r}")
            if response.headers.get("access-control-allow-origin") != ORIGIN:
                problems.append("preflight doesn't allow the origin")
            if response.headers.get("access-control-allow-credentials") != "true":
                problems.append("preflight doesn't allow credentials")
        else:
            if response.status_code != 204:
                problems.append(f"scrudful's preflight answered {response.status_code}")
            if response.content:
                problems.append("preflight has content")
            if "content-type" in response.headers:
                problems.append("preflight has a Content-Type")
        assert problems == []
    assert CALLS == {"throttle": 0, "user": 0, "allowed": 0}
    assert LOADS == []


#: ``(label, app, method, url, kind)``: FastAPI's 404 for an unknown URL (and
#: for one a convertor refuses), a raised 404, Starlette's 405, FastAPI's
#: 422, the HSR route's 404 and 405, and the 500, each described or accepted.
UNDESCRIBED = [
    ("unknown URL 404", "example", "GET", "/nope", "user"),
    ("missing user 404", "example", "GET", "/users/99", "user"),
    ("POST 405", "example", "POST", "/users/1", "user"),
    ("path 422", "example", "GET", "/users/abc", "user"),
    ("HSR route 405", "example", "POST", "/_hsr/users.user_id", "user"),
    ("HSR route 404", "example", "GET", "/_hsr/nope", "user"),
    ("unknown URL 404", "wire", "GET", "/nope", "note"),
    ("missing note 404", "wire", "GET", "/notes/999", "note"),
    ("POST 405", "wire", "POST", "/notes/1", "note"),
    ("path 422", "wire", "GET", "/notes/x", "note"),
    ("bad token 401", "wire", "GET", "/notes/1", "note"),
    ("throttled 429", "wire", "GET", "/throttled/1", "throttled"),
    ("missing csv 404", "wire", "GET", "/csv/999", "csv"),
    ("500", "wire", "GET", "/boom/1", "boom"),
    ("missing item 404", "hooked", "GET", "/h/items/999", "hooked"),
    ("POST 405", "hooked", "POST", "/h/items/1", "hooked"),
    ("HSR route 405", "hooked", "PUT", "/h/_hsr/items.iid", "hooked"),
]


@pytest.mark.parametrize("accept", [None, "application/json"], ids=["absent", "json"])
@pytest.mark.parametrize(
    "label, app, method, url, kind",
    UNDESCRIBED,
    ids=[f"{u[1]}{u[3]}-{u[0]}" for u in UNDESCRIBED],
)
def test_undescribed(clients, accept, label, app, method, url, kind):
    hsr = hsr_of(clients, kind)
    client = TestClient(APPS[app], raise_server_exceptions=False)
    del client.headers["accept"]
    headers = {} if accept is None else {"Accept": accept}
    if label == "bad token 401":
        headers["Authorization"] = "Bearer nobody"
    response = client.request(method, url, headers=headers)
    client.close()
    assert response.status_code >= 400, label
    response_headers = headers_of(response)
    assert undescribed_problems(accept, response_headers, hsr) == []
    content = bool(response.content)
    assert link_problems(method, response.status_code, response_headers, content) == []


@pytest.mark.parametrize("accept", [None, "application/json"], ids=["absent", "json"])
def test_the_csv_entity_is_described_whatever_the_accept(clients, accept):
    """The audit's ``check_accept.py``: a ``text/csv`` entity is described by
    its HSR, so it may answer an absent or a JSON-only ``Accept``."""
    hsr = hsr_of(clients, "csv")
    client = TestClient(WIRE)
    del client.headers["accept"]
    response = client.get(
        "/csv/1", headers={} if accept is None else {"Accept": accept}
    )
    client.close()
    assert response.headers["content-type"].startswith("text/csv")
    assert undescribed_problems(accept, headers_of(response), hsr) == []
    assert link_problems("GET", 200, headers_of(response), True, hsr) == []


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda e: e.id)
def test_expose_headers(clients, exchange):
    response = send(clients, exchange)
    expected = {"Link", "Allow"} | {
        name for name in ("ETag", "Location") if name.lower() in response.headers
    }
    assert expected - exposed(response) == set()


@pytest.mark.parametrize(
    "exchange", [e for e in EXCHANGES if e.app == "wire"], ids=lambda e: e.id
)
def test_expose_headers_cross_origin(clients, exchange):
    """Starlette's ``CORSMiddleware`` replaces the expose headers of a
    cross-origin response with its own ``expose_headers``: given
    ``EXPOSE_HEADERS``, they still cover the response. (The 500, sent by
    ``ServerErrorMiddleware`` outside it, keeps scrudful's.)"""
    response = send(clients, exchange, Origin=ORIGIN)
    expected = {"Link", "Allow"} | {
        name for name in ("ETag", "Location") if name.lower() in response.headers
    }
    assert expected - exposed(response) == set()
    if exchange.status != 500:
        assert response.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.parametrize("kind, headers", OPTIONS, ids=OPTION_IDS)
def test_expose_headers_of_options(clients, kind, headers):
    response = clients[KINDS[kind].app].options(KINDS[kind].url, headers=headers)
    assert {"Link", "Allow"} <= exposed(response)


# --- Problem details, kept Links, the 500 ------------------------------------------


@pytest.mark.parametrize(
    "url, headers, body",
    [
        ("/notes/1", {**WRITER, **CURRENT}, {"name": ""}),
        ("/notes", WRITER, {}),
        ("/notes/x", {**WRITER, **CURRENT}, {"name": "x"}),
    ],
)
def test_the_validation_422_is_problem_details(clients, url, headers, body):
    """With a problem-details error entity, FastAPI's validation 422 is
    problem details, as the HSR's entry says, its errors the ``errors``
    member."""
    method = "POST" if url == "/notes" else "PUT"
    response = clients["wire"].request(method, url, headers=headers, json=body)
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_JSON
    problem = response.json()
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(problem)
    assert problem == problem_from_errors(
        422, problem["errors"], "Request validation failed"
    )
    assert problem["errors"]
    kind = "notes" if url == "/notes" else "note"
    assert [link for link, _ in parse_links(response.headers["link"])] == [
        PROBLEM.schema_link_or_func,
        PROBLEM.context_link_or_func,
        target(kind),
    ]


@pytest.mark.parametrize("headers", [{}, JSON, {"Accept": "text/html"}])
def test_the_500_is_problem_details(clients, headers):
    """The audit's text/plain 500: now problem details with the error Links
    and the HSR link, whatever the ``Accept``."""
    response = clients["wire"].get("/boom/1", headers=headers)
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json() == problem_from_errors(500, None)
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(response.json())
    assert [link for link, _ in parse_links(response.headers["link"])] == [
        PROBLEM.schema_link_or_func,
        PROBLEM.context_link_or_func,
        target("boom"),
    ]


@pytest.mark.parametrize(
    "method, headers, status",
    [
        ("GET", {}, 200),
        ("GET", FRESH, 304),
        ("GET", MALFORMED, 400),
        ("GET", STALE, 412),
        ("PUT", {}, 428),
    ],
)
def test_a_dependency_link_is_kept(clients, method, headers, status):
    """The audit's dependency Link, dropped on the early 400/412/428: kept,
    beside the content's (or the error's) Links and the HSR link."""
    response = clients["wire"].request(method, "/linked/1", headers=headers)
    assert response.status_code == status
    links = parse_links(response.headers["link"])
    assert links[0] == ("https://people.example/ada", {"rel": "author"})
    assert links[-1][0] == target("linked")


@pytest.mark.parametrize(
    "method, headers, status",
    [("GET", MALFORMED, 400), ("GET", STALE, 412), ("PUT", {}, 428)],
)
def test_a_dependency_s_success_links_leave_the_early_errors(
    clients, method, headers, status
):
    """Ruling R27: a dependency setting the success representation's schema
    and context links doesn't relabel the early 400/412/428, whose problem
    details body the error entity's Links describe (agreeing with the HSR);
    its author link stays."""
    hsr = hsr_of(clients, "described")
    response = clients["wire"].request(method, "/described/1", headers=headers)
    assert response.status_code == status
    assert [link for link, _ in parse_links(response.headers["link"])] == [
        "https://people.example/ada",
        PROBLEM.schema_link_or_func,
        PROBLEM.context_link_or_func,
        target("described"),
    ]
    assert link_problems(method, status, headers_of(response), True, hsr) == []


def test_own_links_win(clients):
    """A handler's own schema link (another case of the relation) replaces
    scrudful's; one of another relation stays beside the others."""
    response = clients["wire"].get("/own/1")
    assert [
        (link, params["rel"]) for link, params in parse_links(response.headers["link"])
    ] == [
        (f"{BASE}/schemas/note.json", "DescribedBy"),
        (f"{BASE}/notes", "up"),
        (f"{BASE}/contexts/note.jsonld", "http://www.w3.org/ns/json-ld#context"),
        (target("own"), HSR_DOCUMENT_REL),
    ]


def test_a_returned_error_has_only_the_hsr_link(clients):
    """Excluded from the suite, documented: an error the endpoint returns
    (not raises) is content scrudful can't tell from a success's, so it gets
    none of the error entity's Links, only the HSR link. Raise it
    (``HTTPException``) to give it the error entity's."""
    response = clients["wire"].get("/returned/1")
    assert response.status_code == 410
    assert response.headers["link"] == hsr_link(target("returned"))
    assert link_problems("GET", 410, headers_of(response), True) != []


@pytest.mark.parametrize("kind", ["note", "hooked", "user"])
def test_the_hsr_link_is_served(clients, kind):
    """The HSR link of a response leads to the document its OPTIONS links to."""
    client = clients[KINDS[kind].app]
    links = parse_links(client.get(KINDS[kind].url).headers["link"])
    (link,) = [t for t, params in links if params.get("rel") == HSR_DOCUMENT_REL]
    options = parse_links(client.options(KINDS[kind].url).headers["link"])
    assert options == [(link, {"rel": HSR_DOCUMENT_REL, "type": "application/json"})]
    assert client.get(link.removeprefix(BASE)).status_code == 200
