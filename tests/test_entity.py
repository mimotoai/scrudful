"""The shared ``Entity`` and its media type."""

import pytest

from scrudful import fastapi, ninja, rest_framework
from scrudful.entity import Entity


def test_default_media_type():
    assert Entity().media_type == "application/json"


def test_media_type_normalized():
    entity = Entity(media_type="Application/Problem+JSON; Charset=UTF-8")
    assert entity.media_type == "application/problem+json; Charset=UTF-8"


def test_bad_media_type():
    with pytest.raises(ValueError, match="not a media type"):
        Entity(media_type="json")


def test_in_band_rejects_context():
    with pytest.raises(ValueError, match="takes no context link"):
        Entity(media_type="text/turtle", context_link_or_func="https://h/c")
    assert Entity(media_type="text/turtle", schema_link_or_func="https://h/s")


def test_other_requires_context_type():
    with pytest.raises(ValueError, match="give context_type_or_func"):
        Entity(media_type="text/csv", context_link_or_func="https://h/m")
    entity = Entity(
        media_type="text/csv",
        context_link_or_func="https://h/m",
        context_type_or_func="application/csvm+json",
    )
    assert entity.context_type_or_func == "application/csvm+json"


def test_reexported():
    for module in (ninja, rest_framework, fastapi):
        assert module.Entity is Entity
        assert "Entity" in module.__all__
