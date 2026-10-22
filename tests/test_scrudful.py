from importlib.metadata import version
from pathlib import Path

from scrudful import __version__

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None


def test_version():
    if tomllib is None:
        assert __version__ == version("scrudful")
        return
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert __version__ == expected


def test_version_of_an_uninstalled_source_tree(monkeypatch):
    import importlib
    import importlib.metadata

    import scrudful

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        importlib.reload(scrudful)
        assert scrudful.__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(scrudful)
    assert scrudful.__version__ == __version__
