"""Mechanics of the ``scrudful.fastapi`` decorator, tested with tiny ad-hoc apps."""

import dataclasses
import inspect
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import pytest
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    Security,
)
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import BaseModel, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Mount, Router

from scrudful import fastapi as fastapi_module
from scrudful.fastapi import (
    CONDITIONAL_READ_RESPONSES,
    CONDITIONAL_WRITE_RESPONSES,
    Entity,
    _annotation_is,
    _EndpointConfig,
    add_error_links,
    add_options,
    http_schema_resource,
    scrudful,
)
from scrudful._hsr import hsr_link
from scrudful.problem import problem_from_errors
from tests import fastapi_future_annotations as future_endpoints
from tests.semantic_http import (
    HSR_DOCUMENT_REL,
    PROBLEM_DETAILS_SCHEMA,
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    validate_http_schema_resource,
)

# Non-zero microseconds: the header is floored to whole seconds and conditional
# requests echoing it must still match.
MODIFIED = datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
MODIFIED_HTTP = "Tue, 02 Jan 2024 03:04:05 GMT"
EARLIER_HTTP = "Tue, 02 Jan 2024 03:04:04 GMT"
IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA = "http://example.com/schema.json"
CONTEXT = "http://example.com/context.jsonld"
#: The base URL of the add_options calls: the test client's, so the relative
#: links resolve as against its requests.
TEST_BASE_URL = "http://testserver/"
SCHEMA_LINK = f'<{SCHEMA}>; rel="describedby"; type="application/schema+json"'
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


def test_future_annotations_are_evaluated_in_the_endpoint_module(make_client):
    # FastAPI < 0.124 evaluates string annotations in the wrapper's globals, so
    # the wrapper's signature must carry the classes themselves; otherwise the
    # body model becomes a required query parameter (422).
    params = inspect.signature(future_endpoints.replace_item).parameters
    assert params["request"].annotation is Request
    assert params["item_id"].annotation is int
    assert params["body"].annotation is future_endpoints.Body
    assert params["_scrudful_response"].annotation is Response

    response = make_client(future_endpoints.make_app()).put(
        "/items/3", json={"name": "a"}, headers={"If-Match": '"v3"'}
    )
    assert response.status_code == 200
    assert response.json() == {"name": "a", "method": "PUT"}
    assert response.headers["etag"] == '"v3"'


