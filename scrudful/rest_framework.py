"""SCRUDful conditional requests and Link headers for Django REST Framework.

The HTTP logic lives in :mod:`scrudful.conditional` and :mod:`scrudful.utils`;
this module only adapts it to DRF viewsets, ``APIView`` methods and function
views.

Callbacks
    Every ``*_func`` / ``*_or_func`` callable is called with the positional
    arguments of the wrapped view up to and including the request, followed by
    the URL arguments of the request: ``func(view, request, **url_kwargs)`` for
    view methods and :func:`scrudful_api_view`, ``func(request, **url_kwargs)``
    for function views decorated with :func:`scrudful`. Keyword arguments that
    view methods pass to each other (such as DRF's ``partial=True``) are never
    passed on. The link callables of view methods are also called when the
    HTTP Schema Resource is built, without a request or URL arguments, as
    ``func(view, None)`` (see :func:`http_schema_resource`).

    The validator callbacks run before the view, so before its own lookup
    (``get_object()``) and object permission checks: they must apply the same
    scoping, e.g. ``view.get_queryset().filter(pk=pk)`` rather than
    ``Model.objects.filter(pk=pk)``. Otherwise an object the view would hide
    reveals its validators (304) and its existence (412/428 instead of 404).
    ``object_validators`` avoids this: the callbacks then take the object
    ``get_object()`` returns, already scoped and permission-checked.

Validators
    Without validator callbacks no preconditions are evaluated: the If-*
    headers are ignored and the view behaves like an undecorated one (apart
    from the Link and expose headers). A validator callback returning ``None``
    means the target resource has no current representation; when both
    validators are ``None`` the preconditions are skipped (except for PUT,
    which may create the resource) and the view decides, e.g. by answering
    404. A create's list validators are the exception (see
    ``list_etag_func`` on :class:`ScrudfulViewFunc`): the collection always
    has a representation, so raise ``Http404`` in the callback to answer a
    POST to a missing one.

    When either validator callback is configured, PUT, PATCH and DELETE on an
    existing resource must be conditional: a request without If-Match (or a
    valid If-Unmodified-Since, when ``last_modified_func`` is set, or
    ``If-None-Match: *``, which then fails with 412) is answered with 428
    Precondition Required. A malformed If-Match or If-None-Match is
    answered with 400, a failed precondition with 412 and a matching GET/HEAD
    with 304 carrying the validators. These responses are returned without
    calling the view: 400/412/428 as a DRF ``Response`` (a ``JsonResponse`` for
    plain Django views) with a JSON body, 304 without a body or content Links
    (each gets the HTTP Schema Resource link, see "Headers"). With an
    ``error`` entity the 400/412/428 carry its Links and are served as JSON
    with its media type, whatever renderer DRF's content negotiation picked
    (the browsable API's included): the view's first JSON renderer renders
    them (DRF's ``JSONRenderer`` if it has none).

    After the view runs, 2xx responses to GET/HEAD get the validators computed
    for the precondition check, and 2xx responses to PUT/PATCH get freshly
    computed ones (the callbacks are called again, RFC 9110 §9.3.4). Other
    methods and non-2xx responses get none. Headers the view set itself are
    never overwritten.

Concurrency
    The preconditions are evaluated before the view runs, so two concurrent
    requests carrying the same If-Match can both pass. The callbacks run inside
    the view call, in the same database transaction as the write when the
    request is atomic (``ATOMIC_REQUESTS = True``, or ``transaction.atomic``
    applied outside the decorator). For strict lost-update protection, lock the
    row in the validator callback on unsafe methods
    (``Model.objects.select_for_update().filter(pk=pk)``), or make the write
    itself conditional (``UPDATE ... WHERE version = <expected>``). A create's
    preconditions are the collection's (``list_etag_func``, see
    :class:`ScrudfulViewFunc`): lock a row per collection in a list validator
    callback (for a POST only), in a request-wide transaction, and take the
    same lock in every other write that changes the collection's validators.

Headers
    The Link header is added to the responses whose content the view's links
    describe: the 2xx but 204 it returns, to any method but TRACE (whose
    content is the echoed request). Another response it returns (an error,
    a 204, a redirect) gets none of these Links (only the HTTP Schema
    Resource link, below), and keeps any Link the view set. Raise an error
    rather than return it to give it the ``error``
    entity's Links, through :func:`exception_handler`. The Link header is
    appended to any Link value the view set; a Link of the view's with the
    relation of the schema link (``describedby`` by default) replaces that
    link, and one with either context relation (JSON-LD or ``rdf-context``,
    one slot) the context link (see :func:`scrudful._adapter.merge_links`).
    Every link URL is resolved against the base URL (``base_url``, else
    ``Meta.base_url``, else ``SCRUDFUL["BASE_URL"]``) when one is set, as in
    the HTTP Schema Resource, else against the request's absolute URI
    (``request.build_absolute_uri()``), so relative values such as
    ``"/schemas/item"`` are sent absolute.
    Every response of a scrudful view routed through the URLconf also links
    the view's HTTP Schema Resource (Semantic HTTP 0.1, "Finding the HTTP
    Schema Resource"): the 2xx, 304 and HEAD, scrudful's 400/412/428, an
    error or a 204 the view returns, and every error DRF answers through the
    view's exception handler (whichever it is: a serializer's 400, the
    401/403/404/405/406/415/429 of ``initial()`` and the handler, OPTIONS's
    included): a link with the HTTP Schema Resource relation
    (``scrudful._hsr.HSR_LINK_REL``) and ``type="application/json"`` to the
    :func:`hsr_urls` route (else ``hsr_url``'s URL), appended to the Link (a
    Link with that relation the view sets wins). The view class's
    ``finalize_response`` adds it, as scrudful sets the class up with its
    OPTIONS (see :func:`_install_metadata`).
    A request that came through no URL (a view called directly, as with
    ``APIRequestFactory``) gets none, nor does a CORS preflight or a view
    whose routed function serves no HTTP Schema Resource (``@api_view`` +
    ``@scrudful``); a routed request of a scrudful view without the
    :func:`hsr_urls` include raises ``ImproperlyConfigured`` (see
    :func:`_response_hsr_link`). When one of the view's ``renderer_classes``
    renders something other than JSON (the browsable API), its responses
    also send ``Vary: Accept``.
    ``Access-Control-Expose-Headers`` is extended with ``Link`` and ``Allow``,
    and whichever of ``ETag`` and ``Location`` the response carries
    (django-cors-headers replaces that header when ``CORS_EXPOSE_HEADERS`` is
    set; list the names there instead). Exceptions raised by the view (e.g.
    ``Http404``) are rendered by DRF without the error Links, unless the
    project's ``EXCEPTION_HANDLER`` is :func:`exception_handler`, which gives
    them the Links of the view's ``error`` entity (and, with a
    problem-details entity, makes a validation 400 problem details).

OPTIONS
    With :class:`ScrudfulMetadata` (installed in place of DRF's default
    metadata class by :func:`scrudful_viewset`, :func:`scrudful_api_view` and
    a class-body :func:`scrudful` method, but not by :func:`scrudful` below
    ``@api_view``), OPTIONS is answered as Semantic HTTP 0.1 has it, in this
    order:

    1. A CORS preflight (an OPTIONS with ``Origin`` and
       ``Access-Control-Request-Method``) is answered 204, without content,
       ``Content-Type`` or Links, before DRF's ``dispatch``: no content
       negotiation, authentication (so no CSRF check), permission check,
       throttle or loading runs for it. Django middleware runs first, so a
       CORS middleware adds its headers (or answers the preflight itself).
    2. The request is authenticated and throttled as DRF does in
       ``initial()``, but OPTIONS itself is not gated by the permission
       checks: an anonymous requester of a resource it may not use gets a
       204 with ``Allow: OPTIONS``. Bad credentials are answered as DRF
       answers them (a 401, or a 403 when the first authenticator has no
       ``WWW-Authenticate``), a refused throttle with DRF's 429.
    3. Each method the view serves is checked with its view-level
       permissions (``check_permissions``), on a request of that method
       (``rest_framework.request.clone_request``, also the view's
       ``request``, and the method's action as a viewset's ``action``,
       meanwhile). A method with an ``allowed_func`` (its handler's, else
       ``Meta.allowed_func``) skips this check: the hook alone decides, with
       the object.
    4. Only if one method passed step 3 is the object loaded, on an item's
       route, with ``get_object()``, its ``check_object_permissions``
       suppressed, as the first method that passed (GET, when it did) loads
       it: with a request of that method as the view's ``request`` and,
       on a viewset, that method's action as ``action`` (so a
       ``get_queryset`` scoped by action gives the queryset that method
       gets, not OPTIONS's ``metadata``'s). An item's route is one whose
       ``lookup_url_kwarg``, else ``lookup_field``, is in the URL kwargs;
       with a ``get_object`` of the view's own, whatever its URL kwarg is
       named, a viewset route whose action is ``retrieve``, ``update``,
       ``partial_update``, ``destroy`` or an ``@action(detail=True)``, or
       another view's route with a URL kwarg (but ``format``). So on a view
       whose methods are all gated by permissions, a requester refused every
       method gets ``Allow: OPTIONS`` whether the object exists or not, as a
       GET of either is refused alike: OPTIONS is no existence oracle for
       it. A method with
       an ``allowed_func`` passes step 3 for every requester (the hook has no
       object-free check), so the object is always loaded on such a view and
       a missing one is a 404 whoever asks: there, OPTIONS tells anyone which
       objects exist, unless ``get_object()`` itself hides them. A
       ``get_object()`` that raises (``Http404`` for a missing object, or the
       404 of an object the requester may not read) is answered with that
       error, through the view's exception handler (:func:`exception_handler`
       gives it the error Links), and the HTTP Schema Resource link.
    5. ``Allow`` lists exactly the methods this requester may use: OPTIONS,
       and each method that passed step 3 and, with an object,
       ``check_object_permissions``; or whose
       ``allowed_func(view, request, method, obj)`` returns ``True``. DRF's
       ``PermissionDenied`` or ``NotAuthenticated`` (or Django's
       ``PermissionDenied``) means not allowed; anything else raised
       propagates, answered as in step 4: an object check raising
       ``Http404`` (``DjangoObjectPermissions`` for an object the requester
       may not read) answers that 404. No throttle runs per method. HEAD,
       which DRF dispatches to the GET handler, is listed when the view
       serves it and GET is allowed.
    6. The answer is a 204 without content or ``Content-Type`` (whatever
       renderer DRF's content negotiation picked, the browsable API's
       included), with ``Allow``, one HTTP Schema Resource link (to its
       :func:`hsr_urls` route, else ``hsr_url``'s URL) and
       ``Access-Control-Expose-Headers``.

    A 405 keeps DRF's ``Allow``, every method the view serves. An ``Accept``
    that none of the view's renderers serves (e.g. ``text/html`` on a view
    without the browsable API) still gets DRF's 406, raised before OPTIONS
    is handled. The HTTP Schema Resource itself is served by GET, by
    :func:`hsr_urls` (or :func:`http_schema_resource_view`): a view's OPTIONS
    raises ``ImproperlyConfigured`` without either (see :func:`_hsr_target`).
    With ``metadata_class = None`` OPTIONS is DRF's 405; with a metadata
    class that isn't a :class:`ScrudfulMetadata`, DRF's 200 with its body.

System checks
    ``manage.py check``, which ``runserver`` and ``migrate`` run before they
    start, reports the misconfigurations of the scrudful views' routes. The
    :func:`hsr_urls` pattern checks them in Django's URL check (tagged
    ``urls``), which loads the URLconf; without that pattern in the
    URLconf, the check this module registers with
    ``django.core.checks.register`` when imported does, so only once
    something imported it before the checks started (an
    ``AppConfig.ready()`` of the project's, or the test run). Either scans
    the root URLconf for the routes of scrudful views, as :func:`hsr_urls`
    does, and reports as errors:

    - ``scrudful.E001``: a scrudful view's route without a URL name (unless
      the view has an ``hsr_url``, which needs none);
    - ``scrudful.E002``: a URL name given to the routes of two different
      scrudful views;
    - ``scrudful.E003``: scrudful views relying on the :func:`hsr_urls`
      route (no ``Meta.hsr_url`` or ``SCRUDFUL["HSR_URL"]``) without the
      route included in the URLconf;
    - ``scrudful.E004``: a view whose route is turned off without an
      ``hsr_url``;
    - ``scrudful.E005``: a view whose HTTP Schema Resource can't be built
      (a relative link URL without a base URL, a schema without an RDF
      context, content that isn't JSON without a context type, or anything
      else building it raises but a link callable);
    - ``scrudful.E006``: an invalid ``SCRUDFUL`` setting: not a dict of its
      keys, or a key whose value is invalid (one error for each). While the
      setting is invalid, what depends on it is not checked (no document
      is built).

    and as a warning:

    - ``scrudful.W001``: a link callable raising when called as
      ``func(view, None)``, naming the route, the callable and the
      exception. A warning, not an error, as ``migrate`` runs the checks
      first: a callable that reads the database fails on a database not
      migrated yet, and must not stop ``migrate``. The document is built
      again, the callable called again, when it is first served; one that
      can't work then (it needs the request, say) fails that request.

    Each would otherwise fail on a request. The check builds each document
    as :func:`http_schema_resource` does (no request, no database access by
    scrudful; link callables are called), without keeping it.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import inspect
import types
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, NamedTuple

from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.signals import setting_changed
from django.http import (
    Http404,
    HttpRequest,
    HttpResponseBase,
    JsonResponse,
)
from django.urls import (
    NoReverseMatch,
    URLPattern,
    URLResolver,
    get_resolver,
    get_urlconf,
    path,
    reverse,
)
from django.utils.cache import patch_vary_headers
from django.utils.encoding import iri_to_uri
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import (
    APIException,
    ErrorDetail,
    NotAuthenticated,
    NotFound,
    ValidationError,
)
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.decorators import api_view
from rest_framework.fields import IntegerField
from rest_framework.generics import GenericAPIView
from rest_framework.metadata import BaseMetadata, SimpleMetadata
from rest_framework.mixins import UpdateModelMixin
from rest_framework.pagination import BasePagination
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request, clone_request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.views import exception_handler as drf_exception_handler

# Re-exported: the link defaults of every adapter (tests/test_adapter.py).
from scrudful._adapter import DEFAULT_CONTEXT_REL  # noqa: F401
from scrudful._adapter import DEFAULT_CONTEXT_TYPE  # noqa: F401
from scrudful._adapter import (
    DEFAULT_SCHEMA_REL,
    DEFAULT_SCHEMA_TYPE,
    METHOD_KEYS,
    check_callables,
    check_error_body_func,
    check_error_media_type,
    check_validators,
    context_defaults,
    context_link_type,
    describe_content,
    describe_read,
    describe_write,
    essence,
    evaluates_create_preconditions,
    evaluates_preconditions,
    expose_headers,
    has_default_entity,
    is_json,
    is_json_syntax,
    merge_links,
    parse_query_int,
    reason_phrase,
    set_validator_headers,
    validator_policy,
    with_error_body,
)
from scrudful._django import (
    SETTING_ACCESSORS,
    aware,
    error_handler,
    hsr_not_allowed,
    hsr_response,
    no_content,
    page_url,
    scrudful_setting,
    setting_base_url,
    setting_error,
    setting_hsr_cache_control,
    setting_hsr_route,
    setting_hsr_url,
    slice_page,
)
from scrudful._django import conditional_response as django_conditional_response
from scrudful._django import precondition_headers as django_precondition_headers
from scrudful._hsr import (
    HsrConfigError,
    absolute_for_hsr,
    allow_value,
    check_base_url,
    check_hsr_cache_control,
    check_hsr_url_result,
    hsr_link,
    is_preflight,
    options_operation,
)
from scrudful.conditional import (
    ConditionalResult,
    evaluate_preconditions,
    format_http_date,
    http_timestamp,
    quote_etag,
)
from scrudful.collection import (
    InvalidPage,
    collection_schema,
    envelope_entry,
    page_envelope,
)
from scrudful.entity import Entity
from scrudful.problem import PROBLEM_JSON, problem_from_errors
from scrudful.utils import join_links, link_content, resolve_uri

__all__ = [
    "Entity",
    "ScrudfulAPIViewMetadata",
    "ScrudfulMetadata",
    "ScrudfulPagination",
    "ScrudfulViewFunc",
    "exception_handler",
    "handler404",
    "handler500",
    "hsr_urls",
    "http_schema_resource",
    "http_schema_resource_view",
    "scrudful",
    "scrudful_api_view",
    "scrudful_viewset",
]


def handler404(error: Entity) -> Callable[..., HttpResponseBase]:
    """A Django ``handler404`` that answers JSON unless the client prefers HTML.

    In ``urls.py``: ``handler404 = scrudful.rest_framework.handler404(ERROR)``,
    with ``ERROR`` an :class:`Entity` of plain (non-callable) link values.
    """
    return error_handler(404, error)


def handler500(error: Entity) -> Callable[..., HttpResponseBase]:
    """A Django ``handler500`` that answers JSON unless the client prefers HTML.

    In ``urls.py``: ``handler500 = scrudful.rest_framework.handler500(ERROR)``.
    """
    return error_handler(500, error)


# Set on the request while a ScrudfulViewFunc handles it, so that a wrapped
# method calling another one (partial_update -> update) is handled once.
_ACTIVE_ATTR = "_scrudful_active"
_STOCK_PARTIAL_UPDATE = UpdateModelMixin.partial_update
_LINK_OPTIONS = (
    "schema_link_or_func",
    "schema_rel_or_func",
    "schema_type_or_func",
    "context_link_or_func",
    "context_rel_or_func",
    "context_type_or_func",
)
_VALIDATOR_OPTIONS = ("etag_func", "last_modified_func")
_LIST_VALIDATOR_OPTIONS = ("list_etag_func", "list_last_modified_func")
_OBJECT_OPTIONS = ("object_validators",)
# The object a create saved (see ``object_validators``).
_CREATED_ATTR = "_scrudful_created"
# The object ``get_object()`` returned for the request (see ``object_validators``).
_OBJECT_ATTR = "_scrudful_object"
_ERROR_OPTIONS = ("error",)
# In place of the link options (see ``entity``).
_ENTITY_OPTIONS = ("entity",)
# What the 400/412/428 bodies are built with (paired with ``error``).
_ERROR_BODY_OPTIONS = ("error_body_func",)
# Handler names that never answer a POST: list validators there would never be
# evaluated (see ``ScrudfulViewFunc.__set_name__``).
_NEVER_CREATES = frozenset(
    {
        "get",
        "head",
        "put",
        "patch",
        "delete",
        "options",
        "trace",
        "list",
        "retrieve",
        "update",
        "partial_update",
        "destroy",
    }
)
#: The media type of the content a view's own links describe, without ``entity``.
_DEFAULT_MEDIA_TYPE = "application/json"
#: Options a view's handlers share: ``Meta``'s are kept on the class (see
#: :func:`scrudful_viewset`), for the handlers it doesn't wrap.
_VIEW_OPTIONS = ("base_url", "statuses", "allowed_func")
#: Where a :func:`scrudful_viewset` class's HTTP Schema Resource is served
#: (see :func:`hsr_urls`): ``Meta`` options of the class alone.
_HSR_OPTIONS = ("hsr_route", "hsr_url", "hsr_cache_control")
#: The ``Meta`` options of a :func:`scrudful_viewset` class, checked: the
#: :data:`_VIEW_OPTIONS` and :data:`_HSR_OPTIONS`.
_META_OPTIONS_ATTR = "_scrudful_meta_options"
#: The description of each method's ``default`` response entry: the errors.
_DEFAULT_ERROR = "Error."
#: The errors DRF answers itself, before or around the handler
#: (authentication and permissions, content negotiation, parsing), whose
#: bodies get ``SCRUDFUL["ERROR"]`` when it is set, whatever the handler's
#: ``error`` (see :func:`exception_handler`).
_FRAMEWORK_ERRORS = {
    401: "Unauthorized: the request lacks valid authentication credentials.",
    403: "Forbidden: the requester may not perform this request.",
    406: "Not Acceptable: no representation matches the request's Accept.",
    415: "Unsupported Media Type: the request's content type is not accepted.",
}


class ScrudfulViewFunc:
    """A view callable with SCRUDful conditional requests and Link headers.

    Usually created by :func:`scrudful`, :func:`scrudful_viewset` or
    :func:`scrudful_api_view`; the module docstring describes the behaviour.
    The wrapper takes over the wrapped view's ``__name__``, ``__doc__`` and
    attributes (such as those set by ``@action``) and binds like a function,
    so it can be used as a view method.

    Parameters
    ----------
    view_method : callable
        The wrapped view method or function view.
    etag_func : callable, optional
        Returns the entity-tag of the current representation, or ``None`` if
        there is none. ``str()`` of the result is used: a valid entity-tag
        (``'"abc"'`` or ``'W/"abc"'``) is sent unchanged, anything else is
        wrapped in double quotes (a strong entity-tag). Values that cannot be
        an entity-tag (containing a double quote, space, control character or
        a character above U+00FF) raise ``ValueError``. No library version is
        appended. Weak entity-tags are allowed, but If-Match uses the strong
        comparison, which they never pass: only If-Unmodified-Since (or
        ``If-Match: *``) can then make a write succeed.
    last_modified_func : callable, optional
        Returns the modification time of the current representation as a
        ``datetime`` or an epoch timestamp, or ``None`` if there is none. Naive
        datetimes are taken to be in the default time zone
        (``settings.TIME_ZONE``), as Django does. Sub-second precision is
        discarded.
    schema_link_or_func : str or callable, optional
        URL of the schema of the request and response bodies. A relative URL
        such as ``"/schemas/item"`` is resolved against the base URL
        (``base_url``, else ``SCRUDFUL["BASE_URL"]``) when one is set, else,
        on a response, against the request's absolute URI; the HTTP Schema
        Resource needs the base URL for it. If no value is given, no schema
        link is sent.
    schema_rel_or_func : str or callable, optional
        ``rel`` of the schema link. Defaults to ``"describedby"``.
    schema_type_or_func : str or callable, optional
        ``type`` of the schema link. Defaults to ``"application/schema+json"``.
    context_link_or_func : str or callable, optional
        URL of the JSON-LD context of the bodies, resolved like the schema
        URL. If no value is given, no context link is sent.
    context_rel_or_func : str or callable, optional
        ``rel`` of the context link. Defaults to
        ``"http://www.w3.org/ns/json-ld#context"``.
    context_type_or_func : str or callable, optional
        ``type`` of the context link. Defaults to ``"application/ld+json"``.
    entity : Entity, optional
        The schema and context links and the media type of the content the
        handler serves (and, for POST, PUT and PATCH, accepts), in place of
        the six options above (giving both is a ``TypeError``). The HTTP
        Schema Resource describes the content under its ``media_type``, and
        the responses get its Links (an RDF context link with the
        ``rdf-context`` relation for content that is not JSON). Without it,
        the content is ``application/json`` with the links of the options
        above.
    statuses : mapping, optional
        The success statuses of each method the handler serves, for the HTTP
        Schema Resource: a mapping of lowercase method names (``"get"``,
        ``"post"`` ...; HEAD takes GET's unless given) to lists of 2xx
        status codes, e.g. ``{"post": [200]}`` for a POST action answering
        200. Each one but 204 is described with the content. A method it
        doesn't list has DRF's: 201 for POST, 204 for DELETE, else 200.
    allowed_func : callable, optional
        ``allowed_func(view, request, method, obj)``, whether the requester
        may use ``method`` (on ``obj``, the loaded object, else ``None``),
        for the ``Allow`` of OPTIONS: when given, it alone decides (see
        "OPTIONS" in the module docstring). ``request`` is a request of
        ``method``; it must return a ``bool`` (else ``TypeError``), and may
        raise DRF's ``PermissionDenied`` or ``NotAuthenticated`` for
        ``False``.
    base_url : str, optional
        The absolute URL that relative link URLs are resolved against
        (default ``SCRUDFUL["BASE_URL"]``), in the HTTP Schema Resource and
        the Link headers alike.
    error : Entity, optional
        The schema and context of the bodies of scrudful's own 400, 412 and
        428 responses, which then carry its Links (a 304 has no body and gets
        none). Its link callables are called like the others; its
        ``media_type`` decides the context link's defaults. These bodies are
        JSON objects, so ``media_type`` must be JSON content
        (``application/json`` or ``+json``, e.g.
        ``application/problem+json``), and they are served as JSON with that
        Content-Type whatever renderer DRF's content negotiation picked (the
        browsable API's included): the view's first JSON renderer (one of its
        ``renderer_classes`` with an ``application/json`` or ``+json`` media
        type, else DRF's ``JSONRenderer``) renders them. Without ``error``
        these responses carry no error Links and are rendered as before.
    error_body_func : callable, optional
        ``error_body_func(request, result)`` returns the body (a ``dict``;
        ``request`` is the DRF ``Request``; a plain function, not ``async``)
        of scrudful's own 400, 412 or 428 response in place of
        ``{"detail": ...}``; ``result`` is the
        :class:`~scrudful.conditional.ConditionalResult`, whose default body is
        ``result.body``. The status, headers, Links and Content-Type are
        unchanged, and it is never called for a 304.
        :func:`scrudful.problem.problem_details` returns RFC 9457 problem
        details (pair it with ``error=Entity(media_type=PROBLEM_JSON)``).
        Without it the bodies are as before.
    object_validators : bool, optional
        For a view method of a generic view (one with ``get_object()``):
        compute the validators from the object ``view.get_object()`` returns,
        *before* preconditions are evaluated, so that a missing or forbidden
        object is a 404/403 and never reveals a validator (or a 412/428).
        ``etag_func(obj)`` and ``last_modified_func(obj)`` then receive only
        the object, and the precondition check knows the resource exists.
        :func:`scrudful_viewset` makes the handler's own ``get_object()``
        return the same instance for the request, and after a successful
        PUT/PATCH loads the object again for the validators of the response
        (none are sent if that fails). Needs ``etag_func`` or
        ``last_modified_func``. Default ``False``: the callbacks are called as
        ``func(view, request, **url_kwargs)``, before ``get_object()``.

        On a *create* (``creates=True``; :func:`scrudful_viewset` sets it for
        ``create``) the validators describe the new resource instead:
        they are not used for the preconditions (``list_etag_func`` and
        ``list_last_modified_func`` are), and a 201 gets ``ETag`` and
        ``Last-Modified`` from the saved instance (``serializer.instance``
        after ``perform_create``), never replacing a header the view set. A
        ``many=True`` create saves several objects and gets neither.
    creates : bool, optional
        The method creates a resource (see ``object_validators``). Needs
        ``object_validators``.
    location_func : callable, optional
        For a create with ``object_validators``: ``location_func(view,
        request, obj)`` returns the ``Location`` of the new object ``obj``
        (passed through ``iri_to_uri(str(...))``); a 201 without a
        ``Location`` gets it. ``None`` sends none.
    list_etag_func, list_last_modified_func : callable, optional
        For a create (a POST to a collection): the validators of the
        collection, its target, called as ``func(view, request,
        **url_kwargs)`` (``func(request, **url_kwargs)`` on a function view),
        with ``object_validators`` too; :func:`scrudful_viewset` passes the
        list's, so they return what a GET of the list computes for the same
        request. A POST's preconditions are evaluated against
        them before the view, with the rules of the other methods: If-Match
        is a strong comparison (``*`` passes; a weak list entity-tag never
        matches), If-Unmodified-Since applies only without If-Match,
        If-None-Match is a weak comparison (``*`` fails: the collection
        always has a representation, even empty), and If-Modified-Since is
        ignored. Unlike a GET of the list, callbacks returning ``None`` don't
        make the collection missing (If-Match with an entity-tag fails,
        ``*`` passes): to answer a POST to a missing collection, e.g. a
        nested route whose parent doesn't exist, with 404, raise ``Http404``
        in the callback. A failed precondition is a 412 and a malformed If-Match or
        If-None-Match a 400, with ``error``'s Links and ``error_body_func``'s
        body, without calling the view. The callbacks are called only for a
        POST carrying If-Match, If-None-Match or If-Unmodified-Since: a
        create without one proceeds (never a 428). On a function view other
        methods ignore them, as does any other handler that doesn't answer a
        POST (e.g. a GET ``@action``), but a class-body handler named after
        a standard non-POST handler rejects them (see Raises). With ``etag_func`` or
        ``last_modified_func``, they need ``creates=True`` (the target's
        validators would be ambiguous otherwise).

        The check and the create are atomic only if the application
        serializes every write that changes the collection's validators with
        conditional creates: lock a row per collection
        (``select_for_update()``) in a list validator callback, with a
        transaction spanning the whole request (``ATOMIC_REQUESTS = True``,
        or ``transaction.atomic`` applied outside the decorator) so that the
        lock is held until the create commits, and take the same lock in the
        other writes. Otherwise two requests carrying the same If-Match can
        both create, or an item PUT can change the collection between the
        check and the create. Unconditional creates call no list callback,
        so they must take the lock themselves (e.g. in ``perform_create``).
        :func:`scrudful_viewset` gives the same callbacks to ``list``: lock
        only when ``request.method == "POST"``, or every GET of the list
        takes the lock too.

    Raises
    ------
    TypeError
        If a validator callback isn't callable, ``error`` isn't an
        :class:`Entity`, the view is ``async def``, ``object_validators``
        is set without a validator callback, or a list validator callback
        is set with a validator callback but without ``creates``; if
        ``entity`` isn't an :class:`Entity` or is given with a link option,
        ``statuses`` isn't a mapping of lists of integers, or
        ``allowed_func`` isn't callable. When the
        class is created, if a list validator callback is set on a
        class-body handler named after a standard non-POST handler (``get``,
        ``head``, ``put``, ``patch``, ``delete``, ``options``, ``trace``,
        ``list``, ``retrieve``, ``update``, ``partial_update`` or
        ``destroy``).
    ValueError
        If ``error``'s media type is not JSON content, or ``statuses`` names
        a method that is not one or a status that is not a 2xx.
    ImproperlyConfigured
        If ``base_url`` is not an absolute URL.
    """

    def __init__(
        self,
        view_method: Callable[..., Any],
        *,
        etag_func: Callable[..., Any] | None = None,
        last_modified_func: Callable[..., Any] | None = None,
        schema_link_or_func: Any = None,
        schema_rel_or_func: Any = None,
        schema_type_or_func: Any = None,
        context_link_or_func: Any = None,
        context_rel_or_func: Any = None,
        context_type_or_func: Any = None,
        error: Entity | None = None,
        error_body_func: (
            Callable[[Any, ConditionalResult], dict[str, Any]] | None
        ) = None,
        object_validators: bool = False,
        location_func: Callable[..., Any] | None = None,
        creates: bool = False,
        list_etag_func: Callable[..., Any] | None = None,
        list_last_modified_func: Callable[..., Any] | None = None,
        entity: Entity | None = None,
        statuses: Mapping[str, Sequence[int]] | None = None,
        allowed_func: Callable[..., Any] | None = None,
        base_url: str | None = None,
    ) -> None:
        statuses = _check_view_options(
            entity,
            statuses,
            allowed_func,
            base_url,
            schema_link_or_func=schema_link_or_func,
            schema_rel_or_func=schema_rel_or_func,
            schema_type_or_func=schema_type_or_func,
            context_link_or_func=context_link_or_func,
            context_rel_or_func=context_rel_or_func,
            context_type_or_func=context_type_or_func,
        )
        if entity is not None:
            # The links are the entity's; ``media_type`` comes from it too.
            schema_link_or_func = entity.schema_link_or_func
            schema_rel_or_func = entity.schema_rel_or_func
            schema_type_or_func = entity.schema_type_or_func
            context_link_or_func = entity.context_link_or_func
            context_rel_or_func = entity.context_rel_or_func
            context_type_or_func = entity.context_type_or_func
        check_validators(etag_func, last_modified_func)
        _check_list_validators(
            etag_func,
            last_modified_func,
            list_etag_func,
            list_last_modified_func,
            creates,
        )
        _check_error(error)
        check_error_body_func(error_body_func)
        _check_sync(view_method)
        if object_validators and etag_func is None and last_modified_func is None:
            raise TypeError(
                "object_validators needs an etag_func or last_modified_func"
            )
        # Copies __name__, __doc__, __wrapped__ and the wrapped view's
        # attributes (e.g. @action's mapping); our own attributes come after.
        functools.update_wrapper(self, view_method)
        self.view_method = view_method
        self.etag_func = etag_func
        self.last_modified_func = last_modified_func
        self.schema_link_or_func = schema_link_or_func
        self.schema_rel_or_func = schema_rel_or_func
        self.schema_type_or_func = schema_type_or_func
        self.context_link_or_func = context_link_or_func
        self.context_rel_or_func = context_rel_or_func
        self.context_type_or_func = context_type_or_func
        self.error = error
        self.error_body_func = error_body_func
        self.object_validators = bool(object_validators)
        if location_func is not None and not callable(location_func):
            raise TypeError("location_func must be callable")
        self.location_func = location_func
        # A create describes the object it saved: see ``object_validators``.
        if (creates or location_func is not None) and not self.object_validators:
            raise TypeError("creates and location_func need object_validators")
        if location_func is not None and not creates:
            raise TypeError("location_func needs creates=True")
        self.creates = bool(creates)
        self.list_etag_func = list_etag_func
        self.list_last_modified_func = list_last_modified_func
        self.entity = entity
        self.statuses = statuses
        self.allowed_func = allowed_func
        self.base_url = base_url
        self._from_viewset = False

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return types.MethodType(self, obj)

    def __set_name__(self, owner: type, name: str) -> None:
        """Give an ``APIView`` with a class-body ``@scrudful`` method its OPTIONS.

        As :func:`scrudful_viewset` and :func:`scrudful_api_view` do (see
        :func:`_install_metadata`): DRF's stock ``SimpleMetadata`` and
        ``APIView.options`` are replaced, anything the class chose is kept.
        A class that is not an ``APIView``, e.g. a mixin, is left alone, but
        each ``APIView`` subclassing it gets its OPTIONS the same way when it
        is created (see :func:`_install_metadata_on_views`).
        """
        if self.has_list_validators and name in _NEVER_CREATES:
            raise TypeError(
                f"{owner.__qualname__}.{name}: list_etag_func and "
                "list_last_modified_func are evaluated for a create (a POST) "
                "only, and this handler never answers one. Move them to the "
                "post or create handler."
            )
        if issubclass(owner, APIView):
            _install_metadata(owner)
        else:
            _install_metadata_on_views(owner)

    @property
    def media_type(self) -> str:
        """The media type of the content: ``entity``'s, else ``application/json``."""
        return _DEFAULT_MEDIA_TYPE if self.entity is None else self.entity.media_type

    @property
    def has_validators(self) -> bool:
        """Whether a validator callback is configured."""
        return self.etag_func is not None or self.last_modified_func is not None

    @property
    def has_list_validators(self) -> bool:
        """Whether a list validator callback (of the collection created in) is set."""
        return (
            self.list_etag_func is not None or self.list_last_modified_func is not None
        )

    def __call__(self, *args: Any, **kwargs: Any) -> HttpResponseBase:
        index = _request_index(args)
        request = args[index]
        if getattr(request, _ACTIVE_ATTR, False):
            return self.view_method(*args, **kwargs)
        setattr(request, _ACTIVE_ATTR, True)
        try:
            return self._handle(request, args, kwargs, index)
        finally:
            setattr(request, _ACTIVE_ATTR, False)
            if self.object_validators and index > 0:
                _forget_object(args[0])

    def _handle(
        self,
        request: HttpRequest | Request,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        index: int,
    ) -> HttpResponseBase:
        cb_args, cb_kwargs = _callback_arguments(args, kwargs, index)
        method = request.method.upper()
        etag = last_modified = None
        view = args[0] if index > 0 else None
        if self.creates and view is not None:
            _record_created(view)
        if self.has_list_validators:
            headers = django_precondition_headers(request)
            # Only a POST carrying a precondition: the callbacks aren't called
            # for one without (nothing to evaluate, never a 428).
            if evaluates_create_preconditions(method, headers):
                list_etag, list_last_modified = _compute_validators(
                    self.list_etag_func,
                    self.list_last_modified_func,
                    cb_args,
                    cb_kwargs,
                )
                result = evaluate_preconditions(
                    method,
                    headers,
                    etag=list_etag,
                    last_modified=list_last_modified,
                    # The collection always has a representation.
                    exists=True,
                    require_precondition=False,
                )
                if result is not None:
                    return self._early_response(result, request, cb_args, cb_kwargs)
        elif (
            self.has_validators and not self.creates and evaluates_preconditions(method)
        ):
            exists = None
            if self.object_validators:
                # Authorization comes first: an exception propagates as the
                # view's own, and no validator is computed.
                obj = _load_object(view)
                # The handler gets this very instance, whether or not the
                # class memoizes get_object (scrudful_viewset does).
                view.get_object = lambda: obj
                etag, last_modified = self._validators((obj,), {})
                exists = True
            else:
                etag, last_modified = self._validators(cb_args, cb_kwargs)
            result = evaluate_preconditions(
                method,
                django_precondition_headers(request),
                etag=etag,
                last_modified=last_modified,
                exists=exists,
                require_precondition=True,
            )
            if result is not None:
                return self._early_response(result, request, cb_args, cb_kwargs)

        response = self.view_method(*args, **kwargs)

        if self.creates:
            if response.status_code == 201:
                self._created_headers(request, view, response)
        elif self.has_validators:
            refresh, send = validator_policy(method, response.status_code)
            if refresh and self.object_validators:
                etag = last_modified = None
                try:
                    _forget_object(view)
                    obj = _load_object(view, fresh=True)
                except (Http404, PermissionDenied, APIException):
                    pass
                else:
                    etag, last_modified = self._validators((obj,), {})
            elif refresh:
                etag, last_modified = self._validators(cb_args, cb_kwargs)
            if send:
                set_validator_headers(response, etag, last_modified)
        if has_default_entity(method, response.status_code):
            # A Link the view set with the relation of the schema link, or
            # either context relation, replaces that link (merge_links); one
            # with another relation replaces none. Another response the view
            # returns (an error, a 204, a redirect, or a TRACE) is not
            # described by its links: it gets none.
            link = merge_links(
                response.get("Link"),
                self.schema_link_header(*cb_args, **cb_kwargs),
                self.context_link_header(*cb_args, **cb_kwargs),
            )
            if link is not None:
                response["Link"] = link
        self.add_expose_headers(response)
        return response

    def _created_headers(
        self, request: HttpRequest | Request, view: Any, response: HttpResponseBase
    ) -> None:
        """Set the Location and validators of the object the create saved."""
        obj = getattr(request, _CREATED_ATTR, None)
        if obj is None:
            return
        if self.location_func is not None and "Location" not in response:
            location = self.location_func(view, request, obj)
            if location is not None:
                response["Location"] = iri_to_uri(str(location))
        etag, last_modified = self._validators((obj,), {})
        set_validator_headers(response, etag, last_modified)

    def _early_response(
        self,
        result: ConditionalResult,
        request: HttpRequest | Request,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> HttpResponseBase:
        """scrudful's own 304, 400, 412 or 428.

        With ``error``, a 400/412/428 carries its Links and is served as JSON
        with its media type, whatever renderer DRF's content negotiation
        picked (see :func:`_render_as_json`), so that it agrees with the
        HTTP Schema Resource.
        """
        result = with_error_body(self.error_body_func, request, result)
        if result.body is None or self.error is None:
            return _conditional_response(result, request)
        return _conditional_response(
            result,
            request,
            self.error_link_header(*args, **kwargs),
            self.error.media_type,
        )

    def _validators(
        self, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> tuple[str | None, int | None]:
        return _compute_validators(
            self.etag_func, self.last_modified_func, args, kwargs
        )

    def add_expose_headers(self, response: HttpResponseBase) -> None:
        """Add the ETag/Link/Location headers present to the expose header."""
        expose_headers(response)

    def schema_link(self, *args: Any, **kwargs: Any) -> Any:
        """Return the schema URL, or ``None``."""
        return _evaluate(self.schema_link_or_func, args, kwargs) or None

    def context_link(self, *args: Any, **kwargs: Any) -> Any:
        """Return the JSON-LD context URL, or ``None``."""
        return _evaluate(self.context_link_or_func, args, kwargs) or None

    def schema_link_header(self, *args: Any, **kwargs: Any) -> str | None:
        """Return the schema link-value, or ``None``.

        A relative URL is resolved against the base URL (see
        :meth:`live_base_url`), else against the absolute URI of the request
        among ``args``.
        """
        return _link_value(
            self.schema_link(*args, **kwargs),
            (self.schema_rel_or_func, self.schema_type_or_func),
            (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
            args,
            kwargs,
            base=self.live_base_url(args),
        )

    def context_link_header(self, *args: Any, **kwargs: Any) -> str | None:
        """Return the RDF context link-value, or ``None``.

        Its defaults depend on :attr:`media_type` (JSON-LD for JSON content;
        none for in-band RDF, which gets no context link). The URL is
        resolved like the schema URL.
        """
        defaults = context_defaults(self.media_type)
        if defaults == (None, None):
            return None
        return _link_value(
            self.context_link(*args, **kwargs),
            (self.context_rel_or_func, self.context_type_or_func),
            defaults,
            args,
            kwargs,
            context_of=self.media_type,
            base=self.live_base_url(args),
        )

    def live_base_url(self, args: Sequence[Any]) -> str | None:
        """The base URL of the responses: ``base_url``, else the view's
        ``Meta.base_url`` (``args[0]``, when a view), else
        ``SCRUDFUL["BASE_URL"]``; ``None`` resolves against the request."""
        if self.base_url is not None:
            return self.base_url
        view = args[0] if args and isinstance(args[0], APIView) else None
        return _view_base_url(view)

    def link_header(self, *args: Any, **kwargs: Any) -> str | None:
        """Return the Link field value of the schema and context links."""
        return join_links(
            self.schema_link_header(*args, **kwargs),
            self.context_link_header(*args, **kwargs),
        )

    def error_link_header(self, *args: Any, **kwargs: Any) -> str | None:
        """Return the Link field value of ``error``'s links, or ``None``."""
        if self.error is None:
            return None
        return join_links(
            *_entity_links(self.error, args, kwargs, self.live_base_url(args))
        )


def scrudful(
    *,
    etag_func: Callable[..., Any] | None = None,
    last_modified_func: Callable[..., Any] | None = None,
    schema_link_or_func: Any = None,
    schema_rel_or_func: Any = None,
    schema_type_or_func: Any = None,
    context_link_or_func: Any = None,
    context_rel_or_func: Any = None,
    context_type_or_func: Any = None,
    error: Entity | None = None,
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None = None,
    object_validators: bool = False,
    location_func: Callable[..., Any] | None = None,
    creates: bool = False,
    list_etag_func: Callable[..., Any] | None = None,
    list_last_modified_func: Callable[..., Any] | None = None,
    entity: Entity | None = None,
    statuses: Mapping[str, Sequence[int]] | None = None,
    allowed_func: Callable[..., Any] | None = None,
    base_url: str | None = None,
) -> Callable[[Callable[..., Any]], ScrudfulViewFunc]:
    """Decorate a view method or function view to make it SCRUDful.

    Works on viewset and ``APIView`` methods (including ``@action`` methods,
    in either decorator order) and on function views, below ``@api_view`` or
    on plain Django views::

        class ItemViewSet(viewsets.ModelViewSet):
            @scrudful(etag_func=item_etag, last_modified_func=item_modified)
            def retrieve(self, request, pk): ...

        @api_view(["GET", "PUT"])
        @scrudful(etag_func=lambda request, pk: ...)
        def item(request, pk): ...

    All arguments are keyword-only; they are described in
    :class:`ScrudfulViewFunc`, and the behaviour in the module docstring.
    Decorating an ``async def`` view raises ``TypeError``: DRF views are
    synchronous.

    In the body of an ``APIView`` (or viewset) class, the decorated method
    also makes the class answer OPTIONS with :class:`ScrudfulMetadata`, as
    :func:`scrudful_viewset` does: DRF's stock ``SimpleMetadata`` and
    ``options`` are replaced, a class's own are kept. Below ``@api_view``
    there is no class yet, and the view keeps DRF's ``SimpleMetadata`` and
    OPTIONS: use :func:`scrudful_api_view` in place of ``@api_view`` +
    ``@scrudful`` for scrudful's.
    """
    options = {
        "etag_func": etag_func,
        "last_modified_func": last_modified_func,
        "schema_link_or_func": schema_link_or_func,
        "schema_rel_or_func": schema_rel_or_func,
        "schema_type_or_func": schema_type_or_func,
        "context_link_or_func": context_link_or_func,
        "context_rel_or_func": context_rel_or_func,
        "context_type_or_func": context_type_or_func,
        "error": error,
        "error_body_func": error_body_func,
        "object_validators": object_validators,
        "location_func": location_func,
        "creates": creates,
        "list_etag_func": list_etag_func,
        "list_last_modified_func": list_last_modified_func,
        "entity": entity,
        "statuses": statuses,
        "allowed_func": allowed_func,
        "base_url": base_url,
    }
    _check_view_options(
        entity,
        statuses,
        allowed_func,
        base_url,
        **{name: options[name] for name in _LINK_OPTIONS},
    )
    check_validators(etag_func, last_modified_func)
    _check_list_validators(
        etag_func, last_modified_func, list_etag_func, list_last_modified_func, creates
    )
    _check_error(error)
    check_error_body_func(error_body_func)
    if object_validators and etag_func is None and last_modified_func is None:
        raise TypeError("object_validators needs an etag_func or last_modified_func")

    def decorator(view_method: Callable[..., Any]) -> ScrudfulViewFunc:
        return ScrudfulViewFunc(view_method, **options)

    return decorator


def scrudful_api_view(
    http_method_names: Sequence[str] | None = None,
    *,
    etag_func: Callable[..., Any] | None = None,
    last_modified_func: Callable[..., Any] | None = None,
    schema_link_or_func: Any = None,
    schema_rel_or_func: Any = None,
    schema_type_or_func: Any = None,
    context_link_or_func: Any = None,
    context_rel_or_func: Any = None,
    context_type_or_func: Any = None,
    error: Entity | None = None,
    error_body_func: Callable[[Any, ConditionalResult], dict[str, Any]] | None = None,
    object_validators: bool = False,
    entity: Entity | None = None,
    statuses: Mapping[str, Sequence[int]] | None = None,
    allowed_func: Callable[..., Any] | None = None,
    base_url: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Like DRF's ``api_view``, for a SCRUDful function view.

    ``http_method_names`` is the list of allowed methods (default
    ``["GET"]``), as for ``api_view``; the other arguments are keyword-only and
    described in :class:`ScrudfulViewFunc`. The view is built by DRF's
    ``api_view``, so the policy decorators (``@renderer_classes``,
    ``@parser_classes``, ``@authentication_classes``, ``@throttle_classes``,
    ``@permission_classes``, ``@schema`` ...) work as usual, and every method
    handler is wrapped in a :class:`ScrudfulViewFunc`. The callbacks are
    called as ``func(view, request, **url_kwargs)``. OPTIONS is answered by
    :class:`ScrudfulMetadata` unless the view's ``metadata_class`` (a
    ``metadata_class`` attribute of the function, or ``DEFAULT_METADATA_CLASS``)
    is something other than DRF's default ``SimpleMetadata``; a custom class
    (or ``None``) is kept, and OPTIONS is then DRF's (its 200 body, or a 405).
    With :class:`ScrudfulMetadata` (or a subclass, which can't override
    ``determine_metadata``), OPTIONS is scrudful's 204 (see "OPTIONS" in the
    module docstring): a view with a custom metadata class gets it by making
    that class subclass :class:`ScrudfulMetadata`. DRF's ``api_view``
    serves only the listed methods: list ``"HEAD"`` too for HEAD requests.
    ::

        @scrudful_api_view(["GET", "PUT"], etag_func=settings_etag)
        def site_settings(request): ...
    """
    if callable(http_method_names):
        raise TypeError(
            "scrudful_api_view must be called with parentheses: "
            "@scrudful_api_view() or @scrudful_api_view(['GET', ...])"
        )
    if http_method_names is None:
        http_method_names = ["GET"]
    if not isinstance(http_method_names, (list, tuple)):
        raise TypeError(
            "scrudful_api_view expected a list of method names, not "
            f"{type(http_method_names).__name__}"
        )
    if object_validators:
        raise TypeError("object_validators needs a view with get_object()")
    decorate = scrudful(
        etag_func=etag_func,
        last_modified_func=last_modified_func,
        schema_link_or_func=schema_link_or_func,
        schema_rel_or_func=schema_rel_or_func,
        schema_type_or_func=schema_type_or_func,
        context_link_or_func=context_link_or_func,
        context_rel_or_func=context_rel_or_func,
        context_type_or_func=context_type_or_func,
        error=error,
        error_body_func=error_body_func,
        entity=entity,
        statuses=statuses,
        allowed_func=allowed_func,
        base_url=base_url,
    )

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        _check_sync(func)
        view = api_view(list(http_method_names))(func)

        def handler(self: APIView, *args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        functools.update_wrapper(handler, func, updated=())
        scrudful_handler = decorate(handler)
        for method in http_method_names:
            setattr(view.cls, method.lower(), scrudful_handler)
        # A metadata_class set on the function is the view's choice. DRF's
        # api_view copies it onto the view class only after 3.15.0, the
        # minimum: copy it here, before _install_metadata keeps or replaces it.
        if hasattr(func, "metadata_class"):
            view.cls.metadata_class = func.metadata_class
        _install_metadata(view.cls)
        return view

    return decorator


def scrudful_viewset(cls: type) -> type:
    """Class decorator to make a DRF viewset SCRUDful.

    The configuration is read from an inner ``Meta`` class (it may be
    inherited). ``retrieve``, ``update``, ``partial_update`` and ``destroy``
    are wrapped with the item configuration below, ``list`` with the
    ``list_*`` configuration, and ``create`` with the item *link*
    configuration, ``error`` and ``error_body_func``, and the list
    validators: its target is the collection, so its preconditions are
    evaluated against the list's validators (see ``list_etag_func`` on
    :class:`ScrudfulViewFunc`), and the item validators describe only the
    object it saves (with ``object_validators``). Methods
    the class doesn't have are not added, and methods decorated with
    :func:`scrudful` explicitly are left alone: an explicit ``create`` must
    then be given list validators of its own when ``Meta`` sets
    ``list_etag_func`` or ``list_last_modified_func`` (``TypeError``
    otherwise), so that its preconditions are never silently unevaluated.
    OPTIONS is answered by :class:`ScrudfulMetadata` only when the class's
    ``metadata_class`` is DRF's default ``SimpleMetadata``; any other
    value (``None``, or a class set on the viewset, a base class or
    ``DEFAULT_METADATA_CLASS``) is kept, and OPTIONS is then DRF's (its 200
    body, or a 405). With :class:`ScrudfulMetadata` (or a subclass, which
    can't override ``determine_metadata``), OPTIONS is scrudful's 204 (see
    "OPTIONS" in the module docstring): a custom metadata class that
    subclasses :class:`ScrudfulMetadata` (or mixes it in first) switches it
    on. A user-defined ``options`` is kept.

    Decorating a subclass of a decorated viewset again re-wraps the original
    methods with the subclass's configuration.

    Callbacks are called as ``func(view, request, **url_kwargs)``; callables
    stored on ``Meta`` are plain functions (``staticmethod`` works too).

    .. warning::

       The item link callbacks (``schema_link_or_func`` and friends) also run
       for ``create`` and POST on the list route, where there is no ``pk``,
       and, when the HTTP Schema Resource is built, as ``func(view, None)``
       (see :func:`http_schema_resource`). Accept ``**kwargs`` (``def
       schema_url(view, request, **kwargs)``) so they work for all of them.

    .. warning::

       An explicit ``@scrudful`` on ``update`` applies to PATCH when
       ``partial_update`` is DRF's stock ``UpdateModelMixin.partial_update``
       (it is then not wrapped; a subclass of a decorated viewset inherits the
       parent's wrapper, so decorate the subclass with :func:`scrudful_viewset`
       too). With a user-defined ``partial_update`` that calls ``update``, the
       wrapper that ``Meta`` configures for ``partial_update`` handles the
       request first and the explicit ``update`` decorator is a pass-through
       for PATCH. Decorate ``partial_update`` explicitly as well to override
       it.

    ::

        @scrudful_viewset
        class ItemViewSet(viewsets.ModelViewSet):
            class Meta:
                etag_func = item_etag
                last_modified_func = item_last_modified
                schema_link_or_func = "https://example.com/item.schema.json"

    The ``Meta`` attributes are:

    etag_func, last_modified_func : callable, optional
        Item validators (see :class:`ScrudfulViewFunc`).
    object_validators : bool, optional
        Compute the item validators from ``get_object()`` (see
        :class:`ScrudfulViewFunc`); the class's ``get_object`` is wrapped to
        return the same instance for the whole request.
    schema_link_or_func, schema_rel_or_func, schema_type_or_func : optional
        Item schema link.
    context_link_or_func, context_rel_or_func, context_type_or_func : optional
        Item JSON-LD context link.
    error : Entity, optional
        Schema and context of the item routes' (and the create's) 400, 412
        and 428 bodies (see :class:`ScrudfulViewFunc`).
    list_etag_func, list_last_modified_func : callable, optional
        Validators of the ``list`` response, against which the preconditions
        of ``create`` are evaluated too (a 400 or 412, never a 428; see
        :class:`ScrudfulViewFunc`).
    list_schema_link_or_func, list_schema_rel_or_func, list_schema_type_or_func
        Schema link of the ``list`` response.
    list_context_link_or_func, list_context_rel_or_func, list_context_type_or_func
        JSON-LD context link of the ``list`` response.
    list_error : Entity, optional
        Schema and context of the ``list`` response's 400 and 412 bodies.
    error_body_func : callable, optional
        As for :class:`ScrudfulViewFunc`, for the item routes and the create
        (whose 400 and 412 carry ``error``).
    list_error_body_func : callable, optional
        As ``error_body_func``, for the ``list`` response's 400 and 412.
    entity, list_entity : Entity, optional
        The item's (and the create's) and the list's content, in place of
        the item and ``list_`` link options (see :class:`ScrudfulViewFunc`).
    statuses : mapping, optional
        The success statuses of each method (see :class:`ScrudfulViewFunc`),
        for every handler of the class, those it doesn't wrap (``@action``
        methods) included unless they give their own.
    allowed_func : callable, optional
        ``allowed_func(view, request, method, obj)``, for the ``Allow`` of
        OPTIONS (see :class:`ScrudfulViewFunc`).
    base_url : str, optional
        The absolute URL relative link URLs are resolved against (see
        :class:`ScrudfulViewFunc`), for the errors, every handler of the
        class and the HTTP Schema Resource link too.
    hsr_route : bool, optional
        ``False`` takes the class's routes out of :func:`hsr_urls` (and needs
        ``hsr_url``); ``True`` keeps them in with ``SCRUDFUL["HSR_ROUTE"]``
        set to ``False``. Default: ``SCRUDFUL["HSR_ROUTE"]``, else ``True``.
    hsr_url : callable, optional
        ``hsr_url(view_name)``, the URL of the HTTP Schema Resource of the
        route named ``view_name`` (namespace-qualified, as
        ``request.resolver_match.view_name``), where it is served elsewhere,
        in place of ``SCRUDFUL["HSR_URL"]`` and the :func:`hsr_urls` route.
    hsr_cache_control : str, optional
        The ``Cache-Control`` of the :func:`hsr_urls` route's responses, in
        place of ``SCRUDFUL["HSR_CACHE_CONTROL"]`` (default ``public,
        max-age=300``).
    href_func : callable, optional
        ``href_func(view, request, obj)``, the URL of an item, for
        :class:`ScrudfulPagination` (required by it; not used by the wrapped
        methods).
    """
    meta = getattr(cls, "Meta", None)

    def options(prefix: str, names: Sequence[str]) -> dict[str, Any]:
        return {name: getattr(meta, prefix + name, None) for name in names}

    shared = options("", _VIEW_OPTIONS)
    shared["statuses"] = _check_view_options(None, **shared)
    hsr = options("", _HSR_OPTIONS)
    _check_hsr_options(**hsr)
    # For the handlers it doesn't wrap (an @action's, say), the errors and
    # the HTTP Schema Resource route.
    setattr(cls, _META_OPTIONS_ATTR, {**shared, **hsr})
    item = options(
        "",
        _VALIDATOR_OPTIONS
        + _LINK_OPTIONS
        + _ENTITY_OPTIONS
        + _ERROR_OPTIONS
        + _ERROR_BODY_OPTIONS
        + _OBJECT_OPTIONS,
    )
    # Its preconditions are evaluated against the collection's validators;
    # its 400/412 carry the item ``error``, so they get its body too.
    create = options(
        "",
        _LINK_OPTIONS
        + _ENTITY_OPTIONS
        + _ERROR_OPTIONS
        + _ERROR_BODY_OPTIONS
        + _LIST_VALIDATOR_OPTIONS,
    )
    if item["object_validators"]:
        if not callable(getattr(cls, "get_object", None)):
            raise TypeError("object_validators needs a view with get_object()")
        _memoize_get_object(cls)
        # The create's validators and Location describe the saved object.
        create.update(
            options("", _VALIDATOR_OPTIONS + _OBJECT_OPTIONS),
            location_func=getattr(meta, "location_func", None),
            creates=True,
        )
    else:
        item["object_validators"] = False
    configs = {
        "create": create,
        "retrieve": item,
        "update": item,
        "partial_update": item,
        "destroy": item,
        "list": options(
            "list_",
            _VALIDATOR_OPTIONS
            + _LINK_OPTIONS
            + _ENTITY_OPTIONS
            + _ERROR_OPTIONS
            + _ERROR_BODY_OPTIONS,
        ),
    }
    for name, config in configs.items():
        config.update(shared)
        method = getattr(cls, name, None)
        if method is None:
            continue
        if isinstance(method, ScrudfulViewFunc):
            if not method._from_viewset:
                _check_explicit_create(cls, name, method, config)
                continue
            method = method.view_method
        if name == "partial_update" and method is _STOCK_PARTIAL_UPDATE:
            update = getattr(cls, "update", None)
            if isinstance(update, ScrudfulViewFunc) and not update._from_viewset:
                # PATCH goes straight to the explicitly decorated update.
                setattr(cls, name, method)
                continue
        wrapper = ScrudfulViewFunc(method, **config)
        wrapper._from_viewset = True
        setattr(cls, name, wrapper)
    _install_metadata(cls)
    return cls


def _check_explicit_create(
    cls: type, name: str, method: ScrudfulViewFunc, config: dict[str, Any]
) -> None:
    """Raise ``TypeError`` if an explicit ``create`` would drop ``Meta``'s
    list validators, leaving its preconditions unevaluated."""
    if name != "create" or method.has_list_validators:
        return
    dropped = [key for key in _LIST_VALIDATOR_OPTIONS if config[key] is not None]
    if dropped:
        raise TypeError(
            f"{cls.__qualname__}.create is decorated with @scrudful, so "
            f"Meta.{' and Meta.'.join(dropped)} don't apply to it and its "
            "preconditions would never be evaluated: pass "
            f"{', '.join(f'{key}=' for key in dropped)} to its @scrudful."
        )


def _memoize_get_object(cls: type) -> None:
    """Make ``cls.get_object`` return one instance per request (once wrapped)."""
    original = cls.get_object  # type: ignore[attr-defined]
    if getattr(original, "_scrudful_memo", False):
        return

    @functools.wraps(original)
    def get_object(self: Any) -> Any:
        request = self.request
        obj = getattr(request, _OBJECT_ATTR, None)
        if obj is None:
            obj = original(self)
            setattr(request, _OBJECT_ATTR, obj)
        return obj

    get_object._scrudful_memo = True  # type: ignore[attr-defined]
    cls.get_object = get_object  # type: ignore[attr-defined]


def _record_created(view: Any) -> None:
    """Make ``view.perform_create`` record the saved instance on the request.

    The wrapper passes any extra arguments (``perform_create(serializer,
    owner=...)``) through and returns what ``perform_create`` returns.
    """
    perform_create = getattr(view, "perform_create", None)
    if not callable(perform_create):
        return
    request = view.request
    setattr(request, _CREATED_ATTR, None)

    def record(serializer: Any, *args: Any, **kwargs: Any) -> Any:
        result = perform_create(serializer, *args, **kwargs)
        # A many=True create saves a list: no single object to describe.
        created = None
        if not getattr(serializer, "many", False):
            created = getattr(serializer, "instance", None)
        setattr(request, _CREATED_ATTR, created)
        return result

    view.perform_create = record


def _forget_object(view: Any) -> None:
    """Drop the per-call ``get_object`` override of :meth:`ScrudfulViewFunc._handle`."""
    view.__dict__.pop("get_object", None)


def _load_object(view: Any, fresh: bool = False) -> Any:
    """``view.get_object()``, after forgetting the memoized object if ``fresh``."""
    get_object = getattr(view, "get_object", None)
    if not callable(get_object):
        raise TypeError("object_validators needs a view with get_object()")
    if fresh:
        try:
            delattr(view.request, _OBJECT_ATTR)
        except AttributeError:
            pass
    return get_object()


def _install_metadata(cls: type) -> None:
    """Give the view class scrudful's OPTIONS, unless it chose its own.

    Only DRF's stock default metadata class, ``SimpleMetadata``, is replaced
    by :class:`ScrudfulMetadata`: ``None`` and any other class (set on the
    class, a base class or ``DEFAULT_METADATA_CLASS``) are the project's
    choice and are kept. DRF's stock ``APIView.options`` is replaced by
    :func:`_options`; an ``options`` the class defines is kept. The class's
    ``dispatch`` and ``initial`` are wrapped (:func:`_preflight_dispatch`,
    :func:`_options_initial`), once; the wrappers act only when scrudful
    answers the request's OPTIONS (:func:`_answers_options`). So is its
    ``finalize_response`` (:func:`_finalize_response`), which gives every
    response of a scrudful view the HTTP Schema Resource link.
    """
    if getattr(cls, "metadata_class", None) is SimpleMetadata:
        cls.metadata_class = ScrudfulMetadata  # type: ignore[attr-defined]
    if getattr(cls, "options", None) is APIView.options:
        cls.options = _options  # type: ignore[attr-defined]
    _wrap_once(cls, "dispatch", _preflight_dispatch)
    _wrap_once(cls, "initial", _options_initial)
    _wrap_once(cls, "finalize_response", _finalize_response)


#: Marks the ``dispatch``, ``initial`` and ``finalize_response`` wrappers of
#: :func:`_install_metadata`.
_OPTIONS_WRAPPER = "_scrudful_options_wrapper"


def _wrap_once(
    cls: type, name: str, wrap: Callable[[Callable[..., Any]], Callable[..., Any]]
) -> None:
    """Set ``cls.<name>`` to ``wrap(cls.<name>)``, unless it is a wrapper
    already (a base class's, which ``cls`` inherits)."""
    current = getattr(cls, name)
    if getattr(current, _OPTIONS_WRAPPER, False):
        return
    wrapper = wrap(current)
    setattr(wrapper, _OPTIONS_WRAPPER, True)
    setattr(cls, name, wrapper)


#: Set on a class whose ``APIView`` subclasses :func:`_install_metadata` sets up.
_INSTALLS_METADATA = "_scrudful_installs_metadata"


def _install_metadata_on_views(mixin: type) -> None:
    """Have :func:`_install_metadata` set up each ``APIView`` subclass of ``mixin``.

    For a mixin with ``@scrudful`` methods: its ``__init_subclass__`` is
    replaced by one that calls the mixin's own (or, without one, the next
    class's in the MRO, as ``super()`` does) and then, on an ``APIView``
    subclass, :func:`_install_metadata`, which keeps a ``metadata_class`` or
    ``options`` the subclass or a class between it and the mixin chose. Once
    per mixin, whatever the number of ``@scrudful`` methods.
    """
    if vars(mixin).get(_INSTALLS_METADATA):
        return
    own = vars(mixin).get("__init_subclass__")

    def __init_subclass__(cls: type, **kwargs: Any) -> None:
        if own is not None:
            # A classmethod (as the class body makes it), bound to ``cls``.
            own.__get__(None, cls)(**kwargs)
        else:
            super(mixin, cls).__init_subclass__(**kwargs)  # type: ignore[misc]
        if issubclass(cls, APIView):
            _install_metadata(cls)

    mixin.__init_subclass__ = classmethod(__init_subclass__)  # type: ignore[assignment]
    setattr(mixin, _INSTALLS_METADATA, True)


def _options(self: APIView, request: Request, *args: Any, **kwargs: Any) -> Any:
    """``APIView.options``: a 204 with the requester's ``Allow`` and the HTTP
    Schema Resource link (see "OPTIONS" in the module docstring).

    When scrudful answers the view's OPTIONS (:func:`_answers_options`), and
    after :func:`_options_initial` authenticated and throttled the request
    (a CORS preflight never gets here: :func:`_preflight_dispatch`):

    1. the HSR link is resolved (:func:`_response_hsr_link`, before anything
       is loaded; its ``ImproperlyConfigured`` propagates; an unrouted
       request has none);
    2. each method the view serves is checked for this requester, with its
       view-level permissions, then the object is loaded only if one of them
       passed (a method with an ``allowed_func`` always does), then the
       methods that passed are checked against the object
       (:func:`_allowed_methods`);
    3. the answer is a 204 without content or ``Content-Type`` (rendered by
       DRF's ``JSONRenderer``, whatever renderer DRF's content negotiation
       picked), ``Allow: allow_value(...)`` (also set in ``view.headers``,
       which DRF's ``finalize_response`` writes over the response's), the HSR
       link and the expose headers.

    An exception of step 2 (the ``Http404`` of a missing object, the 404
    scoped-rbac gives an object the requester may not read, an object check
    raising ``Http404``, a check's unexpected error) is answered by the
    view's ``handle_exception``, as DRF answers a handler's: with the error
    Links of :func:`exception_handler`, and the HSR link that
    :func:`_finalize_response` gives every response; one the exception
    handler doesn't answer propagates.

    Otherwise (``metadata_class = None`` or a metadata class that isn't a
    :class:`ScrudfulMetadata`), this is DRF's ``APIView.options``: a 405, or
    a 200 with the metadata class's body. An ``Accept`` none of the view's
    renderers serves still gets DRF's 406, raised before this runs.
    """
    if not _answers_options(self):
        return APIView.options(self, request, *args, **kwargs)
    hsr = _response_hsr_link(self, request)
    try:
        allowed = _allowed_methods(self, request)
    except Exception as exc:
        # finalize_response adds the HSR link and the expose headers.
        return self.handle_exception(exc)
    allow = allow_value(allowed)
    request.accepted_renderer = JSONRenderer()
    request.accepted_media_type = JSONRenderer.media_type
    # No data: JSONRenderer renders b"", and DRF drops the Content-Type.
    response = Response(status=204)
    response["Allow"] = allow
    # DRF's finalize_response sets the view's own headers, Allow among them.
    headers = getattr(self, "headers", None)
    if isinstance(headers, dict):
        headers["Allow"] = allow
    if hsr is not None:
        response["Link"] = hsr
    expose_headers(response)
    return response


def _answers_options(view: APIView) -> bool:
    """Whether scrudful answers ``view``'s OPTIONS: its ``options`` is
    :func:`_options` (as :func:`_install_metadata` installs it), its
    metadata class is :class:`ScrudfulMetadata` (or a subclass), and it
    serves OPTIONS (``"options"`` is in its ``http_method_names``)."""
    options = getattr(getattr(view, "options", None), "__func__", None)
    metadata_class = getattr(view, "metadata_class", None)
    return (
        options is _options
        and isinstance(metadata_class, type)
        and issubclass(metadata_class, ScrudfulMetadata)
        and "options" in getattr(view, "http_method_names", ())
    )


def _preflight_dispatch(dispatch: Callable[..., Any]) -> Callable[..., Any]:
    """The ``dispatch`` of a view class with scrudful's OPTIONS: a CORS
    preflight (:func:`scrudful._hsr.is_preflight`) that scrudful answers
    (:func:`_answers_options`) gets a 204 without content, ``Content-Type``
    or Links, before DRF's ``dispatch``. So before ``initialize_request`` and
    ``initial()``: no content negotiation, authentication (and so no CSRF
    check, which DRF's ``SessionAuthentication`` makes), permission check,
    throttle or loading runs for it, and no scrudful handler is called.

    Django middleware runs first: CORS middleware (django-cors-headers, say)
    adds its headers to this response, or answers the preflight itself.
    """

    @functools.wraps(dispatch)
    def scrudful_dispatch(
        self: APIView, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> Any:
        if is_preflight(request.method or "", request.headers.get) and (
            _answers_options(self)
        ):
            return no_content()
        return dispatch(self, request, *args, **kwargs)

    return scrudful_dispatch


def _options_initial(initial: Callable[..., Any]) -> Callable[..., Any]:
    """The ``initial`` of a view class with scrudful's OPTIONS: for an
    OPTIONS that scrudful answers (:func:`_answers_options`), DRF's
    ``initial()`` without ``check_permissions``. Content negotiation,
    versioning, authentication (``perform_authentication``: bad credentials
    are DRF's 401) and throttles (``check_throttles``, once, as DRF throttles
    the view) run; the permission checks don't gate OPTIONS itself, as they
    are what its ``Allow`` reports (:func:`_allowed_methods`).
    """

    @functools.wraps(initial)
    def scrudful_initial(
        self: APIView, request: Request, *args: Any, **kwargs: Any
    ) -> Any:
        if request.method != "OPTIONS" or not _answers_options(self):
            return initial(self, request, *args, **kwargs)
        with _instance_attributes(self, check_permissions=_no_check):
            return initial(self, request, *args, **kwargs)

    return scrudful_initial


def _finalize_response(finalize: Callable[..., Any]) -> Callable[..., Any]:
    """The ``finalize_response`` of a view class with scrudful's OPTIONS
    (see :func:`_install_metadata`): DRF's, then, on a scrudful view
    (:func:`_is_scrudful_view`), what every response it answers gets.

    DRF finalizes every response ``dispatch`` answers: the handler's,
    scrudful's own 304/400/412/428, and the errors its exception handler
    answers, those of ``initial()`` (401, 403, 406, 429 ...) and of OPTIONS
    included, whatever the project's ``EXCEPTION_HANDLER``. Each gets:

    - the HTTP Schema Resource link (:func:`_response_hsr_link`), merged
      into the response's Link (a Link with the HSR relation the handler
      set wins, see :func:`scrudful._adapter.merge_links`);
    - ``Vary: Accept`` when one of the view's ``renderer_classes`` renders
      something other than JSON (the browsable API's HTML): the view then
      chooses between described and undescribed content by ``Accept``;
    - the expose headers (``Link`` and ``Allow`` always).

    A CORS preflight scrudful answers never gets here
    (:func:`_preflight_dispatch`), and an exception the exception handler
    doesn't answer (a 500) propagates past it, to Django's ``handler500``.
    """

    @functools.wraps(finalize)
    def scrudful_finalize_response(
        self: APIView,
        request: Request,
        response: HttpResponseBase,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        response = finalize(self, request, response, *args, **kwargs)
        if not _is_scrudful_view(self):
            return response
        hsr = _response_hsr_link(self, request)
        if hsr is not None:
            link = merge_links(response.get("Link"), None, None, hsr)
            if link is not None:
                response["Link"] = link
        if _renders_non_json(self):
            patch_vary_headers(response, ("Accept",))
        expose_headers(response)
        return response

    return scrudful_finalize_response


def _renders_non_json(view: APIView) -> bool:
    """Whether one of ``view``'s ``renderer_classes`` has a media type that
    isn't written as JSON (``application/json`` or ``+json``, JSON-LD's
    included)."""
    for renderer_class in getattr(view, "renderer_classes", ()):
        media_type = getattr(renderer_class, "media_type", None)
        if not (isinstance(media_type, str) and is_json_syntax(media_type)):
            return True
    return False


def _no_check(*args: Any) -> None:
    """In place of ``check_permissions`` / ``check_object_permissions``."""


def _options_object(view: APIView, request: Request, method: str) -> Any:
    """The object an OPTIONS of ``view`` is about, for the object-level checks
    of its ``Allow``, loaded as a ``method`` request loads it: the view's
    ``get_object()``, with a request of ``method`` as ``view.request`` and,
    on a viewset, the action ``method`` maps to as ``view.action`` (so that
    a ``get_queryset`` scoped by action scopes it as for that method), and
    ``check_object_permissions`` suppressed (the checks of each method are
    :func:`_allowed_methods`'s); the view's state is put back after.
    ``None`` where the view loads no object (:func:`_loads_object`: a
    collection, or a view without ``get_object``). What ``get_object()``
    raises propagates (``Http404`` for a missing object).
    """
    values: dict[str, Any] = {
        "request": _clone_request(request, method),
        "check_object_permissions": _no_check,
    }
    action_map = getattr(view, "action_map", None)
    if isinstance(action_map, dict):
        values["action"] = action_map.get(method.lower())
    if not _loads_object(view, values.get("action")):
        return None
    with _instance_attributes(view, **values):
        return view.get_object()


#: The actions of a viewset's item routes (``detail=True`` for an ``@action``).
_DETAIL_ACTIONS = frozenset({"retrieve", "update", "partial_update", "destroy"})


def _loads_object(view: APIView, action: str | None) -> bool:
    """Whether ``view``'s route is an item's, whose object ``get_object()``
    loads: the view has a ``get_object`` and

    - its lookup kwarg (``lookup_url_kwarg``, else ``lookup_field``) is in
      the URL kwargs; or, with a ``get_object`` of the view's own (DRF's
      needs the lookup kwarg), whatever the kwarg is named,
    - on a viewset, ``action`` (the checked method's) is an item action:
      ``retrieve``, ``update``, ``partial_update``, ``destroy``, or an
      ``@action(detail=True)``;
    - on another view, the route has a URL kwarg (but DRF's ``format``).
    """
    if not callable(getattr(view, "get_object", None)):
        return False
    kwargs = getattr(view, "kwargs", None) or {}
    lookup = getattr(view, "lookup_url_kwarg", None) or getattr(
        view, "lookup_field", None
    )
    if lookup is not None and lookup in kwargs:
        return True
    if inspect.unwrap(getattr(type(view), "get_object")) is GenericAPIView.get_object:
        return False
    if isinstance(getattr(view, "action_map", None), dict):
        if action in _DETAIL_ACTIONS:
            return True
        handler = getattr(type(view), action, None) if action else None
        return getattr(handler, "detail", None) is True
    return any(name != "format" for name in kwargs)


#: What a per-method check of OPTIONS raises for "not allowed".
#:
#: ``Http404`` is deliberately absent (ruling R16), unlike ninja's denials:
#: a check that raises it, as DRF's ``DjangoObjectPermissions`` does for an
#: object the requester may not read, hides the object, so the OPTIONS
#: answers that 404 (as a GET would) rather than an ``Allow`` that reveals
#: the object exists.
_DENIALS = (DRFPermissionDenied, NotAuthenticated, PermissionDenied)


def _allowed_methods(view: APIView, request: Request) -> list[str]:
    """The methods of ``view.allowed_methods`` (those it serves) that the
    requester of the OPTIONS ``request`` may use, but OPTIONS, which
    ``allow_value`` always adds. HEAD, which DRF dispatches to the GET
    handler, is GET's answer; it is listed only when the view serves it (a
    ``scrudful_api_view(["GET"])`` doesn't).

    In two passes (:func:`_may_use`), so that a requester whom the view-level
    permissions refuse every method can't tell which objects exist (ruling
    R17):

    1. each method's view-level check: ``check_permissions``, or ``True``
       for a method with an ``allowed_func``, which needs the object;
    2. only when one of them passed, the object is loaded
       (:func:`_options_object`), as the first method that passed (GET,
       when it did) loads it, and each method that passed is checked
       again on it: ``check_object_permissions`` (when there is an
       object), or its ``allowed_func``. When none passed, nothing is
       loaded: a missing and an existing object both give ``Allow:
       OPTIONS``, as a GET of either is refused alike.

    A method with an ``allowed_func`` always passes pass 1 (ruling R17a), so
    a view with one loads the object for every requester: OPTIONS of a
    missing object is then its 404 whoever asks, an existence oracle unless
    ``get_object()`` (its queryset) hides the objects the requester may not
    see.
    """
    served = [method.upper() for method in view.allowed_methods]
    checked = [
        method
        for method in served
        if method != "OPTIONS" and not (method == "HEAD" and "GET" in served)
    ]
    hooks = {method: _allowed_func(view, method) for method in checked}
    allowed = {
        method: hooks[method] is not None
        or _may_use(view, request, method, view.check_permissions)
        for method in checked
    }
    # Loaded as the first method that passed loads it: GET's, when it did.
    loader = next((method for method in checked if allowed[method]), None)
    obj = None if loader is None else _options_object(view, request, loader)
    for method in checked:
        hook = hooks[method]
        if hook is not None:
            allowed[method] = _may_use(
                view, request, method, functools.partial(_hooked, hook, view, obj)
            )
        elif allowed[method] and obj is not None:
            allowed[method] = _may_use(
                view,
                request,
                method,
                functools.partial(_object_check, view, obj),
            )
    if "HEAD" in served and "GET" in served:
        allowed["HEAD"] = allowed["GET"]
    return [method for method, may in allowed.items() if may]


def _may_use(
    view: APIView, request: Request, method: str, check: Callable[[Request], Any]
) -> bool:
    """Whether ``check(clone)`` lets the requester of ``request`` use
    ``method``: it returns, or returns ``True`` (a ``False`` return is not
    allowed).

    ``clone`` is ``clone_request(request, method)`` (DRF's; with its own
    parser context, see :func:`_clone_request`), which is also
    ``view.request`` meanwhile, and a viewset has the method's action as
    ``view.action``, as when DRF dispatches ``method``; both are put back
    after. A :data:`_DENIALS` exception means not allowed; anything else
    raised propagates. No throttle runs.
    """
    clone = _clone_request(request, method)
    values: dict[str, Any] = {"request": clone}
    action_map = getattr(view, "action_map", None)
    if isinstance(action_map, dict):
        values["action"] = action_map.get(method.lower())
    with _instance_attributes(view, **values):
        try:
            return check(clone) is not False
        except _DENIALS:
            return False


def _object_check(view: APIView, obj: Any, clone: Request) -> None:
    """``view.check_object_permissions(clone, obj)``."""
    view.check_object_permissions(clone, obj)


def _hooked(
    allowed_func: Callable[..., Any], view: APIView, obj: Any, clone: Request
) -> bool:
    """``allowed_func(view, clone, clone.method, obj)``, which alone decides.

    Raises
    ------
    TypeError
        If it returns something other than a ``bool``.
    """
    result = allowed_func(view, clone, clone.method, obj)
    if not isinstance(result, bool):
        raise TypeError(f"allowed_func must return a bool, not {type(result).__name__}")
    return result


def _allowed_func(view: APIView, method: str) -> Callable[..., Any] | None:
    """The ``allowed_func`` of ``view``'s ``method``: its
    :class:`ScrudfulViewFunc` handler's, else the viewset's
    ``Meta.allowed_func``, else ``None``."""
    handler = _scrudful_handler(view, method.lower())
    if handler is not None and handler.allowed_func is not None:
        return handler.allowed_func
    return _meta_options(view).get("allowed_func")


def _clone_request(request: Request, method: str) -> Request:
    """DRF's ``clone_request(request, method)``, leaving ``request`` as it was.

    ``Request()`` points the parser context it is given at itself, and
    ``clone_request`` gives the clone ``request``'s: the clone gets a copy,
    and ``request``'s points at ``request`` again. The clone shares the
    underlying ``HttpRequest`` (whose ``method`` it doesn't change), the
    authenticators and the authenticated user.
    """
    context = request.parser_context
    clone = clone_request(request, method)
    clone.parser_context = {**context, "request": clone}
    context["request"] = request
    return clone


@contextlib.contextmanager
def _instance_attributes(obj: Any, **values: Any) -> Iterator[None]:
    """Set these attributes of ``obj``'s, and put back what it had after
    (removing those it didn't have, so that the class's show again)."""
    own = vars(obj)
    saved = {name: own[name] for name in values if name in own}
    for name, value in values.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name in values:
            if name in saved:
                setattr(obj, name, saved[name])
            else:
                own.pop(name, None)


#: The ``ScrudfulMetadata`` hooks of 0.9, removed in 0.10 (see
#: :meth:`ScrudfulMetadata.__init_subclass__`).
_REMOVED_METADATA_HOOKS = (
    *(f"determine_metadata_for_{method}" for method in METHOD_KEYS),
    "_write",
    "required_conditional_headers",
)


class ScrudfulMetadata(BaseMetadata):
    """OPTIONS metadata: the HTTP Schema Resource (Semantic HTTP 0.1) of a view.

    :meth:`determine_metadata` returns :func:`http_schema_resource` of the
    view function the request was routed to (``request.resolver_match``),
    built without the request; for a view called without URL resolution
    (``APIRequestFactory``), the document of the view instance, built the
    same way (``func(view, None)``, never the request) but not cached.

    On a view with the ``options`` that :func:`scrudful_viewset`,
    :func:`scrudful_api_view` and a class-body :func:`scrudful` method
    install, this class (or a subclass) is what makes OPTIONS scrudful's
    204 without content (see "OPTIONS" in the module docstring), which
    doesn't call :meth:`determine_metadata`: the document is served by GET
    (:func:`hsr_urls`), built by :func:`http_schema_resource`. A view that
    only sets ``metadata_class = ScrudfulMetadata``, keeping DRF's
    ``options``, answers OPTIONS with this document as DRF's 200 body.

    Subclassing it changes nothing but the class: a subclass, like this
    class, switches scrudful's OPTIONS on.

    Raises
    ------
    TypeError
        When a subclass, or a class it mixes in before this one, defines
        one of the hooks of 0.9 (``determine_metadata_for_<method>``,
        ``_write``, ``required_conditional_headers``): they took the
        request, and the HTTP Schema Resource is built without one, so they
        would silently do nothing. When a subclass, or a class it mixes in
        before this one, defines ``determine_metadata``: its output would be
        served nowhere, as scrudful's OPTIONS has no body and the
        :func:`hsr_urls` route builds the document with
        :func:`http_schema_resource`.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # The classes before ScrudfulMetadata in the MRO: the subclass and
        # any mixed-in metadata class whose determine_metadata would win.
        overriding = cls.__mro__[: cls.__mro__.index(ScrudfulMetadata)]
        if any("determine_metadata" in vars(klass) for klass in overriding):
            raise TypeError(
                f"{cls.__qualname__}.determine_metadata: in scrudful 0.10 the "
                "output of a ScrudfulMetadata's determine_metadata is served "
                "nowhere: OPTIONS answers 204 without a body, and the HTTP "
                "Schema Resource route builds the document itself, without a "
                "request. Customise the document with the statuses= and "
                "entity= options of @scrudful / scrudful_api_view or the "
                "viewset's Meta instead (see the 0.10.0 changelog)"
            )
        for name in _REMOVED_METADATA_HOOKS:
            # As for determine_metadata: a mixed-in class's would win too.
            owner = next((k for k in overriding if name in vars(k)), None)
            if owner is not None:
                where = "" if owner is cls else f" (defined by {owner.__qualname__})"
                raise TypeError(
                    f"{cls.__qualname__}.{name}{where}: ScrudfulMetadata no "
                    f"longer has {name}. scrudful 0.10 builds the HTTP Schema "
                    "Resource without a request, so customise it with the "
                    "statuses= and entity= options of @scrudful / "
                    "scrudful_api_view or the viewset's Meta instead (see the "
                    "0.10.0 changelog)"
                )

    def determine_metadata(self, request: Request, view: APIView) -> dict[str, Any]:
        func = getattr(getattr(request, "resolver_match", None), "func", None)
        if func is not None and getattr(func, "cls", None) is type(view):
            return http_schema_resource(func)
        return _describe_view(_describe_mode_copy(view))

    def get_method(self, view: APIView, name: str) -> ScrudfulViewFunc | None:
        """Return the :class:`ScrudfulViewFunc` behind ``view.<name>``, if any."""
        return _scrudful_handler(view, name)


def http_schema_resource(view_func: Callable[..., Any]) -> dict[str, Any]:
    """The HTTP Schema Resource (an OpenAPI 3.1 Path Item) of a view.

    ``view_func`` is what DRF's ``as_view(...)`` returns (a viewset's, with
    its action map) or :func:`scrudful_api_view` does: the view function a
    URL pattern routes to. The document is the same for every requester: it
    is built without a request, once per ``view_func`` (a change of the
    ``SCRUDFUL`` or ``REST_FRAMEWORK`` setting discards it), and a copy is
    returned.

    It is built from a *describe-mode* view instance,
    ``view_func.cls(**view_func.initkwargs)``, as DRF's view function builds
    one but without dispatching: ``request`` is ``None``, ``args`` and
    ``kwargs`` are empty, and a viewset has its ``action_map`` and, for each
    method described, ``action``. Link callables are called as
    ``func(view, None)``: they get the view (and so the attributes the
    route's ``initkwargs`` set) but neither a request nor URL arguments, and
    must return the same target as on the responses they describe, or the
    description disagrees with the responses. Relative URLs are resolved
    against the base URL: the handler's ``base_url``, else the viewset's
    ``Meta.base_url``, else ``SCRUDFUL["BASE_URL"]``.

    The document has an operation for each method the view allows
    (``view.allowed_methods``), in the order ``get``, ``put``, ``post``,
    ``delete``, ``options``, ``head``, ``patch``, ``trace``:

    - ``get``, ``post``, ``put``, ``patch`` and ``delete``: the request body,
      the success responses (``statuses``, else 201 for POST, 204 for
      DELETE and 200 for the others) and, for conditional methods,
      scrudful's own 304/400/412/428 responses and the precondition header
      parameters. A request body and a success response (but a 204) have
      the content of the handler's ``entity`` (by default, JSON with the
      handler's links), and the 400, 412 and 428 the content of its
      ``error`` entity;
    - ``head``: the same responses as ``get``, without content;
    - ``options``: a 204 without content;
    - any other method: its success responses.

    Each method but OPTIONS gets a ``default`` entry describing the errors
    :func:`exception_handler` gives the error Links: with the content of the
    handler's ``error`` (a handler that isn't scrudful has the view-wide
    one, see :func:`exception_handler`), else of ``SCRUDFUL["ERROR"]``. When
    ``SCRUDFUL["ERROR"]`` is set and the handler's ``error`` differs from
    it, the 401, 403, 406 and 415 that DRF answers itself, which carry
    ``SCRUDFUL["ERROR"]``, have entries of their own with its content.
    Without scrudful's exception handler the raised errors carry no error Links,
    and ``default`` has no content. HEAD's entries have no content.
    Methods whose handler is not scrudful are described without content or
    preconditions.

    Raises
    ------
    TypeError
        If ``view_func`` is not a DRF view function.
    ImproperlyConfigured
        If a link URL is relative and there is no base URL, a schema link
        has no RDF context link (the message of
        :class:`~scrudful._hsr.HsrConfigError`); a link callable raises when
        called as ``func(view, None)``; or the ``SCRUDFUL`` setting is
        invalid.
    """
    cls = getattr(view_func, "cls", None)
    if not (isinstance(cls, type) and issubclass(cls, APIView)):
        raise TypeError(
            "http_schema_resource() takes a DRF view function, what as_view() "
            f"or scrudful_api_view returns, not {view_func!r}"
        )
    return copy.deepcopy(_built_hsr(view_func))


def _built_hsr(view_func: Callable[..., Any]) -> dict[str, Any]:
    """The HTTP Schema Resource of the DRF view function ``view_func``, built
    once (not a copy)."""
    hsr = _HSRS.get(view_func)
    if hsr is None:
        hsr = _HSRS[view_func] = _describe_view(_describe_mode_view(view_func))
    return hsr


#: The HTTP Schema Resource of each view function (see :func:`http_schema_resource`).
_HSRS: weakref.WeakKeyDictionary[Callable[..., Any], dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)


def _forget_hsrs(sender: Any, setting: str, **kwargs: Any) -> None:
    """Discard the built documents (and the :func:`hsr_urls` scans) when a
    setting they depend on changes (``override_settings``, in tests): link
    callables may ``reverse()`` in the root URLconf."""
    if setting in ("SCRUDFUL", "REST_FRAMEWORK", "ROOT_URLCONF"):
        _HSRS.clear()
    if setting == "ROOT_URLCONF":
        _SCRUDFUL_VIEWS.clear()


setting_changed.connect(_forget_hsrs)


# --- Serving the HTTP Schema Resource ------------------------------------------

#: The message of a missing :func:`hsr_urls` include (see :func:`_hsr_target`).
_NO_HSR_URLS = (
    "add path('', include(scrudful.rest_framework.hsr_urls())) to your root "
    "URLconf, or set SCRUDFUL['HSR_URL']"
)


#: The message of a route turned off without ``hsr_url`` (see :func:`_hsr_target`).
_ROUTE_OFF = (
    "Meta.hsr_route = False (or SCRUDFUL['HSR_ROUTE'] = False) needs "
    "Meta.hsr_url or SCRUDFUL['HSR_URL'], hsr_url(view_name), the URL "
    "the HTTP Schema Resource is served at"
)


def hsr_urls() -> list[URLPattern]:
    """The URL pattern serving every scrudful view's HTTP Schema Resource.

    Include it once, in the root URLconf::

        urlpatterns = [
            ...,
            path("", include(scrudful.rest_framework.hsr_urls())),
        ]

    It serves ``_hsr/<view_name>``, named ``scrudful_hsr``, where
    ``view_name`` is the namespace-qualified URL name of a route
    (``request.resolver_match.view_name``, such as ``"item-detail"`` or
    ``"api:item-detail"``). On the first request it scans the URLconf, through
    every ``include()`` and namespace, for the routes of scrudful views:
    those of a :func:`scrudful_viewset` class, of a view with a
    :class:`ScrudfulViewFunc` handler, or of a :func:`scrudful_api_view`,
    however they are mounted (a router, ``path()``). Each is served
    :func:`http_schema_resource` of its view function.

    The view is a plain Django view: it runs no authentication, permission
    check or throttle, is CSRF-exempt, is public under Django's
    ``LoginRequiredMiddleware`` (``login_required = False``), and answers GET
    and HEAD with:

    - a 200, ``application/json``, the compact JSON of the document, the
      Links of the HTTP Schema Resource's own schema and context, a strong
      ``ETag`` and ``Cache-Control`` (the view's ``Meta.hsr_cache_control``,
      else ``SCRUDFUL["HSR_CACHE_CONTROL"]``, else ``public, max-age=300``);
    - a 304 with the ``ETag`` and ``Cache-Control`` for a matching
      ``If-None-Match`` (weak comparison);
    - a JSON 404, ``{"detail": "Not found."}``, for a name that isn't a
      scrudful view's route, or whose view has its route turned off
      (``Meta.hsr_route = False``, or ``SCRUDFUL["HSR_ROUTE"] = False``
      without ``Meta.hsr_route = True``).

    Other methods get a 405 with ``Allow: GET, HEAD``. The scan is redone
    when ``ROOT_URLCONF`` changes.

    A misconfigured route breaks only its own requests: a scrudful view's
    route without a URL name raises on each of its own requests (its HSR
    link needs the name), and a URL name given to the routes of two
    different scrudful views raises on a GET of ``_hsr/<that name>``; every
    other name is served. Django's system checks (``manage.py check``, and
    ``runserver`` and ``migrate`` before they start) report both, and a
    missing include, before any request (``scrudful.E001`` to
    ``scrudful.E006``, see "System checks" in the module docstring).

    Raises
    ------
    ImproperlyConfigured
        On a GET of a name given to two different scrudful views' routes;
        or when a setting is invalid.
    """
    pattern = path("_hsr/<str:view_name>", _hsr_view, name="scrudful_hsr")
    pattern.__class__ = _HsrURLPattern
    return [pattern]


@csrf_exempt
def _hsr_view(request: HttpRequest, view_name: str) -> HttpResponseBase:
    """The view of :func:`hsr_urls`."""
    if request.method not in ("GET", "HEAD"):
        return hsr_not_allowed()
    scan = _scrudful_views()
    clash = scan.clashes.get(view_name)
    if clash is not None:
        raise ImproperlyConfigured(clash)
    view_func = scan.views.get(view_name)
    if view_func is None or not _hsr_route_on(view_func.cls):
        return JsonResponse({"detail": "Not found."}, status=404)
    return hsr_response(
        request, _built_hsr(view_func), _hsr_cache_control(view_func.cls)
    )


# Public under Django's LoginRequiredMiddleware (Django >= 5.1), which
# reads the attribute of the view a URL resolves to.
_hsr_view.login_required = False  # type: ignore[attr-defined]


def http_schema_resource_view(
    view_func: Callable[..., Any],
) -> Callable[[HttpRequest], HttpResponseBase]:
    """A plain Django view serving the HTTP Schema Resource of ``view_func``.

    ``view_func`` is the view function of a scrudful view, as a URL pattern
    routes to it: what ``as_view(...)`` of a :func:`scrudful_viewset` class
    (or of a view with :class:`ScrudfulViewFunc` handlers) or
    :func:`scrudful_api_view` returns; for a router's route, the pattern's
    ``callback``. The view answers GET and HEAD as the :func:`hsr_urls` route
    does (the document, Links, strong ``ETag``, the view's ``Cache-Control``
    and a 304 for a matching ``If-None-Match``), and any other method with a
    405 (``Allow: GET, HEAD``). Like the route, it runs no authentication,
    permission check or throttle, is CSRF-exempt and is public under
    ``LoginRequiredMiddleware``. The document is built
    on the first request, as :func:`http_schema_resource` builds it.

    It is for serving the document elsewhere than :func:`hsr_urls`, with the
    route turned off and ``hsr_url`` naming where it is mounted, as
    scrud-django puts it beside ``collections-json-schema/<slug>/``::

        @scrudful_viewset
        class CollectionViewSet(viewsets.ModelViewSet):
            class Meta:
                hsr_route = False  # or SCRUDFUL["HSR_ROUTE"] = False

                def hsr_url(view_name):  # or SCRUDFUL["HSR_URL"]
                    return f"https://api.example/collections-hsr/{view_name}/"

        collection = CollectionViewSet.as_view(
            {"get": "retrieve", "put": "update", "delete": "destroy"}
        )
        urlpatterns = [
            path("collections/<slug>/", collection, name="collection"),
            path(
                "collections-hsr/collection/",
                http_schema_resource_view(collection),
            ),
        ]

    Raises
    ------
    TypeError
        If ``view_func`` is not the view function of a scrudful view.
    """
    if not _is_scrudful_view_func(view_func):
        raise TypeError(
            "http_schema_resource_view() takes the view function of a scrudful "
            "view (what as_view() of a scrudful_viewset class or of a view with "
            f"@scrudful handlers, or scrudful_api_view, returns), not {view_func!r}"
        )

    def scrudful_hsr_view(
        request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponseBase:
        if request.method not in ("GET", "HEAD"):
            return hsr_not_allowed()
        return hsr_response(
            request, _built_hsr(view_func), _hsr_cache_control(view_func.cls)
        )

    view = csrf_exempt(scrudful_hsr_view)
    view.login_required = False  # type: ignore[attr-defined]
    return view


class _Scan(NamedTuple):
    """The scrudful views' routes under a URL resolver (see
    :func:`_scan_scrudful_views`)."""

    #: The scrudful view function of each namespace-qualified URL name.
    views: dict[str, Callable[..., Any]]
    #: The error of each URL name given to the routes of two different
    #: scrudful views (excluded from ``views``).
    clashes: dict[str, str]
    #: ``(view function, error)`` of each scrudful view's route without a URL
    #: name.
    unnamed: list[tuple[Callable[..., Any], str]]
    #: The first :func:`hsr_urls` pattern, if the URLconf includes it.
    hsr_pattern: URLPattern | None = None


#: The scrudful views' routes, per URL resolver (see :func:`_scrudful_views`).
_SCRUDFUL_VIEWS: weakref.WeakKeyDictionary[URLResolver, _Scan] = (
    weakref.WeakKeyDictionary()
)


def _scrudful_views() -> _Scan:
    """The scrudful views' routes of the current URLconf, scanned once per URL
    resolver (see :func:`hsr_urls`)."""
    resolver = get_resolver(get_urlconf())
    scan = _SCRUDFUL_VIEWS.get(resolver)
    if scan is None:
        scan = _SCRUDFUL_VIEWS[resolver] = _scan_scrudful_views(resolver)
    return scan


def _scan_scrudful_views(resolver: URLResolver) -> _Scan:
    """The scrudful view function of each URL name under ``resolver``, and the
    errors of the routes it can't serve, each kept apart (R28): an unnamed
    route (which no name reaches) and a name given to the routes of two
    different scrudful views (whose name is left out of ``views``, and whose
    request raises), so that one bad route breaks only its own requests."""
    found: dict[str, tuple[Callable[..., Any], str]] = {}
    clashes: dict[str, str] = {}
    unnamed: list[tuple[Callable[..., Any], str]] = []
    hsr_patterns: list[URLPattern] = []

    def visit(patterns: Sequence[Any], namespaces: list[str], prefix: str) -> None:
        for pattern in patterns:
            route = _join_route(prefix, str(pattern.pattern))
            if isinstance(pattern, URLResolver):
                inner = (
                    namespaces + [pattern.namespace]
                    if pattern.namespace
                    else namespaces
                )
                visit(pattern.url_patterns, inner, route)
                continue
            if isinstance(pattern, _HsrURLPattern) and not hsr_patterns:
                hsr_patterns.append(pattern)
            callback = getattr(pattern, "callback", None)
            if not _is_scrudful_view_func(callback):
                continue
            if pattern.name is None:
                unnamed.append((callback, _unnamed_message(callback, route)))
                continue
            view_name = ":".join([*namespaces, pattern.name])
            seen = found.get(view_name)
            if seen is None:
                found[view_name] = (callback, route)
            elif view_name not in clashes and not _same_view_func(seen[0], callback):
                clashes[view_name] = (
                    f"the URL name {view_name!r} names the routes {seen[1]!r} "
                    f"and {route!r} of two different scrudful views: give each "
                    "its own name, as each has its own HTTP Schema Resource"
                )

    visit(resolver.url_patterns, [], "")
    views = {
        view_name: callback
        for view_name, (callback, _) in found.items()
        if view_name not in clashes
    }
    return _Scan(views, clashes, unnamed, hsr_patterns[0] if hsr_patterns else None)


@checks.register(checks.Tags.urls)
def _check_hsr_urls(
    app_configs: Any = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    """Django's system check of the scrudful views' routes of the root URLconf
    (see "System checks" in the module docstring), when it has no
    :func:`hsr_urls` pattern: with one, the pattern's own check reports
    them (:class:`_HsrURLPattern`), in Django's URL check."""
    if not getattr(settings, "ROOT_URLCONF", None):
        return []
    resolver = get_resolver()
    scan = _scan_scrudful_views(resolver)
    if scan.hsr_pattern is not None:
        return []
    return _scrudful_url_errors(resolver, scan)


