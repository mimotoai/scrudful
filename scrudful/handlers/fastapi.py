import calendar
import datetime
from fastapi import Response
import re

ETAG_MATCH = re.compile(r'''
    \A(      # start of string and capture group
    (?:W/)?  # optional weak indicator
    "        # opening quote
    [^"]*    # any sequence of non-quote characters
    "        # end quote
    )\Z      # end of string and capture group
''', re.X)

# Useful variables for parsing the dates
MONTHS = 'jan feb mar apr may jun jul aug sep oct nov dec'.split()
__D = r'(?P<day>\d{2})'
__D2 = r'(?P<day>[ \d]\d)'
__M = r'(?P<mon>\w{3})'
__Y = r'(?P<year>\d{4})'
__Y2 = r'(?P<year>\d{2})'
__T = r'(?P<hour>\d{2}):(?P<min>\d{2}):(?P<sec>\d{2})'
RFC1123_DATE = re.compile(r'^\w{3}, %s %s %s %s GMT$' % (__D, __M, __Y, __T))
RFC850_DATE = re.compile(r'^\w{6,9}, %s-%s-%s %s GMT$' % (__D, __M, __Y2, __T))
ASCTIME_DATE = re.compile(r'^\w{3} %s %s %s %s$' % (__M, __D2, __T, __Y))


def quote_etag(etag_str):
    """
    If the provided string is already a quoted ETag, return it. Otherwise, wrap
    the string in quotes, making it a strong ETag.
    """
    if ETAG_MATCH.match(etag_str):
        return etag_str
    else:
        return '"%s"' % etag_str


def parse_etags(etag_str):
    """
    Parse a string of ETags given in an If-None-Match or If-Match header as
    defined by RFC 7232. Return a list of quoted ETags, or ['*'] if all ETags
    should be matched.
    """
    if etag_str.strip() == '*':
        return ['*']
    else:
        # Parse each ETag individually, and return any that are valid.
        etag_matches = (ETAG_MATCH.match(etag.strip()) for etag in etag_str.split(','))
        return [match[1] for match in etag_matches if match]


def _precondition_failed(request):
    response = Response(status_code=412)
    # TODO: log the failed response
    return response


def _not_modified(request, response=None):
    new_response = Response(status_code=304)
    if response:
        # Preserve the headers required by Section 4.1 of RFC 7232, as well as
        # Last-Modified.
        for header in ('Cache-Control', 'Content-Location', 'Date', 'ETag', 'Expires', 'Last-Modified', 'Vary'):
            if header in response:
                new_response.headers[header] = response.headers[header]

        # Preserve cookies as per the cookie specification: "If a proxy server
        # receives a response which contains a Set-cookie header, it should
        # propagate the Set-cookie header to the client, regardless of whether
        # the response was 304 (Not Modified) or 200 (OK).
        # https://curl.haxx.se/rfc/cookie_spec.html
        new_response.cookies = response.cookies
    return new_response


def parse_http_date(date):
    """
    Parse a date format as specified by HTTP RFC7231 section 7.1.1.1.
    The three formats allowed by the RFC are accepted, even if only the first
    one is still in widespread use.
    Return an integer expressed in seconds since the epoch, in UTC.
    """
    # email.utils.parsedate() does the job for RFC1123 dates; unfortunately
    # RFC7231 makes it mandatory to support RFC850 dates too. So we roll
    # our own RFC-compliant parsing.
    for regex in RFC1123_DATE, RFC850_DATE, ASCTIME_DATE:
        m = regex.match(date)
        if m is not None:
            break
    else:
        raise ValueError("%r is not in a valid HTTP date format" % date)
    try:
        year = int(m['year'])
        if year < 100:
            current_year = datetime.datetime.utcnow().year
            current_century = current_year - (current_year % 100)
            if year - (current_year % 100) > 50:
                # year that appears to be more than 50 years in the future are
                # interpreted as representing the past.
                year += current_century - 100
            else:
                year += current_century
        month = MONTHS.index(m['mon'].lower()) + 1
        day = int(m['day'])
        hour = int(m['hour'])
        min = int(m['min'])
        sec = int(m['sec'])
        result = datetime.datetime(year, month, day, hour, min, sec)
        return calendar.timegm(result.utctimetuple())
    except Exception as exc:
        raise ValueError("%r is not a valid date" % date) from exc