def test_unresolvable_annotations_fall_back_to_the_unevaluated_signature():
    @scrudful()
    async def endpoint(item_id: "int", thing: "Undefined" = None):  # noqa: F821
        pass

    params = inspect.signature(endpoint).parameters
    assert params["item_id"].annotation == "int"
    assert params["thing"].annotation == "Undefined"


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
        '<http://testserver/schema>; rel="describedby"; type="application/schema+json"'
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
        context_link_or_func=CONTEXT,
    )
    async def get_items():
        return JSONResponse({"a": 1}, headers={"X-Own": "1"})

    response = make_client(app).get("/items")
    assert response.json() == {"a": 1}
    assert response.headers["x-own"] == "1"
    assert response.headers["etag"] == '"v1"'
    assert response.headers["last-modified"] == MODIFIED_HTTP
    assert response.headers["link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert response.headers["access-control-expose-headers"] == "ETag, Link, Allow"


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
    assert response.headers["access-control-expose-headers"] == "Link, Location, Allow"


def test_http_exception_propagates_unchanged(make_client, store):
    app = FastAPI()

    @app.get("/items")
    @scrudful(
        etag_func=store.etag, schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT
    )
    async def get_items():
        raise HTTPException(status_code=409, detail="nope", headers={"X-A": "b"})

    response = make_client(app).get("/items")
    assert response.status_code == 409
    assert response.json() == {"detail": "nope"}
    assert response.headers["x-a"] == "b"
    assert "etag" not in response.headers
    assert "link" not in response.headers


def test_only_a_success_with_content_gets_the_links(make_client, store):
    """The endpoint's Links go on a 2xx but 204, as in OPTIONS."""
    app = FastAPI()
    own = '<http://example.com/own>; rel="describedby"'
    links = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)

    @app.get("/items/{status}")
    @scrudful(**links)
    async def get_item(status: int, response: Response):
        if status == 404:
            return JSONResponse({"detail": "gone"}, status_code=404)
        if status == 405:
            # A Link of the endpoint's own on an error is kept as is.
            return JSONResponse(
                {"detail": "gone"}, status_code=404, headers={"Link": own}
            )
        response.status_code = status
        return {"status": status}

    @app.delete("/items/{pk}", status_code=204)
    @scrudful(**links)
    async def delete_item(pk: int):
        return None

    client = make_client(app)
    response = client.get("/items/200")
    assert response.headers["link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    for status in (400, 404):
        response = client.get(f"/items/{status}")
        assert response.status_code == status
        assert "link" not in response.headers
    response = client.get("/items/405")
    assert response.status_code == 404
    assert response.headers.get_list("link") == [own]
    response = client.delete("/items/1")
    assert response.status_code == 204
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
    assert response.headers["access-control-expose-headers"] == (
        "X-Total, ETag, Link, Allow"
    )


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
    assert client.get("/items").headers["access-control-expose-headers"] == (
        "ETag, Link, Allow"
    )
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
        context_link_or_func=CONTEXT,
    )
    async def callable_defaults():
        return {}

    client = make_client(app)
    response = client.get("/defaults")
    assert response.headers["link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    assert response.headers["access-control-expose-headers"] == "Link, Allow"
    assert client.get("/custom").headers["link"] == (
        f'<{SCHEMA}>; rel="schema"; type="application/schema+json"'
    )
    response = client.get("/none")
    assert "link" not in response.headers
    assert response.headers["access-control-expose-headers"] == "Link, Allow"
    assert client.get("/callable-defaults").headers["link"] == (
        f"{SCHEMA_LINK}, {CONTEXT_LINK}"
    )


def test_link_values_are_escaped(make_client):
    app = FastAPI()

    @app.get("/items/{name}")
    @scrudful(schema_link_or_func=lambda name, **kwargs: f"http://x/{name}")
    async def get_item(name: str):
        return {}

    response = make_client(app).get('/items/a>; rel="next')
    assert response.headers["link"] == (
        '<http://x/a%3E;%20rel=%22next>; rel="describedby"; '
        'type="application/schema+json"'
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
    assert response.headers["access-control-expose-headers"] == "ETag, Link, Allow"


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
    assert response.headers["access-control-expose-headers"] == "ETag, Link, Allow"
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
    [{}, {"If-Unmodified-Since": "yesterday"}, {"If-None-Match": '"v1"'}],
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


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_if_none_match_star_on_an_existing_resource_gives_412(client, store, method):
    """Create-only (RFC 9110 §13.1.2): conditional, so 412 rather than 428."""
    response = client.request(method, "/item", headers={"If-None-Match": "*"})
    assert response.status_code == 412
    assert response.json() == {"detail": "Precondition failed."}
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
    assert response.headers["access-control-expose-headers"] == "ETag, Link, Allow"
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
    @scrudful(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    async def item():
        return {}

    client = make_client(app)
    for method in ("GET", "PUT", "DELETE"):
        for headers in ({}, {"If-Match": "*"}, {"If-Match": "x"}):
            response = client.request(method, "/item", headers=headers)
            assert response.status_code == 200
            assert response.headers["link"] == f"{SCHEMA_LINK}, {CONTEXT_LINK}"
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


# --- generic alias annotations (Python 3.10) -----------------------------------


def test_annotation_is_false_for_generic_aliases():
    assert not _annotation_is(list[int], Request, {})
    assert not _annotation_is(dict[str, int], Response, {})
    assert _annotation_is(Request, Request, {})


def test_generic_alias_parameters_are_decorated(make_client, store):
    app = FastAPI()

    @app.get("/q")
    @scrudful(etag_func=store.etag)
    def query(ids: list[int] = Query([])):
        return {"ids": ids}

    @app.post("/b")
    @scrudful(etag_func=store.etag)
    def body(data: dict[str, int]):
        return data

    client = make_client(app)
    response = client.get("/q?ids=1&ids=2")
    assert response.json() == {"ids": [1, 2]}
    assert response.headers["etag"] == '"v1"'
    response = client.post("/b", json={"a": 1})
    assert response.json() == {"a": 1}
    assert response.status_code == 200


# --- headers set by dependencies on early responses ----------------------------


def _header_app(store):
    app = FastAPI()

    def set_headers(response: Response):
        response.headers.append("Cache-Control", "max-age=60")
        response.headers.append("Cache-Control", "must-revalidate")
        response.headers.append("Vary", "Accept")
        response.headers.append("Vary", "Origin")
        response.headers["Access-Control-Expose-Headers"] = "X-Custom"
        response.headers["ETag"] = '"dependency"'
        response.headers["Content-Type"] = "text/plain"
        response.headers["Content-Encoding"] = "gzip"
        response.set_cookie("session", "abc")
        response.raw_headers.append((b"Set-Cookie", b"raw=1"))

    @app.get("/item", dependencies=[Depends(set_headers)])
    @scrudful(etag_func=store.etag)
    def get_item():
        return {}

    @app.put("/item", dependencies=[Depends(set_headers)])
    @scrudful(etag_func=store.etag)
    def put_item():
        return {}

    return app


def test_304_carries_dependency_headers(make_client, store):
    client = make_client(_header_app(store))
    response = client.get("/item", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.headers.get_list("cache-control") == [
        "max-age=60",
        "must-revalidate",
    ]
    assert response.headers.get_list("vary") == ["Accept", "Origin"]
    assert response.headers.get_list("etag") == ['"v1"']
    assert response.headers.get_list("set-cookie") == [
        "session=abc; Path=/; SameSite=lax",
        "raw=1",
    ]
    assert response.headers["access-control-expose-headers"] == (
        "X-Custom, ETag, Link, Allow"
    )
    assert "content-type" not in response.headers
    assert "content-encoding" not in response.headers
    assert response.content == b""


def test_412_carries_only_dependency_cookies(make_client, store):
    client = make_client(_header_app(store))
    response = client.put("/item", headers={"If-Match": '"other"'})
    assert response.status_code == 412
    assert response.headers.get_list("set-cookie") == [
        "session=abc; Path=/; SameSite=lax",
        "raw=1",
    ]
    assert response.headers["content-type"].startswith("application/json")
    assert "content-encoding" not in response.headers
    assert "cache-control" not in response.headers
    assert "vary" not in response.headers
    assert "etag" not in response.headers
    assert response.headers["access-control-expose-headers"] == "X-Custom, Link, Allow"


# --- default status of the response class --------------------------------------


def test_response_class_default_status_decides_validators(make_client, store):
    app = FastAPI()

    @app.get("/r", response_class=RedirectResponse)
    @scrudful(etag_func=store.etag)
    def redirect():
        return "http://example.com/"

    @app.get("/ok")
    @scrudful(etag_func=store.etag)
    def ok():
        return {}

    @app.get("/created", status_code=201)
    @scrudful(etag_func=store.etag)
    def created():
        return {}

    client = make_client(app)
    response = client.get("/r", follow_redirects=False)
    assert response.status_code == 307
    assert "etag" not in response.headers
    assert client.get("/ok").headers["etag"] == '"v1"'
    response = client.get("/created")
    assert response.status_code == 201
    assert response.headers["etag"] == '"v1"'


class UnhashableResponseClass:
    """A callable response class instance defining ``__eq__`` but no hash."""

    def __init__(self, default_status: int) -> None:
        self.default_status = default_status
        self.media_type = "application/json"
        # A default that depends on the instance: the signature reports it.
        self.__signature__ = inspect.Signature(
            [
                inspect.Parameter(
                    "content", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None
                ),
                inspect.Parameter(
                    "status_code",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=default_status,
                ),
                inspect.Parameter("kwargs", inspect.Parameter.VAR_KEYWORD),
            ]
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UnhashableResponseClass)

    def __call__(self, content=None, status_code=None, **kwargs):
        if status_code is None:
            status_code = self.default_status
        return JSONResponse(content, status_code=status_code, **kwargs)


def test_unhashable_response_class(make_client, store):
    app = FastAPI()
    ok, not_found = UnhashableResponseClass(200), UnhashableResponseClass(404)

    @app.get("/ok", response_class=ok)
    @scrudful(etag_func=store.etag)
    def get_ok():
        return {}

    @app.get("/missing", response_class=not_found)
    @scrudful(etag_func=store.etag)
    def get_missing():
        return {}

    client = make_client(app)
    for _ in range(2):
        response = client.get("/ok")
        assert response.status_code == 200
        assert response.headers["etag"] == '"v1"'
        response = client.get("/missing")
        assert response.status_code == 404
        assert "etag" not in response.headers


def test_default_status_cache_is_bounded():
    assert fastapi_module._default_status(RedirectResponse) == 307
    assert fastapi_module._default_status(UnhashableResponseClass(202)) == 202
    assert fastapi_module._default_status(object()) == 200
    assert fastapi_module._cached_default_status.cache_info().maxsize == 128


def test_included_router_default_response_class(make_client, store):
    router = APIRouter()

    @router.get("/r")
    @scrudful(etag_func=store.etag)
    def redirect():
        return "http://example.com/"

    app = FastAPI()
    app.include_router(router, default_response_class=RedirectResponse)

    response = make_client(app).get("/r", follow_redirects=False)
    assert response.status_code == 307
    assert "etag" not in response.headers


# --- one threadpool hop for sync validator callbacks ---------------------------


def test_sync_validators_share_one_threadpool_call(make_client, monkeypatch):
    app = FastAPI()
    threads = []
    hops = []
    original = fastapi_module.run_in_threadpool

    async def counting(func, *args, **kwargs):
        hops.append(func)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(fastapi_module, "run_in_threadpool", counting)

    def etag(**kwargs):
        threads.append(threading.get_ident())
        return "v1"

    def modified(**kwargs):
        threads.append(threading.get_ident())
        return MODIFIED

    @app.get("/item")
    @scrudful(etag_func=etag, last_modified_func=modified)
    async def get_item():
        return {}

    response = make_client(app).get("/item")
    assert response.headers["etag"] == '"v1"'
    assert response.headers["last-modified"] == MODIFIED_HTTP
    assert len(hops) == 1
    assert len(threads) == 2
    assert threads[0] == threads[1]


def test_mixed_validators_keep_order_and_await_results(make_client):
    app = FastAPI()
    order = []

    async def etag(**kwargs):
        order.append("etag")
        return "v1"

    def modified(**kwargs):
        order.append("last_modified")

        async def later():
            return MODIFIED

        return later()

    @app.get("/item")
    @scrudful(etag_func=etag, last_modified_func=modified)
    async def get_item():
        return {}

    response = make_client(app).get("/item")
    assert order == ["etag", "last_modified"]
    assert response.headers["etag"] == '"v1"'
    assert response.headers["last-modified"] == MODIFIED_HTTP


def test_sync_then_async_validators_keep_order(make_client):
    app = FastAPI()
    order = []

    def etag(**kwargs):
        order.append("etag")
        return "v1"

    async def modified(**kwargs):
        order.append("last_modified")
        return MODIFIED

    @app.get("/item")
    @scrudful(etag_func=etag, last_modified_func=modified)
    async def get_item():
        return {}

    response = make_client(app).get("/item")
    assert order == ["etag", "last_modified"]
    assert response.headers["last-modified"] == MODIFIED_HTTP


def test_sync_awaitable_runs_before_the_next_callback(make_client):
    app = FastAPI()
    order = []

    def etag(**kwargs):
        async def body():
            order.append("etag")
            return "v1"

        return body()

    def modified(**kwargs):
        order.append("last_modified")
        return MODIFIED

    @app.get("/item")
    @scrudful(etag_func=etag, last_modified_func=modified)
    async def get_item():
        return {}

    make_client(app).get("/item")
    assert order == ["etag", "last_modified"]


def test_awaitable_is_awaited_before_a_failing_callback(make_client):
    app = FastAPI()

    def etag(**kwargs):
        async def body():
            return "v1"  # pragma: no cover

        return body()

    def modified(**kwargs):
        raise RuntimeError("boom")

    @app.get("/item")
    @scrudful(etag_func=etag, last_modified_func=modified)
    async def get_item():
        return {}

    with pytest.raises(RuntimeError, match="boom"):
        make_client(app).get("/item")


# --- empty and repeated header lines -----------------------------------------


def test_empty_dependency_expose_header_on_early_responses(make_client, store):
    app = FastAPI()

    def empty(response: Response):
        response.headers["Access-Control-Expose-Headers"] = " , "

    @app.put("/item", dependencies=[Depends(empty)])
    @scrudful(etag_func=store.etag)
    def put_item():
        return {}

    client = make_client(app)
    response = client.put("/item", headers={"If-Match": '"other"'})
    assert response.status_code == 412
    assert response.headers["access-control-expose-headers"] == "Link, Allow"
    response = client.put("/item")
    assert response.status_code == 428
    assert response.headers["access-control-expose-headers"] == "Link, Allow"


def test_empty_if_match_is_absent(make_client, store):
    app = FastAPI()

    @app.get("/item")
    @scrudful(etag_func=store.etag)
    def get_item():
        return {}

    @app.put("/item")
    @scrudful(etag_func=store.etag)
    def put_item():
        return {}

    client = make_client(app)
    assert client.get("/item", headers={"If-Match": ""}).status_code == 200
    assert client.put("/item", headers={"If-Match": ""}).status_code == 428


def test_304_drops_content_language(make_client, store):
    app = FastAPI()

    def language(response: Response):
        response.headers["Content-Language"] = "en"

    @app.get("/item", dependencies=[Depends(language)])
    @scrudful(etag_func=store.etag)
    def get_item():
        return {}

    client = make_client(app)
    assert client.get("/item").headers["content-language"] == "en"
    response = client.get("/item", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert "content-language" not in response.headers


def test_repeated_expose_header_lines_are_all_kept(make_client, store):
    app = FastAPI()

    @app.get("/item")
    @scrudful(etag_func=store.etag)
    def get_item(response: Response):
        response.headers.append("Access-Control-Expose-Headers", "X-One")
        response.headers.append("Access-Control-Expose-Headers", "X-Two")
        return {}

    response = make_client(app).get("/item")
    assert response.headers.get_list("access-control-expose-headers") == [
        "X-One, X-Two, ETag, Link, Allow"
    ]


# --- error Links, absolute links and add_error_links ---------------------------

ERROR = Entity(
    schema_link_or_func="/schemas/error",
    context_link_or_func="http://example.com/error.jsonld",
    media_type="application/problem+json",
)
ERROR_LINKS = (
    '<http://testserver/schemas/error>; rel="describedby"; '
    'type="application/schema+json", <http://example.com/error.jsonld>; '
    'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)


def _error_app(store, error=ERROR):
    app = FastAPI()

    @app.api_route("/items/{item_id}", methods=["GET", "PUT"])
    @scrudful(etag_func=store.etag, **ITEM_LINKS, error=error)
    async def item(item_id: int):
        return {}

    return app


def _exposed(response) -> list[str]:
    value = response.headers.get("access-control-expose-headers", "")
    return [name.strip() for name in value.split(",") if name.strip()]


@pytest.mark.parametrize(
    "method, headers, status",
    [
        ("PUT", {"If-Match": '"other"'}, 412),
        ("PUT", {}, 428),
        ("GET", {"If-None-Match": "v1"}, 400),
        ("PUT", {"If-Match": "v1"}, 400),
    ],
)
def test_error_links_on_early_responses(make_client, store, method, headers, status):
    client = make_client(_error_app(store))
    response = client.request(method, "/items/3", headers=headers)
    assert response.status_code == status
    assert response.headers["link"] == ERROR_LINKS
    assert response.headers["content-type"] == "application/problem+json"
    assert "Link" in _exposed(response)
    assert link_problems(method, status, response.headers, True) == []
    assert "endpoint" not in store.calls


def test_304_gets_no_error_links(make_client, store):
    response = make_client(_error_app(store)).get(
        "/items/3", headers={"If-None-Match": '"v1"'}
    )
    assert response.status_code == 304
    assert "link" not in response.headers
    assert "content-type" not in response.headers


def test_early_responses_without_error_are_unchanged(make_client, store):
    response = make_client(_error_app(store, error=None)).put("/items/3")
    assert response.status_code == 428
    assert "link" not in response.headers
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    "method, headers, status",
    [
        ("PUT", {"If-Match": '"other"'}, 412),
        ("PUT", {}, 428),
        ("GET", {"If-None-Match": "v1"}, 400),
    ],
)
def test_early_responses_without_error_get_the_app_s_error_entity(
    make_client, store, method, headers, status
):
    """Without the endpoint's ``error``, scrudful's own 400, 412 and 428 are
    described by the app's ``add_error_links`` entity, on the wire and in the
    HSR, as the errors the app's handlers answer for it are. Its callables get
    ``request=`` only, as in those handlers."""
    seen = []

    def schema(request=None):  # None in describe mode
        seen.append(type(request).__name__)
        return "/schemas/error"

    app = _error_app(store, error=None)
    add_error_links(
        app,
        Entity(
            schema_link_or_func=schema,
            context_link_or_func="http://example.com/error.jsonld",
            media_type="application/problem+json",
        ),
    )
    add_options(app, base_url="http://testserver/")
    response = make_client(app).request(method, "/items/3", headers=headers)
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    hsr_link = (
        '<http://testserver/_hsr/items.item_id>; rel="https://w3id.org/'
        'semantic-http/ns#http-schema-resource-document"; type="application/json"'
    )
    assert response.headers["link"] == f"{ERROR_LINKS}, {hsr_link}"
    assert "Request" in seen
    hsr = http_schema_resource(app, "/items/{item_id}")
    entry = hsr[method.lower()]["responses"][str(status)]
    assert entry["content"] == {
        "application/problem+json": {
            "schema": {"$ref": "http://testserver/schemas/error"},
            "x-rdf-context": {
                "href": "http://example.com/error.jsonld",
                "type": "application/ld+json",
            },
        }
    }
    assert link_problems(method, status, response.headers, True, hsr) == []


def test_error_link_callables_get_the_endpoint_kwargs(make_client, store):
    app = FastAPI()
    received = []

    async def schema(item_id, **kwargs):
        received.append((item_id, sorted(kwargs)))
        return f"/schemas/error/{item_id}"

    def context(**kwargs):
        return "error.jsonld"

    @app.put("/items/{item_id}")
    @scrudful(
        etag_func=store.etag,
        error=Entity(schema_link_or_func=schema, context_link_or_func=context),
    )
    async def put_item(item_id: int, request: Request):
        return {}

    response = make_client(app).put("/items/3?x=1")
    assert response.status_code == 428
    assert response.headers["link"] == (
        '<http://testserver/schemas/error/3>; rel="describedby"; '
        'type="application/schema+json", <http://testserver/items/error.jsonld>; '
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )
    assert response.headers["content-type"] == "application/json"
    assert received == [(3, ["request"])]


def test_error_must_be_a_json_entity():
    for decorate in (lambda error: scrudful(error=error), _add_to_new_app):
        with pytest.raises(ValueError, match="error"):
            decorate(Entity(media_type="text/html"))
        with pytest.raises(ValueError, match="error"):
            decorate(Entity(media_type="application/ld+json"))
        with pytest.raises(TypeError, match="Entity"):
            decorate({"schema_link_or_func": SCHEMA})
    with pytest.raises(TypeError, match="FastAPI"):
        add_error_links(APIRouter(), ERROR)


def _add_to_new_app(error):
    add_error_links(FastAPI(), error)


#: Links a dependency sets: the success representation's schema and context
#: (relations in other cases, and the rdf-context one), and an author link.
DEPENDENCY_SCHEMA_LINK = (
    '<http://example.com/other>; rel="DescribedBy"; type="application/schema+json"'
)
DEPENDENCY_CONTEXT_LINKS = (
    '<http://example.com/other.jsonld>; rel="http://www.w3.org/ns/JSON-LD#context"; '
    'type="application/ld+json", <http://example.com/other.csvm>; '
    'rel="https://w3id.org/semantic-http/ns#rdf-context"; type="application/csvm+json"'
)
AUTHOR_LINK = '<http://example.com/me>; rel="author"'


def _dependency_links_app(store, error):
    """``/items/{item_id}`` (GET, PUT), whose dependency sets the success
    representation's schema and context links, an author link and a cookie."""
    app = FastAPI()

    def set_headers(response: Response):
        response.headers.append("Link", DEPENDENCY_SCHEMA_LINK)
        response.headers.append("Link", f"{DEPENDENCY_CONTEXT_LINKS}, {AUTHOR_LINK}")
        response.set_cookie("session", "abc")

    @app.api_route(
        "/items/{item_id}",
        methods=["GET", "PUT"],
        dependencies=[Depends(set_headers)],
    )
    @scrudful(etag_func=store.etag, error=error)
    async def item(item_id: int):
        return {}

    add_options(app, base_url=TEST_BASE_URL)
    return app


EARLY_ERRORS = [
    ("PUT", {"If-Match": '"other"'}, 412),
    ("PUT", {}, 428),
    ("GET", {"If-Match": "v1"}, 400),
]


@pytest.mark.parametrize("method, headers, status", EARLY_ERRORS)
def test_dependency_content_links_are_dropped_on_early_errors(
    make_client, store, method, headers, status
):
    """A dependency's ``describedby`` and RDF context links
    describe the success representation, not the early error's body: they
    are dropped from the 400/412/428, which carry the error entity's (and
    agree with the HSR's entry). Its other Links and its cookies are kept."""
    client = make_client(_dependency_links_app(store, ERROR))
    hsr = options_hsr(client, "/items/1")
    response = client.request(method, "/items/1", headers=headers)
    assert response.status_code == status
    assert response.headers["link"] == f"{AUTHOR_LINK}, {ERROR_LINKS}, {ITEMS_HSR_LINK}"
    assert link_problems(method, status, response.headers, True, hsr) == []
    assert response.headers["set-cookie"] == "session=abc; Path=/; SameSite=lax"


@pytest.mark.parametrize("method, headers, status", EARLY_ERRORS)
def test_dependency_content_links_are_dropped_without_an_error_entity(
    make_client, store, method, headers, status
):
    """Without ``error``, nothing describes the early error's body: no
    ``describedby`` or context link at all, as in 0.9; the author link and
    the HSR link stay."""
    client = make_client(_dependency_links_app(store, None))
    response = client.request(method, "/items/1", headers=headers)
    assert response.status_code == status
    assert response.headers["link"] == f"{AUTHOR_LINK}, {ITEMS_HSR_LINK}"


def test_a_304_keeps_the_dependency_links(make_client, store):
    """A 304 stands for the success representation: it keeps every
    dependency header, its schema and context links included, as a 200
    would carry them."""
    client = make_client(_dependency_links_app(store, ERROR))
    response = client.get("/items/1", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.headers["link"] == (
        f"{DEPENDENCY_SCHEMA_LINK}, {DEPENDENCY_CONTEXT_LINKS}, {AUTHOR_LINK}, "
        f"{ITEMS_HSR_LINK}"
    )


def test_relative_link_resolved(make_client):
    app = FastAPI()

    @app.get("/users/{user_id}")
    @scrudful(
        schema_link_or_func="/schemas/user",
        context_link_or_func=lambda **kwargs: "context.jsonld",
    )
    async def get_user(user_id: int):
        return {}

    @app.get("/network-path")
    @scrudful(
        schema_link_or_func="//cdn.example/schema.json", context_link_or_func=CONTEXT
    )
    async def network_path():
        return JSONResponse({})

    client = make_client(app)
    response = client.get("/users/1?x=y")
    assert response.headers["link"] == (
        '<http://testserver/schemas/user>; rel="describedby"; '
        'type="application/schema+json", <http://testserver/users/context.jsonld>; '
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )
    assert link_problems("GET", 200, response.headers, True) == []
    assert parse_links(client.get("/network-path").headers["link"])[0][0] == (
        "http://cdn.example/schema.json"
    )


def test_view_link_not_duplicated(make_client):
    app = FastAPI()
    own_schema = '<http://example.com/own>; rel="DescribedBy"; type="text/plain"'
    own_context = (
        '<http://example.com/own.jsonld>; rel="http://www.w3.org/ns/json-ld#context"'
    )
    other = '<https://x/a,b>; rel="next"; title="a, b"'
    links = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)

    @app.get("/own-schema")
    @scrudful(**links)
    async def schema(response: Response):
        response.headers["Link"] = own_schema
        return {}

    @app.get("/own-context")
    @scrudful(**links)
    async def context():
        return JSONResponse({}, headers={"Link": own_context})

    @app.get("/other")
    @scrudful(**links)
    async def other_relation(response: Response):
        response.headers["Link"] = other
        return {}

    client = make_client(app)
    assert client.get("/own-schema").headers["link"] == f"{own_schema}, {CONTEXT_LINK}"
    assert client.get("/own-context").headers["link"] == (
        f"{own_context}, {SCHEMA_LINK}"
    )
    assert client.get("/other").headers["link"] == (
        f"{other}, {SCHEMA_LINK}, {CONTEXT_LINK}"
    )


def test_endpoint_config_is_attached():
    def etag(**kwargs):
        return "x"

    @scrudful(etag_func=etag, **ITEM_LINKS, error=ERROR)
    async def get_item(item_id: int, req: Request):
        return {}

    @scrudful()
    async def plain():
        return {}

    assert get_item._scrudful == _EndpointConfig(
        etag_func=etag,
        last_modified_func=None,
        schema_link_or_func=SCHEMA,
        schema_rel_or_func=None,
        schema_type_or_func=None,
        context_link_or_func=CONTEXT,
        context_rel_or_func=None,
        context_type_or_func=None,
        error=ERROR,
        request_name="req",
        param_names=frozenset({"item_id", "req"}),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        get_item._scrudful.error = None
    assert plain._scrudful.request_name is None
    assert plain._scrudful.error is None


def _links_app():
    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    async def get_item(item_id: int):
        raise HTTPException(status_code=404, detail="Item not found")

    return app


@pytest.mark.parametrize(
    "method, path, status",
    [
        ("GET", "/items/3", 404),
        ("GET", "/items/x", 422),
        ("POST", "/items/3", 405),
        ("GET", "/nowhere", 404),
    ],
)
def test_add_error_links_http_and_validation(make_client, method, path, status):
    app = _links_app()
    add_error_links(app, ERROR)
    response = make_client(app).request(method, path)
    plain = make_client(_links_app()).request(method, path)
    assert response.status_code == plain.status_code == status
    if status == 422:
        # ERROR is problem details: so is the validation 422.
        assert response.json() == problem_from_errors(
            422, plain.json()["detail"], "Request validation failed"
        )
    else:
        assert response.json() == plain.json()
    assert response.headers.get("allow") == plain.headers.get("allow")
    assert response.headers["link"] == ERROR_LINKS
    assert response.headers["content-type"] == "application/problem+json"
    assert "Link" in _exposed(response)
    assert link_problems(method, status, response.headers, True) == []


def test_add_error_links_wraps_the_defaults():
    app = FastAPI()
    add_error_links(app, ERROR)
    handlers = app.exception_handlers
    assert handlers[StarletteHTTPException].__wrapped__ is http_exception_handler
    assert (
        handlers[RequestValidationError].__wrapped__
        is request_validation_exception_handler
    )


@pytest.mark.parametrize("key", [StarletteHTTPException, HTTPException, 404])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_add_error_links_keeps_custom_handler(make_client, key, asynchronous):
    app = _links_app()

    def handler(request, exc):
        return JSONResponse(
            {"error": exc.detail},
            status_code=exc.status_code,
            headers={"X-Custom": "1"},
        )

    async def async_handler(request, exc):
        return handler(request, exc)

    app.add_exception_handler(key, async_handler if asynchronous else handler)
    add_error_links(app, ERROR)
    response = make_client(app).get("/items/3")
    assert response.status_code == 404
    assert response.json() == {"error": "Item not found"}
    assert response.headers["x-custom"] == "1"
    assert response.headers["link"] == ERROR_LINKS
    assert response.headers["content-type"] == "application/problem+json"


def test_add_error_links_keeps_custom_validation_handler(make_client):
    app = _links_app()

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"invalid": len(exc.errors())}, status_code=400)

    add_error_links(app, ERROR)
    response = make_client(app).get("/items/x")
    assert response.status_code == 400
    assert response.json() == {"invalid": 1}
    assert response.headers["link"] == ERROR_LINKS


def test_add_error_links_twice_does_not_double(make_client):
    app = _links_app()
    add_error_links(app, ERROR)
    add_error_links(app, ERROR)
    client = make_client(app)
    assert client.get("/items/3").headers["link"] == ERROR_LINKS
    assert client.get("/items/x").headers["link"] == ERROR_LINKS
    # The latest call's entity replaces the earlier one.
    app = _links_app()
    add_error_links(app, ERROR)
    add_error_links(
        app,
        Entity(
            schema_link_or_func="http://example.com/other",
            context_link_or_func="http://example.com/other.jsonld",
        ),
    )
    response = make_client(app).get("/items/3")
    assert response.headers["link"] == (
        '<http://example.com/other>; rel="describedby"; '
        'type="application/schema+json", <http://example.com/other.jsonld>; '
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )
    assert response.headers["content-type"] == "application/json"
    handler = app.exception_handlers[StarletteHTTPException]
    assert handler.__wrapped__ is http_exception_handler


def test_add_error_link_callables_get_only_the_request(make_client):
    app = _links_app()
    received = []

    @app.get("/schemas/error", name="error_schema")
    async def error_schema():
        return {}

    def schema(request):
        received.append(type(request))
        return request.url_for("error_schema")

    async def context(*, request):
        return "/contexts/error"

    add_error_links(
        app, Entity(schema_link_or_func=schema, context_link_or_func=context)
    )
    response = make_client(app).get("/items/3")
    assert response.headers["link"] == (
        '<http://testserver/schemas/error>; rel="describedby"; '
        'type="application/schema+json", <http://testserver/contexts/error>; '
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    )
    assert received == [Request]


def test_add_error_links_leaves_bodiless_and_non_json_responses(make_client):
    app = FastAPI()

    @app.get("/not-modified")
    async def not_modified():
        raise HTTPException(status_code=304)

    @app.get("/teapot")
    async def teapot():
        raise HTTPException(status_code=418)

    app.add_exception_handler(
        418, lambda request, exc: PlainTextResponse("short and stout", 418)
    )
    add_error_links(app, ERROR)
    client = make_client(app)
    response = client.get("/not-modified")
    assert response.status_code == 304
    assert "link" not in response.headers
    assert "content-type" not in response.headers
    response = client.get("/teapot")
    assert response.status_code == 418
    assert response.text == "short and stout"
    assert "link" not in response.headers
    assert response.headers["content-type"].startswith("text/plain")


# --- the error entity of the matched endpoint, and late registration -----------

APP_ERROR = Entity(
    schema_link_or_func="http://example.com/app-error.json",
    context_link_or_func="http://example.com/app-error.jsonld",
)
APP_ERROR_LINKS = (
    '<http://example.com/app-error.json>; rel="describedby"; '
    'type="application/schema+json", <http://example.com/app-error.jsonld>; '
    'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)


def _raising_app(store):
    app = FastAPI()

    @app.api_route("/items/{item_id}", methods=["GET", "PUT"])
    @scrudful(
        etag_func=store.etag,
        schema_link_or_func=SCHEMA,
        context_link_or_func=CONTEXT,
        error=ERROR,
    )
    async def item(item_id: int, request: Request):
        raise HTTPException(status_code=400, detail="Bad item")

    @app.get("/plain/{item_id}")
    @scrudful(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    async def plain(item_id: int):
        raise HTTPException(status_code=400, detail="Bad item")

    add_error_links(app, APP_ERROR)
    add_options(app, base_url=TEST_BASE_URL)
    return app


@pytest.mark.parametrize(
    "method, headers", [("GET", {}), ("PUT", {"If-Match": '"v1"'})]
)
def test_raised_error_gets_the_endpoint_error(make_client, store, method, headers):
    """An HTTPException the endpoint raises carries the endpoint's ``error``, as
    the OPTIONS body describes its 400, not the app-wide one."""
    client = make_client(_raising_app(store))
    hsr = options_hsr(client, "/items/3")
    response = client.request(method, "/items/3", headers=headers)
    assert response.status_code == 400
    assert response.json() == {"detail": "Bad item"}
    assert response.headers["link"] == f"{ERROR_LINKS}, {ITEMS_HSR_LINK}"
    assert response.headers["content-type"] == "application/problem+json"
    assert link_problems(method, 400, response.headers, True, hsr) == []


def test_raised_error_without_endpoint_error_gets_the_app_error(make_client, store):
    client = make_client(_raising_app(store))
    response = client.get("/plain/3")
    assert response.status_code == 400
    assert response.headers["link"] == (
        f"{APP_ERROR_LINKS}, {hsr_link('http://testserver/_hsr/plain.item_id')}"
    )
    assert response.headers["content-type"] == "application/json"
    # Not conditional: the OPTIONS body lists no 400 to agree with.
    assert "400" not in options_hsr(client, "/plain/3")["get"]["responses"]
    assert link_problems("GET", 400, response.headers, True) == []


def test_endpoint_error_applies_to_the_methods_it_serves(make_client, store):
    """A 422 of the matched endpoint gets its ``error``; a 405, which no
    endpoint serves, the app-wide one."""
    client = make_client(_raising_app(store))
    response = client.get("/items/x")
    assert response.status_code == 422
    assert response.headers["link"] == f"{ERROR_LINKS}, {ITEMS_HSR_LINK}"
    assert response.headers["content-type"] == "application/problem+json"
    response = client.post("/items/3")
    assert response.status_code == 405
    assert response.headers["link"] == f"{APP_ERROR_LINKS}, {ITEMS_HSR_LINK}"
    assert response.headers["content-type"] == "application/json"


def test_endpoint_error_callables_in_handlers_get_path_params_and_request(
    make_client,
):
    app = FastAPI()
    received = []

    def schema(**kwargs):
        received.append(kwargs)
        return f"/schemas/error/{kwargs['item_id']}"

    error = Entity(schema_link_or_func=schema, context_link_or_func=CONTEXT)

    @app.get("/items/{item_id}")
    @scrudful(error=error)
    async def with_request(item_id: int, req: Request):
        raise HTTPException(status_code=404)

    @app.get("/others/{item_id}")
    @scrudful(error=error)
    async def without_request(item_id: int):
        raise HTTPException(status_code=404)

    add_error_links(app, APP_ERROR)
    client = make_client(app)
    response = client.get("/items/3")
    assert parse_links(response.headers["link"])[0][0] == (
        "http://testserver/schemas/error/3"
    )
    client.get("/others/4")
    assert [sorted(kwargs) for kwargs in received] == [["item_id", "req"], ["item_id"]]
    assert received[0]["item_id"] == "3"  # the raw path parameter
    assert isinstance(received[0]["req"], Request)


def test_add_error_links_after_the_app_started_raises(make_client):
    app = _links_app()
    make_client(app).get("/items/3")
    with pytest.raises(RuntimeError, match="before the app serves requests"):
        add_error_links(app, ERROR)


# --- add_options -----------------------------------------------------------------

ITEM_CONTENT = {
    "application/json": {
        "schema": {"$ref": SCHEMA},
        "x-rdf-context": {"href": CONTEXT, "type": "application/ld+json"},
    }
}
ERROR_CONTENT = {
    "application/problem+json": {
        "schema": {"$ref": "http://testserver/schemas/error"},
        "x-rdf-context": {
            "href": "http://example.com/error.jsonld",
            "type": "application/ld+json",
        },
    }
}
#: The ``default`` entry of a method whose raised errors carry no Links (no
#: add_error_links).
DEFAULT = {"description": "Error."}
OK = {"responses": {"200": {"description": "OK"}, "default": DEFAULT}}
ITEM_LINKS = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)


def hsr_target(name, prefix=""):
    """The HSR link target of the resource whose HSR route name is ``name``."""
    return f"http://testserver{prefix}/_hsr/{name}"


def options_204(client, url, allow, target=None, **kwargs):
    """The OPTIONS response of ``url``: a 204 without content, with ``Allow:
    <allow>`` (exactly, in its order), one HSR link (to ``target``, unless
    ``None``) and the exposed ``Link`` and ``Allow``."""
    response = client.options(url, **kwargs)
    headers = dict(response.headers)
    expected = {method.strip() for method in allow.split(",")}
    assert (
        options_204_problems(response.status_code, headers, response.content, expected)
        == []
    ), (url, response.status_code, response.content)
    assert response.headers["allow"] == allow, url
    assert hsr_link_problems(headers, target) == [], url
    assert {"Link", "Allow"} <= set(_exposed(response))
    return response


def options_hsr(client, url, **kwargs):
    """The HTTP Schema Resource the OPTIONS of ``url`` links to: the 204's HSR
    link, followed by a GET of the HSR route."""
    response = client.options(url, **kwargs)
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
    ), target
    return document.json()


class ItemIn(BaseModel):
    name: str


def _options_app(store):
    app = FastAPI()
    links = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    validators = dict(etag_func=store.etag, last_modified_func=store.last_modified)

    @app.api_route("/items/{item_id}", methods=["GET", "HEAD"])
    @scrudful(**validators, **links, error=ERROR)
    async def get_item(item_id: int):
        return {"name": "one"}

    @app.api_route("/items/{item_id}", methods=["PUT", "PATCH"])
    @scrudful(**validators, **links, error=ERROR)
    def put_item(item_id: int, item: ItemIn):
        store.update()
        return {"name": item.name}

    @app.delete("/items/{item_id}", status_code=204)
    @scrudful(**validators, error=ERROR)
    async def delete_item(item_id: int):
        return None

    @app.post("/items", status_code=201)
    @scrudful(**links, error=ERROR)
    async def create_item(item: ItemIn):
        return {"name": item.name}

    @app.post("/items/{item_id}/actions")
    @scrudful(etag_func=store.etag, **links, error=ERROR)
    async def act(item_id: int):
        return {"name": "one"}

    add_options(app, base_url=TEST_BASE_URL)
    return app


#: The literal entries of :func:`_options_app`'s ``/items/{item_id}``: the
#: expectations don't come from the functions that build them.
NOT_MODIFIED = {
    "description": (
        "Not Modified: the representation matches the request's If-None-Match "
        "or If-Modified-Since validators."
    )
}
MALFORMED = "Malformed If-Match or If-None-Match header."
FAILED = "Precondition Failed."
REQUIRED = (
    "Precondition Required: this request must be conditional (send If-Match or "
    "If-Unmodified-Since)."
)
PRECONDITION_PARAMETERS = [
    {
        "in": "header",
        "name": name,
        "schema": {"type": "string"},
        "required": False,
        "description": (
            f"The {what} of the current representation. Either If-Match or "
            "If-Unmodified-Since is required."
        ),
    }
    for name, what in (
        ("If-Match", "entity-tag"),
        ("If-Unmodified-Since", "Last-Modified date"),
    )
]


def _write_entry(request: str | None, status: str) -> dict:
    responses = {status: {"description": "OK" if status == "200" else "No Content"}}
    if status == "200":
        responses[status]["content"] = ITEM_CONTENT
    responses.update(
        {
            "400": {"description": MALFORMED, "content": ERROR_CONTENT},
            "412": {"description": FAILED, "content": ERROR_CONTENT},
            "428": {"description": REQUIRED, "content": ERROR_CONTENT},
            "default": {"description": "Error."},
        }
    )
    entry: dict = {}
    if request is not None:
        entry["requestBody"] = {
            "description": request,
            "required": True,
            "content": ITEM_CONTENT,
        }
    entry["responses"] = responses
    entry["parameters"] = PRECONDITION_PARAMETERS
    return entry


ITEM_HSR = {
    "get": {
        "responses": {
            "200": {"description": "OK", "content": ITEM_CONTENT},
            "304": NOT_MODIFIED,
            "400": {"description": MALFORMED, "content": ERROR_CONTENT},
            "412": {"description": FAILED, "content": ERROR_CONTENT},
            "default": {"description": "Error."},
        }
    },
    "put": _write_entry("The new content of the resource.", "200"),
    "delete": _write_entry(None, "204"),
    "options": {"responses": {"204": {"description": "No Content"}}},
    "head": {
        "responses": {
            "200": {"description": "OK"},
            "304": NOT_MODIFIED,
            "400": {"description": MALFORMED},
            "412": {"description": FAILED},
            "default": {"description": "Error."},
        }
    },
    "patch": _write_entry("The changes to apply to the resource.", "200"),
}


def test_add_options_body_and_headers(make_client, store):
    client = make_client(_options_app(store))
    response = options_204(
        client,
        "/items/3",
        "GET, PUT, DELETE, OPTIONS, HEAD, PATCH",
        hsr_target("items.item_id"),
    )
    assert response.headers["link"] == hsr_link(hsr_target("items.item_id"))
    assert "content-type" not in response.headers
    body = options_hsr(client, "/items/3")
    assert body == http_schema_resource(client.app, "/items/{item_id}")
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "delete", "options", "head", "patch"]
    assert body == ITEM_HSR


def test_add_options_success_status_and_conditional_post(make_client, store):
    client = make_client(_options_app(store))
    body = options_hsr(client, "/items")
    validate_http_schema_resource(body)
    assert list(body) == ["post", "options"]
    assert body["post"] == {
        "requestBody": {
            "description": "The content for the resource to be created.",
            "required": True,
            "content": ITEM_CONTENT,
        },
        "responses": {
            "201": {"description": "Created", "content": ITEM_CONTENT},
            "default": {"description": "Error."},
        },
    }
    # A conditional POST: scrudful's 400 and 412, never a 428.
    body = options_hsr(client, "/items/3/actions")
    validate_http_schema_resource(body)
    assert set(body["post"]["responses"]) == {"200", "400", "412", "default"}
    assert body["post"]["responses"]["400"]["content"] == ERROR_CONTENT
    # The endpoint takes no body: no requestBody.
    assert "requestBody" not in body["post"]


def test_trace_gets_no_links(make_client):
    """A TRACE's content is the echoed request: OPTIONS gives it none, and
    it gets only the HSR link."""
    app = FastAPI()

    @app.api_route("/items/{item_id}", methods=["GET", "TRACE"])
    @scrudful(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    async def item(item_id: int):
        return {"id": item_id}

    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    body = options_hsr(client, "/items/3")
    assert body["trace"] == OK
    response = client.request("TRACE", "/items/3")
    assert response.status_code == 200
    assert response.headers["link"] == ITEMS_HSR_LINK
    assert client.get("/items/3").headers["link"] == (
        f"{SCHEMA_LINK}, {CONTEXT_LINK}, {ITEMS_HSR_LINK}"
    )


def test_trace_ignores_preconditions(make_client, store):
    """A TRACE's content is no representation: no If-*, no validators."""
    app = FastAPI()

    @app.api_route("/items/{item_id}", methods=["GET", "TRACE"])
    @scrudful(etag_func=store.etag, last_modified_func=store.last_modified)
    async def item(item_id: int):
        return {"id": item_id}

    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    for headers in ({"If-Match": '"nope"'}, {"If-None-Match": '"v1"'}, {}):
        response = client.request("TRACE", "/items/3", headers=headers)
        assert response.status_code == 200, headers
        assert response.json() == {"id": 3}
        assert "etag" not in response.headers
        assert "last-modified" not in response.headers
    assert store.calls == []
    assert client.get("/items/3", headers={"If-Match": '"nope"'}).status_code == 412
    body = options_hsr(client, "/items/3")
    assert body["trace"] == OK
    assert "412" in body["get"]["responses"]


def test_a_context_type_callable_returning_none_takes_the_default(make_client):
    """JSON content: the Links and OPTIONS both fall back to JSON-LD."""
    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(
        schema_link_or_func=SCHEMA,
        context_link_or_func=CONTEXT,
        context_type_or_func=lambda **kwargs: None,
    )
    async def get_item(item_id: int):
        return {}

    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    body = options_hsr(client, "/items/3")
    content = body["get"]["responses"]["200"]["content"]["application/json"]
    assert content["x-rdf-context"] == {"href": CONTEXT, "type": "application/ld+json"}
    response = client.get("/items/3")
    assert response.headers["link"] == (
        f"{SCHEMA_LINK}, {CONTEXT_LINK}, {ITEMS_HSR_LINK}"
    )
    assert link_problems("GET", 200, response.headers, True, body) == []


def test_add_options_success_content_only_on_a_2xx(make_client):
    """A redirect gets no content Links on the wire (only the HSR link), and
    no content in OPTIONS."""
    app = FastAPI()
    links = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)

    @app.get("/items/{item_id}", status_code=307)
    @scrudful(**links)
    async def get_item(item_id: int, response: Response):
        response.headers["Location"] = "/elsewhere"

    @app.put("/items/{item_id}", status_code=303)
    @scrudful(**links)
    async def put_item(item_id: int, response: Response):
        response.headers["Location"] = "/elsewhere"

    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    body = options_hsr(client, "/items/3")
    validate_http_schema_resource(body)
    assert body["get"]["responses"] == {
        "307": {"description": "Temporary Redirect"},
        "default": DEFAULT,
    }
    assert body["put"]["responses"] == {
        "303": {"description": "See Other"},
        "default": DEFAULT,
    }
    for method in ("GET", "PUT"):
        response = client.request(method, "/items/3", follow_redirects=False)
        assert response.status_code in (303, 307)
        assert response.headers["link"] == ITEMS_HSR_LINK


@pytest.mark.parametrize(
    "method, path, headers, json, status",
    [
        ("GET", "/items/3", {}, None, 200),
        ("GET", "/items/3", {"If-Match": '"v0"'}, None, 412),
        ("GET", "/items/3", {"If-None-Match": "v1"}, None, 400),
        ("HEAD", "/items/3", {}, None, 200),
        ("PUT", "/items/3", {"If-Match": '"v1"'}, {"name": "two"}, 200),
        ("PUT", "/items/3", {}, {"name": "two"}, 428),
        ("PATCH", "/items/3", {"If-Match": '"v0"'}, {"name": "two"}, 412),
        ("PATCH", "/items/3", {"If-Match": "v1"}, {"name": "two"}, 400),
        ("DELETE", "/items/3", {}, None, 428),
        ("POST", "/items", {}, {"name": "two"}, 201),
        ("POST", "/items/3/actions", {"If-Match": '"v0"'}, None, 412),
        ("POST", "/items/3/actions", {}, None, 200),
    ],
)
def test_add_options_agrees_with_the_responses(
    make_client, store, method, path, headers, json, status
):
    client = make_client(_options_app(store))
    hsr = options_hsr(client, path)
    response = client.request(method, path, headers=headers, json=json)
    assert response.status_code == status
    content = method != "HEAD"
    assert link_problems(method, status, response.headers, content, hsr) == []


def test_add_options_skips_existing_options(make_client):
    app = FastAPI()
    links = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)

    @app.get("/items/{item_id}")
    @scrudful(**links)
    async def get_item(item_id: int):
        return {}

    @app.options("/items/{item_id}")
    async def own_options(item_id: int):
        return {"own": True}

    @app.get("/plain")
    async def plain():
        return {}

    @app.get("/others")
    @scrudful(**links)
    async def others():
        return {}

    add_options(app, base_url=TEST_BASE_URL)
    routes = list(app.routes)
    add_options(app, base_url=TEST_BASE_URL)
    assert app.routes == routes  # a second call adds nothing
    client = make_client(app)
    assert client.options("/items/3").json() == {"own": True}
    assert client.options("/plain").status_code == 405
    assert client.options("/nowhere").status_code == 404
    options_204(client, "/others", "GET, OPTIONS", hsr_target("others"))
    assert "options" not in app.openapi()["paths"]["/others"]
    assert app.url_path_for("get_item", item_id=3) == "/items/3"


def test_add_options_mixed_path(make_client, store):
    """Review Focus 5: a plain method on a scrudful path is described without
    content or a request body, and ``Allow`` still lists the methods the
    body describes (none of them has a check)."""
    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(etag_func=store.etag, **ITEM_LINKS, error=ERROR)
    async def get_item(item_id: int):
        return {}

    @app.put("/items/{item_id}")
    async def plain_put(item_id: int, item: ItemIn):
        return {}

    async def starlette_patch(request):
        return JSONResponse({})

    app.add_route("/items/{item_id}", starlette_patch, methods=["PATCH"])
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    body = options_hsr(client, "/items/3")
    validate_http_schema_resource(body)
    assert body["put"] == OK
    assert body["patch"] == OK
    assert list(body) == ["get", "put", "options", "patch"]
    options_204(
        client, "/items/3", "GET, PUT, OPTIONS, PATCH", hsr_target("items.item_id")
    )


def test_add_options_link_callables_are_called_in_describe_mode(make_client, store):
    """The HTTP Schema Resource calls the link callables as ``func()``, once
    per path: with neither the path parameters nor the request, which they
    get inside the endpoint. Relative URLs resolve against ``base_url``."""
    app = FastAPI()
    received = []

    def schema(**kwargs):
        received.append(("schema", kwargs))
        return "/schemas/item"

    async def context(**kwargs):
        received.append(("context", kwargs))
        return "context.jsonld"

    async def error_schema(**kwargs):
        received.append(("error", kwargs))
        return "/schemas/error"

    @app.get("/items/{item_id}")
    @scrudful(
        etag_func=store.etag,
        schema_link_or_func=schema,
        context_link_or_func=context,
        error=Entity(schema_link_or_func=error_schema, context_link_or_func=CONTEXT),
    )
    async def get_item(item_id: int, req: Request):
        return {}

    @app.get("/others/{other_id}")
    @scrudful(schema_link_or_func=schema, context_link_or_func=CONTEXT)
    async def get_other(other_id: int):
        return {}

    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    body = options_hsr(client, "/items/3?x=1")
    validate_http_schema_resource(body)
    assert body["get"]["responses"]["200"]["content"] == {
        "application/json": {
            "schema": {"$ref": "http://testserver/schemas/item"},
            "x-rdf-context": {
                "href": "http://testserver/context.jsonld",
                "type": "application/ld+json",
            },
        }
    }
    assert body["get"]["responses"]["400"]["content"] == {
        "application/json": {
            "schema": {"$ref": "http://testserver/schemas/error"},
            "x-rdf-context": {"href": CONTEXT, "type": "application/ld+json"},
        }
    }
    assert received == [("schema", {}), ("context", {}), ("error", {})]
    assert options_hsr(client, "/items/4") == body  # built once
    assert len(received) == 3
    # The Links of the GET agree with it: inside the endpoint the callables
    # get its keyword arguments, and the URLs resolve against base_url too.
    received.clear()
    response = client.get("/items/3")
    assert link_problems("GET", 200, response.headers, True, body) == []
    assert sorted(name for name, _ in received) == ["context", "schema"]
    for _, kwargs in received:
        assert sorted(kwargs) == ["item_id", "req"]
        assert kwargs["item_id"] == 3

    received.clear()
    options_hsr(client, "/others/7")
    assert received == [("schema", {})]


@pytest.mark.parametrize("security", [False, True])
def test_add_options_needs_no_credentials(make_client, security):
    """OPTIONS isn't gated by the app's, a router's or ``include_router``'s
    dependencies, and the document it links to is served without
    credentials. Its ``Allow`` runs only security dependencies: a
    plain ``Depends(deny)`` isn't run, so GET is listed; a ``Security(deny)``
    refuses everything but OPTIONS."""

    def deny():
        raise HTTPException(status_code=401)

    declare = Security if security else Depends
    links = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    app = FastAPI(dependencies=[declare(deny)])
    router = APIRouter(dependencies=[declare(deny)])

    @router.get("/items/{item_id}")
    @scrudful(**links)
    async def get_item(item_id: int):
        return {}

    @app.get("/things")
    @scrudful(**links)
    async def things():
        return {}

    add_options(router, base_url=TEST_BASE_URL)
    app.include_router(router, prefix="/v1", dependencies=[declare(deny)])
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    allow = "OPTIONS" if security else "GET, OPTIONS"
    for path, name in (("/v1/items/3", "v1.items.item_id"), ("/things", "things")):
        assert client.get(path).status_code == 401
        options_204(client, path, allow, hsr_target(name))
        assert list(options_hsr(client, path)) == ["get", "options"]


def test_add_options_covers_included_routers(make_client):
    inner = APIRouter()

    @inner.get("/items/{item_id}")
    @scrudful(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    async def get_item(item_id: int):
        return {}

    outer = APIRouter()
    outer.include_router(inner, prefix="/inner")
    app = FastAPI()
    app.include_router(outer, prefix="/v1")
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    options_204(
        client,
        "/v1/inner/items/3",
        "GET, OPTIONS",
        hsr_target("v1.inner.items.item_id"),
    )
    body = options_hsr(client, "/v1/inner/items/3")
    assert body["get"]["responses"]["200"]["content"] == ITEM_CONTENT
    get = client.get("/v1/inner/items/3")
    assert link_problems("GET", 200, get.headers, True, body) == []


def _prefixed_router(received):
    """``GET /items/{item_id}``, whose schema callable records its kwargs."""
    router = APIRouter()

    def schema(**kwargs):
        received.append(kwargs)
        return "/schemas/item"

    @router.get("/items/{item_id}")
    @scrudful(schema_link_or_func=schema, context_link_or_func=CONTEXT)
    async def get_item(item_id: int):
        return {}

    return router


@pytest.mark.parametrize("on", ["router", "app"])
def test_add_options_under_a_prefix_with_path_parameters(make_client, on):
    """The callables get neither the endpoint's nor the prefix's parameters
    in the HTTP Schema Resource, keyed by the app's full path (or the
    router's own)."""
    received = []
    router = _prefixed_router(received)
    app = FastAPI()
    if on == "router":
        add_options(router, base_url=TEST_BASE_URL)
    app.include_router(router, prefix="/users/{user_id}")
    if on == "app":
        add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    get = client.get("/users/1/items/2")
    assert get.status_code == 200
    assert received == [{"item_id": 2}]
    target = (
        hsr_target("items.item_id", "/users/1")
        if on == "router"
        else hsr_target("users.user_id.items.item_id")
    )
    options_204(client, "/users/1/items/2", "GET, OPTIONS", target)
    body = options_hsr(client, "/users/1/items/2")
    assert body["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "http://testserver/schemas/item"
    }
    assert received[1:] == [{}]
    if on == "router":
        assert body == http_schema_resource(router, "/items/{item_id}")
    else:
        assert body == http_schema_resource(app, "/users/{user_id}/items/{item_id}")
    assert link_problems("GET", 200, get.headers, True, body) == []


def test_add_options_rejects_a_callable_needing_path_parameters():
    """A callable needing path parameters (the endpoint's or a prefix's)
    can't be called as ``func()``: no description of the template."""
    router = APIRouter()

    @router.get("/items/{item_id}")
    @scrudful(schema_link_or_func=lambda item_id, user_id: "/s.json")
    async def get_item(item_id: int):
        return {}

    app = FastAPI()
    app.include_router(router, prefix="/users/{user_id}")
    with pytest.raises(ValueError, match=r"func\(\)") as raised:
        add_options(app, base_url=TEST_BASE_URL)
    assert "schema_link_or_func" in str(raised.value)
    assert "missing a required argument: 'item_id'" in str(raised.value)


def test_outside_callables_get_an_aliased_parameter_by_its_name(make_client):
    """In add_error_links' handlers, as inside the endpoint: ``item``, not the
    URL's ``item_id``."""
    received = []

    def schema(item, **kwargs):
        received.append((item, kwargs))
        return f"/schemas/item?id={item}"

    def owner(user_id: int):
        return user_id

    app = FastAPI()
    error = Entity(schema_link_or_func=schema, context_link_or_func=CONTEXT)

    @app.get("/users/{user_id}/items/{item_id}")
    @scrudful(schema_link_or_func=schema, context_link_or_func=CONTEXT, error=error)
    async def get_item(item: int = Path(alias="item_id"), who=Depends(owner)):
        if item == 3:
            raise HTTPException(status_code=404)
        return {}

    add_error_links(app, APP_ERROR)
    client = make_client(app)
    client.get("/users/1/items/2")
    response = client.get("/users/1/items/3")
    assert response.status_code == 404
    # GET gets the endpoint's kwargs (``who``, not the dependency's
    # ``user_id``); the handler the path parameters among them.
    assert received == [(2, {"who": 1}), ("3", {})]
    assert parse_links(response.headers["link"])[0][0] == (
        "http://testserver/schemas/item?id=3"
    )

    # Checked by the same names: ``item_id`` is no keyword the callable gets.
    app = FastAPI()
    error = Entity(
        schema_link_or_func=lambda item_id: "/e.json", context_link_or_func=CONTEXT
    )

    @app.get("/items/{item_id}")
    @scrudful(error=error)
    async def aliased(item: int = Path(alias="item_id")):
        return {}

    with pytest.raises(TypeError, match=r"\['item'\]"):
        add_error_links(app, APP_ERROR)


def test_add_options_after_the_app_started(make_client):
    """Routes can be added once the app serves requests, so a late call works."""
    app = _links_app()
    client = make_client(app)
    assert client.options("/items/3").status_code == 405
    add_options(app, base_url=TEST_BASE_URL)
    options_204(client, "/items/3", "GET, OPTIONS", hsr_target("items.item_id"))
    assert list(options_hsr(client, "/items/3")) == ["get", "options"]


def test_add_options_needs_an_app_or_router():
    with pytest.raises(TypeError, match="FastAPI application or an APIRouter"):
        add_options(object())


def test_add_options_skips_the_error_of_an_unconditional_endpoint(make_client):
    """Without validators scrudful answers no 400/412/428: the ``error`` content
    is not listed, and its callables are not called."""
    app = FastAPI()
    calls = []

    def error_schema(**kwargs):
        calls.append(kwargs)
        return "/schemas/error"

    @app.put("/items/{item_id}")
    @scrudful(
        **ITEM_LINKS,
        error=Entity(schema_link_or_func=error_schema, context_link_or_func=CONTEXT),
    )
    async def put_item(item_id: int, item: ItemIn):
        return {}

    add_options(app, base_url=TEST_BASE_URL)
    body = options_hsr(make_client(app), "/items/3")
    validate_http_schema_resource(body)
    assert set(body["put"]["responses"]) == {"200", "default"}
    assert body["put"]["responses"]["default"] == DEFAULT
    assert body["put"]["requestBody"]["content"] == ITEM_CONTENT
    assert calls == []


# --- callables used outside the endpoint are checked up front ------------------


def _probe_app():
    """A Task-7-valid endpoint whose callables need its resolved arguments."""
    app = FastAPI()

    def user():
        return "alice"

    error = Entity(
        schema_link_or_func=lambda item_id, item, who: f"/errors/{who}",
        context_link_or_func=CONTEXT,
    )

    @app.put("/items/{item_id}")
    @scrudful(
        etag_func=lambda item_id, item, who: '"v1"',
        schema_link_or_func=lambda item_id, item, who: "/s.json",
        context_link_or_func=CONTEXT,
        error=error,
    )
    async def put_item(item_id: int, item: ItemIn, who: str = Depends(user)):
        return {}

    return app


def test_add_options_rejects_callables_needing_endpoint_arguments():
    with pytest.raises(ValueError, match="put_item") as raised:
        add_options(_probe_app(), base_url=TEST_BASE_URL)
    assert "schema_link_or_func" in str(raised.value)
    assert "func()" in str(raised.value)
    assert "item_id" in str(raised.value)


def test_add_error_links_rejects_endpoint_error_callables_needing_arguments():
    with pytest.raises(TypeError, match="put_item") as raised:
        add_error_links(_probe_app(), APP_ERROR)
    assert "error.schema_link_or_func" in str(raised.value)


def test_add_error_links_rejects_app_error_callables_needing_more_than_request():
    error = Entity(
        schema_link_or_func=lambda request, view: "/e", context_link_or_func=CONTEXT
    )
    with pytest.raises(TypeError, match="schema_link_or_func"):
        add_error_links(FastAPI(), error)


def test_outside_callables_with_kwargs_or_defaults_pass(make_client, store):
    """Callables whose parameters have defaults (or ``**kwargs``) are called
    as ``func()`` in the HTTP Schema Resource, with the path parameters in
    add_error_links' handlers and with the resolved arguments inside the
    endpoint."""
    app = FastAPI()
    received = []
    errors = []

    def schema(item_id=None, item=None, **kwargs):
        received.append((item_id, item))
        return "/s.json"

    def error_schema(item_id=None, **kwargs):
        errors.append(item_id)
        return "/errors/item"

    @app.put("/items/{item_id}")
    @scrudful(
        etag_func=store.etag,
        schema_link_or_func=schema,
        context_link_or_func=CONTEXT,
        error=Entity(schema_link_or_func=error_schema, context_link_or_func=CONTEXT),
    )
    async def put_item(item_id: int, item: ItemIn):
        if item_id == 4:
            raise HTTPException(status_code=404)
        return {}

    add_error_links(app, APP_ERROR)
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    body = options_hsr(client, "/items/3")
    validate_http_schema_resource(body)
    assert received == [(None, None)]  # describe mode: func()
    assert errors == [None]
    response = client.put("/items/4", json={"name": "x"}, headers={"If-Match": '"v1"'})
    assert response.status_code == 404
    assert (
        parse_links(response.headers["link"])[0][0] == "http://testserver/errors/item"
    )
    assert errors[-1] == "4"  # the raw path parameter, in the handler
    assert link_problems("PUT", 404, response.headers, True, body) == []
    response = client.put("/items/3", json={"name": "x"}, headers={"If-Match": '"v1"'})
    assert response.status_code == 200
    assert received[-1] == (3, ItemIn(name="x"))  # inside the endpoint: resolved


# --- OPTIONS resolves each method the way Starlette routes it -------------------


def test_add_options_path_split_across_routers(make_client):
    links = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    reads, writes = APIRouter(), APIRouter()

    @reads.get("/items/{i}")
    @scrudful(**links)
    async def read(i: int):
        return {}

    @writes.put("/items/{i}")
    @scrudful(**links)
    async def write(i: int):
        return {}

    app = FastAPI()
    app.include_router(reads, prefix="/v1")
    app.include_router(writes, prefix="/v1")

    @app.delete("/v1/items/{i}")
    @scrudful(**links)
    async def remove(i: int):
        return {}

    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    options_204(
        client, "/v1/items/1", "GET, PUT, DELETE, OPTIONS", hsr_target("v1.items.i")
    )
    body = options_hsr(client, "/v1/items/1")
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "delete", "options"]
    for method in ("GET", "PUT", "DELETE"):
        response = client.request(method, "/v1/items/1")
        assert response.status_code == 200
        assert link_problems(method, 200, response.headers, True, body) == []


def test_add_options_differently_named_parameters(make_client):
    """Starlette routes ``/items/5`` GET to ``/items/{id}`` and PUT to
    ``/items/{item_id}``: one resource, whose document has both, under
    either template, and whose ``Allow`` lists both."""
    received = []

    def schema(**kwargs):
        received.append(kwargs)
        return SCHEMA

    links = dict(schema_link_or_func=schema, context_link_or_func=CONTEXT)
    reads, writes = APIRouter(), APIRouter()

    @reads.get("/items/{id}")
    @scrudful(**links)
    async def read(id: int):
        return {}

    @writes.put("/items/{item_id}")
    @scrudful(**links)
    async def write(item_id: int):
        return {}

    app = FastAPI()
    app.include_router(reads)
    app.include_router(writes)
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    options_204(client, "/items/5", "GET, PUT, OPTIONS", hsr_target("items.id"))
    body = options_hsr(client, "/items/5")
    assert list(body) == ["get", "put", "options"]
    assert body == http_schema_resource(app, "/items/{id}")
    assert body == http_schema_resource(app, "/items/{item_id}")
    assert received == [{}, {}]  # each endpoint's callable, once


def test_add_options_on_a_router_groups_differently_named_parameters(make_client):
    """Router mode: one OPTIONS route for the resource, not one per template."""
    links = dict(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    router = APIRouter()

    @router.get("/items/{id}")
    @scrudful(**links)
    async def read(id: int):
        return {}

    @router.put("/items/{item_id}")
    @scrudful(**links)
    async def write(item_id: int):
        return {}

    # Another convertor matches other URLs: another resource.
    @router.delete("/things/{n:int}", status_code=204)
    @scrudful()
    async def remove(n: int):
        return None

    @router.get("/things/{name}")
    @scrudful(**links)
    async def thing(name: str):
        return {}

    add_options(router, base_url=TEST_BASE_URL)
    # The HSR route (methods=None, every method) isn't an OPTIONS route.
    automatic = [r for r in router.routes if r.methods and "OPTIONS" in r.methods]
    assert [route.path for route in automatic] == [
        "/items/{id}",
        "/things/{n:int}",
        "/things/{name}",
    ]
    app = FastAPI()
    app.include_router(router)
    client = make_client(app)
    options_204(client, "/items/5", "GET, PUT, OPTIONS", hsr_target("items.id"))
    assert options_hsr(client, "/items/5") == http_schema_resource(
        router, "/items/{item_id}"
    )
    options_204(client, "/things/5", "DELETE, OPTIONS", hsr_target("things.n-int"))
    options_204(client, "/things/abc", "GET, OPTIONS", hsr_target("things.name"))


def test_add_options_mount(make_client):
    async def item(item_id: int):
        return {}

    endpoint = scrudful(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)(item)
    from fastapi.routing import APIRoute

    app = FastAPI()
    app.mount(
        "/m", Router(routes=[APIRoute("/items/{item_id}", endpoint, methods=["GET"])])
    )
    app.router.routes.append(
        Mount("/n", routes=[APIRoute("/items/{item_id}", endpoint, methods=["PUT"])])
    )
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    for path, allow, method, name in [
        ("/m/items/1", "GET, OPTIONS", "GET", "m.items.item_id"),
        ("/n/items/1", "PUT, OPTIONS", "PUT", "n.items.item_id"),
    ]:
        options_204(client, path, allow, hsr_target(name))
        hsr = options_hsr(client, path)
        response = client.request(method, path)
        assert link_problems(method, 200, response.headers, True, hsr) == []


def test_add_options_mounted_fastapi_app(make_client):
    """A mounted app keeps its own middleware: ``add_options`` on the outer app
    leaves it alone, and on the sub-app covers it inside that middleware."""
    sub = FastAPI()
    sub.add_middleware(
        CORSMiddleware, allow_origins=["https://a.example"], allow_methods=["*"]
    )

    @sub.get("/items/{item_id}")
    @scrudful(schema_link_or_func=SCHEMA, context_link_or_func=CONTEXT)
    async def get_item(item_id: int):
        return {}

    app = FastAPI()
    app.mount("/sub", sub)
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    preflight = {"Origin": "https://a.example", "Access-Control-Request-Method": "GET"}
    assert client.options("/sub/items/1").status_code == 405
    response = client.options("/sub/items/1", headers=preflight)
    assert response.headers["access-control-allow-origin"] == "https://a.example"
    add_options(sub, base_url=TEST_BASE_URL)
    # The sub-app's own HSR route, where the sub-app is mounted.
    target = hsr_target("items.item_id", "/sub")
    options_204(client, "/sub/items/1", "GET, OPTIONS", target)
    hsr = options_hsr(client, "/sub/items/1")
    assert hsr["get"]["responses"]["200"]["content"] == ITEM_CONTENT
    response = client.options("/sub/items/1", headers=preflight)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://a.example"
    assert "allow" not in response.headers  # CORSMiddleware's answer


def test_add_error_links_rejects_rel_callables_needing_arguments():
    """The handlers call the rels too (OPTIONS does not)."""

    def user():
        return "alice"

    app = FastAPI()
    error = Entity(
        schema_link_or_func="/e.json",
        schema_rel_or_func=lambda item_id, who: "describedby",
        context_link_or_func=CONTEXT,
    )

    @app.get("/items/{item_id}")
    @scrudful(**ITEM_LINKS, error=error)
    async def get_item(item_id: int, who: str = Depends(user)):
        raise HTTPException(status_code=404)

    add_options(app, base_url=TEST_BASE_URL)  # the rels are not called in OPTIONS
    with pytest.raises(TypeError, match="error.schema_rel_or_func"):
        add_error_links(app, APP_ERROR)
    app_error = Entity(
        schema_link_or_func="/e.json",
        context_link_or_func=CONTEXT,
        context_rel_or_func=lambda request, view: "x",
    )
    with pytest.raises(TypeError, match="context_rel_or_func"):
        add_error_links(FastAPI(), app_error)


def test_mount_callables_get_the_path_parameters_that_reach_them(make_client):
    """A Mount's own ``{path:path}`` is not a path parameter of its routes:
    add_error_links' handlers pass only ``item_id``. Callables needing it
    can't describe the template, so add_options rejects them."""
    from fastapi.routing import APIRoute

    def schema(item_id):
        return f"/s/{item_id}.json"

    async def item(item_id: int):
        raise HTTPException(status_code=404)

    error = Entity(
        schema_link_or_func=lambda item_id: f"/e/{item_id}",
        context_link_or_func=CONTEXT,
    )
    endpoint = scrudful(
        etag_func=lambda **kwargs: "v1",
        schema_link_or_func=schema,
        context_link_or_func=CONTEXT,
        error=error,
    )(item)
    app = FastAPI()
    app.router.routes.append(
        Mount("/n", routes=[APIRoute("/items/{item_id}", endpoint, methods=["GET"])])
    )
    add_error_links(app, APP_ERROR)
    with pytest.raises(ValueError, match=r"func\(\)"):
        add_options(app, base_url=TEST_BASE_URL)
    response = make_client(app).get("/n/items/1")
    assert response.status_code == 404
    assert parse_links(response.headers["link"])[0][0] == "http://testserver/e/1"


@pytest.mark.parametrize("mode", ["app", "router"])
def test_options_answers_the_matched_path(make_client, mode):
    """The app's or the router's OPTIONS answer links to the document of the
    path matched, built without the request: the callables get no
    arguments."""
    seen = []

    def schema(*args, **kwargs):
        seen.append((args, kwargs))
        return "/s/item.json"

    router = APIRouter()

    @router.get("/items/{item_id}")
    @scrudful(schema_link_or_func=schema, context_link_or_func=CONTEXT)
    async def get_item(item_id: int, req: Request):
        return {}

    app = FastAPI()
    if mode == "router":
        add_options(router, base_url=TEST_BASE_URL)
    app.include_router(router)
    if mode == "app":
        add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    options_204(client, "/items/7", "GET, OPTIONS", hsr_target("items.item_id"))
    hsr = options_hsr(client, "/items/7")
    content = hsr["get"]["responses"]["200"]["content"]
    assert content["application/json"]["schema"] == {
        "$ref": "http://testserver/s/item.json"
    }
    owner = router if mode == "router" else app
    assert hsr == http_schema_resource(owner, "/items/{item_id}")
    assert seen == [((), {})]


class _Tag:
    """ASGI middleware tagging every HTTP response with ``x-mw: 1``."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def tagged(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"x-mw", b"1")]
            await send(message)

        await self.app(scope, receive, tagged)


def _mounted_item(error=None):
    from fastapi.routing import APIRoute

    def user():
        return "alice"

    async def item(item_id: int, who: str = Depends(user)):
        raise HTTPException(status_code=404)

    endpoint = scrudful(**ITEM_LINKS, error=error)(item)
    return APIRoute("/items/{item_id}", endpoint, methods=["GET"])


def test_add_error_links_checks_endpoints_under_a_middleware_mount(make_client):
    """The outer app's handlers serve a Mount with middleware, so its endpoints
    are checked, and their ``error`` applies."""
    from starlette.middleware import Middleware

    bad = Entity(
        schema_link_or_func=lambda item_id, who: f"/e/{who}",
        context_link_or_func=CONTEXT,
    )
    app = FastAPI()
    app.router.routes.append(
        Mount("/n", routes=[_mounted_item(bad)], middleware=[Middleware(_Tag)])
    )
    with pytest.raises(TypeError, match="error.schema_link_or_func"):
        add_error_links(app, APP_ERROR)

    good = Entity(
        schema_link_or_func=lambda item_id: f"/e/{item_id}",
        context_link_or_func=CONTEXT,
    )
    app = FastAPI()
    app.router.routes.append(
        Mount("/n", routes=[_mounted_item(good)], middleware=[Middleware(_Tag)])
    )
    add_error_links(app, APP_ERROR)
    response = make_client(app).get("/n/items/1")
    assert response.status_code == 404
    assert parse_links(response.headers["link"])[0][0] == "http://testserver/e/1"


def test_add_options_leaves_middleware_mounts_alone(make_client):
    """OPTIONS under a Mount with middleware is not answered from outside it,
    so that the middleware (here CORS) still runs."""
    from starlette.middleware import Middleware

    app = FastAPI()
    cors = Middleware(
        CORSMiddleware, allow_origins=["https://a.example"], allow_methods=["*"]
    )
    app.router.routes.append(Mount("/n", routes=[_mounted_item()], middleware=[cors]))
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    preflight = {"Origin": "https://a.example", "Access-Control-Request-Method": "GET"}
    response = client.options("/n/items/1", headers=preflight)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://a.example"
    assert "allow" not in response.headers
    assert client.options("/n/items/1").status_code == 405


@pytest.mark.skipif(
    "middleware" not in inspect.signature(Router).parameters,
    reason="Router(middleware=...) needs a newer Starlette",
)
def test_add_options_leaves_middleware_routers_alone(make_client):
    from starlette.middleware import Middleware

    app = FastAPI()
    inner = Router(routes=[_mounted_item()], middleware=[Middleware(_Tag)])
    app.router.routes.append(Mount("/r", app=inner))
    add_options(app, base_url=TEST_BASE_URL)
    # Routed into the inner Router (through its middleware), which has no
    # OPTIONS route: its 405, not an answer from outside it.
    assert make_client(app).options("/r/items/1").status_code == 405


# --- The HSR link on every response, kept dependency Links, JSON 500, 422 -------

#: The HSR route URL of ``/items/{item_id}`` (add_options' base URL is the
#: test client's).
ITEMS_HSR = "http://testserver/_hsr/items.item_id"
ITEMS_HSR_LINK = hsr_link(ITEMS_HSR)
SERVER_ERROR = problem_from_errors(500, None)


def _key(x_key: str | None = Header(default=None)) -> str | None:
    """The requester: ``X-Key``; ``bad`` is refused (401)."""
    if x_key == "bad":
        raise HTTPException(401, "Bad key", headers={"WWW-Authenticate": "Key"})
    return x_key


def _everywhere_app(store, *, options: bool = True) -> FastAPI:
    """``/items/{item_id}``: GET and HEAD (404 for item 404, a 500 for item
    500), PUT for the ``writer`` key only (403); and a plain route."""
    app = FastAPI()
    links = dict(
        etag_func=store.etag,
        schema_link_or_func=SCHEMA,
        context_link_or_func=CONTEXT,
        error=ERROR,
    )

    @app.get("/items/{item_id}")
    @app.head("/items/{item_id}")
    @scrudful(**links)
    async def get_item(item_id: int, key: str | None = Depends(_key)):
        if item_id == 404:
            raise HTTPException(404, "No such item")
        if item_id == 500:
            raise RuntimeError("boom")
        return {"id": item_id}

    @app.put("/items/{item_id}")
    @scrudful(**links)
    async def put_item(item_id: int, key: str | None = Depends(_key)):
        if key != "writer":
            raise HTTPException(403, "Read only")
        return {"id": item_id}

    @app.get("/plain/{n}")
    async def plain(n: int):
        if n == 500:
            raise RuntimeError("boom")
        raise HTTPException(404, "Nothing here")

    add_error_links(app, ERROR)
    if options:
        add_options(app, base_url=TEST_BASE_URL)
    return app


#: ``(label, method, url, headers, status)``: every kind of response of a
#: described resource.
EVERY_RESPONSE = [
    ("200", "GET", "/items/1", {}, 200),
    ("HEAD", "HEAD", "/items/1", {}, 200),
    ("304", "GET", "/items/1", {"If-None-Match": '"v1"'}, 304),
    ("HEAD 304", "HEAD", "/items/1", {"If-None-Match": '"v1"'}, 304),
    ("400", "GET", "/items/1", {"If-None-Match": "v1"}, 400),
    ("412", "PUT", "/items/1", {"If-Match": '"other"', "X-Key": "writer"}, 412),
    ("428", "PUT", "/items/1", {"X-Key": "writer"}, 428),
    ("raised 404", "GET", "/items/404", {}, 404),
    ("401", "GET", "/items/1", {"X-Key": "bad"}, 401),
    ("403", "PUT", "/items/1", {"If-Match": '"v1"', "X-Key": "reader"}, 403),
    ("405", "POST", "/items/1", {}, 405),
    ("422", "GET", "/items/abc", {}, 422),
    ("500", "GET", "/items/500", {}, 500),
]


@pytest.mark.parametrize(
    "label, method, url, headers, status",
    EVERY_RESPONSE,
    ids=[case[0] for case in EVERY_RESPONSE],
)
def test_every_response_carries_the_hsr_link(
    store, label, method, url, headers, status
):
    client = TestClient(_everywhere_app(store), raise_server_exceptions=False)
    response = client.request(method, url, headers=headers)
    assert response.status_code == status
    assert hsr_link_problems(response.headers, ITEMS_HSR) == []
    assert {"Link", "Allow"} <= set(_exposed(response))
    content = method != "HEAD" and status != 304
    assert link_problems(method, status, response.headers, content) == []
    # Once, last: after the content's (or the error's) Links.
    assert response.headers["link"].endswith(ITEMS_HSR_LINK)
    assert response.headers.get_list("link") == [response.headers["link"]]


@pytest.mark.parametrize(
    "url, status", [("/nowhere", 404), ("/plain/1", 404), ("/plain/500", 500)]
)
def test_an_undescribed_path_gets_no_hsr_link(store, url, status):
    """An unrouted 404, and the errors of a path without a scrudful endpoint,
    carry the error entity's Links but no HSR link: there is no resource
    whose HSR they could link to."""
    client = TestClient(_everywhere_app(store), raise_server_exceptions=False)
    response = client.get(url)
    assert response.status_code == status
    assert response.headers["link"] == ERROR_LINKS


def test_without_add_options_no_hsr_link(store):
    """Without :func:`add_options` no HSR is served: no response links one."""
    client = TestClient(
        _everywhere_app(store, options=False), raise_server_exceptions=False
    )
    for method, url, headers, status in [
        ("GET", "/items/1", {}, 200),
        ("PUT", "/items/1", {"X-Key": "writer"}, 428),
        ("GET", "/items/404", {}, 404),
        ("GET", "/items/500", {}, 500),
    ]:
        response = client.request(method, url, headers=headers)
        assert response.status_code == status
        assert HSR_DOCUMENT_REL not in response.headers.get("link", "")


def test_a_preflight_reaching_an_endpoint_gets_no_hsr_link(make_client, store):
    """A CORS preflight that an OPTIONS endpoint of the app's own answers
    carries no Links."""
    app = FastAPI()

    @app.api_route("/items/{item_id}", methods=["GET", "OPTIONS"])
    @scrudful(etag_func=store.etag)
    async def item(item_id: int):
        return {}

    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    preflight = {"Origin": "https://a.example", "Access-Control-Request-Method": "GET"}
    assert "link" not in client.options("/items/1", headers=preflight).headers
    assert client.options("/items/1").headers["link"] == ITEMS_HSR_LINK


def test_a_view_s_own_hsr_link_wins(make_client, store):
    own = "https://elsewhere.example/hsr/items"
    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(etag_func=store.etag, **ITEM_LINKS, error=ERROR)
    async def get_item(item_id: int, response: Response):
        if item_id == 404:
            raise HTTPException(404, headers={"Link": hsr_link(own)})
        response.headers["Link"] = hsr_link(own)
        return {}

    add_error_links(app, ERROR)
    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    for url, status in (("/items/1", 200), ("/items/404", 404)):
        response = client.get(url)
        assert response.status_code == status
        assert hsr_link_problems(response.headers, own) == []


def test_hsr_link_in_app_mode_under_a_prefix_and_a_mount(store):
    """With :func:`add_options` on the app, a route of a router included
    under a prefix (twice) or under a Mount links to the app's HSR route of
    its full template, on every response, the raised errors and the 500
    included."""
    from fastapi.routing import APIRoute

    router = APIRouter()

    async def item(item_id: int):
        if item_id == 404:
            raise HTTPException(404)
        if item_id == 500:
            raise RuntimeError("boom")
        return {}

    endpoint = scrudful(
        etag_func=store.etag,
        schema_link_or_func=SCHEMA,
        context_link_or_func=CONTEXT,
        error=ERROR,
    )(item)
    router.add_api_route("/items/{item_id}", endpoint, methods=["GET"])
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.include_router(router, prefix="/v2/{org}")
    app.router.routes.append(
        Mount("/m/{tenant}", routes=[APIRoute("/items/{item_id}", endpoint)])
    )
    add_error_links(app, ERROR)
    add_options(app, base_url=TEST_BASE_URL)
    client = TestClient(app, raise_server_exceptions=False)
    for prefix, name in [
        ("/v1", "v1.items.item_id"),
        ("/v2/acme", "v2.org.items.item_id"),
        ("/m/t", "m.tenant.items.item_id"),
    ]:
        target = f"http://testserver/_hsr/{name}"
        for method, item_id, headers, status in [
            ("GET", 1, {}, 200),
            ("GET", 1, {"If-None-Match": '"v1"'}, 304),
            ("GET", 1, {"If-Match": '"no"'}, 412),
            ("GET", 404, {}, 404),
            ("GET", "x", {}, 422),
            ("GET", 500, {}, 500),
            ("DELETE", 1, {}, 405),
        ]:
            response = client.request(
                method, f"{prefix}/items/{item_id}", headers=headers
            )
            assert response.status_code == status, (prefix, status)
            assert hsr_link_problems(response.headers, target) == [], (prefix, status)
        assert client.get(target).status_code == 200


def test_hsr_link_in_router_mode(make_client, store):
    """With :func:`add_options` on a router, its resources' responses link to
    the router's HSR routes, under the prefix the router is included with."""
    router = APIRouter()

    @router.get("/items/{item_id}")
    @scrudful(
        etag_func=store.etag,
        schema_link_or_func=SCHEMA,
        context_link_or_func=CONTEXT,
        error=ERROR,
    )
    async def get_item(item_id: int):
        if item_id == 404:
            raise HTTPException(404)
        return {}

    add_options(router, base_url=TEST_BASE_URL)
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    add_error_links(app, ERROR)
    client = make_client(app)
    target = "http://testserver/v1/_hsr/items.item_id"
    for headers, status in (({}, 200), ({"If-None-Match": '"v1"'}, 304)):
        response = client.get("/v1/items/1", headers=headers)
        assert response.status_code == status
        assert hsr_link_problems(response.headers, target) == []
    response = client.get("/v1/items/404")
    assert response.status_code == 404
    assert hsr_link_problems(response.headers, target) == []
    assert client.get(target).status_code == 200


def test_dependency_link_kept_on_412(make_client, store):
    """The early 400/412/428 keep a Link a dependency set, beside the error
    entity's Links and the HSR link, and its cookies."""
    author = '<http://example.com/me>; rel="author"'
    app = FastAPI()

    def set_headers(response: Response):
        response.headers.append("Link", author)
        response.set_cookie("session", "abc")

    @app.api_route(
        "/items/{item_id}",
        methods=["GET", "PUT"],
        dependencies=[Depends(set_headers)],
    )
    @scrudful(etag_func=store.etag, error=ERROR)
    async def item(item_id: int):
        return {}

    add_options(app, base_url=TEST_BASE_URL)
    client = make_client(app)
    for method, headers, status in [
        ("PUT", {"If-Match": '"other"'}, 412),
        ("PUT", {}, 428),
        ("GET", {"If-Match": "v1"}, 400),
    ]:
        response = client.request(method, "/items/1", headers=headers)
        assert response.status_code == status
        assert response.headers["link"] == (
            f"{author}, {ERROR_LINKS}, {ITEMS_HSR_LINK}"
        )
        assert response.headers["set-cookie"] == ("session=abc; Path=/; SameSite=lax")
    # A 304 keeps it too, with the HSR link.
    response = client.get("/items/1", headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response.headers["link"] == f"{author}, {ITEMS_HSR_LINK}"


def _boom_app(*, endpoint_error=None, **app_options) -> FastAPI:
    app = FastAPI(**app_options)

    @app.get("/items/{item_id}")
    @scrudful(**ITEM_LINKS, error=endpoint_error)
    async def item(item_id: int):
        raise RuntimeError("boom")

    return app


PLAIN_ERROR = Entity(
    schema_link_or_func="/schemas/error",
    context_link_or_func="http://example.com/error.jsonld",
)


@pytest.mark.parametrize(
    "error, body",
    [(ERROR, SERVER_ERROR), (PLAIN_ERROR, {"detail": "Internal Server Error"})],
    ids=["problem", "json"],
)
def test_500_is_json_with_links(error, body):
    app = _boom_app()
    add_error_links(app, error)
    add_options(app, base_url=TEST_BASE_URL)
    response = TestClient(app, raise_server_exceptions=False).get("/items/1")
    assert response.status_code == 500
    assert response.json() == body
    assert response.headers["content-type"] == error.media_type
    assert response.headers["link"] == f"{ERROR_LINKS}, {ITEMS_HSR_LINK}"
    assert {"Link", "Allow"} <= set(_exposed(response))
    assert link_problems("GET", 500, response.headers, True) == []
    if error is ERROR:
        Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(response.json())


def test_500_of_an_endpoint_with_its_own_error():
    """The 500 of an endpoint with its own ``error`` gets that entity, as its
    other raised errors do, and is problem details when it is."""
    app = _boom_app(endpoint_error=ERROR)
    add_error_links(app, PLAIN_ERROR)
    response = TestClient(app, raise_server_exceptions=False).get("/items/1")
    assert response.status_code == 500
    assert response.json() == SERVER_ERROR
    assert response.headers["content-type"] == "application/problem+json"


def test_500_still_raises_in_the_test_client():
    """Starlette's ServerErrorMiddleware sends the handler's response, then
    raises the exception again (for the server's log)."""
    app = _boom_app()
    add_error_links(app, ERROR)
    with pytest.raises(RuntimeError, match="boom"):
        TestClient(app).get("/items/1")


def test_500_in_debug_is_starlette_s_traceback():
    """Documented: with ``debug=True`` ServerErrorMiddleware answers with its
    traceback page, not the app's server error handler."""
    app = _boom_app(debug=True)
    add_error_links(app, ERROR)
    response = TestClient(app, raise_server_exceptions=False).get("/items/1")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/plain")
    assert "link" not in response.headers


def _failing_link_app() -> FastAPI:
    """An endpoint failing (the database is down) whose ``error`` schema
    callable fails too, outside it: its 500's Links can't be built."""

    def flaky_schema(item_id: int = 0) -> str:
        if not item_id:  # describe mode
            return "/schemas/item-error"
        raise ConnectionError("database is down (link callable)")

    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(
        **ITEM_LINKS,
        error=Entity(schema_link_or_func=flaky_schema, context_link_or_func=CONTEXT),
    )
    def get_item(item_id: int):
        raise ConnectionError("database is down (endpoint)")

    add_options(app, base_url=TEST_BASE_URL)
    add_error_links(app, PLAIN_ERROR)
    return app


def test_500_whose_links_fail_keeps_the_original_exception():
    """A link callable failing in the server error handler doesn't replace
    the endpoint's exception, which Starlette's ServerErrorMiddleware raises
    again (for the server's log) as it was."""
    with pytest.raises(ConnectionError, match=r"\(endpoint\)") as raised:
        TestClient(_failing_link_app()).get("/items/1")
    assert raised.value.__context__ is None


def test_500_whose_links_fail_is_sent_without_them(caplog):
    """The JSON 500 is sent without the Links that failed (the HSR link is
    kept), and the link callable's error is logged on ``scrudful.fastapi``."""
    client = TestClient(_failing_link_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="scrudful.fastapi"):
        response = client.get("/items/1")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert response.headers["content-type"] == "application/json"
    assert response.headers["link"] == ITEMS_HSR_LINK
    (record,) = [r for r in caplog.records if r.name == "scrudful.fastapi"]
    assert "(link callable)" in str(record.exc_info[1])
    assert "database is down (endpoint)" in record.getMessage()


def test_500_whose_hsr_link_fails_is_sent_without_it(caplog):
    """So is one whose HSR link fails (an ``hsr_url`` raising): the error
    entity's Links are kept."""
    calls = []

    def hsr_url(path: str) -> str:
        calls.append(path)
        if len(calls) > 1:
            raise LookupError("no HSR URL now")
        return "https://docs.example/items"

    app = _boom_app()
    add_error_links(app, ERROR)
    add_options(app, base_url=TEST_BASE_URL, hsr_url=hsr_url)
    with caplog.at_level(logging.ERROR, logger="scrudful.fastapi"):
        response = TestClient(app, raise_server_exceptions=False).get("/items/1")
    assert response.status_code == 500
    assert response.headers["link"] == ERROR_LINKS
    (record,) = [r for r in caplog.records if r.name == "scrudful.fastapi"]
    assert isinstance(record.exc_info[1], LookupError)
    calls.clear()
    with pytest.raises(RuntimeError, match="boom"):
        TestClient(app).get("/items/1")


def test_a_failing_link_of_another_error_is_the_500(make_client):
    """Outside the server error handler a failing link callable is the
    error: Starlette's server error handler answers it (with the Links it
    can build)."""

    def broken(**kwargs: Any) -> str:
        raise ConnectionError("broken link callable")

    app = FastAPI()

    @app.get("/items/{item_id}")
    @scrudful(
        **ITEM_LINKS,
        error=Entity(schema_link_or_func=broken, context_link_or_func=CONTEXT),
    )
    async def get_item(item_id: int):
        raise HTTPException(404)

    add_error_links(app, PLAIN_ERROR)
    with pytest.raises(ConnectionError, match="broken link callable"):
        TestClient(app).get("/items/1")


@pytest.mark.parametrize("key", [Exception, 500])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_500_wraps_an_existing_server_error_handler(key, asynchronous):
    app = _boom_app()

    def handler(request, exc):
        return JSONResponse({"oops": str(exc)}, status_code=500)

    async def async_handler(request, exc):
        return handler(request, exc)

    original = async_handler if asynchronous else handler
    app.add_exception_handler(key, original)
    add_error_links(app, ERROR)
    add_options(app, base_url=TEST_BASE_URL)
    assert app.exception_handlers[key].__wrapped__ is original
    # Only the app's own: none added beside it.
    assert (500 if key is Exception else Exception) not in app.exception_handlers
    response = TestClient(app, raise_server_exceptions=False).get("/items/1")
    assert response.status_code == 500
    assert response.json() == {"oops": "boom"}
    assert response.headers["link"] == f"{ERROR_LINKS}, {ITEMS_HSR_LINK}"
    assert response.headers["content-type"] == "application/problem+json"


def test_500_leaves_a_non_json_server_error_handler_alone():
    app = _boom_app()

    @app.exception_handler(Exception)
    async def text(request, exc):
        return PlainTextResponse("oops", status_code=500)

    add_error_links(app, ERROR)
    response = TestClient(app, raise_server_exceptions=False).get("/items/1")
    assert response.text == "oops"
    assert "link" not in response.headers


def test_500_handler_twice_is_wrapped_once():
    app = _boom_app()
    add_error_links(app, PLAIN_ERROR)
    add_error_links(app, ERROR)
    response = TestClient(app, raise_server_exceptions=False).get("/items/1")
    assert response.json() == SERVER_ERROR
    assert response.headers["link"] == ERROR_LINKS


class _Positive(BaseModel):
    n: int

    @field_validator("n")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 0:
            # Its ValueError is in the error's ``ctx``: not JSON.
            raise ValueError("negative")
        return value


def _validated_app() -> FastAPI:
    app = FastAPI()

    @app.post("/items/{item_id}")
    @scrudful(**ITEM_LINKS)
    async def create(item_id: int, body: _Positive):
        return {}

    return app


@pytest.mark.parametrize(
    "url, body", [("/items/1", {"n": -1}), ("/items/x", {"n": 1}), ("/items/1", {})]
)
def test_422_problem_details(make_client, url, body):
    app = _validated_app()
    add_error_links(app, ERROR)
    add_options(app, base_url=TEST_BASE_URL)
    response = make_client(app).post(url, json=body)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(problem)
    # FastAPI's own errors, as its default handler encodes them.
    plain = make_client(_validated_app()).post(url, json=body).json()
    assert problem == jsonable_encoder(
        problem_from_errors(422, plain["detail"], "Request validation failed")
    )
    assert response.headers["link"] == f"{ERROR_LINKS}, {ITEMS_HSR_LINK}"


def test_422_unchanged_without_problem_details(make_client):
    app = _validated_app()
    add_error_links(app, PLAIN_ERROR)
    response = make_client(app).post("/items/1", json={"n": -1})
    plain = make_client(_validated_app()).post("/items/1", json={"n": -1})
    assert response.status_code == 422
    assert response.json() == plain.json()
    assert response.headers["content-type"] == "application/json"
