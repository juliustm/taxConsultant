# utils/device_auth.py
"""
Credentials for the scanner PWA.

A device holds one session at a time. That is not enforced by a rule somewhere; it
falls out of the session living in a single column on the device row, so issuing a
new one necessarily destroys the old one.

Two token kinds, both shaped `<device_id>.<secret>`:

  * enrolment - single-use, minted by an admin, spent once to open a session. Stored
    in the clear because the admin has to be able to re-render its QR while it is
    outstanding, and NULLed the instant it is spent.
  * session - long-lived, held by the phone, stored only as a SHA-256 hash.

The `<device_id>.` prefix is what makes a rejection explainable. A bare random token
that no longer matches anything can only produce "unauthorised"; one that names its
device can say *why* - revoked, signed out, or replaced by another phone - which is
the difference between a field user retrying forever and a field user calling the
admin.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import g, jsonify, request

from models.user import db, Device

# 32 bytes of urlsafe base64. Long enough that the id prefix giving away which device
# a token belongs to costs nothing.
TOKEN_BYTES = 32

# last_seen_at is written at most this often. Without it every sync poll from every
# device is a write to the same SQLite file, which is the one thing this deployment
# cannot afford to do casually.
LAST_SEEN_THROTTLE = timedelta(minutes=1)

# Why a session token was refused. The PWA shows these to the user verbatim, so they
# are phrased for the person holding the phone, not for a log.
REJECTION_MESSAGES = {
    'unknown': 'This device is not recognised. Ask your admin for a new activation link.',
    'revoked': 'This device has been revoked by your admin.',
    'signed_out': 'This device was signed out by your admin.',
    'replaced': 'This device was activated on another phone.',
}


def _mint(device_id):
    return f'{device_id}.{secrets.token_urlsafe(TOKEN_BYTES)}'


def _split(raw):
    """(device_id, token) from a prefixed token, or (None, None) if it is malformed."""
    if not raw or not isinstance(raw, str):
        return None, None
    device_id, _, _secret = raw.partition('.')
    if not _secret or not device_id.isdigit():
        return None, None
    return int(device_id), raw


def hash_token(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def issue_enrolment_token(device):
    """
    Puts a fresh single-use activation token on the device, replacing any outstanding
    one. Does not commit - the caller owns the transaction.
    """
    device.enrolment_token = _mint(device.id)
    device.enrolment_issued_at = datetime.utcnow()
    return device.enrolment_token


def consume_enrolment_token(raw, user_agent=None):
    """
    Spends an activation token and opens a session on its device.

    Returns (session_token, device) on success, or (None, reason) on failure. The
    token is cleared here rather than at the point the link is opened: opening a URL
    is something a mail scanner or a link preview can do by accident, and spending a
    single-use credential on that would strand the field user.
    """
    device_id, token = _split(raw)
    if device_id is None:
        return None, 'unknown'

    device = db.session.get(Device, device_id)
    if device is None or not device.enrolment_token:
        return None, 'unknown'
    if not hmac.compare_digest(device.enrolment_token, token):
        return None, 'unknown'
    if device.is_revoked:
        return None, 'revoked'

    session_token = start_session(device, user_agent=user_agent)
    db.session.commit()
    return session_token, device


def start_session(device, user_agent=None):
    """
    Opens a session, ending whichever one the device held before.

    This overwrite is the whole exclusivity mechanism: the previous phone's token no
    longer hashes to what is stored, so its next request is refused as 'replaced'.
    """
    token = _mint(device.id)
    now = datetime.utcnow()

    device.session_token_hash = hash_token(token)
    device.session_started_at = now
    device.session_user_agent = (user_agent or '')[:255] or None
    device.activated_at = device.activated_at or now
    device.last_seen_at = now
    device.enrolment_token = None
    device.enrolment_issued_at = None
    return token


def end_session(device):
    """Signs the device out. It needs a new activation token to come back."""
    device.session_token_hash = None
    device.session_started_at = None
    device.session_user_agent = None


def resolve_session(raw):
    """
    (device, None) for a live session, or (None, reason) naming why it is not.

    'signed_out' and 'replaced' are told apart by whether the device holds any session
    at all - a device with no session hash was signed out deliberately, one whose hash
    simply does not match has been taken over by another phone.
    """
    device_id, token = _split(raw)
    if device_id is None:
        return None, 'unknown'

    device = db.session.get(Device, device_id)
    if device is None:
        return None, 'unknown'
    if device.is_revoked:
        return None, 'revoked'
    if not device.session_token_hash:
        return None, 'signed_out'
    if not hmac.compare_digest(device.session_token_hash, hash_token(token)):
        return None, 'replaced'
    return device, None


def touch(device):
    """Records that the device was heard from, at most once a minute."""
    now = datetime.utcnow()
    if device.last_seen_at is None or now - device.last_seen_at >= LAST_SEEN_THROTTLE:
        device.last_seen_at = now
        db.session.commit()


def bearer_token():
    header = request.headers.get('Authorization') or ''
    if not header.startswith('Bearer '):
        return None
    return header[7:].strip() or None


def device_required(f):
    """
    Guards the scanner's API with a device session, exposed as `g.device`.

    The credential is read from the Authorization header and never from a cookie. The
    PWA is same-origin with the admin dashboard and this project has no CSRF
    protection anywhere, so a header-borne token is what keeps a device session from
    being usable by a page it did not come from - and keeps it from ever being
    mistaken for an admin login.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        device, reason = resolve_session(bearer_token())
        if device is None:
            return jsonify({
                'error': REJECTION_MESSAGES.get(reason, REJECTION_MESSAGES['unknown']),
                'reason': reason,
            }), 401
        g.device = device
        touch(device)
        return f(*args, **kwargs)
    return decorated
