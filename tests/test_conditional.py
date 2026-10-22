import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from scrudful.conditional import (
    PRECONDITION_REQUIRED_METHODS,
    SAFE_METHODS,
    ConditionalResult,
    evaluate_preconditions,
    format_http_date,
    http_timestamp,
    parse_etag_list,
    parse_http_date,
    quote_etag,
    validator_headers,
)

# RFC 9110 §5.6.7 example date.
TS = 784111777
IMF = "Sun, 06 Nov 1994 08:49:37 GMT"
RFC850 = "Sunday, 06-Nov-94 08:49:37 GMT"
ASCTIME = "Sun Nov  6 08:49:37 1994"

ETAG = '"v1"'
OTHER = '"v2"'

PRECONDITION_FAILED = {"detail": "Precondition failed."}


def test_method_sets():
    assert PRECONDITION_REQUIRED_METHODS == frozenset({"PUT", "PATCH", "DELETE"})
    assert SAFE_METHODS == frozenset({"GET", "HEAD"})


# --- quote_etag -------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("abc", '"abc"'),
        ('"abc"', '"abc"'),
        ('W/"abc"', 'W/"abc"'),
        ("", '""'),
        ('""', '""'),
        (42, '"42"'),
        (0, '"0"'),
        (
            uuid.UUID("12345678-1234-5678-1234-567812345678"),
            '"12345678-1234-5678-1234-567812345678"',
        ),
        ("a,b", '"a,b"'),
        ("W/abc", '"W/abc"'),
        ("café", '"café"'),  # obs-text
        ("!#~", '"!#~"'),
    ],
)
def test_quote_etag(value, expected):
    assert quote_etag(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "a b",
        'a"b',
        '"abc',
        'abc"',
        '"a"b"',
        'W/"a b"',
        "a\tb",
        "a\r\nb",
        "a\x00b",
        "a\x7fb",
        "€",  # outside obs-text (%x80-FF)
    ],
)
def test_quote_etag_invalid(value):
    with pytest.raises(ValueError):
        quote_etag(value)


# --- parse_etag_list --------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, []),
        ("", []),
        ("   ", []),
        ("*", ["*"]),
        ("  *  ", ["*"]),
        ('"a"', ['"a"']),
        ('"a", "b"', ['"a"', '"b"']),
        ('"a","b"', ['"a"', '"b"']),
        ('W/"a", "b"', ['W/"a"', '"b"']),
        ('"a,b"', ['"a,b"']),
        ('"a,b", W/"c,d"', ['"a,b"', 'W/"c,d"']),
        (' \t"a" ,\t "b" ', ['"a"', '"b"']),
        ('"a", , "b",', ['"a"', '"b"']),  # empty list elements are ignored
        ('""', ['""']),
    ],
)
def test_parse_etag_list(value, expected):
    assert parse_etag_list(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        '"a", abc',
        '"a',
        'a"',
        '"a" "b"',
        'w/"a"',
        'W/ "a"',
        '"a b"',
        '*, "a"',
        '"a", *',
        "**",
        '"a"x',
        "garbage!!",
        '"€"',
    ],
)
def test_parse_etag_list_malformed(value):
    assert parse_etag_list(value) is None


# --- http_timestamp ---------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (datetime(1994, 11, 6, 8, 49, 37), TS),
        (datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc), TS),
        (
            datetime(1994, 11, 6, 14, 19, 37, tzinfo=timezone(timedelta(hours=5.5))),
            TS,
        ),
        (
            datetime(1994, 11, 6, 3, 49, 37, tzinfo=ZoneInfo("America/New_York")),
            TS,
        ),
        (datetime(1994, 11, 6, 8, 49, 37, 999999), TS),
        (
            datetime(1994, 11, 6, 8, 49, 37, 999999, tzinfo=timezone.utc),
            TS,
        ),
        (datetime(1970, 1, 1), 0),
        (datetime(1970, 1, 1, tzinfo=timezone.utc), 0),
        (0, 0),
        (0.0, 0),
        (TS, TS),
        (TS + 0.999, TS),
        (-0.5, -1),
        (datetime(1969, 12, 31, 23, 59, 59, 500000), -1),
    ],
)
def test_http_timestamp(value, expected):
    result = http_timestamp(value)
    assert result == expected
    if expected is not None:
        assert type(result) is int