class _HsrURLPattern(URLPattern):
    """The URL pattern of :func:`hsr_urls`, whose check (run by Django's URL
    system check, which loads the URLconf) reports the errors of the
    scrudful views' routes of the root URLconf: Django's URL check runs
    whether or not this module was imported before the checks started."""

    def check(self) -> list[checks.CheckMessage]:
        messages = list(super().check())
        if not getattr(settings, "ROOT_URLCONF", None):
            return messages
        resolver = get_resolver()
        scan = _scan_scrudful_views(resolver)
        if scan.hsr_pattern is self:  # once, though included twice
            messages.extend(_scrudful_url_errors(resolver, scan))
        return messages


def _scrudful_url_errors(
    resolver: URLResolver, scan: _Scan
) -> list[checks.CheckMessage]:
    """The messages of the system check (see :func:`_check_hsr_urls`): the
    ``SCRUDFUL`` setting's errors (:func:`_setting_errors`), then the
    routes' (:func:`_hsr_url_errors`)."""
    errors = _setting_errors()
    return errors + _hsr_url_errors(resolver, scan, settings_valid=not errors)


def _setting_errors() -> list[checks.CheckMessage]:
    """``scrudful.E006`` for the ``SCRUDFUL`` setting: one if it isn't a dict
    of its keys, else one for each key whose value is invalid (each key's
    accessor, :data:`scrudful._django.SETTING_ACCESSORS`, names it)."""
    try:
        scrudful_setting("BASE_URL", None)  # the dict and its keys
    except ImproperlyConfigured as exc:
        return [checks.Error(str(exc), id="scrudful.E006")]
    errors: list[checks.CheckMessage] = []
    for accessor in SETTING_ACCESSORS.values():
        try:
            accessor()
        except ImproperlyConfigured as exc:
            errors.append(checks.Error(str(exc), id="scrudful.E006"))
    return errors


