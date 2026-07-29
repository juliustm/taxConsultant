# tests/test_device_auth.py
"""
Device enrolment and sessions.

The property worth defending here is exclusivity: one device is one phone. It is not
enforced by a check anywhere - it falls out of the session hash being a single column,
so activating anywhere new necessarily invalidates everywhere old. That is easy to
break accidentally the first time someone adds a session table, so it is asserted
directly rather than inferred from the schema.

The other property is that losing a session never loses receipts. A signed-out phone
still holds the only copy of anything it has not uploaded.
"""
import pytest

from models.user import db, Device, Submission
from utils.device_auth import (
    consume_enrolment_token, hash_token, issue_enrolment_token, resolve_session,
)


@pytest.fixture
def enrolled(app):
    """A device with an outstanding, unspent activation token."""
    device = Device(name='Field phone')
    db.session.add(device)
    db.session.flush()
    token = issue_enrolment_token(device)
    db.session.commit()
    return device, token


@pytest.fixture
def admin(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


# --- Enrolment ---------------------------------------------------------------

def test_a_token_names_its_device(enrolled):
    """
    The device id is part of the token.

    Without it, a token that no longer matches anything is unattributable, and every
    rejection collapses to a bare 401 - the field user is told "no" and never why.
    """
    device, token = enrolled
    assert token.startswith(f'{device.id}.')


def test_activation_opens_a_session_and_spends_the_token(enrolled):
    device, token = enrolled

    session_token, activated = consume_enrolment_token(token, user_agent='iPhone')

    assert activated.id == device.id
    assert device.session_token_hash == hash_token(session_token)
    assert device.session_user_agent == 'iPhone'
    assert device.activated_at is not None
    # Single use: nothing is left for a second phone to spend.
    assert device.enrolment_token is None


def test_a_spent_token_cannot_be_used_again(enrolled):
    _device, token = enrolled
    consume_enrolment_token(token)

    session_token, reason = consume_enrolment_token(token)

    assert session_token is None
    assert reason == 'unknown'


def test_a_forged_token_for_a_real_device_is_refused(enrolled):
    device, _token = enrolled

    session_token, reason = consume_enrolment_token(f'{device.id}.not-the-real-secret')

    assert session_token is None
    assert reason == 'unknown'
    # The real token survives; a guess must not burn the credential it guessed at.
    assert device.enrolment_token is not None


def test_a_revoked_device_cannot_be_activated(enrolled):
    from datetime import datetime
    device, token = enrolled
    device.revoked_at = datetime.utcnow()
    db.session.commit()

    session_token, reason = consume_enrolment_token(token)

    assert session_token is None
    assert reason == 'revoked'


# --- Exclusivity -------------------------------------------------------------

def test_activating_a_second_phone_signs_the_first_one_out(enrolled):
    """The whole point of the design: one device, one phone, no configuration."""
    device, first_token = enrolled
    first_session, _ = consume_enrolment_token(first_token)

    # The admin issues another link and a different phone uses it.
    second_token = issue_enrolment_token(device)
    db.session.commit()
    second_session, _ = consume_enrolment_token(second_token)

    assert resolve_session(second_session)[0].id == device.id

    stale, reason = resolve_session(first_session)
    assert stale is None
    # Named precisely, so the old phone can say what happened rather than guess.
    assert reason == 'replaced'


def test_a_signed_out_device_is_told_it_was_signed_out(enrolled):
    from utils.device_auth import end_session
    device, token = enrolled
    session_token, _ = consume_enrolment_token(token)

    end_session(device)
    db.session.commit()

    assert resolve_session(session_token) == (None, 'signed_out')


def test_a_revoked_device_outranks_a_live_session(enrolled):
    from datetime import datetime
    device, token = enrolled
    session_token, _ = consume_enrolment_token(token)

    device.revoked_at = datetime.utcnow()
    db.session.commit()

    assert resolve_session(session_token) == (None, 'revoked')


@pytest.mark.parametrize('token', ['', 'no-dot', 'abc.def', '999.whatever', None])
def test_malformed_tokens_are_refused_without_raising(app, token):
    assert resolve_session(token)[0] is None


# --- The guard ---------------------------------------------------------------

def test_the_api_refuses_a_request_with_no_session(app):
    response = app.test_client().get('/scan/api/me')
    assert response.status_code == 401


def test_the_api_explains_why_it_refused(app, enrolled):
    """
    A refusal has to be readable by the person holding the phone.

    "Unauthorised" tells a field user to keep retrying something that will never work;
    "activated on another phone" tells them to call their admin.
    """
    device, token = enrolled
    session_token, _ = consume_enrolment_token(token)

    issue_enrolment_token(device)
    db.session.commit()
    consume_enrolment_token(device.enrolment_token)   # another phone takes over

    response = app.test_client().get(
        '/scan/api/me', headers={'Authorization': f'Bearer {session_token}'}
    )

    assert response.status_code == 401
    body = response.get_json()
    assert body['reason'] == 'replaced'
    assert 'another phone' in body['error']


def test_a_device_session_is_not_an_admin_session(app, config, enrolled):
    """
    A device token must not open the dashboard.

    Both live on the same origin, so the only thing keeping them apart is that one is
    a bearer header and the other a signed cookie. This asserts they have not been
    quietly unified.

    '/' answers everyone now - it is the public front page until an admin cookie says
    otherwise - so the check is that the phone is treated as a member of the public,
    and that the dashboard's own data is still shut to it.
    """
    _device, token = enrolled
    session_token, _ = consume_enrolment_token(token)
    headers = {'Authorization': f'Bearer {session_token}'}
    client = app.test_client()

    front_door = client.get('/', headers=headers)
    assert front_door.status_code == 200
    assert 'Receipts Dashboard' not in front_door.get_data(as_text=True)

    submissions = client.get('/api/submissions', headers=headers)
    assert submissions.status_code == 302
    assert '/admin/login' in submissions.headers['Location']


def test_last_seen_is_recorded(app, enrolled):
    _device, token = enrolled
    session_token, device = consume_enrolment_token(token)
    device.last_seen_at = None
    db.session.commit()

    app.test_client().get('/scan/api/me', headers={'Authorization': f'Bearer {session_token}'})

    assert db.session.get(Device, device.id).last_seen_at is not None


# --- Admin lifecycle ---------------------------------------------------------

def test_adding_a_device_issues_an_activation_link(admin):
    admin.post('/admin/devices', data={'device_name': "Ali's phone"})

    device = Device.query.filter_by(name="Ali's phone").one()
    assert device.enrolment_token is not None
    assert device.status == 'awaiting_activation'


def test_the_admin_page_shows_the_activation_link(admin, enrolled):
    device, token = enrolled

    body = admin.get('/admin/devices').get_data(as_text=True)

    assert f'/scan/a/{token}' in body
    # And the QR of that same link, rendered server-side.
    assert 'data:image/png;base64,' in body


def test_issuing_a_new_link_kills_the_old_one(admin, enrolled):
    device, old_token = enrolled

    admin.post(f'/admin/devices/{device.id}/issue-link')

    assert device.enrolment_token != old_token
    assert consume_enrolment_token(old_token) == (None, 'unknown')


def test_revoking_a_device_kills_its_session_and_its_api_key(admin, enrolled):
    device, token = enrolled
    session_token, _ = consume_enrolment_token(token)
    api_key = device.api_key

    admin.post(f'/admin/devices/{device.id}/revoke')

    assert resolve_session(session_token) == (None, 'revoked')
    # The bot credential goes too - revoked has to mean revoked, not "revoked for phones".
    refused = admin.post('/receipt', headers={'Authorization': f'Bearer {api_key}'},
                         data={'receipturl': 'https://verify.tra.go.tz/X_101010'})
    assert refused.status_code == 403


def test_a_device_with_receipts_cannot_be_deleted(admin, enrolled):
    """
    Submission.device_id is NOT NULL, so deleting the device behind a receipt would
    leave the ledger pointing at nothing.
    """
    device, _token = enrolled
    db.session.add(Submission(
        device_id=device.id, input_type='url',
        input_data='https://verify.tra.go.tz/X_101010', status='completed',
    ))
    db.session.commit()

    admin.post(f'/admin/devices/{device.id}/delete', follow_redirects=True)

    assert db.session.get(Device, device.id) is not None


def test_an_unused_device_can_be_deleted(admin, enrolled):
    device, _token = enrolled
    device_id = device.id

    admin.post(f'/admin/devices/{device_id}/delete', follow_redirects=True)

    assert db.session.get(Device, device_id) is None


def test_rotating_the_api_key_breaks_the_old_one(admin, enrolled):
    device, _token = enrolled
    old_key = device.api_key

    admin.post(f'/admin/devices/{device.id}/rotate-key')

    assert device.api_key != old_key
    refused = admin.post('/receipt', headers={'Authorization': f'Bearer {old_key}'},
                         data={'receipturl': 'https://verify.tra.go.tz/X_101010'})
    assert refused.status_code == 403


def test_device_management_requires_an_admin(app, enrolled):
    device, _token = enrolled
    anonymous = app.test_client()

    for path in ('/admin/devices', f'/admin/devices/{device.id}/revoke'):
        response = anonymous.post(path, data={'device_name': 'x'})
        assert response.status_code == 302
        assert '/admin/login' in response.headers['Location']
