# utils/tra.py
"""
Client for the TRA receipt verification portal (verify.tra.go.tz).

The portal used to be stateful: you fetched /{code}_{HHMMSS} to open a session and
then /Verify/Verified?Secret=HH:MM:SS on the same cookie jar. That contract is gone.

It is now stateless and keyed entirely off the Referer header:

  * /{code}_{HHMMSS} no longer renders the receipt at all - it returns a time-entry
    page, so fetching it first is pointless and only burns a request against a
    rate-limited endpoint.
  * The receipt comes from /Verify/Verified?Secret=<HH:MM:SS>, with the Secret
    percent-encoded (raw colons bounce to /Home/Warning) and a Referer pointing at
    https://verify.tra.go.tz/{code}_{HHMMSS} (no Referer lands on /Home/Index).

Failures are distinguished by the URL the portal leaves us on, and are raised as
distinct exceptions so the caller can pick a retry policy instead of treating every
outcome as one silent failure.
"""
import re
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests

TRA_HOST = 'verify.tra.go.tz'
TRA_BASE_URL = f'https://{TRA_HOST}'
USER_AGENT = 'Mozilla/5.0 KaraniAI/1.0'
# (connect + TLS handshake, read). The connect half is deliberately short: a bad
# backend usually shows up as a stalled handshake, and we would rather drop it and
# dial again than sit on it.
REQUEST_TIMEOUT = (8, 20)
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# The portal's TLS endpoint is unreliable: a sizeable share of handshakes fail with
# 'SSLV3_ALERT_UNEXPECTED_MESSAGE' or simply stall, and the failures come and go over
# minutes. Measured from one host: 5 of 8 connections failed in one burst, 12 of 12
# succeeded twenty minutes later, with the same TLS settings throughout. It is not
# our ClientHello (restricting the key-exchange group changes nothing) and it is not
# rate limiting (failures happen on the first connection after an idle period), so
# most likely a subset of backends behind the load balancer is broken at any moment.
# A fresh connection usually lands on a healthy one, so retry the dial a few times.
CONNECT_ATTEMPTS = 4
CONNECT_RETRY_SECONDS = 1

# Present on a genuine receipt page and on nothing else the portal serves.
RECEIPT_MARKER = 'RECEIPT VERIFICATION CODE'
TIME_ENTRY_MARKER = 'please provide your receipt time'

_RECEIPT_URL_RE = re.compile(r'([A-Za-z0-9]+)_(\d{2})(\d{2})(\d{2})/?\s*$')


class TraError(Exception):
    """
    Base class for portal failures.

    `retryable` says whether re-issuing the exact same request could ever succeed,
    e.g. the vendor has not uploaded the receipt yet. It is False for outcomes that
    are permanent for this submission (wrong time in the URL) or that indicate a bug
    on our side (Referer rejected).
    """
    retryable = False


class TraReceiptNotUploaded(TraError):
    """/Home/Missing - the vendor has not pushed this receipt to TRA yet."""
    retryable = True


class TraThrottled(TraError):
    """/Home/Warning - rate limited (or a malformed Secret, which we no longer send)."""
    retryable = True


class TraTransportError(TraError):
    """Network failure, 5xx, or a redirect chain we refused to follow."""
    retryable = True


class TraUnexpectedResponse(TraError):
    """A page we do not recognise - most likely the portal changed again."""
    retryable = True


class TraWrongReceiptTime(TraError):
    """Bounced back to the time-entry page: the time in the submitted URL is wrong."""
    retryable = False


class TraRefererRejected(TraError):
    """/Home/Index - the portal did not accept our Referer. Our bug, not TRA's."""
    retryable = False


def build_receipt_url(code, receipt_time) -> str:
    """
    The portal address for a receipt, from its verification code and printed time.

    The inverse of parse_receipt_url, and the only place that construction lives. Two
    very different callers build this address - the vision model, from a transcription
    of the paper, and an admin, reading the same paper by hand off the photograph - and
    they must not be able to disagree about what the portal expects.

    The time is read as fields rather than as a run of digits, because it arrives as
    '10:47:40', as '9:20:22', as '10:47' where the EFD printed no seconds, and as the
    bare '104740' copied out of a URL. Stripping the punctuation out of those gives six,
    five, four and six digits for four times that are all perfectly readable.

    Raises ValueError with a sentence for whoever typed it, rather than returning None:
    a half-guessed code costs a request against a rate-limited portal and, worse, could
    land on somebody else's receipt.
    """
    cleaned = ''.join(ch for ch in (code or '') if ch.isalnum())
    if not cleaned:
        raise ValueError('A verification code is needed - it is the long code printed under the QR square.')

    parts = [part for part in re.split(r'\D+', str(receipt_time or '')) if part]
    # Six digits in one field is the URL's own form, not three fields run together.
    if len(parts) == 1 and len(parts[0]) == 6:
        parts = [parts[0][0:2], parts[0][2:4], parts[0][4:6]]
    # An EFD that printed no seconds leaves two; the portal still wants all six digits.
    if len(parts) == 2:
        parts.append('0')
    if len(parts) != 3 or any(len(part) > 2 for part in parts):
        raise ValueError("The receipt time should read as HH:MM:SS, e.g. '10:47:40'.")

    hours, minutes, seconds = (int(part) for part in parts)
    if hours > 23 or minutes > 59 or seconds > 59:
        raise ValueError(f'{hours:02d}:{minutes:02d}:{seconds:02d} is not a time of day.')

    return f'{TRA_BASE_URL}/{cleaned}_{hours:02d}{minutes:02d}{seconds:02d}'