def _hsr_url_errors(
    resolver: URLResolver, scan: _Scan, settings_valid: bool
) -> list[checks.CheckMessage]:
    """The routes' errors of :func:`_scrudful_url_errors`.

    Each entry is checked apart. With an invalid setting
    (``settings_valid`` false; :func:`_setting_errors` reports it), the
    entries whose answer depends on it are left out, and no HTTP Schema
    Resource is built: each build reads the setting.
    """
    errors: list[checks.CheckMessage] = []
    for callback, message in scan.unnamed:
        try:
            hsr_url = _hsr_url(callback.cls)
        except ImproperlyConfigured:  # the setting's, reported
            continue
        if hsr_url is None:  # an hsr_url needs no name
            errors.append(checks.Error(message, obj=callback.cls, id="scrudful.E001"))
    for message in scan.clashes.values():
        errors.append(checks.Error(message, id="scrudful.E002"))
    needs_route = []
    for view_name, view_func in scan.views.items():
        try:
            if _hsr_url(view_func.cls) is not None:
                continue
            route_on = _hsr_route_on(view_func.cls)
        except ImproperlyConfigured:  # the setting's, reported
            continue
        if route_on:
            needs_route.append(view_name)
        else:
            errors.append(
                checks.Error(
                    f"the route {view_name!r}: {_ROUTE_OFF}",
                    obj=view_func.cls,
                    id="scrudful.E004",
                )
            )
    if needs_route:
        try:
            reverse(
                "scrudful_hsr",
                kwargs={"view_name": needs_route[0]},
                urlconf=resolver.urlconf_name,
            )
        except NoReverseMatch:
            errors.append(checks.Error(_NO_HSR_URLS, id="scrudful.E003"))
    if not settings_valid:
        return errors
    for view_name, view_func in scan.views.items():
        errors.extend(_hsr_build_messages(view_name, view_func))
    return errors