def test_http_timestamp_epoch_is_not_none():
    assert http_timestamp(datetime(1970, 1, 1)) is not None


@pytest.mark.parametrize("value", ["1994-11-06", True, object()])
def test_http_timestamp_rejects_other_types(value):
    with pytest.raises(TypeError):
        http_timestamp(value)


# --- format_http_date / parse_http_date -------------------------------------


def test_format_http_date():
    assert format_http_date(TS) == IMF
    assert format_http_date(0) == "Thu, 01 Jan 1970 00:00:00 GMT"


@pytest.mark.parametrize("value", [IMF, RFC850, ASCTIME])
def test_parse_http_date_formats(value):
    assert parse_http_date(value) == TS


@pytest.mark.parametrize("ts", [0, TS, 1700000000, 4102444799])
def test_http_date_round_trip(ts):
    assert parse_http_date(format_http_date(ts)) == ts


def test_parse_http_date_asctime_two_digit_day():
    assert parse_http_date("Sun Nov 16 08:49:37 1994") == TS + 10 * 86400


@pytest.mark.parametrize(
    "now, value, year",
    [
        ("2026-06-01", "Sunday, 06-Nov-94 08:49:37 GMT", 1994),
        ("2026-06-01", "Thursday, 06-Nov-76 08:49:37 GMT", 2076),  # exactly 50
        ("2026-06-01", "Saturday, 06-Nov-77 08:49:37 GMT", 1977),  # more than 50
        ("2026-06-01", "Friday, 06-Nov-26 08:49:37 GMT", 2026),
        ("2099-06-01", "Tuesday, 06-Nov-01 08:49:37 GMT", 2001),
    ],
)
def test_parse_http_date_rfc850_two_digit_year(now, value, year):
    with freeze_time(now):
        result = parse_http_date(value)
    expected = datetime(year, 11, 6, 8, 49, 37, tzinfo=timezone.utc)
    assert result == int(expected.timestamp())


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        "1994-11-06T08:49:37Z",
        "Sun, 06 Nov 1994 08:49:37 UTC",
        "Sun, 06 Nov 1994 08:49:37 GMT\n",
        "Sun, 06 Foo 1994 08:49:37 GMT",
        "Sun, 32 Nov 1994 08:49:37 GMT",
        "Sun, 06 Nov 1994 25:49:37 GMT",
        "Sun, 06 Nov 1994 08:49:37",
        "Sun, ١٦ Nov 1994 08:49:37 GMT",  # non-ASCII digits
        "Sunday, 06-Nov-1994 08:49:37 GMT",
    ],
)
def test_parse_http_date_invalid(value):
    assert parse_http_date(value) is None


# --- validator_headers ------------------------------------------------------


@pytest.mark.parametrize(
    "etag, last_modified, expected",
    [
        (None, None, {}),
        (ETAG, None, {"ETag": ETAG}),
        (None, TS, {"Last-Modified": IMF}),
        (None, 0, {"Last-Modified": "Thu, 01 Jan 1970 00:00:00 GMT"}),
        (ETAG, TS, {"ETag": ETAG, "Last-Modified": IMF}),
    ],
)
def test_validator_headers(etag, last_modified, expected):
    assert validator_headers(etag, last_modified) == expected


# --- evaluate_preconditions -------------------------------------------------


def evaluate(
    method,
    headers=None,
    *,
    etag=ETAG,
    last_modified=TS,
    require_precondition=False,
):
    return evaluate_preconditions(
        method,
        headers or {},
        etag=etag,
        last_modified=last_modified,
        require_precondition=require_precondition,
    )


def not_modified(etag=ETAG, last_modified=TS):
    return ConditionalResult(
        status=304,
        headers=validator_headers(etag, last_modified),
        body=None,
    )


def precondition_failed():
    return ConditionalResult(status=412, headers={}, body=PRECONDITION_FAILED)


def precondition_required(*accepted):
    return ConditionalResult(
        status=428,
        headers={},
        body={
            "detail": "This request must be conditional.",
            "accepted-precondition-headers": list(accepted),
        },
    )


ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@pytest.mark.parametrize("method", ALL_METHODS)
def test_unconditional_request_proceeds(method):
    assert evaluate(method) is None


# Step 1: malformed entity-tag lists -> 400.


