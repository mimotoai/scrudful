"""The example's NinjaAPI: the role API and its documents, under ``/api/``."""

from ninja import NinjaAPI

from roles import api as roles
from roles import documents
from scrudful.ninja import ScrudfulRouter

api = NinjaAPI(
    title="scrudful django-ninja example",
    urls_namespace="api",
    auth=roles.RbacContextHeader(),
    # Operations declared on the API itself (@api.get(...)) go to its default
    # router: a ScrudfulRouter, so scrudful ones get OPTIONS too. Scrudful
    # operations on a plain ninja Router get no OPTIONS (405).
    default_router=ScrudfulRouter(),
)
api.add_router("/roles", roles.router)
api.add_router("", documents.router)