def parse_http_date_safe(date):
    """
    Same as parse_http_date, but return None if the input is invalid.
    """
    try:
        return parse_http_date(date)
    except Exception:
        pass


def get_conditional_response(request, etag=None, last_modified=None, response=None):
    # Only return conditional responses on successful requests.
    if response and not (200 <= response.status_code < 300):
        return response

    # Get HTTP request headers.
    if_match_etags = parse_etags(request.headers.get('if-match', ''))
    if_unmodified_since = request.headers.get('if-unmodified-since')
    if_unmodified_since = if_unmodified_since and parse_http_date_safe(
        if_unmodified_since)
    if_none_match_etags = parse_etags(request.headers.get('if-none-match', ''))
    if_modified_since = request.headers.get('if-modified-since')
    if_modified_since = if_modified_since and parse_http_date_safe(if_modified_since)

    # Step 1 of section 6 of RFC 7232: Test the If-Match precondition.
    if if_match_etags and not _if_match_passes(etag, if_match_etags):
        return _precondition_failed(request)

    # Step 2: Test the If-Unmodified-Since precondition.
    if (not if_match_etags and if_unmodified_since and
            not _if_unmodified_since_passes(last_modified, if_unmodified_since)):
        return _precondition_failed(request)

    # Step 3: Test the If-None-Match precondition.
    if if_none_match_etags and not _if_none_match_passes(etag, if_none_match_etags):
        if request.method in ('GET', 'HEAD'):
            return _not_modified(request, response)
        else:
            return _precondition_failed(request)

    # Step 4: Test the If-Modified-Since precondition.
    if (
        not if_none_match_etags and
        if_modified_since and
        not _if_modified_since_passes(last_modified, if_modified_since) and
        request.method in ('GET', 'HEAD')
    ):
        return _not_modified(request, response)

    # Step 5: Test the If-Range precondition (not supported).
    # Step 6: Return original response since there isn't a conditional response.
    return response


def _if_match_passes(target_etag, etags):
    """
    Test the If-Match comparison as defined in section 3.1 of RFC 7232.
    """
    if not target_etag:
        # If there isn't an ETag, then there can't be a match.
        return False
    elif etags == ['*']:
        # The existence of an ETag means that there is "a current
        # representation for the target resource", even if the ETag is weak,
        # so there is a match to '*'.
        return True
    elif target_etag.startswith('W/'):
        # A weak ETag can never strongly match another ETag.
        return False
    else:
        # Since the ETag is strong, this will only return True if there's a
        # strong match.
        return target_etag in etags


def _if_unmodified_since_passes(last_modified, if_unmodified_since):
    """
    Test the If-Unmodified-Since comparison as defined in section 3.4 of
    RFC 7232.
    """
    return last_modified and last_modified <= if_unmodified_since


def _if_none_match_passes(target_etag, etags):
    """
    Test the If-None-Match comparison as defined in section 3.2 of RFC 7232.
    """
    if not target_etag:
        # If there isn't an ETag, then there isn't a match.
        return True
    elif etags == ['*']:
        # The existence of an ETag means that there is "a current
        # representation for the target resource", so there is a match to '*'.
        return False
    else:
        # The comparison should be weak, so look for a match after stripping
        # off any weak indicators.
        target_etag = target_etag.strip('W/')
        etags = (etag.strip('W/') for etag in etags)
        return target_etag not in etags


def _if_modified_since_passes(last_modified, if_modified_since):
    """
    Test the If-Modified-Since comparison as defined in section 3.3 of RFC 7232.
    """
    return not last_modified or last_modified > if_modified_since
