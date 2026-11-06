"""``error_body_func`` and ``scrudful.problem.problem_details`` in each adapter."""

import json
import uuid
from typing import Any, NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as FastAPIClient
from jsonschema import Draft202012Validator
from ninja import NinjaAPI, Router
from ninja.testing import TestClient as NinjaClient
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from scrudful import fastapi as sf_fastapi
from scrudful import ninja as sf_ninja
from scrudful import rest_framework as sf_drf
from scrudful.conditional import ConditionalResult
from scrudful.entity import Entity
from scrudful._adapter import reason_phrase
from scrudful.problem import PROBLEM_JSON, problem_details, problem_from_errors
from tests.semantic_http import PROBLEM_DETAILS_SCHEMA

calls: list = []


@pytest.fixture(autouse=True)
def _reset():
    calls.clear()


def body_func(request, result):
    calls.append((request, result.status))
    return {"custom": result.status, "was": result.body}


class Sent(NamedTuple):
    status: int
    content_type: str
    content: bytes

    @property
    def body(self) -> Any:
        return json.loads(self.content)


# --- the pure function -----------------------------------------------------------


def test_problem_details_members():
    result = ConditionalResult(
        status=428,
        headers={},
        body={
            "detail": "This request must be conditional.",
            "accepted-precondition-headers": ["If-Match"],
        },
    )
    assert problem_details(None, result) == {
        "type": "about:blank",
        "title": "Precondition Required",
        "status": 428,
        "detail": "This request must be conditional.",
        "accepted-precondition-headers": ["If-Match"],
    }
    result = ConditionalResult(412, {}, {"detail": "Precondition failed."})
    assert problem_details(None, result) == {
        "type": "about:blank",
        "title": "Precondition Failed",
        "status": 412,
        "detail": "Precondition failed.",
    }


def test_problem_json_constant():
    assert PROBLEM_JSON == "application/problem+json"


# --- the three adapters, one tiny GET/PUT resource with an ETag "v1" -----------------


def drf_send(method: str, headers: dict[str, str], **options: Any) -> Sent:
    @sf_drf.scrudful_api_view(
        ["GET", "PUT"], etag_func=lambda view, request: "v1", **options
    )
    def view(request):
        return Response({"ok": True})

    wsgi = {
        "HTTP_" + name.upper().replace("-", "_"): value
        for name, value in headers.items()
    }
    factory = APIRequestFactory()
    response = view(getattr(factory, method.lower())("/item", **wsgi))
    if hasattr(response, "render"):
        response.render()
    return Sent(
        response.status_code,
        response.get("Content-Type", "").split(";")[0],
        response.content,
    )


def ninja_send(method: str, headers: dict[str, str], **options: Any) -> Sent:
    router = Router()

    def operation(request, obj):
        return {"ok": True}

    for verb in ("get", "put"):
        decorate = sf_ninja.scrudful(
            load=lambda request: object(), etag_func=lambda obj: "v1", **options
        )
        handler = decorate(operation)
        getattr(router, verb)("/item")(handler)
    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", router)
    response = getattr(NinjaClient(api), method.lower())("/item", headers=headers)
    return Sent(
        response.status_code,
        response.headers.get("Content-Type", "").split(";")[0],
        response.content,
    )


def fastapi_send(method: str, headers: dict[str, str], **options: Any) -> Sent:
    app = FastAPI()

    async def etag(**kwargs):
        return "v1"

    @app.get("/item")
    @sf_fastapi.scrudful(etag_func=etag, **options)
    async def get_item():
        return {"ok": True}

    @app.put("/item")
    @sf_fastapi.scrudful(etag_func=etag, **options)
    async def put_item():
        return {"ok": True}

    with FastAPIClient(app) as client:
        response = getattr(client, method.lower())("/item", headers=headers)
    return Sent(
        response.status_code,
        response.headers.get("content-type", "").split(";")[0],
        response.content,
    )


adapters = pytest.mark.parametrize(
    "send", [drf_send, ninja_send, fastapi_send], ids=["drf", "ninja", "fastapi"]
)


@adapters
def test_error_body_func_replaces_412_body(send):
    sent = send("PUT", {"If-Match": '"other"'}, error_body_func=body_func)
    assert sent.status == 412
    assert sent.body == {"custom": 412, "was": {"detail": "Precondition failed."}}
    assert [status for _, status in calls] == [412]
    assert calls[0][0] is not None  # the request


