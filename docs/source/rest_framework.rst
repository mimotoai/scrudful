Django REST Framework
=====================

``scrudful.rest_framework`` adapts the shared conditional-request core to DRF
viewsets, ``APIView`` methods and function views. Install it with
``pip install "scrudful[drf]"``. The example project in
``examples/scrudful_rest_framework`` shows a viewset, a read-only viewset and a
function view. See :doc:`usage` for the behaviour table, the caveats of
``scrudful_viewset`` and the concurrency note.

OPTIONS requests are answered by ``ScrudfulMetadata`` only when the view's
``metadata_class`` is DRF's default ``SimpleMetadata``. A custom class, ``None``,
or a project-wide ``DEFAULT_METADATA_CLASS`` is kept, so subclass or mix in
``ScrudfulMetadata`` if you use your own metadata class and want scrudful's
OPTIONS output.

An explicit ``@scrudful`` on ``update`` also applies to PATCH when
``partial_update`` is DRF's stock ``UpdateModelMixin.partial_update``.

.. automodule:: scrudful.rest_framework
   :members:
   :member-order: bysource
