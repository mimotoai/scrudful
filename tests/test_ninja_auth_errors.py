"""ninja's 401, 403, 422 and 429 agree with the HTTP Schema Resource.

An operation's ``error`` differs from the API-wide :func:`add_error_links`
entity: every 401, 403, 422 and 429 answered for the operation, whether ninja
answers it before the operation runs (a CSRF failure, an
``AuthorizationError``, an ``HttpError`` of an auth callback) or ``before=``,
``after_throttles=``, ``load`` or the operation raises it, carries the
API-wide entity, which the HSR's explicit entries describe. Without an
API-wide entity, every one of them carries the operation's ``error``, which
the HSR's ``default`` describes. The module is its own URLconf.
"""

import json
from typing import Any

import pytest
from django.test import Client
from django.urls import path
from ninja import NinjaAPI, Schema
from ninja.errors import AuthorizationError, HttpError
from ninja.security import APIKeyCookie, APIKeyHeader
from ninja.throttling import BaseThrottle

from scrudful.ninja import Entity, ScrudfulRouter, add_error_links, scrudful
from scrudful.problem import PROBLEM_JSON
from tests.semantic_http import link_problems, parse_links, status_entry_problems

pytestmark = [pytest.mark.urls(__name__), pytest.mark.django_db]

BASE_URL = "http://testserver/"
API_WIDE = Entity(
    "https://api.example/schemas/problem.json",
    context_link_or_func="https://api.example/contexts/problem.jsonld",
    media_type=PROBLEM_JSON,
)
OWN = Entity(
    "https://api.example/schemas/own-error",
    context_link_or_func="https://api.example/contexts/own-error",
)
LINKS = dict(
    schema_link_or_func="https://api.example/schemas/thing",
    context_link_or_func="https://api.example/contexts/thing",
)


class Thing(Schema):
    id: int


class Key(APIKeyHeader):
    param_name = "X-Key"

    def authenticate(self, request: Any, key: Any) -> Any:
        if key == "banned":
            raise HttpError(403, "banned")
        if key == "unauthorized":
            raise AuthorizationError()
        return key if key else None


class Cookie(APIKeyCookie):
    """Session-style auth: ninja checks CSRF before calling it."""

    param_name = "key"

    def authenticate(self, request: Any, key: Any) -> Any:
        return key


def _refusal(value: str | None) -> int | None:
    """The status a hook raises: ``deny`` is a 403, else the status given."""
    if value is None:
        return None
    return 403 if value == "deny" else int(value)


def before(request: Any, **kwargs: Any) -> None:
    status = _refusal(request.headers.get("X-Before"))
    if status is not None:
        raise HttpError(status, "before says no")


def after_throttles(request: Any, **kwargs: Any) -> None:
    status = _refusal(request.headers.get("X-After"))
    if status is not None:
        raise HttpError(status, "after_throttles says no")


def load(request: Any, pk: int, **kwargs: Any) -> Any:
    """The object; a pk above 1000 is refused with the status ``pk - 1000``."""
    if pk > 1000:
        raise HttpError(pk - 1000, "load says no")
    return {"id": pk}


class Throttle(BaseThrottle):
    """Refuses a request sending ``X-Throttle: yes``."""

    def allow_request(self, request: Any) -> bool:
        return request.headers.get("X-Throttle") != "yes"

    def wait(self) -> None:
        return None


router = ScrudfulRouter(base_url=BASE_URL)


def get_thing(request: Any, pk: int, obj: Any) -> Any:
    if pk == 401:
        raise HttpError(401, "who are you")
    if pk == 403:
        raise HttpError(403, "not yours")
    if pk in (404, 422, 429):
        raise HttpError(pk, "the operation says no")
    return obj


router.add_api_operation(
    "/{pk}",
    ["GET"],
    scrudful(
        load=load,
        error=OWN,
        before=before,
        after_throttles=after_throttles,
        **LINKS,
    )(get_thing),
    response=Thing,
    auth=Key(),
    throttle=[Throttle()],
)


