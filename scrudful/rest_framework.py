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
    plain Django views) with a JSON body, 304 without a body.

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
    The Link header is added to every response the view returns and is appended
    to any Link value the view set. ``Access-Control-Expose-Headers`` is
    extended with whichever of ``ETag``, ``Link`` and ``Location`` the response
    carries (django-cors-headers replaces that header when ``CORS_EXPOSE_HEADERS``
    is set; list the names there instead). Exceptions raised by the view (e.g.
    ``Http404``) propagate unchanged and are rendered by DRF without these
    headers.
"""

from __future__ import annotations

import copy
import functools
import inspect
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from django.http import (
    HttpRequest,
    HttpResponseBase,
    HttpResponseNotModified,
    JsonResponse,
)
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.metadata import BaseMetadata, SimpleMetadata
from rest_framework.mixins import UpdateModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from scrudful.conditional import (
    SAFE_METHODS,
    ConditionalResult,
    evaluate_preconditions,
    http_timestamp,
    quote_etag,
    validator_headers,
)
from scrudful.utils import (
    EXPOSABLE_HEADERS,
    join_links,
    link_content,
    merge_expose_headers,
)

__all__ = [
    "ScrudfulAPIViewMetadata",
    "ScrudfulMetadata",
    "ScrudfulViewFunc",
    "scrudful",
    "scrudful_api_view",
    "scrudful_viewset",
]

DEFAULT_SCHEMA_REL = "describedBy"
DEFAULT_SCHEMA_TYPE = "application/json"
DEFAULT_CONTEXT_REL = "http://www.w3.org/ns/json-ld#context"
DEFAULT_CONTEXT_TYPE = "application/ld+json"

_PRECONDITION_FIELDS = (
    "if-match",
    "if-none-match",
    "if-modified-since",
    "if-unmodified-since",
)
_FRESH_VALIDATOR_METHODS = frozenset({"PUT", "PATCH"})
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
        URL of the schema of the response body. If no value is given, no
        schema link is sent.
    schema_rel_or_func : str or callable, optional
        ``rel`` of the schema link. Defaults to ``"describedBy"``.
    schema_type_or_func : str or callable, optional
        ``type`` of the schema link. Defaults to ``"application/json"``.
    context_link_or_func : str or callable, optional
        URL of the JSON-LD context of the response body. If no value is given,
        no context link is sent.
    context_rel_or_func : str or callable, optional
        ``rel`` of the context link. Defaults to
        ``"http://www.w3.org/ns/json-ld#context"``.
    context_type_or_func : str or callable, optional
        ``type`` of the context link. Defaults to ``"application/ld+json"``.
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
    ) -> None:
        _check_validators(etag_func, last_modified_func)
        _check_sync(view_method)
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
        self._from_viewset = False

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return types.MethodType(self, obj)

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
        if self.has_validators:
            etag, last_modified = self._validators(cb_args, cb_kwargs)
            result = evaluate_preconditions(
                method,
                _precondition_headers(request),
                etag=etag,
                last_modified=last_modified,
                require_precondition=True,
            )
            if result is not None:
                return _conditional_response(result, isinstance(request, Request))

        response = self.view_method(*args, **kwargs)

        if self.has_validators and 200 <= response.status_code < 300:
            if method in _FRESH_VALIDATOR_METHODS:
                etag, last_modified = self._validators(cb_args, cb_kwargs)
            if method in SAFE_METHODS or method in _FRESH_VALIDATOR_METHODS:
                for name, value in validator_headers(etag, last_modified).items():
                    if name not in response:
                        response[name] = value
        link = join_links(response.get("Link"), self.link_header(*cb_args, **cb_kwargs))
        if link is not None:
            response["Link"] = link
        self.add_expose_headers(response)
        return response

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
            if isinstance(value, datetime) and timezone.is_naive(value):
                value = timezone.make_aware(value, timezone.get_default_timezone())
            last_modified = http_timestamp(value)
        return etag, last_modified

    def add_expose_headers(self, response: HttpResponseBase) -> None:
        """Add the ETag/Link/Location headers present to the expose header."""
        _expose_headers(response)

    def schema_link(self, *args: Any, **kwargs: Any) -> Any:
        """Return the schema URL, or ``None``."""
        return _evaluate(self.schema_link_or_func, args, kwargs) or None

    def context_link(self, *args: Any, **kwargs: Any) -> Any:
        """Return the JSON-LD context URL, or ``None``."""
        return _evaluate(self.context_link_or_func, args, kwargs) or None

    def schema_link_header(self, *args: Any, **kwargs: Any) -> str | None:
        """Return the schema link-value, or ``None``."""
        return self._link_value(
            self.schema_link(*args, **kwargs),
            (self.schema_rel_or_func, self.schema_type_or_func),
            (DEFAULT_SCHEMA_REL, DEFAULT_SCHEMA_TYPE),
            args,
            kwargs,
        )

    def context_link_header(self, *args: Any, **kwargs: Any) -> str | None:
        """Return the JSON-LD context link-value, or ``None``."""
        return self._link_value(
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

    @staticmethod
    def _link_value(
        url: Any,
        rel_and_type: tuple[Any, Any],
        defaults: tuple[str, str],
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> str | None:
        if not url:
            return None
        rel = _evaluate(rel_and_type[0], args, kwargs) or defaults[0]
        content_type = _evaluate(rel_and_type[1], args, kwargs) or defaults[1]
        return link_content(url, rel, content_type)


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
    }
    _check_validators(etag_func, last_modified_func)

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
    decorate = scrudful(
        etag_func=etag_func,
        last_modified_func=last_modified_func,
        schema_link_or_func=schema_link_or_func,
        schema_rel_or_func=schema_rel_or_func,
        schema_type_or_func=schema_type_or_func,
        context_link_or_func=context_link_or_func,
        context_rel_or_func=context_rel_or_func,
        context_type_or_func=context_type_or_func,
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
    configuration only: its target is the collection, so the item validators
    do not apply to it. Methods the class doesn't have are not added, and
    methods decorated with :func:`scrudful` explicitly are left alone. OPTIONS
    is answered by :class:`ScrudfulMetadata` only when the class's
    ``metadata_class`` is DRF's default ``SimpleMetadata``; any other value
    (``None``, or a class set on the viewset, a base class or
    ``DEFAULT_METADATA_CLASS``) is kept, so subclass or mix in
    :class:`ScrudfulMetadata` to combine it with a custom metadata class. A
    user-defined ``options`` is kept.

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
       (it is then not wrapped). With a user-defined ``partial_update`` that
       calls ``update``, the wrapper that ``Meta`` configures for
       ``partial_update`` handles the request first and the explicit
       ``update`` decorator is a pass-through for PATCH. Decorate
       ``partial_update`` explicitly as well to override it.

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
    schema_link_or_func, schema_rel_or_func, schema_type_or_func : optional
        Item schema link.
    context_link_or_func, context_rel_or_func, context_type_or_func : optional
        Item JSON-LD context link.
    list_etag_func, list_last_modified_func : callable, optional
        Validators of the ``list`` response.
    list_schema_link_or_func, list_schema_rel_or_func, list_schema_type_or_func
        Schema link of the ``list`` response.
    list_context_link_or_func, list_context_rel_or_func, list_context_type_or_func
        JSON-LD context link of the ``list`` response.
    """
    meta = getattr(cls, "Meta", None)

    def options(prefix: str, names: Sequence[str]) -> dict[str, Any]:
        return {name: getattr(meta, prefix + name, None) for name in names}

    item = options("", _VALIDATOR_OPTIONS + _LINK_OPTIONS)
    configs = {
        "create": options("", _LINK_OPTIONS),
        "retrieve": item,
        "update": item,
        "partial_update": item,
        "destroy": item,
        "list": options("list_", _VALIDATOR_OPTIONS + _LINK_OPTIONS),
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


def _install_metadata(cls: type) -> None:
    """Make OPTIONS use :class:`ScrudfulMetadata` unless the class chose another.

    Only DRF's stock default, ``SimpleMetadata``, is replaced: ``None`` and any
    other class (set on the class, a base class or ``DEFAULT_METADATA_CLASS``)
    are the project's choice and are kept.
    """
    if getattr(cls, "metadata_class", None) is SimpleMetadata:
        cls.metadata_class = ScrudfulMetadata  # type: ignore[attr-defined]


_READ_RESPONSES = {
    "304": {
        "description": "Not Modified: the representation matches the request's "
        "If-None-Match or If-Modified-Since validators."
    },
    "400": {"description": "Malformed If-Match or If-None-Match header."},
    "412": {"description": "Precondition Failed."},
}
_WRITE_RESPONSES = {
    "400": {"description": "Malformed If-Match or If-None-Match header."},
    "412": {"description": "Precondition Failed."},
    "428": {
        "description": "Precondition Required: this request must be conditional "
        "(send If-Match or If-Unmodified-Since)."
    },
}


class ScrudfulMetadata(BaseMetadata):
    """OPTIONS metadata describing the methods a SCRUDful view serves.

    The body maps each method the route serves (``get``, ``post``, ``put``,
    ``patch``, ``delete``) to an OpenAPI-like operation: the request body and
    its schema/context links, the responses, and for conditional writes the
    precondition header parameters. The routed methods are taken from the
    viewset's action map (so list, detail, nested and ``@action`` routes are
    each described correctly), or from the view's allowed methods. Link
    callbacks are called with the same arguments as for the request
    (``func(view, request, **url_kwargs)``). Methods that are not SCRUDful are
    described without links or preconditions.
    """

    def determine_metadata(self, request: Request, view: APIView) -> dict[str, Any]:
        args = (view, request, *getattr(view, "args", ()))
        kwargs = dict(getattr(view, "kwargs", None) or {})
        metadata = {}
        for method, handler in self._handlers(view):
            describe = getattr(self, f"determine_metadata_for_{method}")
            metadata[method] = describe(handler, args, kwargs)
        return metadata

    @staticmethod
    def _handlers(view: APIView) -> Iterator[tuple[str, Any]]:
        action_map = getattr(view, "action_map", None)
        if action_map is None:
            action_map = {name: name for name in view.http_method_names}
        for method in ("get", "post", "put", "patch", "delete"):
            name = action_map.get(method)
            handler = getattr(view, name, None) if name else None
            if handler is not None:
                yield method, getattr(handler, "__func__", handler)

    def get_method(self, view: APIView, name: str) -> ScrudfulViewFunc | None:
        """Return the :class:`ScrudfulViewFunc` behind ``view.<name>``, if any."""
        handler = getattr(view, name, None)
        handler = getattr(handler, "__func__", handler)
        return handler if isinstance(handler, ScrudfulViewFunc) else None

    def determine_metadata_for_get(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        ok: dict[str, Any] = {"description": "OK"}
        content = _content(handler, args, kwargs)
        if content:
            ok["content"] = content
        responses = {"200": ok}
        if _has_validators(handler):
            responses.update(copy.deepcopy(_READ_RESPONSES))
        return {"responses": responses}

    def determine_metadata_for_post(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._write(
            handler, args, kwargs, "The content for the resource to be created.", "201"
        )

    def determine_metadata_for_put(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._write(
            handler, args, kwargs, "The new content of the resource.", "200"
        )

    def determine_metadata_for_patch(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._write(
            handler, args, kwargs, "The changes to apply to the resource.", "200"
        )

    def determine_metadata_for_delete(
        self, handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._write(handler, args, kwargs, None, "204")

    def _write(
        self,
        handler: Any,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
        body_description: str | None,
        status: str,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if body_description is not None:
            request_body: dict[str, Any] = {
                "description": body_description,
                "required": True,
            }
            content = _content(handler, args, kwargs)
            if content:
                request_body["content"] = content
            metadata["requestBody"] = request_body
        metadata["responses"] = {status: {"description": _STATUS_TEXT[status]}}
        if _has_validators(handler):
            if status == "201":
                # POST is evaluated but never required to be conditional.
                metadata["responses"].update(
                    {
                        code: copy.deepcopy(_WRITE_RESPONSES[code])
                        for code in ("400", "412")
                    }
                )
            else:
                metadata["responses"].update(copy.deepcopy(_WRITE_RESPONSES))
                metadata["parameters"] = self.required_conditional_headers(handler)
        return metadata

    def required_conditional_headers(
        self, method: ScrudfulViewFunc
    ) -> list[dict[str, Any]] | None:
        """Describe the precondition headers a conditional write accepts.

        One of them is required; a header is marked ``required`` when it is
        the only one accepted.
        """
        headers = []
        if method.etag_func is not None:
            headers.append(
                ("If-Match", "The entity-tag of the current representation.")
            )
        if method.last_modified_func is not None:
            headers.append(
                (
                    "If-Unmodified-Since",
                    "The Last-Modified date of the current representation.",
                )
            )
        if not headers:
            return None
        either = " Either If-Match or If-Unmodified-Since is required."
        return [
            {
                "in": "header",
                "name": name,
                "schema": {"type": "string"},
                "required": len(headers) == 1,
                "description": (
                    description if len(headers) == 1 else description + either
                ),
            }
            for name, description in headers
        ]


#: Kept for backwards compatibility; :class:`ScrudfulMetadata` now describes
#: ``APIView`` function views too.
ScrudfulAPIViewMetadata = ScrudfulMetadata

_STATUS_TEXT = {"200": "OK", "201": "Created", "204": "No Content"}


def _check_validators(etag_func: Any, last_modified_func: Any) -> None:
    for name, func in (
        ("etag_func", etag_func),
        ("last_modified_func", last_modified_func),
    ):
        if func is not None and not callable(func):
            raise TypeError(f"{name} must be callable, not {type(func).__name__}")


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


def _precondition_headers(request: HttpRequest | Request) -> dict[str, str]:
    # WSGI/ASGI servers combine repeated field lines with "," (RFC 9110 §5.3).
    headers = {}
    for name in _PRECONDITION_FIELDS:
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def _conditional_response(result: ConditionalResult, drf: bool) -> HttpResponseBase:
    response: HttpResponseBase
    if result.body is None:
        response = HttpResponseNotModified()
        for name, value in result.headers.items():
            response[name] = value
    elif drf:
        response = Response(result.body, status=result.status, headers=result.headers)
    else:
        response = JsonResponse(result.body, status=result.status)
        for name, value in result.headers.items():
            response[name] = value
    _expose_headers(response)
    return response


def _expose_headers(response: HttpResponseBase) -> None:
    names = [name for name in EXPOSABLE_HEADERS if name in response]
    if names:
        response["Access-Control-Expose-Headers"] = merge_expose_headers(
            response.get("Access-Control-Expose-Headers"), names
        )


def _has_validators(handler: Any) -> bool:
    return isinstance(handler, ScrudfulViewFunc) and handler.has_validators


def _content(
    handler: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> dict[str, Any] | None:
    if not isinstance(handler, ScrudfulViewFunc):
        return None
    links = {}
    schema = handler.schema_link(*args, **kwargs)
    if schema:
        links["schema"] = str(schema)
    context = handler.context_link(*args, **kwargs)
    if context:
        links["context"] = str(context)
    return {"application/json": links} if links else None
