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


def test_entity_rejects_schema_without_context():
    with pytest.raises(ValueError, match="x-rdf-context"):
        Entity(schema_link_or_func="https://x/s")


class Lazy:
    """A lazy URL, as Django's ``reverse_lazy`` gives: evaluating it (its
    truth, its text) is counted."""

    evaluated = 0

    def __bool__(self):
        Lazy.evaluated += 1
        return True

    def __str__(self):
        Lazy.evaluated += 1
        return "https://x/lazy"


def test_lazy_links_are_not_evaluated_when_made():
    """A lazy link counts as given without being evaluated, so an entity can
    name a URL of a URLconf not loaded yet."""
    Lazy.evaluated = 0
    entity = Entity(Lazy(), context_link_or_func=Lazy())
    assert Lazy.evaluated == 0
    # And a lazy schema link alone is refused, as a plain one is.
    with pytest.raises(ValueError, match="context"):
        Entity(Lazy())
    assert Lazy.evaluated == 0
    assert entity.context_link_or_func is not None