@router.post("/{pk}", response=Thing, auth=Cookie())
@scrudful(load=lambda request, pk, **kw: {"id": pk}, error=OWN, **LINKS)
def post_thing(request: Any, pk: int, obj: Any) -> Any:
    return obj


api = NinjaAPI(urls_namespace="auth-errors")
api.add_router("/things", router)
add_error_links(api, API_WIDE)


#: The same operations without an API-wide entity: every 401, 403, 422 and
#: 429 of an operation carries its ``error``, which the HSR's ``default``
#: describes.
plain_router = ScrudfulRouter(base_url=BASE_URL)
plain_router.add_api_operation(
    "/{pk}",
    ["GET"],
    scrudful(load=load, error=OWN, before=before, **LINKS)(get_thing),
    response=Thing,
    auth=Key(),
    throttle=[Throttle()],
)
plain_router.add_api_operation(
    "/{pk}",
    ["POST"],
    scrudful(load=load, error=OWN, **LINKS)(
        lambda request, pk, obj: obj  # pragma: no cover (CSRF refuses it)
    ),
    response=Thing,
    auth=Cookie(),
)
plain_api = NinjaAPI(urls_namespace="auth-errors-plain")
plain_api.add_router("/things", plain_router)

#: A problem-details ``error`` without an API-wide entity: ninja's validation
#: 422 is reshaped as problem details, as the entity describes it.
PROBLEM = Entity(
    "https://api.example/schemas/problem.json",
    context_link_or_func="https://api.example/contexts/problem.jsonld",
    media_type=PROBLEM_JSON,
)
problem_router = ScrudfulRouter(base_url=BASE_URL)
problem_router.add_api_operation(
    "/{pk}",
    ["GET"],
    scrudful(load=load, error=PROBLEM, **LINKS)(get_thing),
    response=Thing,
)
problem_api = NinjaAPI(urls_namespace="auth-errors-problem")
problem_api.add_router("/things", problem_router)

urlpatterns = [
    path("p/", api.urls),
    path("q/", plain_api.urls),
    path("r/", problem_api.urls),
]


def hsr_of(client: Client, url: str) -> dict:
    return json.loads(client.get(url).content)


def schema_links(response: Any) -> list[str]:
    return [
        target
        for target, params in parse_links(response.get("Link"))
        if params.get("rel") == "describedby"
    ]


def test_the_hsr_lists_403_with_the_api_wide_entity():
    hsr = hsr_of(Client(), "/p/things/_hsr/pk")
    for method in ("get", "post"):
        responses = hsr[method]["responses"]
        assert {"401", "403"} <= responses.keys()
        for status in ("401", "403"):
            [schema] = [
                entry["schema"]["$ref"]
                for entry in responses[status]["content"].values()
            ]
            assert schema == API_WIDE.schema_link_or_func
        assert responses["403"]["description"].startswith("Forbidden")


