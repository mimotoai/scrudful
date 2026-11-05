"""The suite's Django settings: the DRF example project's, without ``SCRUDFUL``.

The example sets ``SCRUDFUL["BASE_URL"]`` to the URL it is served at; the
tests set the ``SCRUDFUL`` they need (with pytest-django's ``settings``
fixture), so that a test without one exercises the defaults.
"""

from scrudful_rest_framework.settings import *  # noqa: F401,F403

SCRUDFUL = None
