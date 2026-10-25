"""Unit tests of the framework-neutral page slicing and envelope assembly."""

import pytest
from django.http import QueryDict

from scrudful.collection import (
    ENVELOPE_FIELDS,
    PAGE_FIELDS,
    InvalidPage,
    envelope_entry,
    page_envelope,
)
from scrudful._django import page_url, slice_page


def test_slice_page_counts_and_slices():
    assert slice_page(list(range(5)), 2, 2) == (5, [2, 3])


def test_slice_page_last_partial_page():
    assert slice_page(list(range(5)), 3, 2) == (5, [4])


def test_slice_page_past_last_is_invalid():
    with pytest.raises(InvalidPage):
        slice_page(list(range(5)), 4, 2)


def test_slice_page_one_of_empty_exists():
    assert slice_page([], 1, 10) == (0, [])


def test_slice_page_two_of_empty_is_invalid():
    with pytest.raises(InvalidPage):
        slice_page([], 2, 10)


def test_page_url_sorts_query_and_replaces_page():
    query = QueryDict("z=1&page=9&a=2")
    assert (
        page_url("http://testserver/things?x=y#f", query, 3)
        == "http://testserver/things?a=2&page=3&z=1"
    )


def test_page_url_does_not_mutate_query():
    query = QueryDict("page=9")
    page_url("http://testserver/things", query, 1)
    assert query["page"] == "9"


def test_envelope_entry_has_envelope_fields():
    entry = envelope_entry(
        href="/a", etag='"e"', last_modified=None, link=None, content={"x": 1}
    )
    assert tuple(entry) == ENVELOPE_FIELDS
    assert entry["content"] == {"x": 1}
    assert entry["last_modified"] is None


def _envelope(count, number, size=2):
    return page_envelope(
        count=count,
        number=number,
        size=size,
        page_url=lambda n: f"http://testserver/things?page={n}",
        entries=[],
    )


def test_page_envelope_middle_page():
    page = _envelope(5, 2)
    assert tuple(page) == PAGE_FIELDS
    assert page["count"] == 5
    assert page["page_count"] == 3
    assert page["first"].endswith("?page=1")
    assert page["previous"].endswith("?page=1")
    assert page["next"].endswith("?page=3")
    assert page["last"].endswith("?page=3")


def test_page_envelope_ends_have_no_previous_or_next():
    first = _envelope(5, 1)
    assert first["previous"] is None
    assert first["next"].endswith("?page=2")
    last = _envelope(5, 3)
    assert last["next"] is None
    assert last["previous"].endswith("?page=2")


def test_page_envelope_empty_last_is_page_one():
    page = _envelope(0, 1)
    assert page["page_count"] == 0
    assert page["last"].endswith("?page=1")
    assert page["previous"] is None
    assert page["next"] is None