@pytest.mark.parametrize(
    "url, headers, status",
    [
        # ninja's authentication, before the operation runs.
        ("/p/things/1", {"X-Key": "banned"}, 403),
        ("/p/things/1", {"X-Key": "unauthorized"}, 403),
        ("/p/things/1", {}, 401),
        # before= raises it.
        ("/p/things/1", {"X-Key": "ok", "X-Before": "deny"}, 403),
        # after_throttles= raises it.
        ("/p/things/1", {"X-Key": "ok", "X-After": "deny"}, 403),
        # The operation raises it.
        ("/p/things/403", {"X-Key": "ok"}, 403),
        ("/p/things/401", {"X-Key": "ok"}, 401),
    ],
    ids=[
        "auth-error",
        "AuthorizationError",
        "no-key",
        "before",
        "after_throttles",
        "op-403",
        "op-401",
    ],
)
def test_401_and_403_carry_the_api_wide_entity(url, headers, status):
    client = Client()
    hsr = hsr_of(client, "/p/things/_hsr/pk")
    response = client.get(url, headers=headers)
    assert response.status_code == status
    assert link_problems("GET", status, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [API_WIDE.schema_link_or_func]


def test_a_csrf_403_carries_the_api_wide_entity():
    client = Client(enforce_csrf_checks=True)
    hsr = hsr_of(client, "/p/things/_hsr/pk")
    client.cookies["key"] = "secret"
    response = client.post("/p/things/1")
    assert response.status_code == 403
    assert b"CSRF" in response.content
    assert link_problems("POST", 403, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [API_WIDE.schema_link_or_func]


def test_other_errors_keep_the_operations_entity():
    client = Client()
    hsr = hsr_of(client, "/p/things/_hsr/pk")
    response = client.get("/p/things/404", headers={"X-Key": "ok"})
    assert response.status_code == 404
    assert link_problems("GET", 404, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [OWN.schema_link_or_func]


def test_without_an_api_wide_entity_the_framework_errors_are_under_default():
    responses = hsr_of(Client(), "/q/things/_hsr/pk")["get"]["responses"]
    # Every 401, 403, 422 and 429 carries the operation's ``error``, which
    # ``default`` describes: no explicit entry.
    assert not {"401", "403", "422", "429"} & responses.keys()
    [schema] = [
        entry["schema"]["$ref"] for entry in responses["default"]["content"].values()
    ]
    assert schema == OWN.schema_link_or_func


@pytest.mark.parametrize(
    "url, headers, status",
    [
        # ninja's authentication, before the operation runs.
        ("/q/things/1", {"X-Key": "banned"}, 403),
        ("/q/things/1", {"X-Key": "unauthorized"}, 403),
        ("/q/things/1", {}, 401),
        # before= raises it.
        ("/q/things/1", {"X-Key": "ok", "X-Before": "deny"}, 403),
        # The operation raises it.
        ("/q/things/403", {"X-Key": "ok"}, 403),
        ("/q/things/401", {"X-Key": "ok"}, 401),
    ],
    ids=["auth-error", "AuthorizationError", "no-key", "before", "op-403", "op-401"],
)
def test_without_an_api_wide_entity_401_and_403_carry_the_operations_entity(
    url, headers, status
):
    """Whoever answers it, ninja or the operation: one entity for every 401
    and 403 of the operation, so each agrees with the entry Agreement picks."""
    client = Client()
    hsr = hsr_of(client, "/q/things/_hsr/pk")
    response = client.get(url, headers=headers)
    assert response.status_code == status
    assert status_entry_problems("GET", status, hsr) == []
    assert link_problems("GET", status, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [OWN.schema_link_or_func]


def test_without_an_api_wide_entity_a_csrf_403_carries_the_operations_entity():
    client = Client(enforce_csrf_checks=True)
    hsr = hsr_of(client, "/q/things/_hsr/pk")
    client.cookies["key"] = "secret"
    response = client.post("/q/things/1")
    assert response.status_code == 403
    assert b"CSRF" in response.content
    assert status_entry_problems("POST", 403, hsr) == []
    assert link_problems("POST", 403, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [OWN.schema_link_or_func]


@pytest.mark.parametrize("status", [422, 429])
def test_the_hsr_lists_422_and_429_with_the_api_wide_entity(status):
    responses = hsr_of(Client(), "/p/things/_hsr/pk")["get"]["responses"]
    [schema] = [
        entry["schema"]["$ref"] for entry in responses[str(status)]["content"].values()
    ]
    assert schema == API_WIDE.schema_link_or_func


@pytest.mark.parametrize("status", [422, 429])
@pytest.mark.parametrize(
    "url, headers",
    [
        ("/p/things/{status}", {"X-Key": "ok"}),
        ("/p/things/1", {"X-Key": "ok", "X-Before": "{status}"}),
        ("/p/things/1", {"X-Key": "ok", "X-After": "{status}"}),
        ("/p/things/{load_pk}", {"X-Key": "ok"}),
    ],
    ids=["operation", "before", "after_throttles", "load"],
)
def test_a_raised_422_or_429_carries_the_api_wide_entity(url, headers, status):
    """ninja's own 422 (validation) and 429 (throttling) carry the API-wide
    entity, which the HSR's explicit entries describe: so does one the
    operation raises itself, as its 401 and 403 do."""
    values = {"status": status, "load_pk": 1000 + status}
    client = Client()
    hsr = hsr_of(client, "/p/things/_hsr/pk")
    response = client.get(
        url.format(**values),
        headers={key: value.format(**values) for key, value in headers.items()},
    )
    assert response.status_code == status
    assert link_problems("GET", status, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [API_WIDE.schema_link_or_func]


@pytest.mark.parametrize(
    "url, headers, status",
    [
        ("/p/things/abc", {"X-Key": "ok"}, 422),
        ("/p/things/1", {"X-Key": "ok", "X-Throttle": "yes"}, 429),
    ],
    ids=["validation", "throttle"],
)
def test_ninjas_own_422_and_429_carry_the_api_wide_entity(url, headers, status):
    """ninja's own 422 (validation) and 429 (throttling), which it answers
    before the operation runs, carry the API-wide entity too."""
    client = Client()
    hsr = hsr_of(client, "/p/things/_hsr/pk")
    response = client.get(url, headers=headers)
    assert response.status_code == status
    assert status_entry_problems("GET", status, hsr) == []
    assert link_problems("GET", status, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [API_WIDE.schema_link_or_func]


def test_a_raised_403_of_load_carries_the_api_wide_entity():
    client = Client()
    hsr = hsr_of(client, "/p/things/_hsr/pk")
    response = client.get("/p/things/1403", headers={"X-Key": "ok"})
    assert response.status_code == 403
    assert link_problems("GET", 403, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [API_WIDE.schema_link_or_func]


@pytest.mark.parametrize(
    "url, headers, status",
    [
        # ninja answers them before the operation runs.
        ("/q/things/abc", {"X-Key": "ok"}, 422),
        ("/q/things/1", {"X-Key": "ok", "X-Throttle": "yes"}, 429),
        # before= raises them.
        ("/q/things/1", {"X-Key": "ok", "X-Before": "422"}, 422),
        ("/q/things/1", {"X-Key": "ok", "X-Before": "429"}, 429),
        # load raises them.
        ("/q/things/1422", {"X-Key": "ok"}, 422),
        # The operation raises them.
        ("/q/things/422", {"X-Key": "ok"}, 422),
        ("/q/things/429", {"X-Key": "ok"}, 429),
    ],
    ids=[
        "validation",
        "throttle",
        "before-422",
        "before-429",
        "load-422",
        "op-422",
        "op-429",
    ],
)
def test_without_an_api_wide_entity_422_and_429_carry_the_operations_entity(
    url, headers, status
):
    """ninja's own 422 and 429 carry the operation's ``error``, as those it
    raises do, so each agrees with the entry Agreement picks."""
    client = Client()
    hsr = hsr_of(client, "/q/things/_hsr/pk")
    response = client.get(url, headers=headers)
    assert response.status_code == status
    assert status_entry_problems("GET", status, hsr) == []
    assert link_problems("GET", status, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [OWN.schema_link_or_func]


def test_without_an_api_wide_entity_a_problem_error_reshapes_the_validation_422():
    client = Client()
    hsr = hsr_of(client, "/r/things/_hsr/pk")
    response = client.get("/r/things/abc")
    assert response.status_code == 422
    assert response["Content-Type"].startswith(PROBLEM_JSON)
    body = json.loads(response.content)
    assert body["status"] == 422 and isinstance(body["errors"], list)
    assert link_problems("GET", 422, dict(response.items()), True, hsr) == []
    assert schema_links(response) == [PROBLEM.schema_link_or_func]