def _hsr_build_messages(
    view_name: str, view_func: Callable[..., Any]
) -> list[checks.CheckMessage]:
    """Build the HTTP Schema Resource of the route ``view_name`` as the route
    builds it, without keeping it (the settings may change before a
    request), and report why it can't be.

    A link callable that raises is the warning ``scrudful.W001``: what it
    does may work only later (a callable reading the database fails before
    ``migrate`` has created its tables, and ``migrate`` runs this check
    first), and the document is built again when it is first served. Any
    other failure, scrudful's configuration errors and whatever else the
    build raises, is the error ``scrudful.E005``, so that one view never
    stops the check of the others.
    """
    where = f"the HTTP Schema Resource of the route {view_name!r} can't be built"
    try:
        _describe_view(_describe_mode_view(view_func))
    except _LinkCallableError as exc:
        return [
            checks.Warning(
                f"{where} now: {exc}",
                hint=(
                    "It is built when it is first served, calling the "
                    "callable again: if it can't work by then (it needs the "
                    "request or a URL kwarg, say), that request fails."
                ),
                obj=view_func.cls,
                id="scrudful.W001",
            )
        ]
    except ImproperlyConfigured as exc:
        return [checks.Error(f"{where}: {exc}", obj=view_func.cls, id="scrudful.E005")]
    except Exception as exc:
        return [
            checks.Error(f"{where}: {exc!r}", obj=view_func.cls, id="scrudful.E005")
        ]
    return []


