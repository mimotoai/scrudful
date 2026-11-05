Conditional-request core
========================

``scrudful.conditional`` is the framework-agnostic implementation of RFC 9110
section 13 used by the three adapters; ``scrudful.utils`` holds the ``Link`` and
``Access-Control-Expose-Headers`` helpers. You only need them to build an
adapter for another framework, with one exception:
:data:`scrudful.utils.EXPOSE_HEADERS`, the headers scrudful exposes to browsers
(``Link`` and ``Allow`` always, ``ETag`` and ``Location`` when present), is the
list to give a CORS middleware that sets its own expose list (see "CORS" in
:doc:`usage`).

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
