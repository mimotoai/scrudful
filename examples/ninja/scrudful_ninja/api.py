"""The example's NinjaAPI: the role API and its documents, under ``/api/``."""

from ninja import NinjaAPI

from roles import api as roles
from roles import documents
from scrudful.ninja import ScrudfulRouter, add_error_links

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
# The 401, 422 and other errors ninja answers itself get the error entity's
# Links too. Call this after registering any exception handlers of your own.
add_error_links(api, roles.ERROR)