@pytest.mark.parametrize("method", ALL_METHODS)
@pytest.mark.parametrize(
    "header, name",
    [("if-match", "If-Match"), ("if-none-match", "If-None-Match")],
)
@pytest.mark.parametrize("value", ["abc", '"a", b', '"unterminated'])
def test_malformed_etag_header_is_400(method, header, name, value):
    assert evaluate(method, {header: value}) == ConditionalResult(
        status=400, headers={}, body={"detail": f"Malformed {name} header."}
    )


def test_malformed_if_match_reported_before_if_none_match():
    result = evaluate("GET", {"if-match": "a", "if-none-match": "b"})
    assert result.body == {"detail": "Malformed If-Match header."}


def test_malformed_takes_precedence_over_428():
    result = evaluate("PUT", {"if-match": "abc"}, require_precondition=True)
    assert result.status == 400


@pytest.mark.parametrize(
    "header", ["if-match", "if-none-match", "if-modified-since", "if-unmodified-since"]
)
@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_empty_header_values_are_absent(method, header):
    assert evaluate(method, {header: ""}) is None


def test_empty_if_match_does_not_satisfy_428():
    assert evaluate(
        "PUT", {"if-match": ""}, require_precondition=True
    ) == precondition_required("If-Match", "If-Unmodified-Since")


# Step 2: no current representation.


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"if-match": "*"},
        {"if-match": ETAG},
        {"if-none-match": "*"},
        {"if-none-match": ETAG},
        {"if-unmodified-since": IMF},
        {"if-modified-since": IMF},
    ],
)
def test_missing_resource_proceeds_except_put(method, headers):
    assert (
        evaluate(
            method,
            headers,
            etag=None,
            last_modified=None,
            require_precondition=True,
        )
        is None
    )


@pytest.mark.parametrize("require_precondition", [True, False])
def test_put_create_with_if_none_match_star_proceeds(require_precondition):
    assert (
        evaluate(
            "PUT",
            {"if-none-match": "*"},
            etag=None,
            last_modified=None,
            require_precondition=require_precondition,
        )
        is None
    )


def test_put_create_without_preconditions_proceeds():
    assert (
        evaluate("PUT", etag=None, last_modified=None, require_precondition=True)
        is None
    )


@pytest.mark.parametrize("value", ["*", ETAG, f"{OTHER}, {ETAG}"])
def test_put_with_if_match_on_missing_is_412(value):
    assert (
        evaluate("PUT", {"if-match": value}, etag=None, last_modified=None)
        == precondition_failed()
    )


def test_put_with_if_unmodified_since_on_missing_is_ignored():
    assert (
        evaluate("PUT", {"if-unmodified-since": IMF}, etag=None, last_modified=None)
        is None
    )


# Step 3: 428 Precondition Required.


@pytest.mark.parametrize("method", sorted(PRECONDITION_REQUIRED_METHODS))
@pytest.mark.parametrize(
    "etag, last_modified, accepted",
    [
        (ETAG, TS, ["If-Match", "If-Unmodified-Since"]),
        (ETAG, None, ["If-Match"]),
        (None, TS, ["If-Unmodified-Since"]),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"if-unmodified-since": "not a date"},
        {"if-none-match": OTHER},
        {"if-modified-since": IMF},
    ],
)
def test_precondition_required(method, etag, last_modified, accepted, headers):
    assert evaluate(
        method,
        headers,
        etag=etag,
        last_modified=last_modified,
        require_precondition=True,
    ) == precondition_required(*accepted)


@pytest.mark.parametrize("method", sorted(PRECONDITION_REQUIRED_METHODS))
def test_only_if_unmodified_since_without_last_modified_is_428(method):
    assert evaluate(
        method,
        {"if-unmodified-since": IMF},
        last_modified=None,
        require_precondition=True,
    ) == precondition_required("If-Match")


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "OPTIONS"])
def test_precondition_not_required_for_other_methods(method):
    assert evaluate(method, require_precondition=True) is None


@pytest.mark.parametrize("method", sorted(PRECONDITION_REQUIRED_METHODS))
def test_precondition_not_required_when_disabled(method):
    assert evaluate(method, require_precondition=False) is None


