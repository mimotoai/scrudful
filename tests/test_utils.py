import pytest

from scrudful.utils import (
    EXPOSABLE_HEADERS,
    get_string_or_evaluate,
    join_links,
    link_content,
    merge_expose_headers,
)

# --- link_content -----------------------------------------------------------


@pytest.mark.parametrize(
    "url, rel, content_type, expected",
    [
        # Plain values are byte-identical to the historical output.
        (
            "http://example.com/schemas/widget",
            "describedBy",
            "application/json",
            '<http://example.com/schemas/widget>; rel="describedBy";'
            ' type="application/json"',
        ),
        (
            "http://example.com/contexts/widget",
            "http://www.w3.org/ns/json-ld#context",
            "application/ld+json",
            "<http://example.com/contexts/widget>;"
            ' rel="http://www.w3.org/ns/json-ld#context";'
            ' type="application/ld+json"',
        ),
        (
            "/relative/path?a=1&b=2#frag",
            "next",
            "text/html",
            '</relative/path?a=1&b=2#frag>; rel="next"; type="text/html"',
        ),
        # All RFC 3986 reserved/unreserved characters and '%' are kept.
        (
            "http://u@h:1/a-._~:/?#[]@!$&'()*+,;=%20",
            "r",
            "t",
            '<http://u@h:1/a-._~:/?#[]@!$&\'()*+,;=%20>; rel="r"; type="t"',
        ),
    ],
)
def test_link_content(url, rel, content_type, expected):
    assert link_content(url, rel, content_type) == expected


@pytest.mark.parametrize(
    "url, encoded",
    [
        (
            'http://h/a>; rel="next", <http://evil',
            "http://h/a%3E;%20rel=%22next%22,%20%3Chttp://evil",
        ),
        ("http://h/<x>", "http://h/%3Cx%3E"),
        ('http://h/"q"', "http://h/%22q%22"),
        ("http://h/a b", "http://h/a%20b"),
        ("http://h/café", "http://h/caf%C3%A9"),
        ("http://h/€", "http://h/%E2%82%AC"),
        ("http://h/a\r\nSet-Cookie: x", "http://h/a%0D%0ASet-Cookie:%20x"),
        ("http://h/\x00\x7f", "http://h/%00%7F"),
        ("http://h/a\\b{c}|^`", "http://h/a%5Cb%7Bc%7D%7C%5E%60"),
    ],
)
def test_link_content_encodes_url(url, encoded):
    assert link_content(url, "r", "t") == f'<{encoded}>; rel="r"; type="t"'


@pytest.mark.parametrize(
    "rel, quoted",
    [
        ('a"b', r'"a\"b"'),
        ("a\\b", r'"a\\b"'),
        ('x", <http://evil>; rel="next', r'"x\", <http://evil>; rel=\"next"'),
        ("a b", '"a b"'),
    ],
)
def test_link_content_escapes_params(rel, quoted):
    assert link_content("u", rel, rel) == f"<u>; rel={quoted}; type={quoted}"


@pytest.mark.parametrize("value", ["a\r\nb", "a\nb", "a\rb", "a\x00b", "a\x7fb"])
def test_link_content_rejects_ctl_in_rel(value):
    with pytest.raises(ValueError):
        link_content("u", value, "t")


@pytest.mark.parametrize("value", ["a\r\nb", "a\nb", "a\rb", "a\x00b", "a\x7fb"])
def test_link_content_rejects_ctl_in_type(value):
    with pytest.raises(ValueError):
        link_content("u", "r", value)


@pytest.mark.parametrize("content_type", [None, ""])
def test_link_content_without_type(content_type):
    assert link_content("u", "r", content_type) == '<u>; rel="r"'


def test_link_content_type_defaults_to_none():
    assert link_content("u", "r") == '<u>; rel="r"'


# --- join_links -------------------------------------------------------------


@pytest.mark.parametrize(
    "values, expected",
    [
        ((), None),
        ((None,), None),
        (("", None), None),
        (("<a>",), "<a>"),
        (("<a>", None, "<b>"), "<a>, <b>"),
        (("", "<b>"), "<b>"),
        (("<a>", "<b>", "<c>"), "<a>, <b>, <c>"),
    ],
)
def test_join_links(values, expected):
    assert join_links(*values) == expected


# --- merge_expose_headers ---------------------------------------------------


def test_exposable_headers():
    assert EXPOSABLE_HEADERS == ("ETag", "Link", "Location")


@pytest.mark.parametrize(
    "existing, names, expected",
    [
        (None, [], None),
        ("", [], None),
        (" , ", [], None),
        (None, ["Link"], "Link"),
        (None, ["ETag", "Link", "Location"], "ETag, Link, Location"),
        ("X-Request-Id", ["Link"], "X-Request-Id, Link"),
        ("link", ["Link", "ETag"], "link, ETag"),
        ("X-A,  x-b ,", ["X-B", "ETag", "etag"], "X-A, x-b, ETag"),
        (None, ["Link", "LINK"], "Link"),
        ("ETag, ETAG", [], "ETag"),
        ("X-A", iter(["Location"]), "X-A, Location"),
    ],
)
def test_merge_expose_headers(existing, names, expected):
    assert merge_expose_headers(existing, names) == expected


# --- get_string_or_evaluate -------------------------------------------------


def test_get_string_or_evaluate():
    assert get_string_or_evaluate(None) is None
    assert get_string_or_evaluate("") is None
    assert get_string_or_evaluate("x") == "x"
    assert get_string_or_evaluate(lambda *a, **k: (a, k), 1, b=2) == ((1,), {"b": 2})
