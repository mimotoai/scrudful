"""Tests of the example Django REST Framework project.

``examples/scrudful_rest_framework`` is on the pytest ``pythonpath`` and its
settings are the pytest-django settings, so these tests drive the real router,
``ModelViewSet``/``ReadOnlyModelViewSet``, model and serializer.
"""

from datetime import datetime, timezone

import pytest
from django.db import connection
from django.urls import include, path
from items import views
from items.models import Item
from rest_framework.routers import SimpleRouter
from rest_framework.test import APIClient

from scrudful._adapter import allow_header, describe_options, options_links
from tests.semantic_http import (
    link_problems,
    parse_links,
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
    assert response["Link"] == LINKS
    assert response["Access-Control-Expose-Headers"] == "ETag, Link"


def test_echoed_etag_gives_304_with_validators(client, item):
    etag = client.get(detail(item))["ETag"]
    response = client.get(detail(item), headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""
    assert response["ETag"] == '"v1"'
    assert response["Last-Modified"] == MODIFIED_HTTP
    assert response["Access-Control-Expose-Headers"] == "ETag"


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
    assert response["Link"] == LINKS
    assert response["Access-Control-Expose-Headers"] == "ETag, Link"


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
    # A 204 has no content for the item's Links to describe.
    assert "Link" not in response
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
urlpatterns = [path("", include("items.urls")), path("", include(scoped_router.urls))]


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
    response = client.post(
        "/items/", {"name": "New"}, format="json", headers={"If-Match": '"x"'}
    )
    assert response.status_code == 201
    item = Item.objects.get(name="New")
    assert response["Location"] == f"{BASE}/items/{item.pk}/"
    assert "ETag" not in response
    assert "Last-Modified" not in response
    assert response["Link"] == LINKS
    assert response["Access-Control-Expose-Headers"] == "Link, Location"


def test_list_has_a_collection_etag(client, item):
    response = client.get("/items/")
    assert response.status_code == 200
    assert [entry["name"] for entry in response.json()] == ["One"]
    etag = response["ETag"]
    assert "Last-Modified" not in response
    assert response["Link"] == (
        f'<{BASE}/schemas/items.json>; rel="describedby"; '
        'type="application/schema+json", '
        f'<{BASE}/contexts/item.jsonld>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"'
    )
    assert response["Access-Control-Expose-Headers"] == "ETag, Link"
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
    assert response["Link"] == LINKS
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
        body = client.options(path_).json()
        validate_http_schema_resource(body)
        assert list(body) == ["get", "options", "head"]


# --- OPTIONS metadata ---------------------------------------------------------


def test_options_describes_the_detail_route(client, item):
    response = client.options(detail(item))
    assert response.status_code == 200
    metadata = response.json()
    validate_http_schema_resource(metadata)
    assert list(metadata) == ["get", "put", "delete", "options", "head", "patch"]
    assert metadata["get"]["responses"]["200"] == {
        "description": "OK",
        "content": ITEM_CONTENT,
    }
    assert set(metadata["get"]["responses"]) == {"200", "304", "400", "412"}
    assert set(metadata["head"]["responses"]) == {"200", "304", "400", "412"}
    for method in ("put", "patch"):
        assert metadata[method]["requestBody"]["content"] == ITEM_CONTENT
        assert set(metadata[method]["responses"]) == {"200", "400", "412", "428"}
    assert metadata["put"]["requestBody"]["description"] == (
        "The new content of the resource."
    )
    assert metadata["patch"]["requestBody"]["description"] == (
        "The changes to apply to the resource."
    )
    assert set(metadata["delete"]["responses"]) == {"204", "400", "412", "428"}
    for method in ("put", "patch", "delete"):
        names = [p["name"] for p in metadata[method]["parameters"]]
        assert names == ["If-Match", "If-Unmodified-Since"]
        # Either header satisfies the requirement, so neither is required.
        assert not any(p["required"] for p in metadata[method]["parameters"])


def test_options_describes_the_list_route(client):
    metadata = client.options("/items/").json()
    validate_http_schema_resource(metadata)
    assert list(metadata) == ["get", "post", "options", "head"]
    assert metadata["post"]["requestBody"]["description"] == (
        "The content for the resource to be created."
    )
    assert set(metadata["post"]["responses"]) == {"201", "400"}
    assert "parameters" not in metadata["post"]
    assert set(metadata["get"]["responses"]) == {"200", "304", "400", "412"}


@pytest.mark.parametrize(
    "path",
    ["/items/{pk}/", "/items/", "/readonly-items/{pk}/", "/readonly-items/", "/stats/"],
)
def test_options_valid_0_1(client, item, path):
    path = path.format(pk=item.pk)
    response = client.options(path)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    body = response.json()
    validate_http_schema_resource(body)
    assert response["Link"] == options_links()
    assert response["Allow"] == allow_header(body)
    assert body["options"] == describe_options()
    # The body lists exactly the methods the route serves, HEAD included.
    assert ("head" in body) == (client.head(path).status_code != 405)
    assert "head" in body
    assert link_problems("OPTIONS", 200, dict(response.items()), True, body) == []


def test_options_write_response_content(client, item):
    metadata = client.options(detail(item)).json()
    validate_http_schema_resource(metadata)
    for method in ("put", "patch"):
        assert metadata[method]["requestBody"]["content"] == ITEM_CONTENT
        assert metadata[method]["responses"]["200"]["content"] == ITEM_CONTENT
    listing = client.options("/items/").json()
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
    body = client.options("/stats/").json()
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
    ("options", ITEM, {}, 200),
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
    ("options", LIST, {}, 200),
    ("put", LIST, {}, 405),
    ("get", READONLY, {}, 200),
    ("get", READONLY, {"headers": NOT_MODIFIED}, 304),
    ("get", READONLY, {"headers": IF_MATCH_STALE}, 412),
    ("head", READONLY, {}, 200),
    ("head", READONLY, {"headers": NOT_MODIFIED}, 304),
    ("options", READONLY, {}, 200),
    ("get", "/readonly-items/999/", {}, 404),
    ("post", READONLY, {}, 405),
    ("get", READONLY_LIST, {}, 200),
    ("get", READONLY_LIST, {"headers": NOT_MODIFIED}, 304),
    ("head", READONLY_LIST, {}, 200),
    ("options", READONLY_LIST, {}, 200),
    ("post", READONLY_LIST, {**BODY}, 405),
    ("get", STATS, {}, 200),
    ("get", STATS, {"headers": NOT_MODIFIED}, 304),
    ("get", STATS, {"headers": IF_MATCH_STALE}, 412),
    ("head", STATS, {}, 200),
    ("head", STATS, {"headers": NOT_MODIFIED}, 304),
    ("options", STATS, {}, 200),
    ("post", STATS, {}, 405),
]
# Statuses with nothing in the OPTIONS bodies to agree with: DRF's 404 and 405
# are not listed, and a 304 (a HEAD's too) has no content, hence no entry. Their
# Links are still checked by the rules that don't need the body.
NO_ENTRY = {304, 404, 405}


@pytest.mark.parametrize(
    "method, path, arguments, status",
    CONFORMANCE,
    ids=[f"{m}-{p}-{s}-{i}" for i, (m, p, _, s) in enumerate(CONFORMANCE)],
)
def test_example_links_everywhere(client, item, method, path, arguments, status):
    """Every response, a HEAD included, follows the Semantic HTTP 0.1 Link rules
    and agrees with the OPTIONS body of its path."""
    path = path.format(pk=item.pk)
    hsr = client.options(path).json()
    response = getattr(client, method)(path, **arguments)
    assert response.status_code == status
    content = method != "head" and bool(response.content)
    assert content == (status not in (204, 304) and method != "head")
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
