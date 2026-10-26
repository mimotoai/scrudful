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
    passed on.

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
    404.

    When either validator callback is configured, PUT, PATCH and DELETE on an
    existing resource must be conditional: a request without If-Match (or a
    valid If-Unmodified-Since, when ``last_modified_func`` is set, or
    ``If-None-Match: *``, which then fails with 412) is answered with 428
    Precondition Required. A malformed If-Match or If-None-Match is
    answered with 400, a failed precondition with 412 and a matching GET/HEAD
    with 304 carrying the validators. These responses are returned without
    calling the view: 400/412/428 as a DRF ``Response`` (a ``JsonResponse`` for
    plain Django views) with a JSON body, 304 without a body or Link. With an
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
    itself conditional (``UPDATE ... WHERE version = <expected>``).

Headers
    The Link header is added to the responses whose content the view's links
    describe: the 2xx but 204 it returns, to any method but TRACE (whose
    content is the echoed request). Another response it returns (an error,
    a 204, a redirect) gets no Links from scrudful, and keeps any Link the
    view set. Raise an error rather than return it to give it the ``error``
    entity's Links, through :func:`exception_handler`. The Link header is
    appended to any Link value the view set; a Link of the view's with the
    relation of the schema link (``describedby`` by default) replaces that
    link, and one with either context relation (JSON-LD or ``rdf-context``,
    one slot) the context link (see :func:`scrudful._adapter.merge_links`).
    Every link URL is resolved
    against the request's absolute URI (``request.build_absolute_uri()``),
    so relative values such as ``"/schemas/item"`` are sent absolute.
    ``Access-Control-Expose-Headers`` is extended with whichever of ``ETag``,
    ``Link`` and ``Location`` the response carries (django-cors-headers
    replaces that header when ``CORS_EXPOSE_HEADERS`` is set; list the names
    there instead). Exceptions raised by the view (e.g. ``Http404``) propagate
    unchanged and are rendered by DRF without these headers, unless the
    project's ``EXCEPTION_HANDLER`` is :func:`exception_handler`, which gives
    them the Links of the view's ``error`` entity.

OPTIONS
    With :class:`ScrudfulMetadata` (installed in place of DRF's default
    metadata class by :func:`scrudful_viewset`, :func:`scrudful_api_view` and
    a class-body :func:`scrudful` method, but not by :func:`scrudful` below
    ``@api_view``), OPTIONS is answered with a Semantic HTTP 0.1 HTTP Schema
    Resource: a JSON body describing each method the view allows, the Links
    of the HTTP Schema Resource's own schema and JSON-LD context, and an
    ``Allow`` listing exactly the body's methods. It is ``application/json``
    whatever renderer DRF's content negotiation picked, the browsable API's
    included. An ``Accept`` that none of the view's renderers serves (e.g.
    ``text/html`` on a view without the browsable API) still gets DRF's 406,
    raised before OPTIONS is handled.
