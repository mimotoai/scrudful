"""Operations defined in a module with ``from __future__ import annotations``.

All annotations here are strings naming this module's globals. ninja evaluates
the strings it finds on a view function in that function's ``__globals__``,
which for the wrapper are scrudful.ninja's. The hidden parameters (the loaded
object and the handler's own ``HttpResponse``) must still be recognised.
Used by ``test_ninja_adapter.py``.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from ninja import Router, Schema
from ninja.errors import HttpError

from scrudful.ninja import scrudful


class Body(Schema):
    name: str


class Item:
    def __init__(self, pk: int, name: str) -> None:
        self.pk = pk
        self.name = name
        self.version = 1


ITEMS: dict[int, Item] = {}


def load(request: HttpRequest, pk: int, **params: object) -> Item:
    try:
        return ITEMS[pk]
    except KeyError:
        raise HttpError(404, "Not Found") from None


def etag(item: Item) -> str:
    return f"v{item.version}"


@scrudful(load=load, load_as="item", etag_func=etag)
def get_item(
    request: HttpRequest, pk: int, response: HttpResponse, item: Item
) -> dict[str, object]:
    response["X-Handler"] = "yes"
    return {"pk": item.pk, "name": item.name}


@scrudful(load=load, load_as="item", etag_func=etag)
def put_item(
    request: HttpRequest, pk: int, body: Body, item: Item
) -> dict[str, object]:
    item.name = body.name
    item.version += 1
    return {"pk": item.pk, "name": item.name}


def make_router() -> Router:
    ITEMS.clear()
    ITEMS[1] = Item(1, "a")
    router = Router()
    router.get("/items/{pk}")(get_item)
    router.put("/items/{pk}")(put_item)
    return router
