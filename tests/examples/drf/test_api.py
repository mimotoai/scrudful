"""Tests of the example Django REST Framework project.

``examples/scrudful_rest_framework`` is on the pytest ``pythonpath`` and its
settings are the pytest-django settings, so these tests drive the real router,
``ModelViewSet``/``ReadOnlyModelViewSet``, model and serializer.
"""

from datetime import datetime, timezone

import pytest
from django.db import connection
from items import views
from items.models import Item
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

BASE = "http://testserver"
# Non-zero microseconds: the header is floored to whole seconds and
# conditional requests echoing it must still match (D-C1).
MODIFIED = datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
MODIFIED_HTTP = "Tue, 02 Jan 2024 03:04:05 GMT"
EARLIER_HTTP = "Tue, 02 Jan 2024 03:04:04 GMT"
SCHEMA_LINK = (
    f'<{BASE}/schemas/item.json>; rel="describedBy"; type="application/schema+json"'
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
    assert response["Link"] == LINKS
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

    def spy(request, pk, field):
        seen.append((field, connection.in_atomic_block))
        return item_value(request, pk, field)

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
    assert "Link" not in response
    assert response["Access-Control-Expose-Headers"] == "ETag"
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
    assert response["Link"] == SCHEMA_LINK
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
    assert set(client.options(f"/readonly-items/{item.pk}/").json()) == {"get"}
    assert set(client.options("/readonly-items/").json()) == {"get"}


# --- OPTIONS metadata ---------------------------------------------------------


def test_options_describes_the_detail_route(client, item):
    response = client.options(detail(item))
    assert response.status_code == 200
    metadata = response.json()
    assert set(metadata) == {"get", "put", "patch", "delete"}
    content = {
        "application/json": {
            "schema": f"{BASE}/schemas/item.json",
            "context": f"{BASE}/contexts/item.jsonld",
        }
    }
    assert metadata["get"]["responses"]["200"] == {
        "description": "OK",
        "content": content,
    }
    assert set(metadata["get"]["responses"]) == {"200", "304", "400", "412"}
    for method in ("put", "patch"):
        assert metadata[method]["requestBody"]["content"] == content
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
    assert set(metadata) == {"get", "post"}
    assert metadata["post"]["requestBody"]["description"] == (
        "The content for the resource to be created."
    )
    assert set(metadata["post"]["responses"]) == {"201"}
    assert "parameters" not in metadata["post"]
    assert set(metadata["get"]["responses"]) == {"200", "304", "400", "412"}


# --- Function view ------------------------------------------------------------


def test_stats_function_view(client, item):
    response = client.get("/stats/")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    etag = response["ETag"]
    assert etag == client.get("/items/")["ETag"]
    assert client.get("/stats/", headers={"If-None-Match": etag}).status_code == 304
    assert client.post("/stats/").status_code == 405
    assert set(client.options("/stats/").json()) == {"get"}