def _join_route(prefix: str, route: str) -> str:
    """``prefix`` and ``route`` joined, as ``ResolverMatch.route`` joins them."""
    return prefix + route.removeprefix("^") if prefix else route


def _unnamed_message(callback: Any, route: str) -> str:
    cls = getattr(callback, "cls", None)
    what = f"{cls.__module__}.{cls.__qualname__}" if isinstance(cls, type) else "it"
    return (
        f"the route {route!r} of the scrudful view {what} has no URL name: "
        "name it (path(..., name=...)), as its HTTP Schema Resource is served "
        "and linked by name (see scrudful.rest_framework.hsr_urls)"
    )


def _is_scrudful_view_func(callback: Any) -> bool:
    """Whether ``callback`` is a DRF view function of a scrudful view: of a
    :func:`scrudful_viewset` class, or with a :class:`ScrudfulViewFunc` handler
    (:func:`scrudful_api_view`'s included) for a method it routes."""
    cls = getattr(callback, "cls", None)
    if not (isinstance(cls, type) and issubclass(cls, APIView)):
        return False
    if getattr(cls, _META_OPTIONS_ATTR, None) is not None:
        return True
    return bool(_view_func_handlers(callback))


def _view_func_handlers(view_func: Callable[..., Any]) -> list[ScrudfulViewFunc]:
    """The :class:`ScrudfulViewFunc` handlers of the methods ``view_func``
    routes: its action map's, else the class's handlers for the methods of
    ``http_method_names``."""
    cls = view_func.cls  # type: ignore[attr-defined]
    actions = getattr(view_func, "actions", None)
    names = (
        list(actions.values())
        if actions is not None
        else [method.lower() for method in cls.http_method_names]
    )
    handlers = (getattr(cls, name, None) for name in names)
    return [handler for handler in handlers if isinstance(handler, ScrudfulViewFunc)]