@adapters
def test_error_body_func_keeps_428_accepted_headers(send):
    sent = send("PUT", {}, error_body_func=problem_details)
    assert sent.status == 428
    assert sent.body == {
        "type": "about:blank",
        "title": "Precondition Required",
        "status": 428,
        "detail": "This request must be conditional.",
        "accepted-precondition-headers": ["If-Match"],
    }


@adapters
def test_error_body_func_400_malformed_if_match(send):
    sent = send("PUT", {"If-Match": "garbage"}, error_body_func=body_func)
    assert sent.status == 400
    assert sent.body == {
        "custom": 400,
        "was": {"detail": "Malformed If-Match header."},
    }


@adapters
def test_error_body_func_not_called_for_304(send):
    sent = send("GET", {"If-None-Match": '"v1"'}, error_body_func=body_func)
    assert sent.status == 304
    assert sent.content == b""
    assert calls == []


@adapters
def test_without_error_body_func_the_body_is_unchanged(send):
    sent = send("PUT", {"If-Match": '"other"'})
    assert sent.body == {"detail": "Precondition failed."}


@adapters
def test_problem_json_error_entity_content_type(send):
    sent = send(
        "PUT",
        {"If-Match": '"other"'},
        error=Entity(media_type=PROBLEM_JSON),
        error_body_func=problem_details,
    )
    assert sent.status == 412
    assert sent.content_type == PROBLEM_JSON
    assert sent.body["title"] == "Precondition Failed"


@adapters
def test_error_body_func_returning_none_is_a_type_error(send):
    with pytest.raises(TypeError, match="error_body_func"):
        send("PUT", {}, error_body_func=lambda request, result: None)


def test_error_body_func_must_not_be_a_coroutine_function():
    async def hook(request, result):
        return {}

    with pytest.raises(TypeError, match="error_body_func"):
        sf_drf.scrudful_api_view(["GET"], error_body_func=hook)
    with pytest.raises(TypeError, match="error_body_func"):
        sf_ninja.scrudful(load=lambda r: 1, error_body_func=hook)


@adapters
def test_error_body_func_must_be_callable(send):
    with pytest.raises(TypeError, match="error_body_func"):
        send("GET", {}, error_body_func="nope")


def test_ninja_list_error_body_func():
    router = Router()

    @router.get("/items", response=list[int])
    @sf_ninja.scrudful_list(
        href_func=lambda request, item: f"/items/{item}",
        list_etag_func=None,
        list_last_modified_func=lambda items: 1_700_000_000,
        error_body_func=body_func,
    )
    def list_items(request):
        return [1, 2]

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", router)
    response = NinjaClient(api).get("/items", headers={"If-None-Match": "garbage"})
    assert response.status_code == 400
    assert response.json()["custom"] == 400


def test_ninja_create_error_body_func():
    router = Router()

    @router.post("/items", response={201: int})
    @sf_ninja.scrudful_create(
        location_func=lambda request, item: f"/items/{item}",
        list_load=lambda request, **params: [1, 2],
        list_etag_func=lambda items: f"n{len(items)}",
        error_body_func=body_func,
    )
    def create_item(request):
        return 3

    api = NinjaAPI(urls_namespace=f"api-{uuid.uuid4().hex}")
    api.add_router("", router)
    response = NinjaClient(api).post("/items", headers={"If-Match": '"n1"'})
    assert response.status_code == 412
    assert response.json()["custom"] == 412


def test_drf_viewset_meta_error_body_func():
    from rest_framework import viewsets

    def list_body_func(request, result):
        return {"list": result.status}

    @sf_drf.scrudful_viewset
    class V(viewsets.ViewSet):
        class Meta:
            etag_func = staticmethod(lambda view, request, pk: "v1")
            error_body_func = staticmethod(body_func)
            list_error_body_func = staticmethod(list_body_func)

        def update(self, request, pk):
            return Response({})

        def list(self, request):
            return Response([])

        def create(self, request):
            return Response({}, status=201)

    assert V.update.error_body_func is body_func
    assert V.list.error_body_func is list_body_func
    # A create's 400/412 carry the item error entity: the item's body.
    assert V.create.error_body_func is body_func


def test_problem_from_errors_is_valid_problem_details():
    errors = [{"loc": ["body", "name"], "msg": "Field required"}]
    body = problem_from_errors(422, errors)
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(body)
    assert body["status"] == 422 and body["title"] == reason_phrase(422)
    assert body["type"] == "about:blank" and body["detail"] == reason_phrase(422)
    assert body["errors"][0]["msg"] == "Field required"
    assert "errors" not in problem_from_errors(404, None)
    assert problem_from_errors(400, [], detail="Bad input")["detail"] == "Bad input"
