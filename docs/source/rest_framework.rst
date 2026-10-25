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

``scrudful_viewset``, ``scrudful_api_view`` and a ``@scrudful`` method in the
body of an ``APIView`` (or viewset) class install it. A function view with
``@scrudful`` below a plain ``@api_view`` is SCRUDful, but there is no class
to install it on when ``@scrudful`` runs: it keeps DRF's ``SimpleMetadata``
and gets no HTTP Schema Resource. Use ``scrudful_api_view`` in place of
``@api_view`` + ``@scrudful``:

.. code-block:: python

    # OPTIONS: DRF's SimpleMetadata, no HTTP Schema Resource.
    @api_view(["GET", "PUT"])
    @scrudful(etag_func=item_etag, error=ERROR)
    def item(request, pk): ...


    # OPTIONS: the HTTP Schema Resource.
    @scrudful_api_view(["GET", "PUT"], etag_func=item_etag, error=ERROR)
    def item(request, pk): ...

(The callbacks of the first get ``(request, **url_kwargs)``, those of the
second ``(view, request, **url_kwargs)``.)

A view that uses ``ScrudfulMetadata`` only through ``DEFAULT_METADATA_CLASS``,
without scrudful decorators, gets DRF's ``Allow`` order, which differs from the
body's.

The view's schema and context links describe its success content: they go on
a 2xx response other than 204 that the view returns, and on no other (nor on
a TRACE's, whose content is the echoed request). A 204 (a DELETE's), a
redirect, or an error the view *returns* (``Response(serializer.errors,
status=400)``, a 404 ``Response``) gets no Links from scrudful; a ``Link``
the view set on it is kept as is. To give an error the error entity's Links,
*raise* it (``serializers.ValidationError``, ``NotFound``, ...) and install
``exception_handler`` (below).

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
entity's media type, whatever the request's ``Accept``. The entity is the
view's: DRF has no app-wide error entity, so give each view's handlers
``error=``. The HTTP Schema Resource lists a PUT, PATCH or POST 400 under that
media type (conditional or not) only when the view's exception handler is this
one. Without the setting, or without an ``error`` entity, such errors are
DRF's own, with no Link. The HTTP Schema Resource doesn't list 404 and 405, so
they have no entry to agree with. The example project
(``examples/scrudful_rest_framework``) is conformant on the wire: its tests
check every response against the Semantic HTTP 0.1 rules.

The responses served as the error entity's media type (scrudful's own 400,
412 and 428, and this handler's) bypass DRF's content negotiation: the first
of the view's ``renderer_classes`` with a JSON media type (``application/json``
or ``+json``) renders them, so a project's own JSON renderer (camel-case keys,
orjson ...) formats them as it formats the rest, and DRF's ``JSONRenderer``
does when the view has none.

The browsable API
-----------------

DRF's browsable API is not conformant: a ``GET`` with ``Accept: text/html``
gets an HTML page carrying the Links of the JSON content (the JSON schema and
the JSON-LD context relation), which do not describe HTML. OPTIONS and the
error responses above are JSON whatever the ``Accept``, but the other
responses follow DRF's content negotiation. For full conformance, drop
``BrowsableAPIRenderer`` from ``DEFAULT_RENDERER_CLASSES`` (or from the view's
``renderer_classes``):

.. code-block:: python

    REST_FRAMEWORK = {
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        "EXCEPTION_HANDLER": "scrudful.rest_framework.exception_handler",
    }

An ``Accept`` none of the view's renderers serves then gets DRF's 406, for
OPTIONS too.

An explicit ``@scrudful`` on ``update`` also applies to PATCH when
``partial_update`` is DRF's stock ``UpdateModelMixin.partial_update``.

.. note::

   The schema Link's default relation is ``describedby``, which Semantic HTTP
   0.1 requires of a Schema Link Header. Overriding it
   (``schema_rel_or_func="profile"``) makes the response's Link no longer a
   Schema Link Header, while the OPTIONS body still describes that schema as
   the response's schema: the API is then not conformant. Use the default.

Set ``error=`` on every conditional decorator (or ``Meta.error`` /
``Meta.list_error``) so that scrudful's 400, 412 and 428 carry the error
entity's Links and are served as JSON with its ``media_type``, which must be
JSON content (``application/json`` or ``+json``, other than JSON-LD).
``exception_handler`` does not cover them: scrudful returns them, it does not
raise them.

.. automodule:: scrudful.rest_framework
   :members:
   :member-order: bysource
   :exclude-members: Entity
