"""Run the tests of the example django-ninja project (``examples/ninja``).

The example has its own settings, while this suite's ``DJANGO_SETTINGS_MODULE``
is the DRF example's, so its tests run in a separate pytest process.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "ninja"


def test_example_project():
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(EXAMPLE), str(ROOT)])}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "examples/ninja/tests",
            "-q",
            "-p",
            "no:randomly",
            "--ds=scrudful_ninja.settings",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