def parse_receipt_url(url: str):
    """
    Extracts (code, hhmmss) from a submitted receipt URL or bare receipt code.

    Accepts anything ending in <code>_<HHMMSS>, so both
    'https://verify.tra.go.tz/58E41A514_092022' and '58E41A514_092022' work.
    """
    match = _RECEIPT_URL_RE.search(url or '')
    if not match:
        raise ValueError("Invalid receipt URL format: expected '<code>_<HHMMSS>'.")

    code, hours, minutes, seconds = match.groups()
    if int(hours) > 23 or int(minutes) > 59 or int(seconds) > 59:
        raise ValueError(f"Invalid receipt time in URL: {hours}:{minutes}:{seconds}")

    return code, f'{hours}{minutes}{seconds}'


def fetch_receipt_html(url: str, timeout: int = REQUEST_TIMEOUT) -> str:
    """
    Returns the raw HTML of the verified receipt page, or raises a TraError subclass.

    A single request: the code URL is not fetched first because it no longer renders
    the receipt.
    """
    code, hhmmss = parse_receipt_url(url)
    secret = f'{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}'

    # Both of these are rebuilt from the parsed code rather than passed through from
    # the submission, so a submitted http:// URL can never downgrade the request.
    verify_url = f"{TRA_BASE_URL}/Verify/Verified?Secret={quote(secret, safe='')}"
    referer = f'{TRA_BASE_URL}/{code}_{hhmmss}'

    headers = {
        'User-Agent': USER_AGENT,
        'Referer': referer,
        'Accept': 'text/html,application/xhtml+xml',
    }

    final_url, response = _get_https_only(verify_url, headers, timeout)
    return _interpret(final_url, response, code, hhmmss)


def _get_https_only(url: str, headers: dict, timeout: int):
    """
    Follows redirects by hand, upgrading TRA's plaintext Location headers back to
    HTTPS and refusing to leave the portal's host.

    requests would happily follow the http:// hops TRA emits, putting the receipt
    code on the wire in cleartext before it bounces back to HTTPS.
    """
    for _ in range(MAX_REDIRECTS + 1):
        response = _get_with_connect_retries(url, headers, timeout)

        if response.status_code not in REDIRECT_STATUSES:
            return url, response

        location = response.headers.get('Location')
        if not location:
            raise TraTransportError(f'HTTP {response.status_code} from {url} with no Location header.')

        url = _next_hop(url, location)

    raise TraTransportError(f'Redirect chain exceeded {MAX_REDIRECTS} hops.')


def _get_with_connect_retries(url: str, headers: dict, timeout):
    """
    Issues one GET, redialling when the connection itself fails.

    Only connection-level failures are retried (see CONNECT_ATTEMPTS). Those never
    reached the application, so a redial costs nothing against TRA's HTTP rate limit
    and gets a fresh pick of backend. A read timeout, by contrast, means the request
    was delivered, so it is reported rather than repeated.
    """
    last_error = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        except requests.exceptions.ConnectionError as e:
            last_error = e
            print(f'[TRA] Connection attempt {attempt}/{CONNECT_ATTEMPTS} failed: {type(e).__name__}')
            if attempt < CONNECT_ATTEMPTS:
                time.sleep(CONNECT_RETRY_SECONDS)
        except requests.exceptions.RequestException as e:
            raise TraTransportError(f'Request to {url} failed: {e}') from e

    raise TraTransportError(
        f'Could not connect to the portal after {CONNECT_ATTEMPTS} attempts: {last_error}'
    ) from last_error


def _next_hop(current_url: str, location: str) -> str:
    parts = urlsplit(urljoin(current_url, location))

    if (parts.hostname or '').lower() != TRA_HOST:
        raise TraTransportError(f'Refusing cross-host redirect to {parts.geturl()}')
    if parts.scheme not in ('http', 'https'):
        raise TraTransportError(f'Refusing redirect to unsupported scheme: {parts.scheme}')

    # TRA redirects via plain http; put it back on TLS before we follow it.
    return urlunsplit(('https', TRA_HOST, parts.path, parts.query, parts.fragment))


def _interpret(final_url: str, response, code: str, hhmmss: str) -> str:
    """Maps where the portal left us to a receipt or a typed failure."""
    path = urlsplit(final_url).path.lower().rstrip('/')
    body = response.text or ''
    where = f'HTTP {response.status_code} at {final_url}'

    if response.status_code >= 500:
        raise TraTransportError(f'Portal error: {where}')

    if path.startswith('/home/missing'):
        raise TraReceiptNotUploaded(f'Vendor has not uploaded this receipt to TRA yet ({where}).')

    if path.startswith('/home/warning'):
        raise TraThrottled(f'Portal rejected the request as too many requests ({where}).')

    if path in ('', '/home', '/home/index'):
        raise TraRefererRejected(
            f'Portal rejected our Referer and served its landing page ({where}). '
            'This is a client bug, not a TRA outage.'
        )

    if path.startswith('/verify/verified'):
        if RECEIPT_MARKER in body.upper():
            return body
        if TIME_ENTRY_MARKER in body.lower():
            raise TraWrongReceiptTime(f'Portal asked for the receipt time again ({where}).')
        raise TraUnexpectedResponse(f'Verified page without a receipt on it ({where}).')

    if path.endswith(f'/{code}_{hhmmss}'.lower()) or TIME_ENTRY_MARKER in body.lower():
        raise TraWrongReceiptTime(
            f'Portal returned the time-entry page ({where}): the time in the receipt URL is wrong.'
        )

    raise TraUnexpectedResponse(f'Unrecognised portal response ({where}).')
