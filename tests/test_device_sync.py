# tests/test_device_sync.py
"""
The scanner's sync endpoints.

Two things are being defended.

The first is idempotency. A phone that has been offline holds the only copy of a
receipt and will keep resending it until the server says it arrived - so a response
lost on the way back must not create a second submission. Every path through sync is
therefore keyed on the uuid the phone minted before it ever had a network.

The second is that one sync is one wake-up. The task runner fetches from a portal that
rate-limits at roughly eight rapid requests, so a device returning from a day offline
with thirty receipts must nudge it once, not thirty times.
"""
import io

import pytest

from models.user import db, Device, Submission
from utils.device_auth import consume_enrolment_token, issue_enrolment_token


@pytest.fixture
def phone(app, monkeypatch):
    """An activated device, plus a client that authenticates as it."""
    # The runner trigger spawns a greenlet that would try to reach the app over the
    # network. Counted instead, since how often it fires is itself under test.
    import main
    calls = []
    monkeypatch.setattr(main.gevent, 'spawn', lambda *a, **kw: calls.append(a))

    device = Device(name='Field phone')
    db.session.add(device)
    db.session.flush()
    token = issue_enrolment_token(device)
    db.session.commit()
    session_token, _ = consume_enrolment_token(token)

    client = app.test_client()

    class Phone:
        def __init__(self):
            self.device = device
            self.wakeups = calls

        def post(self, path, **kwargs):
            headers = kwargs.pop('headers', {})
            headers['Authorization'] = f'Bearer {session_token}'
            return client.post(path, headers=headers, **kwargs)

        def get(self, path, **kwargs):
            headers = kwargs.pop('headers', {})
            headers['Authorization'] = f'Bearer {session_token}'
            return client.get(path, headers=headers, **kwargs)

    return Phone()


def scan(uuid, code='58E41A514', time='092022'):
    return {
        'client_uuid': uuid,
        'receipturl': f'https://verify.tra.go.tz/{code}_{time}',
        'captured_at': '2026-07-27T08:15:00Z',
    }


# --- Batch sync --------------------------------------------------------------

def test_a_batch_creates_one_submission_per_scan(phone):
    response = phone.post('/scan/api/sync', json={
        'items': [scan('a', 'AAA111'), scan('b', 'BBB222'), scan('c', 'CCC333')],
    })

    assert response.status_code == 200
    results = response.get_json()['results']
    assert [r['status'] for r in results] == ['accepted'] * 3
    assert Submission.query.count() == 3


def test_a_batch_wakes_the_runner_exactly_once(phone):
    """Thirty receipts is one nudge; the portal cannot take thirty."""
    phone.post('/scan/api/sync', json={
        'items': [scan(f'u{i}', f'CODE{i:03d}') for i in range(30)],
    })

    assert Submission.query.count() == 30
    assert len(phone.wakeups) == 1


def test_an_empty_batch_does_not_wake_the_runner(phone):
    phone.post('/scan/api/sync', json={'items': []})

    assert phone.wakeups == []


def test_resending_the_same_scan_creates_nothing_new(phone):
    """
    The retry case: the submission was created but the response never arrived, so the
    phone sends it again. It must be told the receipt is safe, not given a second one.
    """
    first = phone.post('/scan/api/sync', json={'items': [scan('same-uuid')]})
    second = phone.post('/scan/api/sync', json={'items': [scan('same-uuid')]})

    assert Submission.query.count() == 1
    assert first.get_json()['results'][0]['status'] == 'accepted'
    assert second.get_json()['results'][0]['status'] == 'duplicate'
    # Same row, so the phone can clear it from the outbox either way.
    assert (first.get_json()['results'][0]['submission_id']
            == second.get_json()['results'][0]['submission_id'])


def test_a_bad_item_does_not_sink_the_batch(phone):
    response = phone.post('/scan/api/sync', json={
        'items': [scan('good-1'), {'client_uuid': 'bad'}, scan('good-2', 'ZZZ999')],
    })

    results = response.get_json()['results']
    assert [r['status'] for r in results] == ['accepted', 'rejected', 'accepted']
    assert Submission.query.count() == 2


def test_the_verification_code_is_read_at_intake(phone):
    """A submission that never verifies still has to carry the receipt's identity."""
    phone.post('/scan/api/sync', json={'items': [scan('a', '58E41A514', '092022')]})

    assert Submission.query.one().receipt_code == '58E41A514'


