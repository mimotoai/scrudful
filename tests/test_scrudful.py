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
