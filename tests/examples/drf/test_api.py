"""Tests of the example Django REST Framework project.

``examples/scrudful_rest_framework`` is on the pytest ``pythonpath`` and its
settings are the pytest-django settings, so these tests drive the real router,
``ModelViewSet``/``ReadOnlyModelViewSet``, model and serializer.
"""

from datetime import datetime, timezone

import django.test
import pytest
from django.db import connection
from django.urls import include, path, resolve
from items import views
from items.models import Item
from rest_framework.routers import SimpleRouter
from rest_framework.test import APIClient

from scrudful._hsr import allow_value, hsr_link, options_operation
from scrudful.rest_framework import hsr_urls, http_schema_resource
from tests import test_wire_drf as wire
from tests.semantic_http import (
    hsr_get_problems,
    hsr_link_problems,
    link_problems,
    options_204_problems,
    parse_links,
    undescribed_problems,
    validate_http_schema_resource,
)

pytestmark = pytest.mark.django_db

BASE = "http://testserver"
# Non-zero microseconds: the header is floored to whole seconds and
# conditional requests echoing it must still match (D-C1).
MODIFIED = datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
MODIFIED_HTTP = "Tue, 02 Jan 2024 03:04:05 GMT"
EARLIER_HTTP = "Tue, 02 Jan 2024 03:04:04 GMT"
SCHEMA_LINK = (
    f'<{BASE}/schemas/item.json>; rel="describedby"; type="application/schema+json"'
)
CONTEXT_LINK = (
    f'<{BASE}/contexts/item.jsonld>; rel="http://www.w3.org/ns/json-ld#context"; '
    'type="application/ld+json"'
)
LINKS = f"{SCHEMA_LINK}, {CONTEXT_LINK}"
PRECONDITION_REQUIRED = {
    "detail": "This request must be conditional.",
    "accepted-precondition-headers": ["If-Match", "If-Unmodified-Since"],
}
PRECONDITION_FAILED = {"detail": "Precondition failed."}
ITEM_CONTENT = {
    "application/json": {
        "schema": {"$ref": f"{BASE}/schemas/item.json"},
        "x-rdf-context": {
            "href": f"{BASE}/contexts/item.jsonld",
            "type": "application/ld+json",
        },
    }
}


ERROR_CONTENT = {
    "application/json": {
        "schema": {"$ref": f"{BASE}/schemas/error.json"},
        "x-rdf-context": {
            "href": f"{BASE}/contexts/error.jsonld",
            "type": "application/ld+json",
        },
    }
}


@pytest.fixture(autouse=True)
def base_url(settings):
    """The example's ``SCRUDFUL["BASE_URL"]`` for APIClient's host (the
    suite's settings, ``tests/settings.py``, have no ``SCRUDFUL``)."""
    settings.SCRUDFUL = {"BASE_URL": f"{BASE}/"}


def hsr_of(client, path):
    """The HTTP Schema Resource of the route of ``path``, as the example's
    ``hsr_urls()`` route serves it (the target of its OPTIONS's HSR link)."""
    response = client.get(hsr_path(path))
    assert response.status_code == 200, response.content
    return response.json()


def hsr_path(path):
    """The path of the HTTP Schema Resource of the route of ``path``."""
    return f"/_hsr/{resolve(path).view_name}"


def with_hsr(links, path):
    """``links`` and the HTTP Schema Resource link that every response of the
    route of ``path`` carries."""
    return f"{links}, {hsr_link(BASE + hsr_path(path))}"


def options_problems(response, path, allow=None):
    """How the OPTIONS ``response`` of ``path`` breaks the rules: a 204
    without content, with ``Allow`` (listing the methods of ``allow``, an
    ``Allow`` value, when given) and one HTTP Schema Resource link, to the
    route of ``path``'s."""
    headers = dict(response.items())
    expected = None if allow is None else {m.strip() for m in allow.split(",")}
    return options_204_problems(
        response.status_code, headers, response.content, expected
    ) + hsr_link_problems(headers, f"{BASE}{hsr_path(path)}")


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def item():
    item = Item.objects.create(name="One", description="first")
    Item.objects.filter(pk=item.pk).update(updated_at=MODIFIED)
    item.refresh_from_db()
    return item


def detail(item_or_pk):
    pk = getattr(item_or_pk, "pk", item_or_pk)
    return f"/items/{pk}/"