"""

from __future__ import annotations

import functools
import inspect
import types
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import Http404, HttpRequest, HttpResponseBase
from django.utils.encoding import iri_to_uri
from rest_framework.exceptions import (
    APIException,
    ErrorDetail,
    NotFound,
    ValidationError,
)
from rest_framework.decorators import api_view
from rest_framework.fields import IntegerField
from rest_framework.metadata import BaseMetadata, SimpleMetadata
from rest_framework.mixins import UpdateModelMixin
from rest_framework.pagination import BasePagination
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.views import exception_handler as drf_exception_handler

from scrudful._adapter import (
    DEFAULT_CONTEXT_REL,
    DEFAULT_CONTEXT_TYPE,
    DEFAULT_SCHEMA_REL,
    DEFAULT_SCHEMA_TYPE,
    METHOD_KEYS,
    allow_header,
    check_error_body_func,
    check_error_media_type,
    check_validators,
    conditional_header_parameters,
    context_defaults,
    context_link_type,
    describe_content,
    describe_options,
    describe_read,
    describe_write,
    evaluates_preconditions,
    expose_headers,
    has_default_entity,
    is_json,
    merge_links,
    options_links,
    parse_query_int,
    reason_phrase,
    set_validator_headers,
    validator_policy,
    with_error_body,
)
from scrudful._django import aware, page_url, slice_page
from scrudful._django import conditional_response as django_conditional_response
from scrudful._django import precondition_headers as django_precondition_headers
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
from scrudful.utils import join_links, link_content, resolve_uri

__all__ = [
    "Entity",
    "ScrudfulAPIViewMetadata",
    "ScrudfulMetadata",
    "ScrudfulPagination",
    "ScrudfulViewFunc",
    "exception_handler",
    "scrudful",
    "scrudful_api_view",
    "scrudful_viewset",
]

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
_OBJECT_OPTIONS = ("object_validators",)
# The object a create saved (see ``object_validators``).
_CREATED_ATTR = "_scrudful_created"
# The object ``get_object()`` returned for the request (see ``object_validators``).
_OBJECT_ATTR = "_scrudful_object"
_ERROR_OPTIONS = ("error",)
# What the 400/412/428 bodies are built with; a create never evaluates
# preconditions, so it has none.
_ERROR_BODY_OPTIONS = ("error_body_func",)
#: The media type of the content a view's own links describe.
_VIEW_MEDIA_TYPE = "application/json"


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
        URL of the schema of the request and response bodies, resolved
        against the request's absolute URI (a relative URL such as
        ``"/schemas/item"`` works). If no value is given, no schema link is
        sent.
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
        these responses carry no Link and are rendered as before.
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
        no precondition is evaluated, and a 201 gets ``ETag`` and
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

    Raises
    ------
    TypeError
        If a validator callback isn't callable, ``error`` isn't an
        :class:`Entity`, the view is ``async def``, or ``object_validators``
        is set without a validator callback.
    ValueError
        If ``error``'s media type is not JSON content.
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
    ) -> None:
        check_validators(etag_func, last_modified_func)
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
        if issubclass(owner, APIView):
            _install_metadata(owner)
        else:
            _install_metadata_on_views(owner)

    @property
    def has_validators(self) -> bool:
        """Whether a validator callback is configured."""
        return self.etag_func is not None or self.last_modified_func is not None

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
        if self.has_validators and not self.creates and evaluates_preconditions(method):
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
        OPTIONS body.
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
        etag = None
        if self.etag_func is not None:
            value = self.etag_func(*args, **kwargs)
            etag = None if value is None else quote_etag(value)
        last_modified = None
        if self.last_modified_func is not None:
            value = self.last_modified_func(*args, **kwargs)
            last_modified = http_timestamp(aware(value))
        return etag, last_modified

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

        The URL is resolved against the absolute URI of the request among
        ``args``.
        """
        return _link_value(
            self.schema_link(*args, **kwargs),
            (self.schema_rel_or_func, self.schema_type_or_func),
            (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
            args,
            kwargs,
        )

    def context_link_header(self, *args: Any, **kwargs: Any) -> str | None:
        """Return the JSON-LD context link-value, or ``None``.

        The URL is resolved like the schema URL.
        """
        return _link_value(
            self.context_link(*args, **kwargs),
            (self.context_rel_or_func, self.context_type_or_func),
            (DEFAULT_CONTEXT_REL, DEFAULT_CONTEXT_TYPE),
            args,
            kwargs,
        )

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
        return join_links(*_entity_links(self.error, args, kwargs))


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
    there is no class yet, and the view keeps DRF's ``SimpleMetadata``, so
    its OPTIONS is no HTTP Schema Resource: use :func:`scrudful_api_view`
    in place of ``@api_view`` + ``@scrudful`` for one.
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
    }
    check_validators(etag_func, last_modified_func)
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
    (or ``None``) is kept, so subclass :class:`ScrudfulMetadata` to combine them.
    With :class:`ScrudfulMetadata` (or a subclass) the OPTIONS response is an
    HTTP Schema Resource (see :class:`ScrudfulMetadata`). DRF's ``api_view``
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
    configuration (and ``error``) only: its target is the collection, so the
    item validators do not apply to it. Methods the class doesn't have are not
    added, and methods decorated with :func:`scrudful` explicitly are left
    alone. OPTIONS is answered by :class:`ScrudfulMetadata` only when the
    class's ``metadata_class`` is DRF's default ``SimpleMetadata``; any other
    value (``None``, or a class set on the viewset, a base class or
    ``DEFAULT_METADATA_CLASS``) is kept, so subclass or mix in
    :class:`ScrudfulMetadata` to combine it with a custom metadata class. With
    :class:`ScrudfulMetadata` (or a subclass) the OPTIONS response is an HTTP
    Schema Resource (see :class:`ScrudfulMetadata`). A user-defined
    ``options`` is kept.

    Decorating a subclass of a decorated viewset again re-wraps the original
    methods with the subclass's configuration.

    Callbacks are called as ``func(view, request, **url_kwargs)``; callables
    stored on ``Meta`` are plain functions (``staticmethod`` works too).

    .. warning::

       The item link callbacks (``schema_link_or_func`` and friends) also run
       for ``create`` and for OPTIONS and POST on the list route, where there
       is no ``pk``. Accept ``**kwargs`` (``def schema_url(view, request,
       **kwargs)``) so they work for both.

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
        Validators of the ``list`` response.
    list_schema_link_or_func, list_schema_rel_or_func, list_schema_type_or_func
        Schema link of the ``list`` response.
    list_context_link_or_func, list_context_rel_or_func, list_context_type_or_func
        JSON-LD context link of the ``list`` response.
    list_error : Entity, optional
        Schema and context of the ``list`` response's 400 and 412 bodies.
    error_body_func : callable, optional
        As for :class:`ScrudfulViewFunc`, for the item routes (not the create).
    list_error_body_func : callable, optional
        As ``error_body_func``, for the ``list`` response's 400 and 412.
    href_func : callable, optional
        ``href_func(view, request, obj)``, the URL of an item, for
        :class:`ScrudfulPagination` (required by it; not used by the wrapped
        methods).
    """
    meta = getattr(cls, "Meta", None)

    def options(prefix: str, names: Sequence[str]) -> dict[str, Any]:
        return {name: getattr(meta, prefix + name, None) for name in names}

    item = options(
        "",
        _VALIDATOR_OPTIONS
        + _LINK_OPTIONS
        + _ERROR_OPTIONS
        + _ERROR_BODY_OPTIONS
        + _OBJECT_OPTIONS,
    )
    create = options("", _LINK_OPTIONS + _ERROR_OPTIONS)
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
            _VALIDATOR_OPTIONS + _LINK_OPTIONS + _ERROR_OPTIONS + _ERROR_BODY_OPTIONS,
        ),
    }
    for name, config in configs.items():
        method = getattr(cls, name, None)
        if method is None:
            continue
        if isinstance(method, ScrudfulViewFunc):
            if not method._from_viewset:
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
    """Make OPTIONS use :class:`ScrudfulMetadata` unless the class chose another.

    Only DRF's stock default, ``SimpleMetadata``, is replaced: ``None`` and any
    other class (set on the class, a base class or ``DEFAULT_METADATA_CLASS``)
    are the project's choice and are kept. DRF's stock ``APIView.options`` is
    replaced by :func:`_options`; an ``options`` the class defines is kept.
    """
    if getattr(cls, "metadata_class", None) is SimpleMetadata:
        cls.metadata_class = ScrudfulMetadata  # type: ignore[attr-defined]
    if getattr(cls, "options", None) is APIView.options:
        cls.options = _options  # type: ignore[attr-defined]


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
    """``APIView.options``, answering with an HTTP Schema Resource.

    When the view's metadata class is :class:`ScrudfulMetadata` (or a
    subclass), the 200 is rendered by DRF's ``JSONRenderer`` whatever renderer
    DRF's content negotiation picked (the browsable API's included), and
    carries the HTTP Schema Resource's own Links and an ``Allow`` listing the
    body's methods. Other responses, and those of any other metadata class,
    are DRF's (``metadata_class = None`` gives a 405; an ``Accept`` none of
    the view's renderers serves, DRF's 406, raised before this runs).
    """
    response = APIView.options(self, request, *args, **kwargs)
    metadata_class = self.metadata_class
    if not (
        isinstance(metadata_class, type)
        and issubclass(metadata_class, ScrudfulMetadata)
        and response.status_code == 200
    ):
        return response
    request.accepted_renderer = JSONRenderer()
    request.accepted_media_type = JSONRenderer.media_type
    # A fresh Response: there is no Link of the view's to keep.
    response["Link"] = options_links()
    allow = allow_header(response.data)
    response["Allow"] = allow
    # DRF's finalize_response sets the view's own headers, Allow among them.
    headers = getattr(self, "headers", None)
    if isinstance(headers, dict):
        headers["Allow"] = allow
    expose_headers(response)
    return response


class ScrudfulMetadata(BaseMetadata):
    """OPTIONS metadata: the HTTP Schema Resource (Semantic HTTP 0.1) of a view.

    The body is an OpenAPI Path Item with one operation for each method the
    view allows (``view.allowed_methods``), in the order ``get``, ``put``,
    ``post``, ``delete``, ``options``, ``head``, ``patch``, ``trace``:

    - ``get``, ``post``, ``put``, ``patch`` and ``delete``: the request body,
      the success response (201 for POST, 204 for DELETE, else 200) and, for
      conditional methods, scrudful's own 304/400/412/428 responses and the
      precondition header parameters. A request body and a success response
      (but the 204) have the content of the view's links (``application/json``,
      a ``schema`` ``$ref`` and an ``x-rdf-context``), and the 400, 412 and
      428 the content of its ``error`` entity;
    - ``head``: the same responses as ``get``, without content;
    - ``options``: this response, whose content is the HTTP Schema Resource;
    - any other method: a 200.

    The handler of each method is the view's (a viewset's action map routes
    list, detail, nested and ``@action`` routes each to their own). Link
    callbacks are called with the same arguments as for the request
    (``func(view, request, **url_kwargs)``), and every URL is resolved against
    the request's absolute URI. Those of the ``error`` entity are called only
    for a method whose description has its content: a conditional one, or a
    PUT, PATCH or POST whose serializer 400 :func:`exception_handler` gives
    its Links. Methods that are not SCRUDful are described without content
    or preconditions.

    The Links, ``Allow`` and JSON rendering of the OPTIONS response are added
    by the ``options`` that :func:`scrudful_viewset` and
    :func:`scrudful_api_view` install; a view that only sets
    ``metadata_class = ScrudfulMetadata`` gets the body alone.
    """

    def determine_metadata(self, request: Request, view: APIView) -> dict[str, Any]:
        args = (view, request, *getattr(view, "args", ()))
        kwargs = dict(getattr(view, "kwargs", None) or {})
        allowed = {method.lower() for method in view.allowed_methods}
        metadata = {}
        for method in METHOD_KEYS:
            if method not in allowed:
                continue
            describe = getattr(self, f"determine_metadata_for_{method}", None)
            if describe is None:
                metadata[method] = {"responses": {"200": {"description": "OK"}}}
            else:
                metadata[method] = describe(_handler(view, method), args, kwargs)
        return metadata

    def get_method(self, view: APIView, name: str) -> ScrudfulViewFunc | None:
        """Return the :class:`ScrudfulViewFunc` behind ``view.<name>``, if any."""
        handler = _handler(view, name)
        return handler if isinstance(handler, ScrudfulViewFunc) else None

    def determine_metadata_for_get(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        conditional = _is_conditional(handler, "get")
        return describe_read(
            _content(handler, args, kwargs),
            conditional,
            # Only scrudful's 400 and 412 have it: its callables run only then.
            error_content=(
                _error_content(handler, args, kwargs) if conditional else None
            ),
        )

    def determine_metadata_for_head(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return describe_read(None, _is_conditional(handler, "head"))

    def determine_metadata_for_options(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return describe_options()

    def determine_metadata_for_post(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._write("post", handler, args, kwargs)

    def determine_metadata_for_put(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._write("put", handler, args, kwargs)

    def determine_metadata_for_patch(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._write("patch", handler, args, kwargs)

    def determine_metadata_for_delete(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._write("delete", handler, args, kwargs)

    def _write(
        self,
        method: str,
        handler: Any,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        content = None if method == "delete" else _content(handler, args, kwargs)
        conditional = _is_conditional(handler, method)
        view = args[0] if args else None
        # The serializer's 400 carries the error Links only through
        # :func:`exception_handler` (or a handler delegating to it, which
        # carries its marker), so only then is it described.
        serializer_400 = (
            method != "delete"
            and getattr(view, "get_exception_handler", None) is not None
            and getattr(view.get_exception_handler(), "scrudful_error_links", False)
        )
        # The error entity's callables run only when its content is described.
        error_content = (
            _error_content(handler, args, kwargs)
            if conditional or serializer_400
            else None
        )
        metadata = describe_write(
            method,
            content,
            conditional,
            conditional and handler.etag_func is not None,
            conditional and handler.last_modified_func is not None,
            response_content=content,
            error_content=error_content,
        )
        if error_content and serializer_400:
            metadata["responses"].setdefault(
                "400",
                {"description": reason_phrase(400), "content": error_content},
            )
        if "parameters" in metadata:
            # Through the method, so that a subclass overriding it is used.
            metadata["parameters"] = self.required_conditional_headers(handler)
        return metadata

    def required_conditional_headers(
        self, method: ScrudfulViewFunc
    ) -> list[dict[str, Any]] | None:
        """Describe the precondition headers a conditional write accepts.

        One of them is required; a header is marked ``required`` when it is
        the only one accepted.
        """
        return conditional_header_parameters(
            method.etag_func is not None, method.last_modified_func is not None
        )


#: Kept for backwards compatibility; :class:`ScrudfulMetadata` now describes
#: ``APIView`` function views too.
ScrudfulAPIViewMetadata = ScrudfulMetadata


def _check_sync(view: Callable[..., Any]) -> None:
    if inspect.iscoroutinefunction(view):
        name = getattr(view, "__qualname__", repr(view))
        raise TypeError(f"@scrudful does not support async views ({name})")


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
    :class:`ScrudfulViewFunc`), so that it agrees with the OPTIONS body.
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
    ``patch``, ``trace``. With none, the response is DRF's, unchanged. The
    entity's callables are called as ``func(view, request, **url_kwargs)``.

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
    error = _error_entity(view, request)
    if error is None or not isinstance(request, Request):
        return response
    args, kwargs = _callback_arguments((view, request), {}, 1)
    link = merge_links(response.get("Link"), *_entity_links(error, args, kwargs))
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


def _error_entity(view: APIView, request: Any) -> Entity | None:
    """The error entity of an error of ``view`` answering ``request``.

    The ``error`` of the request's own handler (``view.action``'s, a
    viewset's, else the request method's) when it is a
    :class:`ScrudfulViewFunc`, even ``None``. Otherwise (an OPTIONS, a 405
    for a method no handler serves, a handler that is not scrudful), the
    view-wide one, from the scrudful handlers of the view's methods (a
    viewset's route's action map): the one ``error`` they share; else, when
    they have several, the GET handler's; else the first in the order of
    :data:`~scrudful._adapter.METHOD_KEYS`. The OPTIONS body lists none of
    these errors, so any error entity of the view describes them; a body
    with content then always has Links.
    """
    metadata = ScrudfulMetadata()
    for name in (getattr(view, "action", None), getattr(request, "method", None)):
        handler = metadata.get_method(view, name.lower()) if name else None
        if handler is not None:
            return handler.error
    errors: dict[str, Entity] = {}
    for method in METHOD_KEYS:
        if method.upper() not in view.allowed_methods:
            continue
        handler = metadata.get_method(view, method)
        if handler is not None and handler.error is not None:
            errors[method] = handler.error
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

    With validators, for a method :func:`scrudful._adapter.evaluates_preconditions`
    accepts (not TRACE).
    """
    if isinstance(handler, ScrudfulViewFunc) and handler.creates:
        return False  # it describes the object it saved, and checks nothing
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


