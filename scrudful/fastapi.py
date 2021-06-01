from datetime import timezone
from email.utils import formatdate
from fastapi import Request, Response
from functools import wraps
from inspect import iscoroutinefunction

from scrudful import __version__
from scrudful.handlers.fastapi import get_conditional_response, quote_etag
from scrudful.utils import get_string_or_evaluate, link_content


def scrudful(
    etag_func=None,
    last_modified_func=None,
    schema_link_or_func=None,
    schema_rel_or_func=None,
    schema_type_or_func=None,
    context_link_or_func=None,
    context_rel_or_func=None,
    context_type_or_func=None,
):
    """Decorator to make a view SCRUDful

    Parameters
    ----------
    etag_func : callable, optional
        A function to retrieve the etag for the request. If no function is provided
        the `ETag` response header won't be included in the response headers.
    last_modified_func : callable, optional
        A function to retrieve a `DateTime` to use for the the `Last-Modified`
        response header. If no function is provided the `Last-Modified` response
        header won't be included in the response headers.
    schema_link_or_func : str or callable, opitional
        The URL of the schema for the response body or a function to retrieve it. If
        no value or function is specified no schema link will be included in the
        response headers.
    schema_rel_or_func : str or callable, optional
        The value of the `rel` attribute to be provided with the link to the schema
        in a HTTP link response header. The default value will be `"describedBy"` if
        no value or function is provided.
    schema_type_or_func : str or callable, optional
        The value of the `type` attribute to be provided with the link to the schema
        in a HTTP link response header. The default value will be
        `"application/json"` if no value or function is provided.
    context_link_or_func : str or callable, optional
        The URL of the linked data context for the response body or a function to
        retrieve it. If no value or function is specified no linked data context
        link will be included in the response headers.
    context_rel_or_func : str or callable, optional
        The value of the `rel` attribute to be provided with the link to the linked
        data context in a HTTP link response header. The default value will be
        `""http://www.w3.org/ns/json-ld#context"`.
    context_type_or_func : str or callable, optional
        The value of the `type` attribute to be provided with the link to the linked
        data context in a HTTP link response header. The default value will be
        `""application/ld+json"`.
    """
    def decorator(api_function):
        @wraps(api_function)
        async def wrapper(request: Request, response: Response, *args, **kwargs):
            # Check for missing required headers
            if request.method in ("PUT", "DELETE"):
                missing_required_headers = []
                if etag_func and not request.headers.get("if-match"):
                    missing_required_headers.append("If-Match")
                if last_modified_func and not request.headers.get("if-unmodified-since"):
                    missing_required_headers.append("If-Unmodified-Since")
                if missing_required_headers:
                    response.status_code = 400
                    return {"missing-required-headers": missing_required_headers}

            # Compute values (if any) for the requested resource.
            def get_last_modified():
                if last_modified_func:
                    last_modified = last_modified_func(*args, **kwargs)
                    if last_modified:
                        return last_modified.replace(tzinfo=timezone.utc).timestamp()
                return None

            # ETAG & Last Modified Date generation
            etag = None
            last_modified = None
            if request.method not in ("POST", "OPTIONS"):
                if etag_func:
                    etag = etag_func(*args, **kwargs)
                    etag = etag + __version__ if etag else None
                etag = quote_etag(etag) if etag else None
                last_modified = get_last_modified()

            # Conditional Response Check
            cond_response = get_conditional_response(
                request,
                etag=etag,
                last_modified=last_modified
            )

            def add_expose_headers(response):
                """If the Link and/or Location header are provided on the response add the
                'Access-Control-Expose-Headers` header to expose them over CORS requests.
                """
                expose_headers = ""
                if "Link" in response.headers:
                    expose_headers = "Link"
                if "Location" in response.headers:
                    if expose_headers:
                        expose_headers = expose_headers + ", "
                    expose_headers = expose_headers + "Location"
                if expose_headers:
                    response.headers["Access-Control-Expose-Headers"] = expose_headers

            def schema_link(*args, **kwargs):
                return get_string_or_evaluate(schema_link_or_func, *args, **kwargs)

            def schema_link_header(*args, **kwargs):
                link = schema_link(*args, **kwargs)
                if link:
                    link_rel = (
                        get_string_or_evaluate(schema_rel_or_func, *args, **kwargs,)
                        or "describedBy"
                    )
                    link_type = (
                        get_string_or_evaluate(schema_type_or_func, *args, **kwargs,)
                        or "application/json"
                    )
                    return link_content(link, link_rel, link_type)
                return None

            def context_link(*args, **kwargs):
                return get_string_or_evaluate(context_link_or_func, *args, **kwargs)

            def context_link_header(*args, **kwargs):
                link = context_link(*args, **kwargs)
                if link:
                    link_rel = (
                        get_string_or_evaluate(context_rel_or_func, *args, **kwargs,)
                        or "http://www.w3.org/ns/json-ld#context"
                    )
                    link_type = (
                        get_string_or_evaluate(context_type_or_func, *args, **kwargs,)
                        or "application/ld+json"
                    )
                    return link_content(link, link_rel, link_type)
                return None

            if cond_response:
                return cond_response
            else:
                schema_link = schema_link_header(*args, **kwargs) or ""
                context_link = context_link_header(*args, **kwargs) or ""
                join_links = ", " if schema_link and context_link else ""
                combined_link_content = schema_link + join_links + context_link
                if etag:
                    response.headers["ETag"] = etag
                if last_modified:
                    last_modified = formatdate(last_modified, usegmt=True)
                    response.headers["Last-Modified"] = last_modified
                if combined_link_content:
                    response.headers["Link"] = combined_link_content

                add_expose_headers(response)
                if iscoroutinefunction(api_function):
                    return await api_function(request, response, *args, **kwargs)
                else:
                    return api_function(request, response, *args, **kwargs)

        return wrapper
    return decorator
