"""``page`` and ``page_size``: DRF accepts exactly the integers ninja's pydantic
query model accepts (pydantic v2's lax ``int`` from a string)."""

import sys

import pytest
from django.utils import translation
from items.models import Item
from rest_framework.fields import IntegerField
from rest_framework.test import APIClient
from scrudful._adapter import parse_query_int

pytestmark = [
    pytest.mark.urls("tests.test_rest_framework_envelope"),
]

#: Strings covering pydantic's rules: whitespace (Unicode White_Space only),
#: signs, decimal zeros, underscores, leading zeros, non-ASCII digits, the
#: 4300-character limit, and the quirks of its leading-zero stripping.
SAMPLES = [
    *["", " ", "\t", "1", "01", "007", "00", "0", "-0", "+0", "+00", "-00"],
    *[" 1", "1 ", "\t1\n", "\xa01", "1　", " 1", "\x851", " 1"],
    *["\x1c1", "1\x1f", "﻿1", "​1", "᠎1", "\x001", "1 1"],
    *["+1", "-1", "++1", "--1", "+-1", "-+1", "1+", "1-", "+", "-", "."],
    *["1.0", "1.00", "+1.0", "-1.0", " 1.0 ", "0.0", "-00.000", "1.", ".0"],
    *["1.5", "1.01", "1.0.0", "1.0_0", "1._0", "1_.0", "1_0.0", "1.0e0"],
    *["1e3", "1E0", "0x10", "0o7", "0b1", "inf", "nan", "1,0", "1/2"],
    *["1_000", "+1_000", "-1_000", "1__0", "_1", "1_", "+_1", "-_1", "0_0"],
    *["0_8", "0__8", "0___8", "0_", "0__", "0__29_0", "00__-_1", "0_-5"],
    *["0-51", "00-51", "0+51", "0-0", "0-00", "0-01", "0-10", "+0-5", "-0-5"],
    *["0-_5", "0-5.0", "0-0.0", "0-00.0", "-0001", "-0_01", "0_-01", "+0-01"],
    *["٣", "３", "¹", "1٣", "٣.٠", "１", "e", "x", "1a"],
    *["9223372036854775807", "9223372036854775808", "-9223372036854775809"],
    *["9223372036854775808.0", "1_9223372036854775808"],
    "9" * 4300,
    "9" * 4301,
    "-" + "9" * 4299,
    "-" + "9" * 4300,
    "+" + "9" * 4300,
    " " + "9" * 4300,
    "9" * 4300 + ".0",
    "9" * 4301 + ".0",
    "9" * 4299 + "_9",
    "9" * 4300 + "_9",
    "9_" * 2150 + "9",
    "0" * 5000,
    "0" * 5000 + "1",
    "-" + "0" * 5000 + "1",
    "0-" + "9" * 4299,
    "0-" + "9" * 4300,
]


def short(value):
    return value if len(value) < 30 else f"{value[:8]}...({len(value)})"


@pytest.mark.parametrize("raw", SAMPLES, ids=[repr(short(s)) for s in SAMPLES])
def test_parse_query_int_agrees_with_pydantic(raw):
    pydantic = pytest.importorskip("pydantic")
    try:
        expected = pydantic.TypeAdapter(int).validate_python(raw)
    except pydantic.ValidationError:
        expected = None
    assert parse_query_int(raw) == expected


@pytest.fixture
def items(db):
    return [Item.objects.create(name=name) for name in ("a", "b", "c")]


@pytest.mark.parametrize(
    "query",
    [
        {"page": "1.0"},
        {"page": "٣"},
        {"page": "３"},
        {"page": " 2 "},
        {"page": "0.0"},
        {"page": "-1.0"},
        {"page": "1_0"},
        {"page": "0-1"},
        {"page": "9" * 30},
        {"page": "9" * 4301},
        {"page_size": "1.00"},
        {"page_size": "2.5"},
        {"page_size": "0_2"},
        {"page_size": "9" * 30},
        {"page": "x", "page_size": "1.0"},
    ],
    ids=lambda query: repr({k: short(v) for k, v in query.items()}),
)
def test_drf_and_ninja_agree_on_page_parameters(items, query):
    client = APIClient()
    drf = client.get("/drf/items/", query)
    ninja = client.get("/ninja/items", query)
    # The same outcome: DRF's 400 is ninja's 422 (its validation error).
    assert {400: 422}.get(drf.status_code, drf.status_code) == ninja.status_code
    if drf.status_code == 200:
        assert drf.json()["content"] == ninja.json()["content"]
        assert drf.json()["count"] == ninja.json()["count"]


@pytest.mark.parametrize(
    "value, message, code",
    [
        ("٣", "A valid integer is required.", "invalid"),
        ("1.5", "A valid integer is required.", "invalid"),
        ("9" * 4301, "A valid integer is required.", "invalid"),
        ("0.0", "Ensure this value is greater than or equal to 1.", "min_value"),
        ("-1.0", "Ensure this value is greater than or equal to 1.", "min_value"),
        ("0-1", "Ensure this value is greater than or equal to 1.", "min_value"),
    ],
)
def test_drf_page_errors_keep_messages_and_codes(items, value, message, code):
    from rest_framework.exceptions import ValidationError
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from tests.test_rest_framework_envelope import SmallPagination, SmallViewSet

    request = Request(APIRequestFactory().get("/drf/small/", {"page": value}))
    with pytest.raises(ValidationError) as caught:
        SmallPagination().paginate_queryset(Item.objects.all(), request, SmallViewSet())
    assert caught.value.detail == {"page": [message]}
    assert caught.value.get_codes() == {"page": [code]}


def test_drf_accepts_pydantic_forms(items):
    client = APIClient()
    body = client.get("/drf/items/", {"page": "2.0", "page_size": " 2 "}).json()
    assert [entry["content"]["name"] for entry in body["content"]] == ["c"]


@pytest.fixture
def lowered_int_digits():
    """``int()``'s digit limit lowered below pydantic's 4300, as an app may."""
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    yield
    sys.set_int_max_str_digits(limit)


@pytest.mark.parametrize("raw", ["9" * 1000, "0" + "9" * 1000, "9" * 1000 + ".0"])
def test_parse_query_int_past_the_interpreter_limit(lowered_int_digits, raw):
    assert parse_query_int(raw) is None


def test_drf_page_past_the_interpreter_limit_is_a_400(items, lowered_int_digits):
    response = APIClient().get("/drf/items/", {"page": "9" * 1000})
    assert response.status_code == 400
    assert response.json()["page"] == ["A valid integer is required."]


@pytest.mark.parametrize("value, code", [("x", "invalid"), ("0", "min_value")])
def test_drf_page_errors_are_translated_as_drf_s(items, value, code):
    from rest_framework.exceptions import ValidationError
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from tests.test_rest_framework_envelope import SmallPagination, SmallViewSet

    with translation.override("fr"):
        request = Request(APIRequestFactory().get("/drf/small/", {"page": value}))
        with pytest.raises(ValidationError) as caught:
            SmallPagination().paginate_queryset(
                Item.objects.all(), request, SmallViewSet()
            )
        expected = IntegerField.default_error_messages[code].format(min_value=1)
    assert caught.value.detail == {"page": [expected]}
    assert "valid integer" not in expected and "Ensure" not in expected