@pytest.mark.parametrize("method", sorted(PRECONDITION_REQUIRED_METHODS))
@pytest.mark.parametrize(
    "headers",
    [
        {"if-match": ETAG},
        {"if-match": "*"},
        {"if-unmodified-since": IMF},
        {"if-match": ETAG, "if-unmodified-since": IMF},
        {"if-match": ETAG, "if-unmodified-since": "not a date"},
    ],
)
def test_either_validator_satisfies_requirement(method, headers):
    assert evaluate(method, headers, require_precondition=True) is None


@pytest.mark.parametrize("method", sorted(PRECONDITION_REQUIRED_METHODS))
@pytest.mark.parametrize("etag, last_modified", [(ETAG, TS), (ETAG, None), (None, TS)])
def test_if_none_match_star_satisfies_requirement_and_fails(
    method, etag, last_modified
):
    """``If-None-Match: *`` is the create-only precondition (RFC 9110
    §13.1.2): on an existing resource it is 412, not 428."""
    assert (
        evaluate(
            method,
            {"if-none-match": "*"},
            etag=etag,
            last_modified=last_modified,
            require_precondition=True,
        )
        == precondition_failed()
    )


@pytest.mark.parametrize("method", sorted(PRECONDITION_REQUIRED_METHODS))
def test_satisfied_requirement_still_evaluated(method):
    assert (
        evaluate(method, {"if-match": OTHER}, require_precondition=True)
        == precondition_failed()
    )
    assert (
        evaluate(
            method,
            {"if-unmodified-since": format_http_date(TS - 1)},
            require_precondition=True,
        )
        == precondition_failed()
    )


# Step 4a: If-Match.


@pytest.mark.parametrize("method", ALL_METHODS)
@pytest.mark.parametrize(
    "etag, last_modified, value, passes",
    [
        (ETAG, TS, ETAG, True),
        (ETAG, TS, f"{OTHER}, {ETAG}", True),
        (ETAG, TS, OTHER, False),
        (ETAG, TS, f"W/{ETAG}", False),  # weak tags never match strongly
        (f"W/{ETAG}", TS, f"W/{ETAG}", False),
        (f"W/{ETAG}", TS, ETAG, False),
        (ETAG, TS, "*", True),
        (f"W/{ETAG}", TS, "*", True),
        (None, TS, "*", True),  # exists via last_modified
        (None, TS, ETAG, False),
        ('"a,b"', TS, '"a,b"', True),
        ('"a,b"', TS, '"a,zzz"', False),
    ],
)
def test_if_match(method, etag, last_modified, value, passes):
    result = evaluate(
        method, {"if-match": value}, etag=etag, last_modified=last_modified
    )
    assert result == (None if passes else precondition_failed())


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_if_match_overrides_if_unmodified_since(method):
    stale = format_http_date(TS - 1)
    assert evaluate(method, {"if-match": ETAG, "if-unmodified-since": stale}) is None
    assert evaluate(method, {"if-match": OTHER, "if-unmodified-since": IMF}) == (
        precondition_failed()
    )


# Step 4b: If-Unmodified-Since.


@pytest.mark.parametrize("method", ALL_METHODS)
@pytest.mark.parametrize(
    "value, passes",
    [
        (IMF, True),  # exact formatted Last-Modified round trip
        (RFC850, True),
        (ASCTIME, True),
        (format_http_date(TS + 1), True),
        (format_http_date(TS - 1), False),
        ("not a date", True),  # invalid -> ignored
    ],
)
def test_if_unmodified_since(method, value, passes):
    result = evaluate(method, {"if-unmodified-since": value})
    assert result == (None if passes else precondition_failed())


def test_if_unmodified_since_round_trip_with_formatted_last_modified():
    last_modified = http_timestamp(datetime(2024, 5, 1, 12, 0, 0, 123456))
    header = validator_headers(None, last_modified)["Last-Modified"]
    assert (
        evaluate(
            "PUT",
            {"if-unmodified-since": header},
            last_modified=last_modified,
            require_precondition=True,
        )
        is None
    )


@pytest.mark.parametrize("method", ALL_METHODS)
def test_if_unmodified_since_ignored_without_last_modified(method):
    assert (
        evaluate(
            method,
            {"if-unmodified-since": format_http_date(0)},
            last_modified=None,
        )
        is None
    )


