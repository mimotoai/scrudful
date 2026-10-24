"""``scrudful.ninja`` imports without Django REST Framework."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# ninja reads the Django settings when it is imported: use the ninja example's,
# which don't install DRF.
ENV = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join([str(ROOT / "examples" / "ninja"), str(ROOT)]),
    "DJANGO_SETTINGS_MODULE": "scrudful_ninja.settings",
}
BLOCK_DRF = "import sys; sys.modules['rest_framework'] = None; "


def run(code):
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=ENV, capture_output=True, text=True
    )


def test_ninja_imports_without_drf():
    result = run(BLOCK_DRF + "import scrudful.ninja")
    assert result.returncode == 0, result.stderr


def test_drf_is_blocked():
    """The check is meaningful: the DRF adapter can't be imported that way."""
    result = run(BLOCK_DRF + "import scrudful.rest_framework")
    assert result.returncode != 0
    assert "rest_framework" in result.stderr
