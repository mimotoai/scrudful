FastAPI
=======

``scrudful.fastapi`` adapts the shared conditional-request core to FastAPI
endpoints. Install it with ``pip install "scrudful[fastapi]"``. See
``examples/fastapi/api.py`` for a complete application, and :doc:`usage` for the
behaviour table and a quickstart.

.. code-block:: python

    @app.get("/users/{user_id}", responses=CONDITIONAL_READ_RESPONSES)
    @scrudful(etag_func=user_etag, last_modified_func=user_last_modified)
    async def get_user(user_id: int): ...

``@scrudful(...)`` must sit below the route decorator.

.. automodule:: scrudful.fastapi
   :members:
   :member-order: bysource
