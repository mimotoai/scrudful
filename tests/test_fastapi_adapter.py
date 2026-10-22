"""Mechanics of the ``scrudful.fastapi`` decorator, tested with tiny ad-hoc apps."""

import inspect
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from scrudful.fastapi import (
    CONDITIONAL_READ_RESPONSES,
    CONDITIONAL_WRITE_RESPONSES,
    scrudful,
)

# Non-zero microseconds: the header is floored to whole seconds and conditional
# requests echoing it must still match.
MODIFIED = datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
MODIFIED_HTTP = "Tue, 02 Jan 2024 03:04:05 GMT"
EARLIER_HTTP = "Tue, 02 Jan 2024 03:04:04 GMT"
IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA = "http://example.com/schema.json"
CONTEXT = "http://example.com/context.jsonld"
SCHEMA_LINK = f'<{SCHEMA}>; rel="describedBy"; type="application/json"'
CONTEXT_LINK = (
    f'<{CONTEXT}>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)


class Store:
    """One resource whose validators the callbacks read."""

    def __init__(self) -> None:
        self.exists = True
        self.version = 1
        self.modified: object = MODIFIED
        self.calls: list[str] = []

    def etag(self, **kwargs: object) -> str | None:
        self.calls.append("etag")
        return f"v{self.version}" if self.exists else None

    def last_modified(self, **kwargs: object) -> object:
        self.calls.append("last_modified")
        return self.modified if self.exists else None

    def update(self) -> None:
        self.version += 1
        self.modified = MODIFIED + timedelta(hours=1)


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def make_client():
    clients = []

    def make(app: FastAPI) -> TestClient:
        client = TestClient(app)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def client(store, make_client) -> TestClient:
    """A conventional item API with both validators configured."""
    app = FastAPI()
    validators = dict(etag_func=store.etag, last_modified_func=store.last_modified)
    calls = store.calls

    def missing() -> HTTPException:
        return HTTPException(status_code=404, detail="Not found")

    @app.get("/item", responses=CONDITIONAL_READ_RESPONSES)
    @scrudful(
        **validators,
        schema_link_or_func=SCHEMA,
        context_link_or_func=CONTEXT,
    )
    async def get_item():
        calls.append("endpoint")
        if not store.exists:
            raise missing()
        return {"version": store.version}

    @app.api_route("/item", methods=["PUT", "PATCH"])
    @scrudful(**validators)
    async def put_item(request: Request, response: Response):
        calls.append("endpoint")
        if not store.exists:
            store.exists = True
            response.status_code = 201
        else:
            store.update()
        return {"version": store.version, "method": request.method}

    @app.delete("/item", status_code=204)
    @scrudful(**validators)
    async def delete_item():
        calls.append("endpoint")
        if not store.exists:
            raise missing()
        store.exists = False

    @app.post("/item", status_code=201)
    @scrudful(**validators)
    async def post_item():
        calls.append("endpoint")
        return {}

    return make_client(app)


# --- signature handling / dependency injection ---------------------------------


def test_endpoint_without_request_or_response_parameters(make_client, store):
    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(etag_func=store.etag)
    async def get_item(item_id: int):
        return {"id": item_id}

    response = make_client(app).get("/items/3")
    assert response.status_code == 200
    assert response.json() == {"id": 3}
    assert response.headers["etag"] == '"v1"'


def test_endpoint_with_differently_named_request_and_response(make_client, store):
    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(etag_func=store.etag)
    def get_item(req: Request, item_id: int, resp: Response):
        resp.headers["X-Path"] = req.url.path
        return {"id": item_id}

    response = make_client(app).get("/items/3")
    assert response.status_code == 200
    assert response.headers["x-path"] == "/items/3"
    assert response.headers["etag"] == '"v1"'


def _annotated_endpoint(received):
    async def endpoint(
        item_id: int, req: Annotated[Request, "doc"], resp: Annotated[Response, "doc"]
    ):
        received.update(req=req, resp=resp)
        resp.headers["X-Path"] = req.url.path
        return {"id": item_id}

    return endpoint


def _stringified_annotated_endpoint(received):
    async def endpoint(
        item_id: int, req: 'Annotated[Request, "doc"]', resp: "Annotated[Response, 1]"
    ):
        received.update(req=req, resp=resp)
        resp.headers["X-Path"] = req.url.path
        return {"id": item_id}

    return endpoint


@pytest.mark.parametrize(
    "factory", [_annotated_endpoint, _stringified_annotated_endpoint]
)
def test_annotated_request_and_response_are_detected(make_client, store, factory):
    app = FastAPI()
    received = {}
    wrapped = scrudful(etag_func=store.etag)(factory(received))
    app.get("/items/{item_id}")(wrapped)

    assert list(inspect.signature(wrapped).parameters) == ["item_id", "req", "resp"]
    response = make_client(app).get("/items/3")
    assert response.status_code == 200
    assert response.json() == {"id": 3}
    assert isinstance(received["req"], Request)
    assert isinstance(received["resp"], Response)
    assert response.headers["x-path"] == "/items/3"
    assert response.headers["etag"] == '"v1"'


def test_string_annotations_are_resolved(make_client, store):
    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(etag_func=store.etag)
    async def get_item(req: "Request", item_id: "int", resp: "Response"):
        resp.headers["X-Method"] = req.method
        return {"id": item_id}

    assert list(inspect.signature(get_item).parameters) == ["req", "item_id", "resp"]
    response = make_client(app).get("/items/3")
    assert response.status_code == 200
    assert response.headers["x-method"] == "GET"
    assert response.headers["etag"] == '"v1"'


def test_wrapper_signature_adds_private_keyword_only_parameters():
    @scrudful()
    async def bare(item_id: int, q: str | None = None):
        """Docstring."""

    params = inspect.signature(bare).parameters
    assert list(params) == ["item_id", "q", "_scrudful_request", "_scrudful_response"]
    assert params["_scrudful_request"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["_scrudful_request"].annotation is Request
    assert params["_scrudful_response"].annotation is Response
    assert bare.__name__ == "bare"
    assert bare.__doc__ == "Docstring."

    @scrudful()
    async def declared(response: Response, request: Request):
        pass

    assert list(inspect.signature(declared).parameters) == ["response", "request"]


def test_private_parameter_names_do_not_collide(make_client):
    app = FastAPI()

    @app.get("/items")
    @scrudful(etag_func=lambda **kwargs: "a")
    async def get_items(_scrudful_request: int = 0):
        return {"value": _scrudful_request}

    params = inspect.signature(get_items).parameters
    assert "_scrudful_request_" in params
    response = make_client(app).get("/items", params={"_scrudful_request": 7})
    assert response.json() == {"value": 7}


def test_injected_parameters_go_before_var_keyword():
    @scrudful()
    async def endpoint(item_id: int, **extra):
        pass

    assert list(inspect.signature(endpoint).parameters) == [
        "item_id",
        "_scrudful_request",
        "_scrudful_response",
        "extra",
    ]


def test_openapi_shows_only_the_endpoint_parameters(make_client, store):
    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(etag_func=store.etag)
    async def get_item(item_id: int, q: str | None = None):
        return {}

    schema = make_client(app).get("/openapi.json").json()
    operation = schema["paths"]["/items/{item_id}"]["get"]
    assert [p["name"] for p in operation["parameters"]] == ["item_id", "q"]
    assert operation["operationId"].startswith("get_item")
    assert "scrudful" not in str(schema)


def test_callbacks_receive_the_endpoint_keyword_arguments(make_client):
    app = FastAPI()
    received = {}

    def get_db():
        return "db-session"

    def etag(**kwargs):
        received["etag"] = kwargs
        return "a"

    def schema(**kwargs):
        received["schema"] = kwargs
        return SCHEMA

    @app.get("/items/{item_id}")
    @scrudful(etag_func=etag, schema_link_or_func=schema)
    async def get_item(item_id: int, q: str = "x", db: str = Depends(get_db)):
        received["endpoint"] = {"item_id": item_id, "q": q, "db": db}
        return {}

    response = make_client(app).get("/items/3?q=y")
    assert response.status_code == 200
    expected = {"item_id": 3, "q": "y", "db": "db-session"}
    assert received == {"etag": expected, "schema": expected, "endpoint": expected}


def test_callbacks_receive_a_declared_request(make_client):
    app = FastAPI()

    @app.get("/schema", name="schema")
    async def get_schema():
        return {}

    @app.get("/items/{item_id}")
    @scrudful(schema_link_or_func=lambda request, **kwargs: request.url_for("schema"))
    async def get_item(request: Request, item_id: int):
        return {}

    response = make_client(app).get("/items/3")
    assert response.headers["link"] == (
        '<http://testserver/schema>; rel="describedBy"; type="application/json"'
    )


def test_sync_endpoint_and_callbacks_run_in_the_threadpool(make_client):
    app = FastAPI()
    threads = {}

    async def etag(**kwargs):
        threads["loop"] = threading.get_ident()
        return "a"

    def last_modified(**kwargs):
        threads["callback"] = threading.get_ident()
        return MODIFIED

    @app.get("/items")
    @scrudful(etag_func=etag, last_modified_func=last_modified)
    def get_items():
        threads["endpoint"] = threading.get_ident()
        return {}

    assert make_client(app).get("/items").status_code == 200
    assert threads["endpoint"] != threads["loop"]
    assert threads["callback"] != threads["loop"]


def test_async_and_awaitable_returning_callbacks(make_client):
    app = FastAPI()

    async def etag(**kwargs):
        return "async"

    async def schema_coroutine():
        return SCHEMA

    async def context_rel(**kwargs):
        return "context"

    @app.get("/items")
    @scrudful(
        etag_func=etag,
        last_modified_func=lambda **kwargs: _awaitable(MODIFIED),
        schema_link_or_func=lambda **kwargs: schema_coroutine(),
        context_link_or_func=CONTEXT,
        context_rel_or_func=context_rel,
    )
    async def get_items():
        return {}

    response = make_client(app).get("/items")
    assert response.headers["etag"] == '"async"'
    assert response.headers["last-modified"] == MODIFIED_HTTP
    assert response.headers["link"] == (
        f'{SCHEMA_LINK}, <{CONTEXT}>; rel="context"; type="application/ld+json"'
    )


async def _awaitable(value):
    return value


def test_sync_endpoint_returning_a_coroutine_is_awaited(make_client):
    app = FastAPI()

    @app.get("/items")
    @scrudful()
    def get_items():
        return _awaitable({"ok": True})

    assert make_client(app).get("/items").json() == {"ok": True}


def test_decorator_arguments_are_keyword_only_and_validated(store):
    with pytest.raises(TypeError):
        scrudful(store.etag)
    with pytest.raises(TypeError, match="etag_func must be callable"):
        scrudful(etag_func="abc")
    with pytest.raises(TypeError, match="last_modified_func must be callable"):
        scrudful(last_modified_func=MODIFIED)


def test_generator_endpoints_are_rejected():
    def gen():
        yield b""

    async def agen():
        yield b""

    for endpoint in (gen, agen):
        with pytest.raises(TypeError, match="generator"):
            scrudful()(endpoint)


def test_decorator_order_scrudful_must_be_below_the_route_decorator(make_client, store):
    """Above the route decorator, the undecorated endpoint is registered."""
    app = FastAPI()

    @scrudful(etag_func=store.etag)
    @app.put("/wrong-order")
    async def put_wrong_order():
        return {}

    @app.put("/right-order")
    @scrudful(etag_func=store.etag)
    async def put_right_order():
        return {}

    client = make_client(app)
    unprotected = client.put("/wrong-order")
    assert unprotected.status_code == 200
    assert "etag" not in unprotected.headers
    assert client.put("/right-order").status_code == 428


# --- responses -------------------------------------------------------------------


def test_returned_response_gets_the_headers(make_client, store):
    app = FastAPI()

    @app.get("/items")
    @scrudful(
        etag_func=store.etag,
        last_modified_func=store.last_modified,
        schema_link_or_func=SCHEMA,
    )
    async def get_items():
        return JSONResponse({"a": 1}, headers={"X-Own": "1"})

    response = make_client(app).get("/items")
    assert response.json() == {"a": 1}
    assert response.headers["x-own"] == "1"
    assert response.headers["etag"] == '"v1"'
    assert response.headers["last-modified"] == MODIFIED_HTTP
    assert response.headers["link"] == SCHEMA_LINK
    assert response.headers["access-control-expose-headers"] == "ETag, Link"


@pytest.mark.parametrize("returned", [True, False])
def test_non_2xx_responses_get_no_validators(make_client, store, returned):
    app = FastAPI()

    @app.get("/items")
    @scrudful(etag_func=store.etag, last_modified_func=store.last_modified)
    async def get_items(response: Response):
        if returned:
            return JSONResponse({"detail": "gone"}, status_code=410)
        response.status_code = 410
        return {"detail": "gone"}

    response = make_client(app).get("/items")
    assert response.status_code == 410
    assert "etag" not in response.headers
    assert "last-modified" not in response.headers


def test_route_status_code_decides_whether_validators_are_sent(make_client, store):
    app = FastAPI()

    @app.get("/accepted", status_code=202)
    @scrudful(etag_func=store.etag)
    async def accepted():
        return {}

    @app.get("/redirect", status_code=307)
    @scrudful(etag_func=store.etag)
    async def redirect(response: Response):
        response.headers["Location"] = "/accepted"
        return None

    client = make_client(app)
    assert client.get("/accepted").headers["etag"] == '"v1"'
    response = client.get("/redirect", follow_redirects=False)
    assert response.status_code == 307
    assert "etag" not in response.headers
    assert response.headers["access-control-expose-headers"] == "Location"


def test_http_exception_propagates_unchanged(make_client, store):
    app = FastAPI()

    @app.get("/items")
    @scrudful(etag_func=store.etag, schema_link_or_func=SCHEMA)
    async def get_items():
        raise HTTPException(status_code=409, detail="nope", headers={"X-A": "b"})

    response = make_client(app).get("/items")
    assert response.status_code == 409
    assert response.json() == {"detail": "nope"}
    assert response.headers["x-a"] == "b"
    assert "etag" not in response.headers
    assert "link" not in response.headers


def test_headers_set_by_the_endpoint_are_kept_and_merged(make_client, store):
    app = FastAPI()

    @app.get("/items")
    @scrudful(
        etag_func=store.etag,
        last_modified_func=store.last_modified,
        schema_link_or_func=SCHEMA,
        context_link_or_func=CONTEXT,
    )
    async def get_items(response: Response):
        response.headers["ETag"] = 'W/"own"'
        response.headers.append("Link", '<http://example.com/1>; rel="prev"')
        response.headers.append("Link", '<http://example.com/3>; rel="next"')
        response.headers["Access-Control-Expose-Headers"] = "X-Total"
        return {}

    response = make_client(app).get("/items")
    assert response.headers["etag"] == 'W/"own"'
    assert response.headers["last-modified"] == MODIFIED_HTTP
    assert response.headers.get_list("link") == [
        '<http://example.com/1>; rel="prev", <http://example.com/3>; rel="next", '
        f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    ]
    assert response.headers["access-control-expose-headers"] == "X-Total, ETag, Link"


def test_cors_middleware_replaces_expose_headers_on_cross_origin_requests(
    make_client, store
):
    """Documented limitation: configure CORSMiddleware's expose_headers instead."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        expose_headers=["ETag", "Link", "Location"],
    )

    @app.get("/items")
    @scrudful(etag_func=store.etag)
    async def get_items():
        return {}

    client = make_client(app)
    assert client.get("/items").headers["access-control-expose-headers"] == "ETag"
    cross_origin = client.get("/items", headers={"Origin": "http://other.example"})
    assert cross_origin.headers["access-control-expose-headers"] == (
        "ETag, Link, Location"
    )


def test_response_model_does_not_turn_precondition_errors_into_500(make_client, store):
    class Item(BaseModel):
        name: str

    app = FastAPI()

    @app.put("/items", response_model=Item)
    @scrudful(etag_func=store.etag, last_modified_func=store.last_modified)
    async def put_items():
        return {"name": "x"}

    client = make_client(app)
    missing = client.put("/items")
    assert missing.status_code == 428
    assert missing.json() == {
        "detail": "This request must be conditional.",
        "accepted-precondition-headers": ["If-Match", "If-Unmodified-Since"],
    }
    failed = client.put("/items", headers={"If-Match": '"v0"'})
    assert failed.status_code == 412
    assert failed.json() == {"detail": "Precondition failed."}
    malformed = client.put("/items", headers={"If-Match": "v1"})
    assert malformed.status_code == 400
    assert client.put("/items", headers={"If-Match": '"v1"'}).json() == {"name": "x"}


def test_conditional_responses_document_the_status_codes(make_client):
    app = FastAPI()
    originals = [CONDITIONAL_READ_RESPONSES, CONDITIONAL_WRITE_RESPONSES]
    before = [{k: dict(v) for k, v in d.items()} for d in originals]

    @app.get("/items", responses=CONDITIONAL_READ_RESPONSES)
    @app.put("/items", responses=CONDITIONAL_WRITE_RESPONSES)
    @scrudful(etag_func=lambda **kwargs: "a")
    async def items():
        return {}

    paths = make_client(app).get("/openapi.json").json()["paths"]
    read = paths["/items"]["get"]["responses"]
    write = paths["/items"]["put"]["responses"]
    assert set(read) == {"200", "304", "400", "412"}
    assert set(write) == {"200", "400", "412", "428"}
    for responses, documented in [
        (read, CONDITIONAL_READ_RESPONSES),
        (write, CONDITIONAL_WRITE_RESPONSES),
    ]:
        for status, response in documented.items():
            assert responses[str(status)]["description"] == response["description"]
    assert "content" not in read["304"]
    assert [CONDITIONAL_READ_RESPONSES, CONDITIONAL_WRITE_RESPONSES] == before


# --- links -----------------------------------------------------------------------


def test_link_defaults_and_custom_values(make_client):
    app = FastAPI()

    @app.get("/defaults")
    @scrudful(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    async def defaults():
        return {}

    @app.get("/custom")
    @scrudful(
        schema_link_or_func=lambda **kwargs: SCHEMA,
        schema_rel_or_func="schema",
        schema_type_or_func=lambda **kwargs: "application/schema+json",
        context_link_or_func=lambda **kwargs: None,
    )
    async def custom():
        return {}

    @app.get("/none")
    @scrudful(schema_rel_or_func="ignored", context_link_or_func="")
    async def none():
        return {}

    @app.get("/callable-defaults")
    @scrudful(
        schema_link_or_func=SCHEMA,
        schema_rel_or_func=lambda **kwargs: None,
        schema_type_or_func=lambda **kwargs: "",
    )
    async def callable_defaults():
        return {}

    client = make_client(app)
    response = client.get("/defaults")
    assert response.headers["link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert response.headers["access-control-expose-headers"] == "Link"
    assert client.get("/custom").headers["link"] == (
        f'<{SCHEMA}>; rel="schema"; type="application/schema+json"'
    )
    response = client.get("/none")
    assert "link" not in response.headers
    assert "access-control-expose-headers" not in response.headers
    assert client.get("/callable-defaults").headers["link"] == SCHEMA_LINK


def test_link_values_are_escaped(make_client):
    app = FastAPI()

    @app.get("/items/{name}")
    @scrudful(schema_link_or_func=lambda name, **kwargs: f"http://x/{name}")
    async def get_item(name: str):
        return {}

    response = make_client(app).get('/items/a>; rel="next')
    assert response.headers["link"] == (
        '<http://x/a%3E;%20rel=%22next>; rel="describedBy"; type="application/json"'
    )


# --- validators --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (5, '"5"'),
        ("abc", '"abc"'),
        ('"abc"', '"abc"'),
        ('W/"abc"', 'W/"abc"'),
        ("", '""'),
        (
            uuid.UUID("12345678-1234-5678-1234-567812345678"),
            '"12345678-1234-5678-1234-567812345678"',
        ),
    ],
)
def test_etag_values_are_quoted_without_a_version_suffix(make_client, value, expected):
    app = FastAPI()

    @app.get("/items")
    @scrudful(etag_func=lambda **kwargs: value)
    async def get_items():
        return {}

    client = make_client(app)
    response = client.get("/items")
    assert response.headers["etag"] == expected
    assert client.get("/items", headers={"If-None-Match": expected}).status_code == 304


def test_invalid_etag_value_is_a_server_error(make_client):
    app = FastAPI()

    @app.get("/items")
    @scrudful(etag_func=lambda **kwargs: "has space")
    async def get_items():
        return {}

    with pytest.raises(ValueError, match="entity-tag"):
        make_client(app).get("/items")


@pytest.mark.parametrize(
    "modified, expected",
    [
        (datetime(2021, 5, 25, 12, 0, tzinfo=IST), "Tue, 25 May 2021 06:30:00 GMT"),
        (datetime(2021, 5, 25, 12, 0, 0, 999999), "Tue, 25 May 2021 12:00:00 GMT"),
        (0, "Thu, 01 Jan 1970 00:00:00 GMT"),
        (1621944000.75, "Tue, 25 May 2021 12:00:00 GMT"),
    ],
)
def test_last_modified_values(make_client, store, modified, expected):
    app = FastAPI()
    store.modified = modified

    @app.get("/items")
    @scrudful(last_modified_func=store.last_modified)
    async def get_items():
        return {}

    client = make_client(app)
    assert client.get("/items").headers["last-modified"] == expected
    response = client.get("/items", headers={"If-Modified-Since": expected})
    assert response.status_code == 304
    assert response.headers["last-modified"] == expected


def test_get_returns_validators(client):
    response = client.get("/item")
    assert response.status_code == 200
    assert response.headers["etag"] == '"v1"'
    assert response.headers["last-modified"] == MODIFIED_HTTP
    assert response.headers["link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert response.headers["access-control-expose-headers"] == "ETag, Link"


@pytest.mark.parametrize(
    "if_none_match",
    ['"v1"', 'W/"v1"', "*", '"x", "v1"', '"x",W/"v1"'],
)
def test_if_none_match_gives_304_with_validators(client, store, if_none_match):
    response = client.get("/item", headers={"If-None-Match": if_none_match})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == '"v1"'
    assert response.headers["last-modified"] == MODIFIED_HTTP
    assert response.headers["access-control-expose-headers"] == "ETag"
    assert "endpoint" not in store.calls


def test_if_none_match_mismatch_gives_200(client):
    response = client.get("/item", headers={"If-None-Match": '"v0", W/"v2"'})
    assert response.status_code == 200


def test_repeated_header_lines_are_combined(client, store):
    response = client.get(
        "/item", headers=[("If-None-Match", '"x"'), ("If-None-Match", '"v1"')]
    )
    assert response.status_code == 304
    response = client.put("/item", headers=[("If-Match", '"x"'), ("If-Match", '"v1"')])
    assert response.status_code == 200
    assert store.version == 2


def test_if_modified_since_round_trip_with_sub_second_last_modified(client):
    last_modified = client.get("/item").headers["last-modified"]
    assert last_modified == MODIFIED_HTTP
    response = client.get("/item", headers={"If-Modified-Since": last_modified})
    assert response.status_code == 304
    response = client.get("/item", headers={"If-Modified-Since": EARLIER_HTTP})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "date",
    [
        "Tuesday, 02-Jan-24 03:04:05 GMT",  # RFC 850
        "Tue Jan  2 03:04:05 2024",  # asctime
    ],
)
def test_obsolete_date_formats_are_accepted(client, date):
    assert client.get("/item", headers={"If-Modified-Since": date}).status_code == 304


def test_unparseable_if_modified_since_is_ignored(client):
    response = client.get("/item", headers={"If-Modified-Since": "yesterday"})
    assert response.status_code == 200


def test_if_match_on_get(client):
    assert client.get("/item", headers={"If-Match": '"v1"'}).status_code == 200
    assert client.get("/item", headers={"If-Match": '"v0"'}).status_code == 412


def test_malformed_if_none_match_gives_400(client, store):
    response = client.get("/item", headers={"If-None-Match": "v1"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Malformed If-None-Match header."}
    assert "endpoint" not in store.calls


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("if_match", ["v1", "USER_1", '"v1', "W/v1", '"v1" x'])
def test_malformed_if_match_gives_400(client, store, method, if_match):
    response = client.request(method, "/item", headers={"If-Match": if_match})
    assert response.status_code == 400
    assert response.json() == {"detail": "Malformed If-Match header."}
    assert "endpoint" not in store.calls
    assert (store.exists, store.version) == (True, 1)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "headers",
    [{}, {"If-Unmodified-Since": "yesterday"}, {"If-None-Match": "*"}],
)
def test_unconditional_writes_give_428(client, store, method, headers):
    response = client.request(method, "/item", headers=headers)
    assert response.status_code == 428
    assert response.json() == {
        "detail": "This request must be conditional.",
        "accepted-precondition-headers": ["If-Match", "If-Unmodified-Since"],
    }
    assert "endpoint" not in store.calls
    assert (store.exists, store.version) == (True, 1)


def test_428_lists_only_the_usable_headers(make_client, store):
    app = FastAPI()

    @app.delete("/etag-only")
    @scrudful(etag_func=store.etag)
    async def etag_only():
        return {}

    @app.delete("/last-modified-only")
    @scrudful(last_modified_func=store.last_modified)
    async def last_modified_only():
        return {}

    client = make_client(app)
    for path, accepted in [
        ("/etag-only", ["If-Match"]),
        ("/last-modified-only", ["If-Unmodified-Since"]),
    ]:
        response = client.delete(path)
        assert response.status_code == 428
        assert response.json()["accepted-precondition-headers"] == accepted
    # Without last_modified_func, If-Unmodified-Since doesn't make it conditional.
    headers = {"If-Unmodified-Since": MODIFIED_HTTP}
    assert client.delete("/etag-only", headers=headers).status_code == 428
    assert client.delete("/last-modified-only", headers=headers).status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
@pytest.mark.parametrize(
    "headers",
    [
        {"If-Match": '"v1"'},
        {"If-Match": "*"},
        {"If-Unmodified-Since": MODIFIED_HTTP},
        {"If-Match": '"v1"', "If-Unmodified-Since": EARLIER_HTTP},
    ],
)
def test_conditional_write_gets_fresh_validators(client, store, method, headers):
    response = client.request(method, "/item", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"version": 2, "method": method}
    assert response.headers["etag"] == '"v2"'
    assert response.headers["last-modified"] == "Tue, 02 Jan 2024 04:04:05 GMT"
    assert response.headers["access-control-expose-headers"] == "ETag"
    assert store.calls == ["etag", "last_modified", "endpoint"] + [
        "etag",
        "last_modified",
    ]


def test_validators_change_after_an_update(client):
    old_etag = client.get("/item").headers["etag"]
    put = client.put("/item", headers={"If-Match": old_etag})
    assert put.status_code == 200
    new_etag = put.headers["etag"]
    assert new_etag != old_etag
    assert client.put("/item", headers={"If-Match": old_etag}).status_code == 412
    assert client.get("/item", headers={"If-None-Match": old_etag}).status_code == 200
    assert client.get("/item", headers={"If-None-Match": new_etag}).status_code == 304
    stale = client.get("/item", headers={"If-Modified-Since": MODIFIED_HTTP})
    assert stale.status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "headers",
    [
        {"If-Match": '"v0"'},
        {"If-Match": 'W/"v1"'},  # weak tags never match strongly
        {"If-Unmodified-Since": EARLIER_HTTP},
        {"If-Match": '"v1"', "If-None-Match": '"v1"'},
        {"If-Match": '"v1"', "If-None-Match": "*"},
    ],
)
def test_failed_preconditions_give_412(client, store, method, headers):
    response = client.request(method, "/item", headers=headers)
    assert response.status_code == 412
    assert response.json() == {"detail": "Precondition failed."}
    assert "etag" not in response.headers
    assert "endpoint" not in store.calls
    assert (store.exists, store.version) == (True, 1)


def test_delete_gets_no_validators(client, store):
    response = client.delete("/item", headers={"If-Match": '"v1"'})
    assert response.status_code == 204
    assert "etag" not in response.headers
    assert "last-modified" not in response.headers
    assert store.exists is False


def test_post_gets_no_validators_but_preconditions_apply(client):
    response = client.post("/item")
    assert response.status_code == 201
    assert "etag" not in response.headers
    assert client.post("/item", headers={"If-Match": '"v0"'}).status_code == 412
    response = client.post("/item", headers={"If-Unmodified-Since": EARLIER_HTTP})
    assert response.status_code == 412
    assert client.post("/item", headers={"If-Match": '"v1"'}).status_code == 201


def test_if_unmodified_since_is_ignored_without_a_modification_date(make_client, store):
    app = FastAPI()

    @app.api_route("/item", methods=["GET", "POST"])
    @scrudful(etag_func=store.etag)
    async def item():
        return {}

    client = make_client(app)
    headers = {"If-Unmodified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"}
    assert client.get("/item", headers=headers).status_code == 200
    assert client.post("/item", headers=headers).status_code == 200


def test_no_preconditions_without_validator_callbacks(make_client):
    app = FastAPI()

    @app.api_route("/item", methods=["GET", "PUT", "DELETE"])
    @scrudful(schema_link_or_func=SCHEMA)
    async def item():
        return {}

    client = make_client(app)
    for method in ("GET", "PUT", "DELETE"):
        for headers in ({}, {"If-Match": "*"}, {"If-Match": "x"}):
            response = client.request(method, "/item", headers=headers)
            assert response.status_code == 200
            assert response.headers["link"] == SCHEMA_LINK
            assert "etag" not in response.headers


def test_missing_resource_is_left_to_the_endpoint(client, store):
    store.exists = False
    assert client.get("/item", headers={"If-None-Match": "*"}).status_code == 404
    assert client.get("/item", headers={"If-Match": '"v1"'}).status_code == 404
    assert client.delete("/item").status_code == 404
    assert client.request("PATCH", "/item").status_code == 201


def test_put_can_create_a_missing_resource(client, store):
    store.exists = False
    assert client.put("/item", headers={"If-Match": "*"}).status_code == 412
    assert client.put("/item", headers={"If-Match": '"v1"'}).status_code == 412
    response = client.put("/item", headers={"If-None-Match": "*"})
    assert response.status_code == 201
    assert response.headers["etag"] == '"v1"'
    assert response.headers["last-modified"] == MODIFIED_HTTP
    assert store.exists is True


def test_put_without_precondition_creates_a_missing_resource(client, store):
    store.exists = False
    assert client.put("/item").status_code == 201


def test_head_needs_an_explicit_route(make_client, store):
    """FastAPI does not route HEAD to GET endpoints; an explicit route works."""
    app = FastAPI()

    @app.get("/get-only")
    @scrudful(etag_func=store.etag)
    async def get_only():
        return {}

    @app.api_route("/get-and-head", methods=["GET", "HEAD"])
    @scrudful(etag_func=store.etag, last_modified_func=store.last_modified)
    async def get_and_head():
        return {}

    client = make_client(app)
    assert client.head("/get-only").status_code == 405
    response = client.head("/get-and-head")
    assert response.status_code == 200
    assert response.headers["etag"] == '"v1"'
    response = client.head("/get-and-head", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.headers["last-modified"] == MODIFIED_HTTP
    response = client.head("/get-and-head", headers={"If-Match": '"v0"'})
    assert response.status_code == 412