def _same_view_func(a: Callable[..., Any], b: Callable[..., Any]) -> bool:
    """Whether ``a`` and ``b`` are one view (``as_view()`` called alike)."""
    return a is b or (
        getattr(a, "cls", None) is getattr(b, "cls", None)
        and getattr(a, "actions", None) == getattr(b, "actions", None)
        and getattr(a, "initkwargs", None) == getattr(b, "initkwargs", None)
    )


def _response_hsr_link(view: APIView, request: HttpRequest | Request) -> str | None:
    """The HTTP Schema Resource link of ``view``'s response to ``request``
    (see "Headers" in the module docstring), or ``None`` when it has none.

    The rule: a response gets the link exactly when the request was routed
    to a scrudful view's route, the resource the link is about.

    - An *unrouted* request, one without ``resolver_match`` (a view called
      directly, as with DRF's ``APIRequestFactory``), gets none: it came
      through no URL, so there is no resource to describe, and no route
      whose HSR :func:`hsr_urls` serves. Django's request handler resolves
      every request it serves, so this never hides a misconfiguration.
    - A request routed to a view function that isn't a scrudful view's
      (:func:`_is_scrudful_view_func`: an ``@api_view`` + ``@scrudful`` view,
      whose handlers are DRF's), or that isn't ``view``'s (``view`` called
      by another view), gets none: no HSR is served for that route.
    - A CORS preflight gets none (it carries no Links).
    - Otherwise the link targets :func:`_hsr_target`, whose
      ``ImproperlyConfigured`` propagates: a routed request of a scrudful
      view without the :func:`hsr_urls` include (or ``hsr_url``) raises, on
      every response.
    """
    match = getattr(request, "resolver_match", None)
    if match is None:
        return None
    func = match.func
    if getattr(func, "cls", None) is not type(view):
        return None
    if not _is_scrudful_view_func(func):
        return None
    if is_preflight(request.method or "", request.headers.get):
        return None
    return hsr_link(_hsr_target(request))


def _hsr_target(request: HttpRequest | Request) -> str:
    """The URL of the HTTP Schema Resource of the view ``request`` was routed
    to: the target of the HSR link of its responses (and OPTIONS). Whether a
    response carries the link at all is :func:`_response_hsr_link`'s rule
    (an unrouted request's doesn't).

    Resolved in this order, with ``view_name`` the request's
    ``resolver_match.view_name``:

    1. ``hsr_url(view_name)``, with the view's ``Meta.hsr_url``, else
       ``SCRUDFUL["HSR_URL"]``, as it returns it;
    2. else, with the route turned off (``Meta.hsr_route = False``, or
       ``SCRUDFUL["HSR_ROUTE"] = False`` without ``Meta.hsr_route = True``),
       ``ImproperlyConfigured``;
    3. else the :func:`hsr_urls` route of ``view_name``, made absolute against
       the view's base URL (``Meta.base_url``; else the ``base_url`` its
       scrudful handlers share, a :func:`scrudful_api_view`'s; else
       ``SCRUDFUL["BASE_URL"]``) when it has one, else with
       ``request.build_absolute_uri()``.

    Raises
    ------
    ImproperlyConfigured
        If the request has no ``resolver_match``; the route is off and there
        is no ``hsr_url``; the request's route has no URL name; the
        :func:`hsr_urls` route can't be reversed (the root URLconf doesn't
        include it); or a setting is invalid.
    """
    match = getattr(request, "resolver_match", None)
    if match is None:
        raise ImproperlyConfigured(
            "the HTTP Schema Resource link needs the request's resolver_match: "
            "a request routed through the URLconf"
        )
    view_func = match.func
    cls = getattr(view_func, "cls", None)
    hsr_url = _hsr_url(cls)
    if hsr_url is not None:
        try:
            return check_hsr_url_result(
                hsr_url(match.view_name), f"hsr_url({match.view_name!r})"
            )
        except HsrConfigError as exc:
            raise ImproperlyConfigured(str(exc)) from exc
    if not _hsr_route_on(cls):
        raise ImproperlyConfigured(_ROUTE_OFF)
    if match.url_name is None:
        raise ImproperlyConfigured(_unnamed_message(view_func, match.route))
    try:
        route = reverse("scrudful_hsr", kwargs={"view_name": match.view_name})
    except NoReverseMatch:
        raise ImproperlyConfigured(_NO_HSR_URLS) from None
    base_url = _hsr_base_url(view_func)
    if base_url is not None:
        return resolve_uri(route, base_url)
    return request.build_absolute_uri(route)


def _hsr_base_url(view_func: Callable[..., Any]) -> str | None:
    """The base URL of ``view_func``'s HSR link (see :func:`_hsr_target`)."""
    cls = getattr(view_func, "cls", None)
    base_url = _class_meta_options(cls).get("base_url")
    if base_url is not None:
        return base_url
    if isinstance(cls, type) and issubclass(cls, APIView):
        handler_bases = {handler.base_url for handler in _view_func_handlers(view_func)}
        if len(handler_bases) == 1 and None not in handler_bases:
            return handler_bases.pop()
    return setting_base_url()


def _hsr_route_on(cls: Any) -> bool:
    """Whether :func:`hsr_urls` serves the view class ``cls``'s routes:
    ``Meta.hsr_route``, else ``SCRUDFUL["HSR_ROUTE"]``, else ``True``."""
    value = _class_meta_options(cls).get("hsr_route")
    if value is not None:
        return value
    return setting_hsr_route()


def _hsr_url(cls: Any) -> Callable[[str], str] | None:
    """``Meta.hsr_url`` of the view class ``cls``, else
    ``SCRUDFUL["HSR_URL"]``, else ``None``."""
    value = _class_meta_options(cls).get("hsr_url")
    if value is not None:
        return value
    return setting_hsr_url()


def _hsr_cache_control(cls: Any) -> str:
    """``Meta.hsr_cache_control`` of the view class ``cls``, else
    ``SCRUDFUL["HSR_CACHE_CONTROL"]``, else ``public, max-age=300``."""
    value = _class_meta_options(cls).get("hsr_cache_control")
    if value is not None:
        return value
    return setting_hsr_cache_control()


