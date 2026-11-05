"""scrudful accepts the ninja internals of each release it is verified
against, also those the installed release doesn't have.

Each fixture in ``fixtures/ninja_releases`` is one method of a release as it
ships it. It is loaded from a module of its own (a class holding that
method alone), whose source scrudful's check reads as it reads ninja's.
"""

import importlib.util
import pathlib
from typing import Any

import pytest

import scrudful.ninja

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ninja_releases"
RELEASES = sorted(FIXTURES.glob("*__*.txt"))


def load_method(fixture: pathlib.Path, tmp_path: pathlib.Path) -> tuple[str, Any]:
    """The guarded name (``"NinjaAPI._get_bound_routers"``) and the method."""
    _, name = fixture.stem.split("__")
    owner, method = name.split(".")
    module_path = tmp_path / f"release_{owner}_{method}.py"
    module_path.write_text(
        "from __future__ import annotations\n\n\n"
        f"class {owner}:\n" + fixture.read_text()
    )
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return name, getattr(getattr(module, owner), method)


def test_there_are_fixtures():
    assert RELEASES


@pytest.mark.parametrize("fixture", RELEASES, ids=lambda path: path.stem)
def test_a_verified_releases_source_is_ninjas(fixture, tmp_path):
    name, func = load_method(fixture, tmp_path)
    digest = scrudful.ninja._source_digest(func)
    assert digest is not None
    assert digest in scrudful.ninja._NINJA_SOURCES[name]


@pytest.mark.parametrize("fixture", RELEASES, ids=lambda path: path.stem)
def test_a_verified_releases_code_is_ninjas(fixture, tmp_path):
    """Where the source isn't installed: its names, locals and arguments."""
    name, func = load_method(fixture, tmp_path)
    code = func.__code__
    expected = scrudful.ninja._NINJA_CODE[name]
    fingerprints = expected if isinstance(expected, frozenset) else {expected}
    assert (code.co_names, code.co_varnames, code.co_argcount) in fingerprints
