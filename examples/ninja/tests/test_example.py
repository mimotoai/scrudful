"""Tests of the example django-ninja project (``examples/ninja``).

They drive the real URLconf, models and API through Django's test clients.
Run them from the repository root with::

    PYTHONPATH=examples/ninja uv run pytest examples/ninja/tests -p no:randomly \\
        --ds=scrudful_ninja.settings
"""

import json
from datetime import datetime, timezone

import jsonschema
import pytest
from asgiref.sync import async_to_sync
from django.test import AsyncClient
from django.utils.http import http_date
from referencing import Registry, Resource
from ninja.errors import HttpError
from referencing.jsonschema import DRAFT202012
from roles.api import save_role
from roles.models import Role

from scrudful._adapter import allow_header, describe_options, options_links
from scrudful.collection import ENVELOPE_FIELDS, PAGE_FIELDS
from scrudful_ninja.api import api
from tests.semantic_http import link_problems, validate_http_schema_resource

pytestmark = pytest.mark.django_db

BASE = "http://testserver"
API = f"{BASE}/api"
ACME = {"X-Rbac-Context": "acme"}
GLOBEX = {"X-Rbac-Context": "globex"}
# Non-zero microseconds: the header is floored to whole seconds.
MODIFIED = datetime(2026, 10, 22, 21, 9, 56, 123456, tzinfo=timezone.utc)
MODIFIED_HTTP = "Thu, 22 Oct 2026 21:09:56 GMT"
PRECONDITION_REQUIRED = {
    "detail": "This request must be conditional.",
    "accepted-precondition-headers": ["If-Match", "If-Unmodified-Since"],
}
PRECONDITION_FAILED = {"detail": "Precondition failed."}
MISSING = 999_999


def links(name):
    """The Link header value of the documents ``name``."""
    return (
        f'<{API}/schemas/{name}>; rel="describedby"; type="application/schema+json", '
        f'<{API}/contexts/{name}>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"'
    )


def content(name):
    """The OPTIONS ``content`` of the documents ``name``."""
    return {
        "application/json": {
            "schema": {"$ref": f"{API}/schemas/{name}"},
            "x-rdf-context": {
                "href": f"{API}/contexts/{name}",
                "type": "application/ld+json",
            },
        }
    }


def exposed(response):
    value = response.headers.get("Access-Control-Expose-Headers", "")
    return {name.strip() for name in value.split(",") if name.strip()}


def detail(pk):
    return f"/api/roles/{pk}"


def make_role(name, rbac_context="acme"):
    role = Role.objects.create(name=name, rbac_context=rbac_context)
    Role.objects.filter(pk=role.pk).update(modified_at=MODIFIED)
    role.refresh_from_db()
    return role


@pytest.fixture
def role():
    return make_role("admin")


@pytest.fixture
def hidden():
    """A role in another rbac_context: acme may not see it."""
    return make_role("auditor", rbac_context="globex")


def put(client, pk, data, **headers):
    return client.put(
        detail(pk), data, content_type="application/json", headers={**ACME, **headers}
    )


def patch(client, pk, data, **headers):
    return client.patch(
        detail(pk), data, content_type="application/json", headers={**ACME, **headers}
    )


# --- Detail GET / HEAD ----------------------------------------------------------


def test_get(client, role):
    response = client.get(detail(role.pk), headers=ACME)
    assert response.status_code == 200
    assert response.json() == {"id": role.pk, "name": "admin", "rbac_context": "acme"}
    assert response.headers["ETag"] == f'"{role.etag}"'
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    assert response.headers["Link"] == links("role")
    assert {"ETag", "Link"} <= exposed(response)


def test_get_not_modified(client, role):
    for headers in (
        {"If-None-Match": f'"{role.etag}"'},
        {"If-Modified-Since": MODIFIED_HTTP},
    ):
        response = client.get(detail(role.pk), headers={**ACME, **headers})
        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["ETag"] == f'"{role.etag}"'
        assert response.headers["Last-Modified"] == MODIFIED_HTTP


def test_head(client, role):
    response = client.head(detail(role.pk), headers=ACME)
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["ETag"] == f'"{role.etag}"'
    response = client.head(
        detail(role.pk), headers={**ACME, "If-None-Match": f'"{role.etag}"'}
    )
    assert response.status_code == 304


