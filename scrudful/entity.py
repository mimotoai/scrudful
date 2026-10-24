"""The :class:`Entity` shared by the DRF, django-ninja and FastAPI adapters.

Stdlib only: importing it pulls in no framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scrudful._adapter import is_in_band, is_json, media_type_key

__all__ = ["Entity"]


@dataclass(frozen=True)
class Entity:
    """The schema and RDF context of a request or response body.

    Each ``schema_*`` and ``context_*`` field is a value or a callable, with
    the same meaning as the ``schema_*`` and ``context_*`` arguments of
    :func:`scrudful`: without a link, no Link is sent. The schema link
    defaults to rel ``"describedby"`` and type ``"application/schema+json"``;
    the context link's defaults depend on ``media_type``. A callable follows
    the calling convention of its adapter: ``func(request, obj)`` for
    django-ninja (``obj`` is the object the operation has at that point, the
    loaded object or ``None``), ``func(view, request, **url_kwargs)`` for DRF
    and ``func(**endpoint_kwargs)`` for FastAPI.

    Attributes
    ----------
    schema_link_or_func, schema_rel_or_func, schema_type_or_func : object
        The schema link (the URL, the rel and the media type of the schema).
    context_link_or_func, context_rel_or_func, context_type_or_func : object
        The context link (the URL, the rel and the media type of the RDF
        context).
    media_type : str
        The media type of the body this entity describes. Normalized to
        lowercase ``type/subtype``, parameters kept as written. It decides
        the context link's defaults:

        - JSON content (``application/json``, ``+json``): rel
          ``"http://www.w3.org/ns/json-ld#context"`` and type
          ``"application/ld+json"``;
        - in-band RDF (JSON-LD, Turtle, N-Triples, N-Quads, TriG, RDF/XML):
          the content carries its own RDF, so no context link is sent;
        - any other content (e.g. ``text/csv``): rel
          ``"https://w3id.org/semantic-http/ns#rdf-context"``, and the type
          of the lifting description must be given.

        An explicit ``context_rel_or_func`` or ``context_type_or_func`` wins
        over these defaults.

    Raises
    ------
    ValueError
        If ``media_type`` is not a media type; if it is in-band and
        ``context_link_or_func`` is set; or if it is neither JSON nor
        in-band, ``context_link_or_func`` is set and ``context_type_or_func``
        is not.
    """

    schema_link_or_func: Any = None
    schema_rel_or_func: Any = None
    schema_type_or_func: Any = None
    context_link_or_func: Any = None
    context_rel_or_func: Any = None
    context_type_or_func: Any = None
    media_type: str = "application/json"

    def __post_init__(self) -> None:
        media_type = media_type_key(self.media_type)
        object.__setattr__(self, "media_type", media_type)
        if not self.context_link_or_func:
            return
        if is_in_band(media_type):
            raise ValueError(
                f"{media_type} content carries its own RDF; it takes no context " "link"
            )
        if not is_json(media_type) and not self.context_type_or_func:
            raise ValueError(
                f"{media_type} content needs a context type: give "
                "context_type_or_func, the media type of the lifting description"
            )
