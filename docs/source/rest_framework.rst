Django REST Framework
=====================

``scrudful.rest_framework`` adapts the shared conditional-request core to DRF
viewsets, ``APIView`` methods and function views. Install it with
``pip install "scrudful[drf]"``. The example project in
``examples/scrudful_rest_framework`` shows a viewset, a read-only viewset and a
function view. See :doc:`usage` for the behaviour table, the caveats of
``scrudful_viewset`` and the concurrency note.

OPTIONS requests are answered with a Semantic HTTP 0.1 HTTP Schema Resource
(``Content-Type: application/json``, the two published HTTP Schema Resource
Links and an ``Allow`` listing the body's methods) by ``ScrudfulMetadata``, but
only when the view's ``metadata_class`` is DRF's default ``SimpleMetadata``. A
custom class, ``None``, or a project-wide ``DEFAULT_METADATA_CLASS`` is kept,
so subclass or mix in ``ScrudfulMetadata`` if you use your own metadata class
and want scrudful's OPTIONS output.

A view that uses ``ScrudfulMetadata`` only through ``DEFAULT_METADATA_CLASS``,
without scrudful decorators, gets DRF's ``Allow`` order, which differs from the
body's.

Errors DRF raises
-----------------

An ``error`` entity gives scrudful's own 400, 412 and 428 its Links. The errors
DRF itself raises (a serializer's 400, the 404 of ``get_object()``, a 405, a 401
or 403) need ``exception_handler``, which settings must name::

    REST_FRAMEWORK = {
        "EXCEPTION_HANDLER": "scrudful.rest_framework.exception_handler",
    }

It calls DRF's handler and, for a view whose handler for the request has an
``error`` entity, adds that entity's Links and serves the response with the
entity's media type, whatever the request's ``Accept``. The HTTP Schema Resource
lists a PUT, PATCH or POST 400 under that media type (conditional or not) only
when the view's exception handler is this one. Without the setting, or
without an ``error`` entity, such errors are DRF's own, with no Link. The HTTP Schema Resource doesn't list 404
and 405, so they have no entry to agree with. The example project
(``examples/scrudful_rest_framework``) is conformant on the wire: its tests check
every response against the Semantic HTTP 0.1 rules.

An explicit ``@scrudful`` on ``update`` also applies to PATCH when
``partial_update`` is DRF's stock ``UpdateModelMixin.partial_update``.

.. note::

   The schema Link's default relation is ``describedby``, which Semantic HTTP
   0.1 requires of a Schema Link Header. Overriding it
   (``schema_rel_or_func="profile"``) makes the response's Link no longer a
   Schema Link Header, while the OPTIONS body still describes that schema as
   the response's schema: the API is then not conformant. Use the default.

Set ``error=`` on the decorator (or ``Meta.error`` / ``Meta.list_error``) so that
scrudful's 400, 412 and 428 carry the error entity's Links and are served as
JSON with its ``media_type``, which must be JSON content (``application/json``
or ``+json``, other than JSON-LD).

.. automodule:: scrudful.rest_framework
   :members:
   :member-order: bysource
   :exclude-members: Entity