def _check_hsr_options(hsr_route: Any, hsr_url: Any, hsr_cache_control: Any) -> None:
    """Check the :data:`_HSR_OPTIONS` of a :func:`scrudful_viewset` ``Meta``."""
    if hsr_route is not None and not isinstance(hsr_route, bool):
        raise TypeError(
            f"Meta.hsr_route must be a bool, not {type(hsr_route).__name__}"
        )
    if hsr_url is not None and not callable(hsr_url):
        raise TypeError(
            "Meta.hsr_url must be a callable, hsr_url(view_name), not "
            f"{type(hsr_url).__name__}"
        )
    if hsr_cache_control is not None and not isinstance(hsr_cache_control, str):
        raise TypeError(
            "Meta.hsr_cache_control must be a str, not "
            f"{type(hsr_cache_control).__name__}"
        )
    if hsr_cache_control is not None:
        check_hsr_cache_control(hsr_cache_control, "Meta.hsr_cache_control")


def _describe_mode_view(view_func: Callable[..., Any]) -> APIView:
    """The request-less view instance of ``view_func``, as its view function
    builds it, without dispatching (see :func:`http_schema_resource`)."""
    view = view_func.cls(**(getattr(view_func, "initkwargs", None) or {}))
    actions = getattr(view_func, "actions", None)
    if actions is not None:
        # As ViewSetMixin.as_view's view does: HEAD goes to GET's action.
        action_map = dict(actions)
        if "get" in action_map and "head" not in action_map:
            action_map["head"] = action_map["get"]
        view.action_map = action_map
        for method, action_name in action_map.items():
            setattr(view, method, getattr(view, action_name))
    elif hasattr(view, "get") and not hasattr(view, "head"):
        # As Django's View.setup does.
        view.head = view.get
    view.request = None
    view.args = ()
    view.kwargs = {}
    return view


def _describe_mode_copy(view: APIView) -> APIView:
    """A request-less copy of the live ``view`` (see
    :meth:`ScrudfulMetadata.determine_metadata`)."""
    copied = copy.copy(view)
    copied.request = None
    copied.args = ()
    copied.kwargs = {}
    action_map = getattr(view, "action_map", None)
    if action_map is not None:
        copied.action_map = dict(action_map)
    return copied


def _describe_view(view: APIView) -> dict[str, Any]:
    """The HTTP Schema Resource of the describe-mode ``view``."""
    action_map = getattr(view, "action_map", None)
    allowed = {method.lower() for method in view.allowed_methods}
    api_error = setting_error() if _is_scrudful_view(view) else None
    error_links = _gives_error_links(view)
    body = {}
    for method in METHOD_KEYS:
        if method not in allowed:
            continue
        if method == "options":
            body[method] = options_operation()
            continue
        if action_map is not None:
            view.action = action_map.get(method)
        body[method] = _describe_method(view, method, api_error, error_links)
    return body


class _LinkCallableError(ImproperlyConfigured):
    """A link callable raised when called in describe mode, as
    ``func(view, None)`` (see :meth:`_Describing.value`); its ``__cause__``
    is what it raised. The system check tells it from scrudful's own
    configuration errors (see :func:`_hsr_build_messages`)."""


class _Describing:
    """Evaluates link values in describe mode, for one method of a view."""

    def __init__(self, view: APIView, method: str, base_url: str | None) -> None:
        self.view = view
        self.base_url = base_url
        #: Names the method's entries in errors.
        self.name = f"{type(view).__name__} {method.upper()}"
        #: The content of each source, by ``(id(source), media_type)``: its
        #: callables are called once per method, however many entries it has.
        self._contents: dict[tuple[int, str], dict[str, Any] | None] = {}

    def value(self, value_or_func: Any) -> Any:
        """``value_or_func``, or what it returns when called as
        ``func(view, None)``.

        Raises
        ------
        ImproperlyConfigured
            If the callable raises (a :class:`_LinkCallableError`, naming
            the callable, this method and the exception, chained to it).
        """
        if not callable(value_or_func):
            return value_or_func
        try:
            return value_or_func(self.view, None)
        except Exception as exc:
            func = getattr(value_or_func, "__qualname__", repr(value_or_func))
            raise _LinkCallableError(
                f"{func}, a link callable of {self.name}, must work without a "
                "request when the HTTP Schema Resource is built (called as "
                f"func(view, None)): {exc!r}"
            ) from exc

    def content(
        self, source: ScrudfulViewFunc | Entity | None, media_type: str, where: str
    ) -> dict[str, Any] | None:
        """The ``content`` of ``source``'s links: its Media Type Object.

        Built by :func:`scrudful._adapter.describe_content` from the schema
        and context URLs and types of ``source`` (a handler or an
        :class:`Entity`), evaluated as for its Links (the context only when
        ``media_type`` takes one), with relative URLs resolved against the
        base URL (:func:`scrudful._hsr.absolute_for_hsr`), as the Links of
        the responses it describes are. ``None`` without a schema URL.
        ``where`` names the entry, for errors.

        Raises
        ------
        ImproperlyConfigured
            If a URL is relative and there is no base URL, a schema has no
            RDF context, or content that is not JSON gets no context type
            (:class:`~scrudful.entity.HsrConfigError`'s message, naming the
            view, the method and the entry); or a link callable raises.
        """
        if source is None:
            return None
        key = (id(source), media_type)
        if key not in self._contents:
            self._contents[key] = self._content(source, media_type, where)
        return copy.deepcopy(self._contents[key])

    def _content(
        self, source: ScrudfulViewFunc | Entity, media_type: str, where: str
    ) -> dict[str, Any] | None:
        """:meth:`content`, uncached."""
        schema = self.value(source.schema_link_or_func)
        if not schema:
            return None
        schema_type = self.value(source.schema_type_or_func)
        context = context_type = None
        rel, default_type = context_defaults(media_type)
        if rel is not None:  # not in-band RDF
            context = self.value(source.context_link_or_func)
        if context:
            context_type = self.value(source.context_type_or_func) or default_type
        where = f"{self.name}'s {where}"
        try:
            schema_url = absolute_for_hsr(
                str(schema), self.base_url, f"{where} schema link"
            )
            context_url = (
                absolute_for_hsr(str(context), self.base_url, f"{where} context link")
                if context
                else None
            )
            try:
                return describe_content(
                    media_type,
                    schema_url,
                    str(schema_type or DEFAULT_SCHEMA_TYPE),
                    context_url,
                    None if context_type is None else str(context_type),
                )
            except ValueError as exc:
                # HsrConfigError, or context_link_type's ValueError (a
                # context_type_or_func returning nothing for non-JSON
                # content): either is this entry's configuration error.
                raise HsrConfigError(f"{where}: {exc}") from exc
        except HsrConfigError as exc:
            raise ImproperlyConfigured(str(exc)) from exc


def _describe_method(
    view: APIView, method: str, api_error: Entity | None, error_links: bool
) -> dict[str, Any]:
    """Describe ``method`` (lowercase, not ``options``) of the describe-mode
    ``view`` (see :func:`http_schema_resource`)."""
    handler = _handler(view, method)
    scrudful_handler = handler if isinstance(handler, ScrudfulViewFunc) else None
    describing = _Describing(view, method, _handler_base_url(view, handler))
    statuses = _statuses(view, scrudful_handler, method)
    conditional = _is_conditional(handler, method)
    content = None
    if scrudful_handler is not None and method not in ("head", "trace"):
        content = describing.content(
            scrudful_handler, scrudful_handler.media_type, "content"
        )

    def error_content() -> dict[str, Any] | None:
        # The error entity's callables run only when its content is described.
        error = None if scrudful_handler is None else scrudful_handler.error
        return describing.content(
            error, error.media_type if error else "", "error response"
        )

    if method in ("get", "head", "trace"):
        description = describe_read(
            None,
            conditional and method != "trace",
            statuses,
            # Only scrudful's 400 and 412 of a GET have it.
            error_content() if conditional and method == "get" else None,
        )
    else:
        # The serializer's 400 carries the error Links only through
        # :func:`exception_handler` (or a handler delegating to it, which
        # carries its marker), so only then is it described.
        serializer_400 = method != "delete" and error_links
        errors = error_content() if conditional or serializer_400 else None
        description = describe_write(
            method,
            None if method == "delete" else content,
            conditional,
            conditional and handler.etag_func is not None,
            conditional and handler.last_modified_func is not None,
            statuses,
            error_content=errors,
        )
        if errors and serializer_400:
            description["responses"].setdefault(
                "400", {"description": reason_phrase(400), "content": errors}
            )
    responses = description["responses"]
    if content:
        for key, response in responses.items():
            if has_default_entity(method, int(key)):
                response["content"] = copy.deepcopy(content)
    _add_error_entries(
        responses, view, method, api_error, error_links, describing, method != "head"
    )
    return description


def _add_error_entries(
    responses: dict[str, Any],
    view: APIView,
    method: str,
    api_error: Entity | None,
    error_links: bool,
    describing: _Describing,
    with_content: bool,
) -> None:
    """Add ``method``'s error entries to ``responses`` (see
    :func:`http_schema_resource`).

    The ``default`` entry has the content of the entity the raised errors
    get from :func:`exception_handler` (:func:`_error_entity`, else
    ``api_error``); when ``api_error`` is set and that entity differs, so do
    DRF's own 401, 403, 406 and 415, which carry ``api_error``. Without
    scrudful's exception handler (``error_links``), the errors carry no
    Links: ``default`` alone, without content. Without ``with_content``
    (HEAD), the same entries without content.
    """

    def entry(description: str, entity: Entity | None, key: str) -> dict[str, Any]:
        described: dict[str, Any] = {"description": description}
        if with_content and entity is not None:
            content = describing.content(entity, entity.media_type, f"{key} response")
            if content:
                described["content"] = content
        return described

    if not error_links:
        responses["default"] = {"description": _DEFAULT_ERROR}
        return
    own = _error_entity(view, method)
    if api_error is not None and own is not None and own != api_error:
        for status, description in _FRAMEWORK_ERRORS.items():
            key = str(status)
            if key not in responses:
                responses[key] = entry(description, api_error, key)
    responses["default"] = entry(
        _DEFAULT_ERROR, api_error if own is None else own, "default"
    )


def _statuses(
    view: APIView, handler: ScrudfulViewFunc | None, method: str
) -> tuple[int, ...] | None:
    """The success statuses of ``method``: the handler's ``statuses``, else
    the view's ``Meta.statuses``, else ``None`` (DRF's); HEAD takes GET's
    when not listed."""
    sources = (
        None if handler is None else handler.statuses,
        _meta_options(view).get("statuses"),
    )
    for statuses in sources:
        if not statuses:
            continue
        if method in statuses:
            return statuses[method]
        if method == "head" and "get" in statuses:
            return statuses["get"]
    return None


#: Kept for backwards compatibility; :class:`ScrudfulMetadata` now describes
#: ``APIView`` function views too.
ScrudfulAPIViewMetadata = ScrudfulMetadata


def _check_sync(view: Callable[..., Any]) -> None:
    if inspect.iscoroutinefunction(view):
        name = getattr(view, "__qualname__", repr(view))
        raise TypeError(f"@scrudful does not support async views ({name})")


def _check_list_validators(
    etag_func: Any,
    last_modified_func: Any,
    list_etag_func: Any,
    list_last_modified_func: Any,
    creates: bool,
) -> None:
    check_callables(
        list_etag_func=list_etag_func, list_last_modified_func=list_last_modified_func
    )
    has_list = list_etag_func is not None or list_last_modified_func is not None
    has_own = etag_func is not None or last_modified_func is not None
    if has_list and has_own and not creates:
        # Both would describe the target: only a create's describe the new object.
        raise TypeError(
            "list_etag_func and list_last_modified_func with etag_func or "
            "last_modified_func need creates=True"
        )


