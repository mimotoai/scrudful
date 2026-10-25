Conditional-request core
========================

``scrudful.conditional`` is the framework-agnostic implementation of RFC 9110
section 13 used by both adapters; ``scrudful.utils`` holds the ``Link`` and
``Access-Control-Expose-Headers`` helpers. You only need them to build an
adapter for another framework.

.. automodule:: scrudful.conditional
   :members:

.. automodule:: scrudful.utils
   :members:

Entity
------

:class:`~scrudful.entity.Entity` is shared by the three adapters, which
re-export it (``from scrudful.ninja import Entity``).

.. automodule:: scrudful.entity
   :members:
