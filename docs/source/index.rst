scrudful
========

scrudful helps you build *SCRUDful* REST APIs (Search, Create, Read, Update,
Delete) with correct HTTP conditional-request semantics (RFC 9110 section 13,
RFC 6585 section 3) and ``Link`` headers (RFC 8288), for Django REST Framework
and FastAPI.

Decorate a view or endpoint, give it validator callbacks (``etag_func``,
``last_modified_func``) and scrudful answers 304, 400, 412 and 428 for you, sets
``ETag`` and ``Last-Modified`` on the right responses, and adds ``Link`` and
``Access-Control-Expose-Headers`` headers.

Install the extra for your framework::

    pip install "scrudful[drf]"
    pip install "scrudful[fastapi]"

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   usage
   rest_framework
   fastapi
   conditional
   changelog


Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