def test_when_the_receipt_was_captured_is_kept_apart_from_when_it_arrived(phone):
    """
    Offline queuing puts days between the two, and every report keys off when the
    money was spent.
    """
    phone.post('/scan/api/sync', json={'items': [scan('a')]})

    submission = Submission.query.one()
    assert submission.captured_at.isoformat() == '2026-07-27T08:15:00'
    assert submission.received_at != submission.captured_at


def test_an_oversized_batch_is_refused(phone):
    response = phone.post('/scan/api/sync', json={
        'items': [scan(f'u{i}') for i in range(200)],
    })

    assert response.status_code == 413
    assert Submission.query.count() == 0


def test_sync_requires_a_session(app):
    response = app.test_client().post('/scan/api/sync', json={'items': [scan('a')]})

    assert response.status_code == 401
    assert Submission.query.count() == 0


# --- Photos ------------------------------------------------------------------

def test_a_photo_is_stored_and_queued(phone):
    response = phone.post('/scan/api/sync/photo', data={
        'client_uuid': 'photo-1',
        'captured_at': '2026-07-27T08:15:00Z',
        'receiptphoto': (io.BytesIO(b'not-really-a-jpeg'), 'receipt.jpg'),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    submission = Submission.query.one()
    assert submission.input_type == 'photo'
    # The database holds the filesystem path; the dashboard is handed a URL elsewhere.
    assert submission.input_data.endswith('.jpg')


def test_resending_the_same_photo_creates_nothing_new(phone):
    def send():
        return phone.post('/scan/api/sync/photo', data={
            'client_uuid': 'photo-1',
            'receiptphoto': (io.BytesIO(b'bytes'), 'receipt.jpg'),
        }, content_type='multipart/form-data')

    first, second = send(), send()

    assert Submission.query.count() == 1
    assert first.get_json()['status'] == 'accepted'
    assert second.get_json()['status'] == 'duplicate'


# --- History and retry -------------------------------------------------------

def test_a_device_sees_only_its_own_submissions(phone):
    other = Device(name='Someone else')
    db.session.add(other)
    db.session.flush()
    db.session.add(Submission(
        device_id=other.id, input_type='url',
        input_data='https://verify.tra.go.tz/OTHER_010101', status='completed',
    ))
    db.session.commit()
    phone.post('/scan/api/sync', json={'items': [scan('mine', 'MINE111')]})

    body = phone.get('/scan/api/submissions').get_json()

    assert [s['receipt_code'] for s in body['submissions']] == ['MINE111']


def test_history_reports_the_uuid_the_phone_minted(phone):
    """This is how the phone reconciles its outbox against what the server has."""
    phone.post('/scan/api/sync', json={'items': [scan('known-uuid')]})

    body = phone.get('/scan/api/submissions').get_json()

    assert body['submissions'][0]['client_uuid'] == 'known-uuid'


def test_a_device_can_retry_its_own_failed_submission(phone):
    phone.post('/scan/api/sync', json={'items': [scan('a')]})
    submission = Submission.query.one()
    submission.status = 'failed'
    submission.failure_reason = 'TraReceiptNotUploaded'
    db.session.commit()

    response = phone.post(f'/scan/api/submissions/{submission.id}/retry')

    assert response.status_code == 202
    assert db.session.get(Submission, submission.id).status == 'queued'
    assert db.session.get(Submission, submission.id).failure_reason is None


def test_a_device_cannot_retry_someone_elses_submission(phone):
    other = Device(name='Someone else')
    db.session.add(other)
    db.session.flush()
    theirs = Submission(
        device_id=other.id, input_type='url', status='failed',
        input_data='https://verify.tra.go.tz/OTHER_010101',
    )
    db.session.add(theirs)
    db.session.commit()

    response = phone.post(f'/scan/api/submissions/{theirs.id}/retry')

    assert response.status_code == 404
    assert db.session.get(Submission, theirs.id).status == 'failed'


# --- The bot path is untouched -----------------------------------------------

def test_the_original_api_key_endpoint_still_works(app, device, monkeypatch):
    """
    /receipt is the contract existing integrations were built against. Extracting the
    intake logic out from under it must not have changed it.
    """
    import main
    monkeypatch.setattr(main.gevent, 'spawn', lambda *a, **kw: None)

    response = app.test_client().post(
        '/receipt',
        headers={'Authorization': f'Bearer {device.api_key}'},
        data={'receipturl': 'https://verify.tra.go.tz/58E41A514_092022',
              'description': 'Fuel'},
    )

    assert response.status_code == 202
    submission = Submission.query.one()
    assert submission.description == 'Fuel'
    assert submission.receipt_code == '58E41A514'
    # A bot has no outbox, so it supplies no uuid and none is invented for it.
    assert submission.client_uuid is None