# --- Authentication and scoping (the rule: nothing before authorization) --------


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "if_headers",
    [{}, {"If-Match": "*"}, {"If-None-Match": "*"}, {"If-Match": '"stale"'}],
)
def test_unauthenticated_is_401_before_load(client, role, method, if_headers):
    if_headers = {**if_headers}
    if method == "get" and "If-Match" not in if_headers:
        if_headers.setdefault("If-None-Match", f'"{role.etag}"')
    response = getattr(client, method)(
        detail(role.pk), content_type="application/json", headers=if_headers
    )
    assert response.status_code == 401
    assert "ETag" not in response.headers
    assert "Last-Modified" not in response.headers


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "if_headers",
    [{}, {"If-Match": "*"}, {"If-None-Match": "*"}, {"If-Match": '"stale"'}],
)
def test_missing_and_hidden_are_404(client, hidden, method, if_headers):
    """load raises 404 for both ("No PUT-create"): they are indistinguishable."""
    data = {"name": "x", "rbac_context": "acme"}
    for pk in (hidden.pk, MISSING):
        response = getattr(client, method)(
            detail(pk),
            data,
            content_type="application/json",
            headers={**ACME, **if_headers},
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        assert "ETag" not in response.headers
        assert "Last-Modified" not in response.headers
    hidden.refresh_from_db()
    assert hidden.name == "auditor"


# --- Writes and error Links -------------------------------------------------------


@pytest.mark.parametrize("write", [put, patch])
def test_write_requires_a_precondition(client, role, write):
    response = write(client, role.pk, {"name": "x", "rbac_context": "acme"})
    assert response.status_code == 428
    assert response.json() == PRECONDITION_REQUIRED
    assert response.headers["Link"] == links("error")
    assert "Link" in exposed(response)


def test_delete_requires_a_precondition(client, role):
    response = client.delete(detail(role.pk), headers=ACME)
    assert response.status_code == 428
    assert response.headers["Link"] == links("error")
    assert Role.objects.filter(pk=role.pk).exists()


def test_stale_write_is_412(client, role):
    response = put(
        client, role.pk, {"name": "x", "rbac_context": "acme"}, **{"If-Match": '"old"'}
    )
    assert response.status_code == 412
    assert response.json() == PRECONDITION_FAILED
    assert response.headers["Link"] == links("error")
    role.refresh_from_db()
    assert role.name == "admin"


def test_malformed_if_match_is_400(client, role):
    response = put(
        client, role.pk, {"name": "x", "rbac_context": "acme"}, **{"If-Match": "nope"}
    )
    assert response.status_code == 400
    assert response.headers["Link"] == links("error")


def test_put(client, role):
    response = put(
        client,
        role.pk,
        {"name": "owner", "rbac_context": "acme"},
        **{"If-Match": f'"{role.etag}"'},
    )
    assert response.status_code == 200
    assert response.json() == {"id": role.pk, "name": "owner", "rbac_context": "acme"}
    updated = Role.objects.get(pk=role.pk)
    # Fresh validators: they describe the representation just written.
    assert updated.etag != role.etag
    assert response.headers["ETag"] == f'"{updated.etag}"'
    assert response.headers["Last-Modified"] == http_date(
        updated.modified_at.timestamp()
    )
    assert response.headers["Link"] == links("role")


def test_put_with_if_unmodified_since(client, role):
    response = put(
        client,
        role.pk,
        {"name": "owner", "rbac_context": "acme"},
        **{"If-Unmodified-Since": MODIFIED_HTTP},
    )
    assert response.status_code == 200


def test_put_into_another_context_is_403(client, role):
    """Authorization of the body: the new rbac_context must be the caller's."""
    response = put(
        client,
        role.pk,
        {"name": "owner", "rbac_context": "globex"},
        **{"If-Match": f'"{role.etag}"'},
    )
    assert response.status_code == 403
    assert "ETag" not in response.headers
    assert response.headers["Link"] == links("error")
    role.refresh_from_db()
    assert role.rbac_context == "acme"


def test_patch(client, role):
    response = patch(
        client, role.pk, {"name": "owner"}, **{"If-Match": f'"{role.etag}"'}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "owner"
    updated = Role.objects.get(pk=role.pk)
    assert response.headers["ETag"] == f'"{updated.etag}"' != f'"{role.etag}"'


def test_patch_rejects_null(client, role):
    response = patch(client, role.pk, {"name": None}, **{"If-Match": f'"{role.etag}"'})
    assert response.status_code == 422


def test_delete(client, role):
    response = client.delete(
        detail(role.pk), headers={**ACME, "If-Match": f'"{role.etag}"'}
    )
    assert response.status_code == 204
    assert "ETag" not in response.headers
    assert "Link" not in response.headers  # no body, no Links
    assert not Role.objects.filter(pk=role.pk).exists()


def test_lost_update_is_caught_by_the_conditional_write(role):
    """A write between the precondition check and the handler's UPDATE: the
    handler writes only if the row still has the ETag the client matched."""
    Role.objects.filter(pk=role.pk).update(etag="concurrent")
    with pytest.raises(HttpError) as raised:
        save_role(role, name="owner")  # role still has the old etag
    assert raised.value.status_code == 412
    assert Role.objects.get(pk=role.pk).name == "admin"


# --- Create ---------------------------------------------------------------------


def test_create(client):
    response = client.post(
        "/api/roles",
        {"name": "viewer", "rbac_context": "acme"},
        content_type="application/json",
        headers=ACME,
    )
    assert response.status_code == 201
    created = Role.objects.get(name="viewer")
    assert response.json() == {
        "id": created.pk,
        "name": "viewer",
        "rbac_context": "acme",
    }
    assert response.headers["Location"] == f"{API}/roles/{created.pk}"
    assert response.headers["ETag"] == f'"{created.etag}"'
    assert response.headers["Last-Modified"] == http_date(
        created.modified_at.timestamp()
    )
    assert response.headers["Link"] == links("role")
    assert {"ETag", "Link", "Location"} <= exposed(response)

    # The 201's validators let the client edit right away, without a GET.
    response = put(
        client,
        created.pk,
        {"name": "reader", "rbac_context": "acme"},
        **{"If-Match": response.headers["ETag"]},
    )
    assert response.status_code == 200


def test_create_in_another_context_is_403(client):
    response = client.post(
        "/api/roles",
        {"name": "viewer", "rbac_context": "globex"},
        content_type="application/json",
        headers=ACME,
    )
    assert response.status_code == 403
    assert "Location" not in response.headers
    assert "ETag" not in response.headers
    assert not Role.objects.exists()


def test_create_unauthenticated(client):
    response = client.post(
        "/api/roles",
        {"name": "viewer", "rbac_context": "acme"},
        content_type="application/json",
    )
    assert response.status_code == 401


# --- General POST (search), an async operation ----------------------------------


def search_body():
    return {"name": "adm"}


def test_search(client, role, hidden):
    make_role("administrator")
    make_role("viewer")
    response = client.post(
        "/api/roles/search",
        search_body(),
        content_type="application/json",
        headers=ACME,
    )
    assert response.status_code == 200
    assert [found["name"] for found in response.json()] == ["admin", "administrator"]
    assert response.headers["Link"] == links("role-search-result")
    assert "ETag" not in response.headers
    assert "Location" not in response.headers


def test_search_with_the_async_client(role, hidden):
    @async_to_sync
    async def search():
        return await AsyncClient().post(
            "/api/roles/search",
            search_body(),
            content_type="application/json",
            headers=ACME,
        )

    response = search()
    assert response.status_code == 200
    assert [found["id"] for found in response.json()] == [role.pk]
    assert response.headers["Link"] == links("role-search-result")


def test_search_unauthenticated(client):
    response = client.post(
        "/api/roles/search", search_body(), content_type="application/json"
    )
    assert response.status_code == 401


# --- List -----------------------------------------------------------------------


def test_list(client, role, hidden):
    other = make_role("viewer")
    response = client.get("/api/roles", headers=ACME)
    assert response.status_code == 200
    assert response.headers["Link"] == links("roles")
    body = response.json()
    assert list(body) == list(PAGE_FIELDS)
    # Only the authorized roles are counted and listed.
    assert body["count"] == 2
    assert body["page_count"] == 1
    assert body["first"] == body["last"] == f"{API}/roles?page=1"
    assert body["previous"] is None and body["next"] is None
    for entry, expected in zip(body["content"], [role, other], strict=True):
        assert list(entry) == list(ENVELOPE_FIELDS)
        assert entry["href"] == f"{API}/roles/{expected.pk}"
        assert entry["content"] == {
            "id": expected.pk,
            "name": expected.name,
            "rbac_context": "acme",
        }
        item = client.get(entry["href"], headers=ACME)
        assert entry["etag"] == item.headers["ETag"]
        assert entry["last_modified"] == item.headers["Last-Modified"]
        assert entry["link"] == item.headers["Link"]


def test_list_pagination(client):
    roles = [make_role(f"role {n}") for n in range(3)]
    body = client.get("/api/roles?page_size=2", headers=ACME).json()
    assert body["count"] == 3
    assert body["page_count"] == 2
    assert body["next"] == f"{API}/roles?page=2&page_size=2"
    assert body["last"] == f"{API}/roles?page=2&page_size=2"
    body = client.get(body["next"], headers=ACME).json()
    assert body["previous"] == f"{API}/roles?page=1&page_size=2"
    assert [entry["content"]["id"] for entry in body["content"]] == [roles[2].pk]

    assert client.get("/api/roles?page=3&page_size=2", headers=ACME).status_code == 404
    # page_size is at most 100 in this API.
    assert client.get("/api/roles?page_size=101", headers=ACME).status_code == 422


def test_list_validators(client, role):
    response = client.get("/api/roles", headers=ACME)
    etag = response.headers["ETag"]
    assert response.headers["Last-Modified"] == MODIFIED_HTTP
    response = client.get("/api/roles", headers={**ACME, "If-None-Match": etag})
    assert response.status_code == 304
    assert response.headers["ETag"] == etag

    # Any change to an authorized role changes the collection's ETag; changes
    # in another rbac_context don't.
    make_role("auditor", rbac_context="globex")
    assert client.get("/api/roles", headers=ACME).headers["ETag"] == etag
    make_role("viewer")
    assert client.get("/api/roles", headers=ACME).headers["ETag"] != etag


def test_list_unauthenticated(client):
    assert client.get("/api/roles").status_code == 401


# --- OPTIONS --------------------------------------------------------------------


def options(client, path, **headers):
    return client.options(path, headers=headers)


def test_options_item(client, role):
    response = options(client, detail(role.pk))
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Link"] == options_links()
    body = response.json()
    validate_http_schema_resource(body)
    assert list(body) == ["get", "put", "delete", "options", "head", "patch"]
    assert response.headers["Allow"] == "GET, PUT, DELETE, OPTIONS, HEAD, PATCH"
    assert response.headers["Allow"] == allow_header(body)
    assert body["options"] == describe_options()
    assert body["get"]["responses"]["200"]["content"] == content("role")
    assert set(body["get"]["responses"]) == {"200", "304", "400", "412"}
    # HEAD is described like GET, without content: its responses have none.
    assert set(body["head"]["responses"]) == set(body["get"]["responses"])
    for response_ in body["head"]["responses"].values():
        assert "content" not in response_
    # The request bodies: PUT a whole role, PATCH a partial one.
    assert body["put"]["requestBody"]["content"] == content("role")
    assert body["patch"]["requestBody"]["content"] == content("role-patch")
    assert body["put"]["responses"]["403"] == {
        "description": "Forbidden",
        "content": content("error"),
    }
    # scrudful's own error bodies carry the error entity.
    for method in ("put", "patch", "delete"):
        responses = body[method]["responses"]
        for status in ("400", "412", "428"):
            assert responses[status]["content"] == content("error")
        assert [p["name"] for p in body[method]["parameters"]] == [
            "If-Match",
            "If-Unmodified-Since",
        ]


def test_options_is_the_same_for_everyone(client, role, hidden):
    """No auth, no load: a missing role and a hidden one are described alike."""
    expected = options(client, detail(role.pk)).json()
    assert options(client, detail(role.pk), **ACME).json() == expected
    assert options(client, detail(hidden.pk)).json() == expected
    assert options(client, detail(MISSING)).json() == expected


def test_options_collection(client):
    response = options(client, "/api/roles")
    body = response.json()
    validate_http_schema_resource(body)
    assert list(body) == ["get", "post", "options"]
    assert response.headers["Allow"] == "GET, POST, OPTIONS"
    assert body["get"]["responses"]["200"]["content"] == content("roles")
    assert "304" in body["get"]["responses"]
    assert body["post"]["requestBody"]["content"] == content("role")
    assert body["post"]["responses"]["201"]["content"] == content("role")
    assert body["post"]["responses"]["403"]["content"] == content("error")


def test_options_search(client):
    body = options(client, "/api/roles/search").json()
    validate_http_schema_resource(body)
    assert list(body) == ["post", "options"]
    assert body["post"]["requestBody"]["content"] == content("role-search")
    assert body["post"]["responses"]["200"]["content"] == content("role-search-result")
    # A search answers 200, not a create's 201.
    assert set(body["post"]["responses"]) == {"200"}


def test_responses_agree_with_options(client, role):
    """Every response's Links are the entry of the HTTP Schema Resource."""
    item = options(client, detail(role.pk)).json()
    collection = options(client, "/api/roles").json()
    data = {"name": "owner", "rbac_context": "acme"}
    current = {"If-Match": f'"{role.etag}"'}
    stale = {"If-Match": '"stale"'}
    responses = [
        ("GET", client.get(detail(role.pk), headers=ACME), item),
        ("HEAD", client.head(detail(role.pk), headers=ACME), item),
        ("GET", client.get("/api/roles", headers=ACME), collection),
        ("PUT", put(client, role.pk, data, **stale), item),
        ("PUT", put(client, role.pk, data), item),
        ("PUT", put(client, role.pk, data, **{"If-Match": "nope"}), item),
        ("PUT", put(client, role.pk, {**data, "rbac_context": "x"}, **current), item),
        ("PUT", put(client, role.pk, data, **current), item),
    ]
    role.refresh_from_db()
    current = {"If-Match": f'"{role.etag}"'}
    responses += [
        ("PATCH", patch(client, role.pk, {"name": "x"}, **current), item),
        (
            "POST",
            client.post(
                "/api/roles",
                {"name": "viewer", "rbac_context": "acme"},
                content_type="application/json",
                headers=ACME,
            ),
            collection,
        ),
    ]
    statuses = [response.status_code for _, response, _ in responses]
    assert statuses == [200, 200, 200, 412, 428, 400, 403, 200, 200, 201]
    for method, response, hsr in responses:
        has_content = bool(response.content)
        assert has_content == (method != "HEAD")
        problems = link_problems(
            method, response.status_code, response.headers, has_content, hsr=hsr
        )
        assert problems == [], (method, response.status_code)


def test_cors_preflight_is_not_broken(client, role):
    """A preflight that reaches the view (no CORS middleware here) gets the
    description; django-cors-headers would answer it before the view."""
    response = options(
        client,
        detail(role.pk),
        Origin="https://app.example.com",
        **{"Access-Control-Request-Method": "PUT"},
    )
    assert response.status_code == 200
    assert response.json() == options(client, detail(role.pk)).json()


# --- The documents the Links point to --------------------------------------------


def document(client, kind, name):
    response = client.get(f"/api/{kind}/{name}")
    assert response.status_code == 200
    return response.json()


def test_documents_describe_the_responses(client, role):
    roles_schema = document(client, "schemas", "roles")
    role_schema = document(client, "schemas", "role")
    jsonschema.Draft202012Validator.check_schema(roles_schema)
    registry = Registry().with_resource(
        f"{API}/schemas/role",
        Resource.from_contents(role_schema, default_specification=DRAFT202012),
    )
    validator = jsonschema.Draft202012Validator(
        roles_schema,
        registry=registry,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    validator.validate(client.get("/api/roles", headers=ACME).json())
    jsonschema.validate(
        client.get(detail(role.pk), headers=ACME).json(),
        role_schema,
    )

    context = document(client, "contexts", "roles")
    assert context["@context"]["content"]["@container"] == "@list"
    for name in ("role", "role-patch", "role-search", "role-search-result", "error"):
        document(client, "schemas", name)
        document(client, "contexts", name)
    assert client.get("/api/schemas/nope").status_code == 404


# --- OpenAPI ----------------------------------------------------------------------


def test_openapi():
    # As served (JSON): the status keys are strings.
    schema = json.loads(json.dumps(api.get_openapi_schema()))
    paths = schema["paths"]
    detail_path = paths["/api/roles/{pk}"]
    assert {"304", "400", "412"} <= set(detail_path["get"]["responses"])
    assert {"400", "412", "428"} <= set(detail_path["put"]["responses"])
    # The list's response schema is the typed envelope.
    listed = paths["/api/roles"]["get"]["responses"]["200"]
    ref = listed["content"]["application/json"]["schema"]["$ref"]
    page = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    assert list(page["properties"]) == list(PAGE_FIELDS)
    # Pagination parameters, no hidden handler parameters.
    names = {p["name"] for p in paths["/api/roles"]["get"]["parameters"]}
    assert names == {"page", "page_size"}
    for operation in detail_path.values():
        assert [p["name"] for p in operation["parameters"]] == ["pk"]
    # OPTIONS is not in the OpenAPI document.
    assert all("options" not in path for path in paths.values())