def _compute_validators(
    etag_func: Callable[..., Any] | None,
    last_modified_func: Callable[..., Any] | None,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> tuple[str | None, int | None]:
    """The quoted entity-tag and epoch timestamp the callbacks return."""
    etag = None
    if etag_func is not None:
        value = etag_func(*args, **kwargs)
        etag = None if value is None else quote_etag(value)
    last_modified = None
    if last_modified_func is not None:
        value = last_modified_func(*args, **kwargs)
        last_modified = http_timestamp(aware(value))
    return etag, last_modified


def _evaluate(
    value_or_func: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> Any:
    if callable(value_or_func):
        return value_or_func(*args, **kwargs)
    return value_or_func


def _request_index(args: Sequence[Any]) -> int:
    for index, arg in enumerate(args):
        if isinstance(arg, (Request, HttpRequest)):
            return index
    raise TypeError("a SCRUDful view must receive the request as a positional argument")


def _callback_arguments(
    args: tuple[Any, ...], kwargs: dict[str, Any], index: int
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """The arguments the callbacks receive: (view,) request and URL args.

    For view methods the URL arguments are the view's ``args``/``kwargs`` (set
    by ``dispatch``), so keyword arguments that methods pass to each other
    (``partial=True``) are not passed on.
    """
    if index > 0:
        view = args[0]
        view_kwargs = getattr(view, "kwargs", None)
        if isinstance(view_kwargs, dict):
            return (*args[: index + 1], *getattr(view, "args", ())), dict(view_kwargs)
    return args, kwargs


def _conditional_response(
    result: ConditionalResult,
    request: HttpRequest | Request,
    link: str | None = None,
    content_type: str | None = None,
) -> HttpResponseBase:
    """scrudful's own early response.

    ``link`` and ``content_type`` (the error entity's) go on a 400, 412 or
    428. A DRF request's response is a DRF ``Response``: rendered by the
    negotiated renderer, or, with ``content_type``, by the view's JSON
    renderer (see :func:`_render_as_json`).
    """
    if result.body is None or not isinstance(request, Request):
        return django_conditional_response(result, link, content_type)
    headers = dict(result.headers)
    if link:
        headers["Link"] = link
    if content_type:
        _render_as_json(request, (request.parser_context or {}).get("view"))
    response = Response(
        result.body, status=result.status, headers=headers, content_type=content_type
    )
    expose_headers(response)
    return response


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """DRF's exception handler, adding the Links of the view's ``error`` entity.

    Errors that DRF itself raises (a serializer's 400, the 404 of
    ``get_object()``, a 405, a 401 or 403 ...) are answered by DRF's
    ``exception_handler``; when the view's handler for the request is a
    :class:`ScrudfulViewFunc` with an ``error`` entity, the response also gets
    that entity's Links and is served as JSON with the entity's ``media_type``
    whatever renderer DRF's content negotiation picked, rendered by the view's
    first JSON renderer as scrudful's own 400, 412 and 428 are (see
    :class:`ScrudfulViewFunc`), so that it agrees with the HTTP Schema
    Resource.
    Without an ``error`` entity, or when the view has no such handler,
    the response is DRF's, unchanged; so is ``None`` for an exception DRF does
    not handle.

    The handler is looked up as :meth:`ScrudfulMetadata.get_method` does:
    ``view.action`` (a viewset's), then the request method. When that is no
    :class:`ScrudfulViewFunc` (an error answering OPTIONS, such as a 403 or
    an unsupported ``?format=``; a 405 for a method no handler serves; a
    handler that isn't scrudful), the view-wide entity supplies the Links:
    the ``error`` the scrudful handlers of the view's methods share; when
    they have different ones, the GET handler's, else the first one in the
    order ``get``, ``put``, ``post``, ``delete``, ``options``, ``head``,
    ``patch``, ``trace``. With none, the response gets the API-wide entity,
    ``SCRUDFUL["ERROR"]``, on a scrudful view (one decorated by
    :func:`scrudful_viewset` or with a :class:`ScrudfulViewFunc` handler),
    and is DRF's, unchanged, without it. When ``SCRUDFUL["ERROR"]`` is set,
    a 401, 403, 406 or 415 of a scrudful view (the errors DRF answers
    itself: authentication and permissions, content negotiation, parsing)
    gets it whatever the handler's ``error``, as the HTTP Schema Resource
    describes (see :func:`http_schema_resource`). The entity's callables are
    called as ``func(view, request, **url_kwargs)``, and its relative URLs
    resolved as the handler's (its ``base_url``, else ``Meta.base_url``,
    else ``SCRUDFUL["BASE_URL"]``, else the request).

    With an entity whose media type is ``application/problem+json``, a
    ``ValidationError``'s 400 (a serializer's) is RFC 9457 problem details,
    the content that entity describes:
    ``problem_from_errors(400, errors, "Request validation failed")``
    (:func:`scrudful.problem.problem_from_errors`), DRF's error data as the
    ``errors`` member. Every response of a scrudful view exposes ``Link``
    and ``Allow``, with an entity or not; the HTTP Schema Resource link is
    added by the view's ``finalize_response`` (see "Headers" in the module
    docstring), whatever the exception handler.

    Settings must name it, as the example project does::

        REST_FRAMEWORK = {
            "EXCEPTION_HANDLER": "scrudful.rest_framework.exception_handler",
        }

    It carries the attribute ``scrudful_error_links = True``, by which
    :class:`ScrudfulMetadata` knows that the serializer's 400 gets the error
    Links, and so describes it. A project handler that delegates to it (logs,
    then returns ``exception_handler(exc, context)``) must carry it too: set
    ``handler.scrudful_error_links = True``, or decorate the handler with
    ``functools.wraps(exception_handler)``, which copies it.

    Parameters
    ----------
    exc : Exception
        The exception raised by the view.
    context : dict
        DRF's exception context (``view``, ``args``, ``kwargs``, ``request``).

    Returns
    -------
    Response or None
        DRF's response for ``exc``, with the error Links, or ``None``.
    """
    response = drf_exception_handler(exc, context)
    view, request = context.get("view"), context.get("request")
    if response is None or not isinstance(view, APIView):
        return response
    if not isinstance(request, Request):
        return response
    if _is_scrudful_view(view):
        # Link and Allow always, with an error entity or not.
        expose_headers(response)
    error = _raised_error_entity(view, request.method, response.status_code)
    if error is None:
        return response
    if (
        isinstance(exc, ValidationError)
        and response.status_code == 400
        and essence(error.media_type) == PROBLEM_JSON
    ):
        response.data = problem_from_errors(
            400, response.data, "Request validation failed"
        )
    args, kwargs = _callback_arguments((view, request), {}, 1)
    base_url = _handler_base_url(view, _request_handler(view, request.method))
    link = merge_links(
        response.get("Link"), *_entity_links(error, args, kwargs, base_url)
    )
    if link is not None:
        response["Link"] = link
    _render_as_json(request, view)
    response.content_type = error.media_type
    expose_headers(response)
    return response


#: The marker of an exception handler that gives the errors the error Links.
exception_handler.scrudful_error_links = True  # type: ignore[attr-defined]


def _render_as_json(request: Request, view: Any) -> None:
    """Bypass DRF's content negotiation for an error served as its entity's type.

    The response is rendered by the first of the view's ``renderer_classes``
    whose media type is JSON content (``application/json`` or ``+json``), so
    a project's own JSON renderer (camel-case keys, orjson ...) formats the
    body as it does elsewhere; by DRF's ``JSONRenderer`` if there is none.
    """
    renderer: Any = JSONRenderer()
    for renderer_class in getattr(view, "renderer_classes", ()):
        media_type = getattr(renderer_class, "media_type", None)
        if isinstance(media_type, str) and is_json(media_type):
            renderer = renderer_class()
            break
    request.accepted_renderer = renderer
    request.accepted_media_type = renderer.media_type


def _raised_error_entity(
    view: APIView, method: str | None, status: int
) -> Entity | None:
    """The error entity of a ``status`` error that :func:`exception_handler`
    answers for ``view``'s ``method`` (see :func:`http_schema_resource`).

    On a scrudful view (:func:`_is_scrudful_view`), ``SCRUDFUL["ERROR"]``
    for the 401, 403, 406 and 415 DRF answers itself when it is set;
    otherwise :func:`_error_entity`'s, else ``SCRUDFUL["ERROR"]``. Not on
    one, :func:`_error_entity`'s.
    """
    api_error = setting_error() if _is_scrudful_view(view) else None
    if api_error is not None and status in _FRAMEWORK_ERRORS:
        return api_error
    own = _error_entity(view, method)
    return api_error if own is None else own


def _request_handler(view: APIView, method: str | None) -> ScrudfulViewFunc | None:
    """The :class:`ScrudfulViewFunc` serving ``method``: ``view.action``'s (a
    viewset's), else the method's; ``None`` if neither is one."""
    for name in (getattr(view, "action", None), method):
        handler = _scrudful_handler(view, name.lower()) if name else None
        if handler is not None:
            return handler
    return None


def _error_entity(view: APIView, method: str | None) -> Entity | None:
    """The error entity of an error of ``view`` answering ``method``.

    The ``error`` of the request's own handler (``view.action``'s, a
    viewset's, else the method's) when it is a
    :class:`ScrudfulViewFunc`, even ``None``. Otherwise (an OPTIONS, a 405
    for a method no handler serves, a handler that is not scrudful), the
    view-wide one, from the scrudful handlers of the view's methods (a
    viewset's route's action map): the one ``error`` they share; else, when
    they have several, the GET handler's; else the first in the order of
    :data:`~scrudful._adapter.METHOD_KEYS`. The OPTIONS body lists none of
    these errors, so any error entity of the view describes them; a body
    with content then always has Links.
    """
    handler = _request_handler(view, method)
    if handler is not None:
        return handler.error
    errors: dict[str, Entity] = {}
    for name in METHOD_KEYS:
        if name.upper() not in view.allowed_methods:
            continue
        handler = _scrudful_handler(view, name)
        if handler is not None and handler.error is not None:
            errors[name] = handler.error
    distinct = []
    for error in errors.values():
        if error not in distinct:
            distinct.append(error)
    if len(distinct) <= 1:
        return distinct[0] if distinct else None
    return errors.get("get", distinct[0])


def _has_validators(handler: Any) -> bool:
    return isinstance(handler, ScrudfulViewFunc) and handler.has_validators


def _is_conditional(handler: Any, method: str) -> bool:
    """Whether ``handler`` evaluates the If-* headers for ``method`` (lower-case).

    With list validators, for POST only (a create's target is the
    collection); a create without them, never; otherwise, with validators,
    for a method :func:`scrudful._adapter.evaluates_preconditions` accepts
    (not CONNECT, OPTIONS or TRACE).
    """
    if not isinstance(handler, ScrudfulViewFunc):
        return False
    if handler.has_list_validators:
        # A create's target is the collection: its POST is checked against
        # the list validators.
        return method == "post"
    if handler.creates:
        return False  # without list validators: nothing to check against
    return _has_validators(handler) and evaluates_preconditions(method.upper())


def _handler(view: APIView, method: str) -> Any:
    """``view.<method>`` as a function (a :class:`ScrudfulViewFunc`, if it is one).

    A viewset routes each method of the action map to its action as
    ``view.<method>``. DRF's stock ``UpdateModelMixin.partial_update`` calls
    ``self.update(partial=True)``: when ``update`` is a
    :class:`ScrudfulViewFunc`, it is what serves the request, so it is the
    handler (as :func:`scrudful_viewset` leaves the stock ``partial_update``
    to it). Both :class:`ScrudfulMetadata` and :func:`exception_handler` look
    handlers up here. A ``partial_update`` of the view's own stays its own.
    """
    handler = getattr(view, method, None)
    handler = getattr(handler, "__func__", handler)
    if handler is _STOCK_PARTIAL_UPDATE:
        update = _handler(view, "update")
        if isinstance(update, ScrudfulViewFunc):
            return update
    return handler


def _check_error(error: Any) -> None:
    if error is None:
        return
    if not isinstance(error, Entity):
        raise TypeError(f"error must be an Entity, not {type(error).__name__}")
    check_error_media_type(error.media_type)


def _resolve(url: Any, args: Sequence[Any], base_url: str | None = None) -> str:
    """``url`` resolved against ``base_url`` when given, else against the
    absolute URI of the request among ``args``."""
    if base_url is not None:
        return resolve_uri(url, base_url)
    for arg in args:
        if isinstance(arg, (Request, HttpRequest)):
            return resolve_uri(url, arg.build_absolute_uri())
    return str(url)


def _link_value(
    url: Any,
    rel_and_type: tuple[Any, Any],
    defaults: tuple[str | None, str | None],
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    context_of: str | None = None,
    base: str | None = None,
) -> str | None:
    """One link-value; the URL is resolved against ``base`` (else the
    request), as in the HTTP Schema Resource.

    The context link of ``context_of`` content takes its type from
    :func:`scrudful._adapter.context_link_type`, as the HTTP Schema Resource
    does.
    """
    if not url:
        return None
    rel = _evaluate(rel_and_type[0], args, kwargs) or defaults[0]
    content_type = _evaluate(rel_and_type[1], args, kwargs)
    if context_of is not None:
        content_type = context_link_type(context_of, content_type)
    content_type = content_type or defaults[1]
    return link_content(_resolve(url, args, base), rel, content_type)


def _entity_links(
    entity: Entity,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    base: str | None = None,
) -> tuple[str | None, str | None]:
    """The schema and the context link-values of ``entity`` (``None`` if unset).

    The context link's defaults come from ``entity.media_type``; in-band RDF
    has none. Relative URLs resolve against ``base``, else the request.
    """
    schema = _link_value(
        _evaluate(entity.schema_link_or_func, args, kwargs),
        (entity.schema_rel_or_func, entity.schema_type_or_func),
        (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
        args,
        kwargs,
        base=base,
    )
    context = None
    defaults = context_defaults(entity.media_type)
    if defaults != (None, None):
        context = _link_value(
            _evaluate(entity.context_link_or_func, args, kwargs),
            (entity.context_rel_or_func, entity.context_type_or_func),
            defaults,
            args,
            kwargs,
            context_of=entity.media_type,
            base=base,
        )
    return schema, context


def _scrudful_handler(view: APIView, name: str) -> ScrudfulViewFunc | None:
    """The :class:`ScrudfulViewFunc` behind ``view.<name>``, if any."""
    handler = _handler(view, name)
    return handler if isinstance(handler, ScrudfulViewFunc) else None


def _meta_options(view: Any) -> dict[str, Any]:
    """The checked :data:`_VIEW_OPTIONS` and :data:`_HSR_OPTIONS` of ``view``'s
    :func:`scrudful_viewset` ``Meta`` (empty for another view)."""
    return _class_meta_options(type(view))


def _class_meta_options(cls: Any) -> dict[str, Any]:
    """:func:`_meta_options` of the view class ``cls``."""
    return getattr(cls, _META_OPTIONS_ATTR, None) or {}


def _view_base_url(view: Any) -> str | None:
    """``view``'s ``Meta.base_url``, else ``SCRUDFUL["BASE_URL"]``, else ``None``."""
    base_url = _meta_options(view).get("base_url")
    return base_url if base_url is not None else setting_base_url()


def _handler_base_url(view: Any, handler: Any) -> str | None:
    """The base URL of ``handler``'s links: its ``base_url``, else the view's
    (:func:`_view_base_url`)."""
    base_url = getattr(handler, "base_url", None)
    if isinstance(handler, ScrudfulViewFunc) and base_url is not None:
        return base_url
    return _view_base_url(view)


def _is_scrudful_view(view: APIView) -> bool:
    """Whether ``view`` is decorated by :func:`scrudful_viewset` or has a
    :class:`ScrudfulViewFunc` handler for a method it allows."""
    if getattr(type(view), _META_OPTIONS_ATTR, None) is not None:
        return True
    return any(
        _scrudful_handler(view, method.lower()) is not None
        for method in view.allowed_methods
    )


def _gives_error_links(view: APIView) -> bool:
    """Whether ``view``'s exception handler is :func:`exception_handler` (or
    one carrying its ``scrudful_error_links`` marker)."""
    get_handler = getattr(view, "get_exception_handler", None)
    return get_handler is not None and bool(
        getattr(get_handler(), "scrudful_error_links", False)
    )


def _check_base_url(url: Any, what: str) -> None:
    """Raise ``ImproperlyConfigured`` unless ``url`` is an absolute URL."""
    try:
        check_base_url(url, what)
    except HsrConfigError as exc:
        raise ImproperlyConfigured(str(exc)) from exc


def _check_view_options(
    entity: Any,
    statuses: Any,
    allowed_func: Any,
    base_url: Any,
    **links: Any,
) -> dict[str, tuple[int, ...]] | None:
    """Check the ``entity``, ``statuses``, ``allowed_func`` and ``base_url``
    options (see :class:`ScrudfulViewFunc`); return ``statuses`` checked, as
    a dict of tuples (or ``None``). ``links`` are the link options given
    with them, which ``entity`` replaces."""
    if entity is not None:
        if not isinstance(entity, Entity):
            raise TypeError(f"entity must be an Entity, not {type(entity).__name__}")
        given = [name for name, value in links.items() if value is not None]
        if given:
            raise TypeError(
                f"entity replaces {', '.join(given)}: give the links in the " "Entity"
            )
    if allowed_func is not None and not callable(allowed_func):
        raise TypeError(
            f"allowed_func must be callable, not {type(allowed_func).__name__}"
        )
    if base_url is not None:
        _check_base_url(base_url, "base_url")
    return _check_statuses(statuses)


def _check_statuses(statuses: Any) -> dict[str, tuple[int, ...]] | None:
    """``statuses`` (see :class:`ScrudfulViewFunc`) as a dict of tuples."""
    if statuses is None:
        return None
    if not isinstance(statuses, Mapping):
        raise TypeError(
            "statuses must map method names to lists of status codes, not "
            f"{type(statuses).__name__}"
        )
    names = [method for method in METHOD_KEYS if method != "options"]
    checked = {}
    for method, codes in statuses.items():
        key = method.lower() if isinstance(method, str) else method
        if key not in names:
            raise ValueError(
                f"statuses: {method!r} is not a method name (one of "
                f"{', '.join(names)})"
            )
        if isinstance(codes, (str, bytes)) or not isinstance(codes, Sequence):
            raise TypeError(
                f"statuses[{method!r}] must be a list of status codes, not "
                f"{type(codes).__name__}"
            )
        if not codes:
            raise ValueError(f"statuses[{method!r}] lists no status")
        for code in codes:
            if not isinstance(code, int) or isinstance(code, bool):
                raise TypeError(
                    f"statuses[{method!r}] must list integers, not {code!r}"
                )
            if not 200 <= code <= 299:
                raise ValueError(
                    f"statuses[{method!r}]: {code} is not a success (2xx) status"
                )
        checked[key] = tuple(dict.fromkeys(codes))
    return checked


class ScrudfulPagination(BasePagination):
    """Paginate a list view into the scrud list envelope.

    The response body is the page envelope that django-ninja's
    ``scrudful_list`` produces (``count``, ``page_count``, ``first``,
    ``previous``, ``next``, ``last``, ``content``), each entry having ``href``,
    ``etag``, ``last_modified``, ``link`` and ``content``. Set it as the view's
    ``pagination_class``; views that don't are unchanged.

    The view needs ``Meta.href_func(view, request, obj)``, returning the URL of
    an item (``ImproperlyConfigured`` otherwise). With ``Meta.object_validators``
    an entry's ``etag`` and ``last_modified`` come from ``Meta.etag_func(obj)``
    and ``Meta.last_modified_func(obj)``; without it they are ``None`` (those
    callbacks take the URL arguments, not an object). An entry's ``link`` holds
    the schema and context links of the view's item ``Meta`` options; their
    callbacks are called as for the item routes, with the object's lookup value
    as the URL keyword (``pk=...``).

    A non-integer or out of range ``page`` or ``page_size`` is a 400 naming
    the parameter (both, when both are bad, unless a subclass overrides
    ``_integer``), each error with the code of a DRF
    ``IntegerField``'s: ``invalid`` (not an integer), ``min_value`` (below 1)
    or ``max_value`` (``page_size`` above ``max_page_size``). A ``page`` past
    the last one is a 404 (page 1 always exists).
    """

    page_size = 100
    max_page_size = 1000
    page_query_param = "page"
    page_size_query_param = "page_size"

    def paginate_queryset(
        self, queryset: Any, request: Request, view: Any = None
    ) -> list[Any]:
        """The objects of the requested page of ``queryset``."""
        meta = getattr(view, "Meta", None)
        href_func = getattr(meta, "href_func", None)
        if href_func is None:
            raise ImproperlyConfigured(
                f"{type(view).__name__} uses ScrudfulPagination: it needs "
                "Meta.href_func(view, request, obj), returning an item's URL"
            )
        # Each parameter goes through _integer. With scrudful's own, their
        # errors are merged, so one 400 names both; an overridden one stops at
        # the first error, as in 0.6.0 (it is not called for page_size once
        # page failed).
        collect = type(self)._integer is ScrudfulPagination._integer
        values, errors = [], {}
        for name, default, maximum in (
            (self.page_query_param, 1, None),
            (self.page_size_query_param, self.page_size, self.max_page_size),
        ):
            try:
                values.append(self._integer(request, name, default, maximum))
            except ValidationError as exc:
                if not collect:
                    raise
                detail = exc.detail
                if isinstance(detail, dict):
                    errors.update(detail)
                else:
                    errors[name] = detail
        if errors:
            raise ValidationError(errors)
        number, size = values
        try:
            # The unordered-queryset warning points at this method's caller.
            self.count, self.objects = slice_page(queryset, number, size)
        except InvalidPage:
            raise NotFound("Invalid page.") from None
        self.request, self.view, self.number, self.size = request, view, number, size
        self.href_func = href_func
        return self.objects

    def get_paginated_response(self, data: Any) -> Response:
        """The envelope of the page, ``data`` being the serialized objects."""
        view, request = self.view, self.request
        meta = getattr(view, "Meta", None)
        validators = bool(getattr(meta, "object_validators", False))
        etag_func = getattr(meta, "etag_func", None) if validators else None
        modified_func = (
            getattr(meta, "last_modified_func", None) if validators else None
        )
        entity = getattr(meta, "entity", None) or Entity(
            **{name: getattr(meta, name, None) for name in _LINK_OPTIONS}
        )
        base_url = _view_base_url(view)
        entries = []
        for obj, item in zip(self.objects, data, strict=True):
            href = self.href_func(view, request, obj)
            if href is None:
                raise TypeError(
                    f"href_func returned None for {obj!r}: it must return the "
                    "item's URL"
                )
            etag = None
            if etag_func is not None:
                value = etag_func(obj)
                etag = None if value is None else quote_etag(value)
            last_modified = None
            if modified_func is not None:
                stamp = http_timestamp(aware(modified_func(obj)))
                last_modified = None if stamp is None else format_http_date(stamp)
            links = _entity_links(
                entity,
                (view, request, *getattr(view, "args", ())),
                self._item_kwargs(obj),
                base_url,
            )
            entries.append(
                envelope_entry(
                    href=iri_to_uri(str(href)),
                    etag=etag,
                    last_modified=last_modified,
                    link=join_links(*links),
                    content=item,
                )
            )
        return Response(
            page_envelope(
                count=self.count,
                number=self.number,
                size=self.size,
                page_url=functools.partial(
                    page_url, request.build_absolute_uri(), request.query_params
                ),
                entries=entries,
            )
        )

    def get_paginated_response_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """The OpenAPI schema of the envelope; ``schema`` describes the list."""
        envelope = collection_schema("#")
        envelope.pop("$schema")
        envelope["properties"]["content"]["items"]["properties"]["content"] = (
            schema.get("items", schema)
        )
        return envelope

    def _integer(
        self, request: Request, name: str, default: int, maximum: int | None
    ) -> int:
        """The query parameter ``name`` as an integer in ``[1, maximum]``.

        :meth:`paginate_queryset` calls it for ``page`` and ``page_size`` and
        merges the errors of both into one ``ValidationError``. A subclass may
        override it (to clamp ``page_size``, say): ``paginate_queryset`` then
        raises the first error, as in 0.6.0, without calling it for
        ``page_size`` once ``page`` failed. Or override
        :meth:`_parse_parameter`, which keeps both errors.

        Raises
        ------
        rest_framework.exceptions.ValidationError
            ``{name: [error]}``, the error of :meth:`_parse_parameter`.
        """
        value, error = self._parse_parameter(request, name, default, maximum)
        if error is not None:
            raise ValidationError({name: [error]})
        return value  # type: ignore[return-value]

    def _parse_parameter(
        self, request: Request, name: str, default: int, maximum: int | None
    ) -> tuple[int, None] | tuple[None, ErrorDetail]:
        """The query parameter ``name`` parsed once: ``(value, None)``, or
        ``(None, error)``.

        ``default`` when it is absent. An integer is what django-ninja's
        ``scrudful_list`` accepts (pydantic's lax ``int``: ``"2.0"`` is 2,
        ``"٣"`` is not an integer), see
        :func:`scrudful._adapter.parse_query_int`. The error's code is
        ``invalid`` (not an integer), ``min_value`` (below 1) or ``max_value``
        (above ``maximum``), with the message a DRF ``IntegerField`` gives
        (translated as DRF's are). A subclass may
        override it to accept other values (``page=last``, say) by returning
        their integer.
        """
        raw = request.query_params.get(name)
        if raw is None:
            return default, None
        messages = IntegerField.default_error_messages
        value = parse_query_int(raw)
        if value is None:
            return None, ErrorDetail(str(messages["invalid"]), code="invalid")
        if value < 1:
            return None, ErrorDetail(
                messages["min_value"].format(min_value=1), code="min_value"
            )
        if maximum is not None and value > maximum:
            return None, ErrorDetail(
                messages["max_value"].format(max_value=maximum), code="max_value"
            )
        return value, None

    def _item_kwargs(self, obj: Any) -> dict[str, Any]:
        """The URL keywords of ``obj``'s detail route, as its callbacks get them.

        The list route's own keywords (a nested route's parent, say) plus the
        lookup keyword set to ``obj``'s value.
        """
        kwargs = dict(getattr(self.view, "kwargs", None) or {})
        field = getattr(self.view, "lookup_field", None)
        if field is None:
            return kwargs
        name = getattr(self.view, "lookup_url_kwarg", None) or field
        try:
            kwargs[name] = getattr(obj, field)
        except AttributeError:
            pass
        return kwargs