# --- Conditional GET (test gap 1) ---------------------------------------------


def test_retrieve_sends_validators_links_and_expose_headers(client, item):
    response = client.get(detail(item))
    assert response.status_code == 200
    assert response.json()["name"] == "One"
    assert response["ETag"] == '"v1"'
    assert response["Last-Modified"] == MODIFIED_HTTP
    assert response["Link"] == with_hsr(LINKS, detail(item))
    assert response["Access-Control-Expose-Headers"] == "ETag, Link, Allow"


def test_echoed_etag_gives_304_with_validators(client, item):
    etag = client.get(detail(item))["ETag"]
    response = client.get(detail(item), headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""
    assert response["ETag"] == '"v1"'
    assert response["Last-Modified"] == MODIFIED_HTTP
    assert response["Access-Control-Expose-Headers"] == "ETag, Link, Allow"


def test_echoed_last_modified_gives_304(client, item):
    last_modified = client.get(detail(item))["Last-Modified"]
    response = client.get(detail(item), headers={"If-Modified-Since": last_modified})
    assert response.status_code == 304
    assert response["ETag"] == '"v1"'
    assert response["Last-Modified"] == MODIFIED_HTTP


def test_older_if_modified_since_gives_200(client, item):
    response = client.get(detail(item), headers={"If-Modified-Since": EARLIER_HTTP})
    assert response.status_code == 200
    assert response["ETag"] == '"v1"'


def test_head_is_conditional(client, item):
    response = client.head(detail(item))
    assert response.status_code == 200
    assert response["ETag"] == '"v1"'
    response = client.head(detail(item), headers={"If-None-Match": '"v1"'})
    assert response.status_code == 304
    assert response["ETag"] == '"v1"'


def test_if_match_on_get(client, item):
    assert client.get(detail(item), headers={"If-Match": '"v1"'}).status_code == 200
    response = client.get(detail(item), headers={"If-Match": '"v0"'})
    assert response.status_code == 412
    assert response.json() == PRECONDITION_FAILED


def test_malformed_if_none_match_gives_400(client, item):
    response = client.get(detail(item), headers={"If-None-Match": "v1"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Malformed If-None-Match header."}


# --- Conditional writes (test gaps 2 and 4) -----------------------------------


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_unconditional_write_gives_428(client, item, method):
    response = getattr(client, method)(detail(item), {"name": "Two"}, format="json")
    assert response.status_code == 428
    assert response.json() == PRECONDITION_REQUIRED
    item.refresh_from_db()
    assert item.version == 1


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_stale_if_match_gives_412(client, item, method):
    response = getattr(client, method)(
        detail(item), {"name": "Two"}, format="json", headers={"If-Match": '"v0"'}
    )
    assert response.status_code == 412
    assert response.json() == PRECONDITION_FAILED
    assert Item.objects.get(pk=item.pk).version == 1


def test_malformed_if_match_gives_400(client, item):
    response = client.put(
        detail(item), {"name": "Two"}, format="json", headers={"If-Match": "v1"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Malformed If-Match header."}


@pytest.mark.parametrize("if_match", ['"v1"', "*", '"v0", "v1"'])
def test_put_with_if_match_alone_succeeds(client, item, if_match):
    response = client.put(
        detail(item), {"name": "Two"}, format="json", headers={"If-Match": if_match}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Two"
    assert response.json()["version"] == 2


def test_put_with_if_unmodified_since_alone_succeeds(client, item):
    response = client.put(
        detail(item),
        {"name": "Two"},
        format="json",
        headers={"If-Unmodified-Since": MODIFIED_HTTP},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2


def test_put_with_old_if_unmodified_since_gives_412(client, item):
    response = client.put(
        detail(item),
        {"name": "Two"},
        format="json",
        headers={"If-Unmodified-Since": EARLIER_HTTP},
    )
    assert response.status_code == 412


def test_invalid_if_unmodified_since_does_not_satisfy_the_requirement(client, item):
    response = client.put(
        detail(item),
        {"name": "Two"},
        format="json",
        headers={"If-Unmodified-Since": "yesterday"},
    )
    assert response.status_code == 428


def test_if_none_match_star_on_put_gives_412(client, item):
    response = client.put(
        detail(item),
        {"name": "Two"},
        format="json",
        headers={"If-Match": '"v1"', "If-None-Match": "*"},
    )
    assert response.status_code == 412


def test_weak_if_match_never_matches(client, item):
    response = client.put(
        detail(item), {"name": "Two"}, format="json", headers={"If-Match": 'W/"v1"'}
    )
    assert response.status_code == 412


@pytest.mark.parametrize("method", ["put", "patch"])
def test_write_response_carries_the_new_validators(client, item, method):
    response = getattr(client, method)(
        detail(item), {"name": "Two"}, format="json", headers={"If-Match": '"v1"'}
    )
    assert response.status_code == 200
    assert response["ETag"] == '"v2"'
    after = client.get(detail(item))
    assert response["ETag"] == after["ETag"]
    assert response["Last-Modified"] == after["Last-Modified"]
    assert response["Last-Modified"] != MODIFIED_HTTP
    assert response["Link"] == with_hsr(LINKS, detail(item))
    assert response["Access-Control-Expose-Headers"] == "ETag, Link, Allow"


def test_edit_loop_reuses_the_write_response_etag(client, item):
    etag = client.get(detail(item))["ETag"]
    for name in ("Two", "Three", "Four"):
        response = client.put(
            detail(item), {"name": name}, format="json", headers={"If-Match": etag}
        )
        assert response.status_code == 200
        etag = response["ETag"]
    assert etag == '"v4"'


def test_patch_updates_only_the_given_fields(client, item):
    response = client.patch(
        detail(item),
        {"description": "changed"},
        format="json",
        headers={"If-Match": '"v1"'},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "One"
    assert response.json()["description"] == "changed"


def test_delete_with_if_match(client, item):
    response = client.delete(detail(item), headers={"If-Match": '"v1"'})
    assert response.status_code == 204
    assert "ETag" not in response
    assert "Last-Modified" not in response
    # A 204 has no content for the item's Links to describe: only the link
    # to its HTTP Schema Resource.
    assert response["Link"] == hsr_link(BASE + hsr_path(detail(item)))
    assert not Item.objects.filter(pk=item.pk).exists()


def test_delete_with_if_unmodified_since(client, item):
    response = client.delete(
        detail(item), headers={"If-Unmodified-Since": MODIFIED_HTTP}
    )
    assert response.status_code == 204


@pytest.mark.django_db(transaction=True)
def test_write_validators_run_in_the_request_transaction(client, item, monkeypatch):
    """ATOMIC_REQUESTS puts the precondition check and the write in one
    transaction, so the callbacks' select_for_update() lock holds until the
    write is committed."""
    seen = []
    item_value = views._item_value

    def spy(view, request, pk, field):
        seen.append((field, connection.in_atomic_block))
        return item_value(view, request, pk, field)

    monkeypatch.setattr(views, "_item_value", spy)
    response = client.put(
        detail(item), {"name": "Two"}, format="json", headers={"If-Match": '"v1"'}
    )
    assert response.status_code == 200
    # Before the write and again for the response's fresh validators.
    assert seen == [
        ("version", True),
        ("updated_at", True),
        ("version", True),
        ("updated_at", True),
    ]
    assert not connection.in_atomic_block


# --- Missing items (test gap 11) ----------------------------------------------


@pytest.mark.parametrize(
    "method, headers",
    [
        ("get", {}),
        ("get", {"If-Match": '"v1"'}),
        ("get", {"If-None-Match": "*"}),
        ("patch", {}),
        ("patch", {"If-Match": '"v1"'}),
        ("delete", {}),
        ("delete", {"If-Match": '"v1"'}),
        ("put", {}),
    ],
)
def test_missing_item_gives_404(client, method, headers):
    response = getattr(client, method)(
        detail(999), {"name": "x"}, format="json", headers=headers
    )
    assert response.status_code == 404


def test_put_with_if_match_on_a_missing_item_gives_412(client):
    """PUT can create, so its If-Match is evaluated (RFC 9110 §13.1.1)."""
    response = client.put(
        detail(999), {"name": "x"}, format="json", headers={"If-Match": '"v1"'}
    )
    assert response.status_code == 412


# --- Queryset scoping ---------------------------------------------------------


class ScopedItemViewSet(views.ItemViewSet):
    """Only shows items named "public" (e.g. per-tenant scoping)."""

    def get_queryset(self):
        return super().get_queryset().filter(name="public")


scoped_router = SimpleRouter()
scoped_router.register("scoped-items", ScopedItemViewSet, basename="scoped-item")
urlpatterns = [
    path("", include("items.urls")),
    path("", include(scoped_router.urls)),
    path("", include(hsr_urls())),
]


@pytest.mark.urls(__name__)
@pytest.mark.parametrize(
    "method, headers",
    [
        ("get", {}),
        ("get", {"If-None-Match": "*"}),
        ("get", {"If-Modified-Since": MODIFIED_HTTP}),
        ("put", {}),
        ("put", {"If-None-Match": "*"}),
        ("patch", {}),
        ("delete", {}),
        ("delete", {"If-Unmodified-Since": MODIFIED_HTTP}),
    ],
)
def test_callbacks_apply_the_view_queryset(client, item, method, headers):
    """An item hidden by get_queryset() is a 404 whatever the preconditions:
    the callbacks must not reveal its validators (304) or its existence
    (412/428)."""
    path_ = f"/scoped-items/{item.pk}/"
    response = getattr(client, method)(
        path_, {"name": "x"}, format="json", headers=headers
    )
    assert response.status_code == 404
    assert "ETag" not in response
    assert "Last-Modified" not in response
    assert Item.objects.get(pk=item.pk).name == "One"


@pytest.mark.urls(__name__)
def test_scoped_viewset_serves_visible_items(client):
    public = Item.objects.create(name="public")
    path_ = f"/scoped-items/{public.pk}/"
    response = client.get(path_)
    assert response.status_code == 200
    assert response["ETag"] == '"v1"'
    assert client.get(path_, headers={"If-None-Match": '"v1"'}).status_code == 304
    assert client.put(path_, {"name": "public"}, format="json").status_code == 428


# --- Collection and create ----------------------------------------------------


def test_create_exposes_location_and_sends_no_validators(client):
    response = client.post("/items/", {"name": "New"}, format="json")
    assert response.status_code == 201
    item = Item.objects.get(name="New")
    assert response["Location"] == f"{BASE}/items/{item.pk}/"
    assert "ETag" not in response
    assert "Last-Modified" not in response
    assert response["Link"] == with_hsr(LINKS, "/items/")
    assert response["Access-Control-Expose-Headers"] == "Link, Location, Allow"


def test_create_checks_the_preconditions_of_the_collection(client, item):
    etag = client.get("/items/")["ETag"]
    stale = client.post(
        "/items/", {"name": "New"}, format="json", headers={"If-Match": '"x"'}
    )
    assert stale.status_code == 412
    assert stale.json() == PRECONDITION_FAILED
    # The create's error entity, though the viewset has no object_validators.
    assert stale["Content-Type"] == "application/json"
    assert f'<{BASE}/schemas/error.json>; rel="describedby"' in stale["Link"]
    assert f"<{BASE}/contexts/error.jsonld>" in stale["Link"]
    assert not Item.objects.filter(name="New").exists()
    current = client.post(
        "/items/", {"name": "New"}, format="json", headers={"If-Match": etag}
    )
    assert current.status_code == 201


def test_list_has_a_collection_etag(client, item):
    response = client.get("/items/")
    assert response.status_code == 200
    assert [entry["name"] for entry in response.json()] == ["One"]
    etag = response["ETag"]
    assert "Last-Modified" not in response
    assert response["Link"] == with_hsr(
        f'<{BASE}/schemas/items.json>; rel="describedby"; '
        'type="application/schema+json", '
        f'<{BASE}/contexts/item.jsonld>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"',
        "/items/",
    )
    assert response["Access-Control-Expose-Headers"] == "ETag, Link, Allow"
    assert client.get("/items/", headers={"If-None-Match": etag}).status_code == 304

    client.post("/items/", {"name": "Two"}, format="json")
    response = client.get("/items/", headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert response["ETag"] != etag


def test_linked_schema_and_context_are_served(client, item):
    schema = client.get("/schemas/item.json")
    assert schema.status_code == 200
    assert schema["Content-Type"] == "application/schema+json"
    assert schema.json()["title"] == "Item"
    context = client.get("/contexts/item.jsonld")
    assert context.status_code == 200
    assert "@context" in context.json()


# --- Read-only viewset (test gap 5) -------------------------------------------


def test_readonly_viewset_reads(client, item):
    response = client.get(f"/readonly-items/{item.pk}/")
    assert response.status_code == 200
    assert response["ETag"] == '"v1"'
    assert response["Link"] == with_hsr(LINKS, f"/readonly-items/{item.pk}/")
    etag = client.get("/readonly-items/")["ETag"]
    response = client.get("/readonly-items/", headers={"If-None-Match": etag})
    assert response.status_code == 304


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/readonly-items/"),
        ("put", "/readonly-items/{pk}/"),
        ("patch", "/readonly-items/{pk}/"),
        ("delete", "/readonly-items/{pk}/"),
    ],
)
def test_readonly_viewset_rejects_writes_with_405(client, item, method, path):
    response = getattr(client, method)(
        path.format(pk=item.pk), {}, format="json", headers={"If-Match": '"v1"'}
    )
    assert response.status_code == 405


def test_readonly_viewset_options_lists_only_get(client, item):
    for path_ in (f"/readonly-items/{item.pk}/", "/readonly-items/"):
        body = hsr_of(client, path_)
        validate_http_schema_resource(body)
        assert list(body) == ["get", "options", "head"]
        response = client.options(path_)
        assert options_problems(response, path_, "GET, OPTIONS, HEAD") == []


# --- OPTIONS metadata ---------------------------------------------------------


def test_options_describes_the_detail_route(client, item):
    response = client.options(detail(item))
    assert options_problems(response, detail(item)) == []
    metadata = hsr_of(client, detail(item))
    validate_http_schema_resource(metadata)
    assert list(metadata) == ["get", "put", "delete", "options", "head", "patch"]
    assert metadata["get"]["responses"]["200"] == {
        "description": "OK",
        "content": ITEM_CONTENT,
    }
    read = {"200", "304", "400", "412", "default"}
    assert set(metadata["get"]["responses"]) == read
    assert set(metadata["head"]["responses"]) == read
    write = {"200", "400", "412", "428", "default"}
    for method in ("put", "patch"):
        assert metadata[method]["requestBody"]["content"] == ITEM_CONTENT
        assert set(metadata[method]["responses"]) == write
    assert metadata["put"]["requestBody"]["description"] == (
        "The new content of the resource."
    )
    assert metadata["patch"]["requestBody"]["description"] == (
        "The changes to apply to the resource."
    )
    assert set(metadata["delete"]["responses"]) == {
        "204",
        "400",
        "412",
        "428",
        "default",
    }
    # The errors the exception handler gives the error entity's Links.
    assert metadata["get"]["responses"]["default"] == {
        "description": "Error.",
        "content": ERROR_CONTENT,
    }
    for method in ("put", "patch", "delete"):
        names = [p["name"] for p in metadata[method]["parameters"]]
        assert names == ["If-Match", "If-Unmodified-Since"]
        # Either header satisfies the requirement, so neither is required.
        assert not any(p["required"] for p in metadata[method]["parameters"])


def test_options_describes_the_list_route(client):
    metadata = hsr_of(client, "/items/")
    validate_http_schema_resource(metadata)
    assert list(metadata) == ["get", "post", "options", "head"]
    assert metadata["post"]["requestBody"]["description"] == (
        "The content for the resource to be created."
    )
    # Its preconditions are evaluated against the list's ETag, never required.
    assert set(metadata["post"]["responses"]) == {"201", "400", "412", "default"}
    assert "parameters" not in metadata["post"]
    assert set(metadata["get"]["responses"]) == {"200", "304", "400", "412", "default"}


@pytest.mark.parametrize(
    "path",
    ["/items/{pk}/", "/items/", "/readonly-items/{pk}/", "/readonly-items/", "/stats/"],
)
def test_options_valid_0_1(client, item, path):
    path = path.format(pk=item.pk)
    response = client.options(path)
    body = hsr_of(client, path)
    validate_http_schema_resource(body)
    # The example has no permission classes: every method the HSR
    # describes is allowed to anyone.
    assert options_problems(response, path, allow_value(body)) == []
    assert response["Allow"] == allow_value(body)
    assert body["options"] == options_operation()
    # The HSR lists exactly the methods the route serves, HEAD included.
    assert ("head" in body) == (client.head(path).status_code != 405)
    assert "head" in body
    assert "HEAD" in response["Allow"]


def test_options_write_response_content(client, item):
    metadata = hsr_of(client, detail(item))
    validate_http_schema_resource(metadata)
    for method in ("put", "patch"):
        assert metadata[method]["requestBody"]["content"] == ITEM_CONTENT
        assert metadata[method]["responses"]["200"]["content"] == ITEM_CONTENT
    listing = hsr_of(client, "/items/")
    validate_http_schema_resource(listing)
    assert listing["post"]["requestBody"]["content"] == ITEM_CONTENT
    assert listing["post"]["responses"]["201"]["content"] == ITEM_CONTENT


# --- Function view ------------------------------------------------------------


def test_stats_function_view(client, item):
    response = client.get("/stats/")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    etag = response["ETag"]
    assert etag == client.get("/items/")["ETag"]
    assert client.get("/stats/", headers={"If-None-Match": etag}).status_code == 304
    assert client.post("/stats/").status_code == 405
    body = hsr_of(client, "/stats/")
    validate_http_schema_resource(body)
    assert list(body) == ["get", "options", "head"]


# --- Conformance of every response (Semantic HTTP 0.1) ------------------------

ITEM = "/items/{pk}/"
LIST = "/items/"
READONLY = "/readonly-items/{pk}/"
READONLY_LIST = "/readonly-items/"
STATS = "/stats/"
IF_MATCH_STALE = {"If-Match": '"v0"'}
IF_MATCH = {"If-Match": '"v1"'}
NOT_MODIFIED = {"If-None-Match": "*"}
MALFORMED = {"If-Match": "v1"}
BODY = {"data": {"name": "Two"}, "format": "json"}
INVALID = {"data": {}, "format": "json"}

# (method, path, request arguments, status).
CONFORMANCE = [
    ("get", ITEM, {}, 200),
    ("get", ITEM, {"headers": NOT_MODIFIED}, 304),
    ("get", ITEM, {"headers": IF_MATCH_STALE}, 412),
    ("get", ITEM, {"headers": MALFORMED | {"If-None-Match": "v1"}}, 400),
    ("head", ITEM, {}, 200),
    ("head", ITEM, {"headers": NOT_MODIFIED}, 304),
    ("put", ITEM, {**BODY, "headers": IF_MATCH}, 200),
    ("put", ITEM, {**BODY}, 428),
    ("put", ITEM, {**BODY, "headers": IF_MATCH_STALE}, 412),
    ("put", ITEM, {**BODY, "headers": MALFORMED}, 400),
    ("put", ITEM, {**INVALID, "headers": IF_MATCH}, 400),
    ("patch", ITEM, {**BODY, "headers": IF_MATCH}, 200),
    ("patch", ITEM, {"data": {"name": ""}, "format": "json", "headers": IF_MATCH}, 400),
    ("patch", ITEM, {**BODY}, 428),
    ("delete", ITEM, {"headers": IF_MATCH}, 204),
    ("delete", ITEM, {}, 428),
    ("delete", ITEM, {"headers": IF_MATCH_STALE}, 412),
    ("options", ITEM, {}, 204),
    ("get", "/items/999/", {}, 404),
    ("put", "/items/999/", {**BODY, "headers": IF_MATCH}, 412),
    ("post", ITEM, {}, 405),
    ("get", LIST, {}, 200),
    ("get", LIST, {"headers": NOT_MODIFIED}, 304),
    ("get", LIST, {"headers": IF_MATCH_STALE}, 412),
    ("head", LIST, {}, 200),
    ("head", LIST, {"headers": NOT_MODIFIED}, 304),
    ("post", LIST, {**BODY}, 201),
    ("post", LIST, {**INVALID}, 400),
    ("options", LIST, {}, 204),
    ("put", LIST, {}, 405),
    ("get", READONLY, {}, 200),
    ("get", READONLY, {"headers": NOT_MODIFIED}, 304),
    ("get", READONLY, {"headers": IF_MATCH_STALE}, 412),
    ("head", READONLY, {}, 200),
    ("head", READONLY, {"headers": NOT_MODIFIED}, 304),
    ("options", READONLY, {}, 204),
    ("get", "/readonly-items/999/", {}, 404),
    ("post", READONLY, {}, 405),
    ("get", READONLY_LIST, {}, 200),
    ("get", READONLY_LIST, {"headers": NOT_MODIFIED}, 304),
    ("head", READONLY_LIST, {}, 200),
    ("options", READONLY_LIST, {}, 204),
    ("post", READONLY_LIST, {**BODY}, 405),
    ("get", STATS, {}, 200),
    ("get", STATS, {"headers": NOT_MODIFIED}, 304),
    ("get", STATS, {"headers": IF_MATCH_STALE}, 412),
    ("head", STATS, {}, 200),
    ("head", STATS, {"headers": NOT_MODIFIED}, 304),
    ("options", STATS, {}, 204),
    ("post", STATS, {}, 405),
]
# Statuses with nothing in the HTTP Schema Resources to agree with: DRF's 404
# and 405 are not listed, and a 304 (a HEAD's too) has no content, hence no
# entry. Their Links are still checked by the rules that don't need the body.
NO_ENTRY = {304, 404, 405}


@pytest.mark.parametrize(
    "method, path, arguments, status",
    CONFORMANCE,
    ids=[f"{m}-{p}-{s}-{i}" for i, (m, p, _, s) in enumerate(CONFORMANCE)],
)
def test_example_links_everywhere(client, item, method, path, arguments, status):
    """Every response, a HEAD included, follows the Semantic HTTP 0.1 Link rules
    and agrees with the HTTP Schema Resource of its path; an OPTIONS is the
    204 with the HSR link."""
    path = path.format(pk=item.pk)
    hsr = hsr_of(client, path)
    response = getattr(client, method)(path, **arguments)
    assert response.status_code == status
    content = method != "head" and bool(response.content)
    assert content == (status not in (204, 304) and method != "head")
    if method == "options":
        assert options_problems(response, path, allow_value(hsr)) == []
        return
    problems = link_problems(
        method.upper(),
        status,
        dict(response.items()),
        content,
        None if status in NO_ENTRY else hsr,
    )
    assert problems == []
    if status == 405:
        # The Links are still those of the error entity.
        assert f'<{BASE}/schemas/error.json>; rel="describedby"' in response["Link"]


@pytest.mark.parametrize("path", [ITEM, LIST, READONLY, READONLY_LIST, STATS])
def test_example_links_are_served_with_the_documents(client, item, path):
    """Every document a Link points at is served."""
    path = path.format(pk=item.pk)
    links = parse_links(client.get(path)["Link"])
    links += parse_links(client.put(path, **BODY, headers=IF_MATCH).get("Link"))
    for target, _ in links:
        assert client.get(target.removeprefix(BASE)).status_code == 200


# --- Conformance settings and routes ----------------------------------------


def test_format_suffix_routes(client, item):
    """The router's ``.json`` routes: the callables take DRF's ``format`` too."""
    for url in (f"/items/{item.pk}.json", "/items.json"):
        hsr = hsr_of(client, url)
        validate_http_schema_resource(hsr)
        response = client.get(url)
        assert response.status_code == 200, url
        assert "ETag" in response
        assert link_problems("GET", 200, dict(response.items()), True, hsr) == []
    response = client.get(f"/items/{item.pk}.json")
    assert response["Link"] == with_hsr(LINKS, f"/items/{item.pk}.json")
    response = client.put(f"/items/{item.pk}.json", {"name": "Two"}, format="json")
    assert response.status_code == 428


def test_format_query_parameter_stays_out_of_the_links(client, item):
    """``?format=json`` is negotiation, not part of the linked documents' URLs."""
    response = client.get(detail(item) + "?format=json")
    assert response.status_code == 200
    assert response["Link"] == with_hsr(LINKS, detail(item))
    response = client.put(detail(item) + "?format=json", {"name": "Two"}, format="json")
    assert response.status_code == 428
    for target, _ in parse_links(response["Link"]):
        assert "?" not in target
        assert client.get(target.removeprefix(BASE)).status_code == 200


def test_only_json_is_served_and_accepted(client, item):
    """No browsable API, and no form bodies, which OPTIONS can't describe."""
    response = client.get(detail(item), headers={"Accept": "text/html"})
    assert response.status_code == 406
    response = client.post("/items/", {"name": "Two"}, format="multipart")
    assert response.status_code == 415
    response = client.post(
        "/items/", "name=Two", content_type="application/x-www-form-urlencoded"
    )
    assert response.status_code == 415
    hsr = hsr_of(client, "/items/")
    assert list(hsr["post"]["requestBody"]["content"]) == ["application/json"]


@pytest.mark.parametrize(
    "view_name, path",
    [
        ("item-detail", "/items/1/"),
        ("item-list", "/items/"),
        ("readonly-item-detail", "/readonly-items/1/"),
        ("readonly-item-list", "/readonly-items/"),
        ("item-stats", "/stats/"),
    ],
)
def test_hsr_urls_serve_every_view(client, view_name, path):
    """The example's URLconf includes ``hsr_urls()``."""
    response = client.get(f"/_hsr/{view_name}")
    headers = dict(response.items())
    assert hsr_get_problems(response.status_code, headers, response.content) == []
    assert response.json() == http_schema_resource(resolve(path).func)


# --- The wire-conformance suite, on the example ----------------------------------
#
# The exchanges of tests/test_wire_drf.py with the example project, through the
# example's own URLconf (its hsr_urls() include and handler404 / handler500),
# with DEBUG off, checked as that suite checks them (tests/semantic_http.py).


@pytest.fixture
def wire_client(settings):
    settings.DEBUG = False
    return django.test.Client(raise_request_exception=False)


@pytest.mark.parametrize("exchange", wire.example_exchanges(), ids=lambda e: e.id)
def test_wire_exchanges(wire_client, exchange):
    """Every response has its content Links and the HSR link of its route,
    and agrees with its HSR entry."""
    world = wire.World()
    response = wire.send(wire_client, world, exchange)
    headers = wire.headers_of(response)
    content = wire.has_content(exchange, response)
    hsr = None
    if exchange.status not in (304, 405):
        hsr = wire.hsr_of(wire_client, exchange.kind)
    problems = link_problems(exchange.method, exchange.status, headers, content, hsr)
    assert problems == []
    assert hsr_link_problems(headers, wire.target(exchange.kind)) == []


@pytest.mark.parametrize(
    "kind, url, allow",
    [
        ("item", "/items/{one}/", wire.ITEM_METHODS),
        ("items", "/items/", {"GET", "HEAD", "POST", "OPTIONS"}),
        ("readonly", "/readonly-items/{one}/", wire.READ),
        ("readonly-list", "/readonly-items/", wire.READ),
        ("stats", "/stats/", wire.READ),
    ],
)
def test_wire_options_and_hsr(wire_client, kind, url, allow):
    """The OPTIONS 204 (its ``Allow`` and HSR link) and the HSR route."""
    response = wire_client.options(wire.World().format(url))
    headers = wire.headers_of(response)
    problems = options_204_problems(
        response.status_code, headers, response.content, allow
    )
    assert problems == []
    assert hsr_link_problems(headers, wire.target(kind)) == []
    document = wire_client.get(wire.HSR_ROUTES[kind])
    headers = wire.headers_of(document)
    assert hsr_get_problems(document.status_code, headers, document.content) == []


@pytest.mark.parametrize("accept", [None, "application/json"], ids=["absent", "json"])
@pytest.mark.parametrize(
    "method, url",
    [
        ("GET", "/nope/"),
        ("GET", "/items/abc/"),
        ("GET", "/items/{missing}/"),
        ("TRACE", "/items/{one}/"),
        ("GET", "/_hsr/nope"),
        ("POST", "/_hsr/item-detail"),
    ],
)
def test_wire_no_undescribed_content(wire_client, accept, method, url):
    """No error is content the HSR doesn't describe, for an absent ``Accept``
    or ``Accept: application/json``: the example's handler404 answers a URL
    no pattern matches with JSON. The HSR route's own 404, for a name that
    is no scrudful route's, is ``{"detail": "Not found."}`` without Links:
    it is no resource's, and its URL is no HSR link's target."""
    hsr = wire.hsr_of(wire_client, "item")
    headers = {} if accept is None else {"Accept": accept}
    url = wire.World().format(url)
    response = wire_client.generic(method, url, headers=headers)
    assert response.status_code >= 400
    response_headers = wire.headers_of(response)
    assert undescribed_problems(accept, response_headers, hsr) == []
    if url != "/_hsr/nope":
        content = bool(response.content)
        status = response.status_code
        assert link_problems(method, status, response_headers, content) == []


def test_wire_django_404_is_html_only_when_preferred(wire_client):
    """The example's handler404: Django's HTML page for a browser, with
    ``Vary: Accept``; JSON with the error Links otherwise."""
    response = wire_client.get("/nope/", headers={"Accept": wire.BROWSER})
    assert response["Content-Type"].startswith("text/html")
    assert "Accept" in response["Vary"]
    response = wire_client.get("/nope/")
    assert response["Content-Type"] == "application/json"
    assert "Accept" in response["Vary"]
    assert link_problems("GET", 404, wire.headers_of(response), True) == []
