"""Tests for scripts/release.py, the CI release checks."""

import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

pytest.importorskip("tomllib")  # the script needs Python 3.11+ (CI runs it on 3.12)
from packaging.version import Version  # noqa: E402

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
_spec = importlib.util.spec_from_file_location("release_script", _PATH)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "error", {}, io.BytesIO())


def pyproject(version="1.0.0", deps=(), dev=("pytest",), tool=None):
    return {
        "project": {"name": "scrudful", "version": version, "dependencies": list(deps)},
        "dependency-groups": {"dev": list(dev)},
        "tool": tool or {"uv": {"build-backend": {"module-root": ""}}},
        "build-system": {"requires": ["uv_build"], "build-backend": "uv_build"},
    }


# --- bump_required -----------------------------------------------------------


@pytest.mark.parametrize(
    "paths, expected",
    [
        (["scrudful/fastapi.py"], True),
        (["scrudful/__init__.py", "docs/index.rst"], True),
        (["docs/source/index.rst", "README.rst", ".gitlab-ci.yml"], False),
        (["tests/test_conditional.py", "examples/fastapi/api.py"], False),
        (["scripts/release.py", "uv.lock"], False),
        ([], False),
    ],
)
def test_bump_required_by_paths(paths, expected):
    assert release.bump_required(paths, pyproject(), pyproject()) is expected


def test_bump_required_by_published_metadata():
    old = pyproject()
    assert release.bump_required(["pyproject.toml"], old, pyproject(deps=["x"]))
    changed = pyproject()
    changed["project"]["description"] = "new"
    assert release.bump_required(["pyproject.toml"], old, changed)
    changed = pyproject()
    changed["build-system"]["requires"] = ["uv_build>=1"]
    assert release.bump_required(["pyproject.toml"], old, changed)


def test_dev_only_pyproject_changes_need_no_bump():
    old = pyproject()
    assert not release.bump_required(["pyproject.toml"], old, pyproject(dev=["x"]))
    assert not release.bump_required(
        ["pyproject.toml"], old, pyproject(tool={"pytest": {}})
    )
    # The version itself is not a package change.
    assert not release.bump_required(["pyproject.toml"], old, pyproject("2.0.0"))


# --- bump_error --------------------------------------------------------------


def versions(*values):
    return [Version(v) for v in values]


@pytest.mark.parametrize(
    "new, target, released",
    [
        ("0.4.0", "0.3.0", ("0.2.0",)),
        ("1.0.0", "0.3.0", ("0.2.0",)),
        ("0.3.0", "0.2.0", ()),  # nothing on PyPI yet
        ("0.3.1", "0.3.0", ("0.2.0", "0.3.0")),
        ("1.0.0rc1", "0.9.0", ("0.9.0",)),
    ],
)
def test_bump_ok(new, target, released):
    assert (
        release.bump_error(Version(new), Version(target), versions(*released)) is None
    )


@pytest.mark.parametrize(
    "new, target, released, message",
    [
        ("0.3.0", "0.3.0", ("0.2.0",), "not higher than the target"),
        ("0.2.9", "0.3.0", ("0.2.0",), "not higher than the target"),
        ("0.3.0", "0.2.0", ("0.2.0", "0.3.0"), "already on PyPI"),
        ("0.2.5", "0.2.0", ("0.3.0",), "not higher than the latest release"),
    ],
)
def test_bump_rejected(new, target, released, message):
    error = release.bump_error(Version(new), Version(target), versions(*released))
    assert message in error


# --- commands ----------------------------------------------------------------


@pytest.fixture
def repo(monkeypatch):
    """Fake git, pyproject.toml files and PyPI for the commands."""
    state = {
        "changed": ["scrudful/fastapi.py"],
        "base": pyproject("0.3.0"),
        "target": pyproject("0.3.0"),
        "head": pyproject("0.4.0"),
        "released": versions("0.2.0", "0.3.0"),
    }
    monkeypatch.setattr(release, "git", lambda *a: "\n".join(state["changed"]))
    monkeypatch.setattr(
        release,
        "pyproject_at",
        lambda ref: {None: state["head"], "BASE": state["base"]}.get(
            ref, state["target"]
        ),
    )
    monkeypatch.setattr(release, "released_versions", lambda: state["released"])
    monkeypatch.setattr(release, "is_released", lambda v: v in state["released"])
    return state


def test_check_bump_passes_with_a_new_version(repo, capsys):
    assert release.main(["check-bump", "BASE", "TARGET"]) == 0
    assert "0.4.0 will be published" in capsys.readouterr().out


def test_check_bump_fails_without_a_bump(repo, capsys):
    repo["head"] = pyproject("0.3.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 1
    out = capsys.readouterr().out
    assert "must bump the version" in out and "uv version --bump" in out


def test_check_bump_skips_non_package_changes(repo, capsys):
    repo["changed"], repo["head"] = ["docs/source/index.rst"], pyproject("0.3.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 0
    assert "no version bump needed" in capsys.readouterr().out


def test_needs_release(repo):
    assert release.main(["needs-release"]) == 0
    repo["head"] = pyproject("0.3.0")
    assert release.main(["needs-release"]) == release.ALREADY_RELEASED


def test_unknown_command_prints_usage(capsys):
    assert release.main(["nope"]) == 2
    assert "check-bump" in capsys.readouterr().err


@pytest.fixture
def gitlab(repo, monkeypatch):
    calls = []
    existing = {}
    monkeypatch.setenv("CI_COMMIT_SHA", "abc123")

    def fake(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            if path in existing:
                return existing[path]
            raise http_error(404)
        return {}

    monkeypatch.setattr(release, "gitlab", fake)
    return calls, existing


def test_create_release_creates_tag_and_release(gitlab):
    calls, _ = gitlab
    assert release.main(["create-release"]) == 0
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/releases")
    assert body["tag_name"] == "v0.4.0" and body["ref"] == "abc123"
    assert "https://pypi.org/project/scrudful/0.4.0/" in body["description"]


def test_create_release_is_idempotent_for_the_same_commit(gitlab):
    calls, existing = gitlab
    existing["/releases/v0.4.0"] = {"commit": {"id": "abc123"}}
    assert release.main(["create-release"]) == 0
    assert [c[0] for c in calls] == ["GET"]


def test_create_release_refuses_a_tag_on_another_commit(gitlab, capsys):
    calls, existing = gitlab
    existing["/releases/v0.4.0"] = {"commit": {"id": "def456"}}
    assert release.main(["create-release"]) == 1
    assert [c[0] for c in calls] == ["GET"]
    assert "already exists for def456" in capsys.readouterr().out


def test_create_release_raises_other_api_errors(gitlab, monkeypatch):
    def fail(method, path, body=None):
        raise http_error(403)

    monkeypatch.setattr(release, "gitlab", fail)
    with pytest.raises(urllib.error.HTTPError) as info:
        release.main(["create-release"])
    assert info.value.code == 403


def test_script_runs_against_this_repository():
    """The real pyproject.toml parses and the published part is computable."""
    project = release.pyproject_at(None)
    assert release.version_of(project) >= Version("0.3.0")
    assert "version" not in release.published_part(project)["project"]