def _resolve(url: Any, args: Sequence[Any]) -> str:
    """``url`` resolved against the absolute URI of the request among ``args``."""
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
) -> str | None:
    """One link-value; the URL is resolved as in the OPTIONS body.

    The context link of ``context_of`` content takes its type from
    :func:`scrudful._adapter.context_link_type`, as the OPTIONS body does.
    """
    if not url:
        return None
    rel = _evaluate(rel_and_type[0], args, kwargs) or defaults[0]
    content_type = _evaluate(rel_and_type[1], args, kwargs)
    if context_of is not None:
        content_type = context_link_type(context_of, content_type)
    content_type = content_type or defaults[1]
    return link_content(_resolve(url, args), rel, content_type)


def _entity_links(
    entity: Entity, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """The schema and the context link-values of ``entity`` (``None`` if unset).

    The context link's defaults come from ``entity.media_type``; in-band RDF
    has none.
    """
    schema = _link_value(
        _evaluate(entity.schema_link_or_func, args, kwargs),
        (entity.schema_rel_or_func, entity.schema_type_or_func),
        (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
        args,
        kwargs,
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
        )
    return schema, context


def _content(
    handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The ``content`` of the view's own links (JSON), or ``None``."""
    if not isinstance(handler, ScrudfulViewFunc):
        return None
    return _media(handler, _VIEW_MEDIA_TYPE, args, kwargs)


def _error_content(
    handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The ``content`` of the handler's ``error`` entity, or ``None``."""
    if not isinstance(handler, ScrudfulViewFunc) or handler.error is None:
        return None
    return _media(handler.error, handler.error.media_type, args, kwargs)


def _media(
    source: ScrudfulViewFunc | Entity,
    media_type: str,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The ``content`` of ``source``'s links: its Media Type Object, or ``None``.

    Built by :func:`scrudful._adapter.describe_content` from the schema and
    context URLs and types of ``source`` (a view or an :class:`Entity`), which
    are evaluated as for its Links (the context only when ``media_type`` takes
    one), and with the URLs resolved the same way, so that the entry agrees
    with the Links of the responses it describes. ``None`` without a schema
    URL.
    """
    schema = _evaluate(source.schema_link_or_func, args, kwargs)
    if not schema:
        return None
    schema_type = _evaluate(source.schema_type_or_func, args, kwargs)
    context = context_type = None
    rel, default_type = context_defaults(media_type)
    if rel is not None:  # not in-band RDF
        context = _evaluate(source.context_link_or_func, args, kwargs)
    if context:
        context_type = (
            _evaluate(source.context_type_or_func, args, kwargs) or default_type
        )
    return describe_content(
        media_type,
        _resolve(schema, args),
        str(schema_type or DEFAULT_SCHEMA_TYPE),
        _resolve(context, args) if context else None,
        None if context_type is None else str(context_type),
    )


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
        values, errors = {}, {}
        for name, default, maximum in (
            (self.page_query_param, 1, None),
            (self.page_size_query_param, self.page_size, self.max_page_size),
        ):
            try:
                values[name] = self._integer(request, name, default, maximum)
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
        number = values[self.page_query_param]
        size = values[self.page_size_query_param]
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
        entity = Entity(**{name: getattr(meta, name, None) for name in _LINK_OPTIONS})
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
