import pytest
from django.utils.functional import lazy
from starlette.datastructures import URL

from scrudful.utils import (
    EXPOSABLE_HEADERS,
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


@pytest.mark.parametrize("value", ["€", "aĀb", "\U0001f600"])
def test_link_content_rejects_non_latin1_params(value):
    with pytest.raises(ValueError, match="outside Latin-1"):
        link_content("u", value, "t")
    with pytest.raises(ValueError, match="outside Latin-1"):
        link_content("u", "r", value)


def test_link_content_allows_latin1_params():
    assert link_content("u", "café", "t") == '<u>; rel="café"; type="t"'


class _Stringy:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


def test_link_content_accepts_starlette_url():
    url = URL("http://x/a b")
    assert (
        link_content(url, "describedby", "application/json")
        == '<http://x/a%20b>; rel="describedby"; type="application/json"'
    )


@pytest.mark.parametrize(
    "url, rel, content_type, expected",
    [
        (_Stringy("http://x/<y>"), "r", "t", '<http://x/%3Cy%3E>; rel="r"; type="t"'),
        (5, "r", "t", '<5>; rel="r"; type="t"'),
        ("u", _Stringy('a"b'), _Stringy("t"), r'<u>; rel="a\"b"; type="t"'),
        (
            "u",
            lazy(lambda: "describedby", str)(),
            lazy(lambda: "application/json", str)(),
            '<u>; rel="describedby"; type="application/json"',
        ),
    ],
)
def test_link_content_stringifies_arguments(url, rel, content_type, expected):
    assert link_content(url, rel, content_type) == expected


def test_link_content_rejects_ctl_in_non_str_rel():
    with pytest.raises(ValueError):
        link_content("u", _Stringy("a\r\nb"), "t")


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