def test_if_unmodified_since_epoch_zero_last_modified():
    assert (
        evaluate("PUT", {"if-unmodified-since": format_http_date(0)}, last_modified=0)
        is None
    )
    assert evaluate(
        "PUT", {"if-unmodified-since": format_http_date(-1)}, last_modified=0
    ) == (precondition_failed())


# Step 4c: If-None-Match.


@pytest.mark.parametrize(
    "etag, last_modified, value, matches",
    [
        (ETAG, TS, ETAG, True),
        (ETAG, TS, f"W/{ETAG}", True),  # weak comparison
        (f"W/{ETAG}", TS, ETAG, True),
        (f"W/{ETAG}", TS, f"W/{ETAG}", True),
        (ETAG, TS, f"{OTHER}, {ETAG}", True),
        (ETAG, TS, OTHER, False),
        (ETAG, TS, "*", True),
        (None, TS, "*", True),
        (None, TS, ETAG, False),
        ('"a,b"', TS, '"a,b"', True),
        ('"a,b"', TS, '"a", "b"', False),
    ],
)
@pytest.mark.parametrize("method", ALL_METHODS)
def test_if_none_match(method, etag, last_modified, value, matches):
    result = evaluate(
        method, {"if-none-match": value}, etag=etag, last_modified=last_modified
    )
    if not matches:
        assert result is None
    elif method in SAFE_METHODS:
        assert result == not_modified(etag, last_modified)
    else:
        assert result == precondition_failed()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_if_none_match_overrides_if_modified_since(method):
    # If-None-Match does not match, so If-Modified-Since must be ignored.
    assert evaluate(method, {"if-none-match": OTHER, "if-modified-since": IMF}) is None


def test_if_match_failure_precedes_if_none_match():
    assert (
        evaluate("GET", {"if-match": OTHER, "if-none-match": ETAG})
        == precondition_failed()
    )


def test_if_unmodified_since_failure_precedes_if_none_match():
    stale = format_http_date(TS - 1)
    assert (
        evaluate("GET", {"if-unmodified-since": stale, "if-none-match": ETAG})
        == precondition_failed()
    )


# Step 4d: If-Modified-Since.


@pytest.mark.parametrize(
    "value, not_modified_expected",
    [
        (IMF, True),  # exact formatted Last-Modified round trip
        (RFC850, True),
        (ASCTIME, True),
        (format_http_date(TS + 1), True),
        (format_http_date(TS - 1), False),
        ("not a date", False),
    ],
)
@pytest.mark.parametrize("method", ALL_METHODS)
def test_if_modified_since(method, value, not_modified_expected):
    result = evaluate(method, {"if-modified-since": value})
    if not_modified_expected and method in SAFE_METHODS:
        assert result == not_modified()
    else:
        assert result is None


def test_if_modified_since_round_trip_with_formatted_last_modified():
    aware = datetime(2024, 5, 1, 8, 0, 0, 654321, tzinfo=ZoneInfo("America/New_York"))
    last_modified = http_timestamp(aware)
    header = validator_headers(None, last_modified)["Last-Modified"]
    assert evaluate(
        "GET", {"if-modified-since": header}, last_modified=last_modified
    ) == not_modified(ETAG, last_modified)


def test_if_modified_since_ignored_without_last_modified():
    assert evaluate("GET", {"if-modified-since": IMF}, last_modified=None) is None


def test_if_modified_since_epoch_zero_last_modified():
    assert evaluate(
        "GET", {"if-modified-since": format_http_date(0)}, etag=None, last_modified=0
    ) == not_modified(None, 0)


# 304 carries validators (RFC 9110 §15.4.5).


@pytest.mark.parametrize(
    "etag, last_modified, headers",
    [
        (ETAG, TS, {"if-none-match": ETAG}),
        (ETAG, None, {"if-none-match": ETAG}),
        (None, TS, {"if-modified-since": IMF}),
        (ETAG, TS, {"if-modified-since": IMF}),
    ],
)
def test_not_modified_carries_validator_headers(etag, last_modified, headers):
    result = evaluate("GET", headers, etag=etag, last_modified=last_modified)
    assert result.status == 304
    assert result.body is None
    assert result.headers == validator_headers(etag, last_modified)


def test_result_is_frozen():
    result = evaluate("PUT", {"if-match": OTHER})
    with pytest.raises(AttributeError):
        result.status = 200
