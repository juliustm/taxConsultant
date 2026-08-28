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
from datetime import date

import pytest

from models.user import db, Device, Receipt, ReceiptItem, Submission
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


def test_a_scan_carrying_both_a_code_and_its_photo_becomes_one_verified_submission(phone):
    """
    The commonest scan of all, and the one that used to throw half of itself away.

    A phone that decodes a receipt's QR code is holding a photograph of that receipt at
    the same instant. The code went up in the JSON batch and the picture was dropped on
    the phone, so every receipt TRA confirmed had no image behind it - nobody could go
    back to the paper to check a line item or settle a dispute about what was bought.

    Both arrive here now, through this endpoint rather than the batch, because a
    photograph cannot go in the batch and splitting the pair across two requests is how
    they become two submissions the moment the second one fails.
    """
    response = phone.post('/scan/api/sync/photo', data={
        'client_uuid': 'both-1',
        'receipturl': 'https://verify.tra.go.tz/58E41A514_092022',
        'receiptphoto': (io.BytesIO(b'jpeg-bytes'), 'receipt.jpg'),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    submission = Submission.query.one()

    # Processed as the URL: the code is the stronger claim about which receipt this is,
    # and it is what the portal answers with its own figures.
    assert submission.input_type == 'url'
    assert submission.input_data == 'https://verify.tra.go.tz/58E41A514_092022'
    assert submission.receipt_code == '58E41A514'

    # And the paper is kept beside it, in its own column so that neither reader of
    # input_data has to learn a second meaning for it.
    assert submission.photo_filename
    assert submission.photo_filename.endswith('.jpg')


def test_a_photo_sent_without_a_code_is_unchanged(phone):
    """
    The other half of the same decision, and the one that must not have moved.

    A photograph with no code is still a photo submission with its filename in
    input_data, exactly as every row already in every database has it.
    """
    phone.post('/scan/api/sync/photo', data={
        'client_uuid': 'photo-only',
        'receiptphoto': (io.BytesIO(b'jpeg-bytes'), 'receipt.jpg'),
    }, content_type='multipart/form-data')

    submission = Submission.query.one()
    assert submission.input_type == 'photo'
    assert submission.input_data.endswith('.jpg')
    assert submission.photo_filename is None


def test_the_photograph_is_found_whichever_column_holds_it(phone, app):
    """
    One question - "is there a picture, and what is it called" - with two storage sites.

    Every page that shows a photograph reads it through these, so a reader that knew
    about only one of the columns would show the paper on old rows and not on new ones,
    or the other way round.
    """
    import main

    phone.post('/scan/api/sync/photo', data={
        'client_uuid': 'with-code',
        'receipturl': 'https://verify.tra.go.tz/58E41A514_092022',
        'receiptphoto': (io.BytesIO(b'jpeg-bytes'), 'receipt.jpg'),
    }, content_type='multipart/form-data')
    phone.post('/scan/api/sync/photo', data={
        'client_uuid': 'no-code',
        'receiptphoto': (io.BytesIO(b'jpeg-bytes'), 'receipt.jpg'),
    }, content_type='multipart/form-data')

    with_code, no_code = Submission.query.order_by(Submission.id).all()

    with app.test_request_context():
        assert main.submission_photo_url(with_code).endswith('.jpg')
        assert main.submission_photo_url(no_code).endswith('.jpg')
        assert main.submission_photo_path(with_code).endswith('.jpg')

    # A URL submission with no picture behind it - the bot path, and every row that
    # predates this - still says so rather than pointing at a file that is not there.
    plain = Submission(device_id=with_code.device_id, input_type='url',
                       input_data='https://verify.tra.go.tz/PLAIN123_010101')
    db.session.add(plain)
    db.session.commit()
    with app.test_request_context():
        assert main.submission_photo_url(plain) is None
        assert main.submission_photo_path(plain) is None


def test_a_verified_submission_with_a_photo_is_announced_with_both(phone):
    """
    The dashboard reads input_data and photo_url as two different things.

    They used to be one field that meant a URL on some rows and an image path on
    others, which was survivable only while a submission could not have both. Collapsing
    them now would put an image path on a row whose input_type says 'url'.
    """
    phone.post('/scan/api/sync/photo', data={
        'client_uuid': 'both-2',
        'receipturl': 'https://verify.tra.go.tz/58E41A514_092022',
        'receiptphoto': (io.BytesIO(b'jpeg-bytes'), 'receipt.jpg'),
    }, content_type='multipart/form-data')

    row = phone.get('/scan/api/submissions').get_json()['submissions'][0]
    assert row['input_type'] == 'url'
    assert row['input_data'] == 'https://verify.tra.go.tz/58E41A514_092022'
    assert row['photo_url'].endswith('.jpg')


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


# --- Pagination and search ----------------------------------------------------

def test_before_id_pages_into_older_history_with_no_overlap(phone):
    for letter in ('a', 'b', 'c'):
        phone.post('/scan/api/sync', json={'items': [scan(letter, letter.upper() * 3)]})

    first_page = phone.get('/scan/api/submissions?limit=2').get_json()
    assert [s['client_uuid'] for s in first_page['submissions']] == ['c', 'b']
    assert first_page['has_more'] is True

    oldest_id_so_far = first_page['submissions'][-1]['id']
    second_page = phone.get(f'/scan/api/submissions?limit=2&before_id={oldest_id_so_far}').get_json()
    assert [s['client_uuid'] for s in second_page['submissions']] == ['a']
    assert second_page['has_more'] is False


def test_search_matches_vendor_name_and_item_description(phone):
    phone.post('/scan/api/sync', json={'items': [scan('coffee', 'COFFEE1')]})
    phone.post('/scan/api/sync', json={'items': [scan('hardware', 'HARDWR1')]})

    coffee_sub = Submission.query.filter_by(client_uuid='coffee').one()
    receipt = Receipt(device_id=phone.device.id, submission_id=coffee_sub.id, vendor_name='Java House')
    receipt.items.append(ReceiptItem(line_number=1, description='Printer Paper'))
    db.session.add(receipt)
    db.session.commit()

    by_vendor = phone.get('/scan/api/submissions?q=java').get_json()
    assert [s['client_uuid'] for s in by_vendor['submissions']] == ['coffee']

    by_item = phone.get('/scan/api/submissions?q=printer').get_json()
    assert [s['client_uuid'] for s in by_item['submissions']] == ['coffee']

    no_match = phone.get('/scan/api/submissions?q=nonexistent').get_json()
    assert no_match['submissions'] == []


def test_search_stays_scoped_to_this_device(phone):
    other = Device(name='Someone else')
    db.session.add(other)
    db.session.flush()
    other_sub = Submission(
        device_id=other.id, input_type='url',
        input_data='https://verify.tra.go.tz/OTHER_010101', status='completed',
    )
    db.session.add(other_sub)
    db.session.flush()
    db.session.add(Receipt(device_id=other.id, submission_id=other_sub.id, vendor_name='Java House'))
    db.session.commit()

    body = phone.get('/scan/api/submissions?q=java').get_json()

    assert body['submissions'] == []


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


# --- The numbers above the list -----------------------------------------------

def _completed_receipt(phone, uuid, code, **fields):
    phone.post('/scan/api/sync', json={'items': [scan(uuid, code)]})
    submission = Submission.query.filter_by(client_uuid=uuid).one()
    submission.status = 'completed'
    receipt = Receipt(device_id=phone.device.id, submission_id=submission.id, **fields)
    db.session.add(receipt)
    db.session.commit()
    return receipt


def test_the_summary_totals_this_devices_month(phone):
    _completed_receipt(phone, 'a', 'AAA1111', receipt_date=date.today(),
                       total_incl_tax_cents=118_00, total_tax_cents=18_00)
    _completed_receipt(phone, 'b', 'BBB1111', receipt_date=date.today(),
                       total_incl_tax_cents=236_00, total_tax_cents=36_00)

    body = phone.get('/scan/api/summary').get_json()

    assert body['month']['receipts'] == 2
    assert body['month']['spend_cents'] == 354_00
    assert body['month']['vat_cents'] == 54_00


def test_the_summary_excludes_cancelled_and_test_receipts(phone):
    """A voided receipt is not money anybody spent, and must not read as if it were."""
    _completed_receipt(phone, 'real', 'AAA1111', receipt_date=date.today(),
                       total_incl_tax_cents=100_00)
    _completed_receipt(phone, 'void', 'BBB1111', receipt_date=date.today(),
                       total_incl_tax_cents=900_00, is_cancelled=True)
    _completed_receipt(phone, 'demo', 'CCC1111', receipt_date=date.today(),
                       total_incl_tax_cents=900_00, is_test=True)

    body = phone.get('/scan/api/summary').get_json()

    assert body['month']['receipts'] == 1
    assert body['month']['spend_cents'] == 100_00


def test_an_old_receipt_captured_today_counts_as_this_months_work(phone):
    """
    The field app counts capture, not spend - the opposite of the dashboard, on
    purpose. An afternoon spent clearing a shoebox of last year's receipts has to show
    as an afternoon's work, not as a month that reads zero.
    """
    _completed_receipt(phone, 'old', 'AAA1111', receipt_date=date(2022, 3, 8),
                       total_incl_tax_cents=50_00)
    # And a receipt whose date could not be read at all still counts.
    _completed_receipt(phone, 'photo', 'BBB1111', receipt_date=None,
                       total_incl_tax_cents=25_00)

    body = phone.get('/scan/api/summary').get_json()

    assert body['month']['receipts'] == 2
    assert body['month']['spend_cents'] == 75_00
    assert body['today']['receipts'] == 2


def test_the_summary_counts_work_in_flight_and_work_needing_attention(phone):
    phone.post('/scan/api/sync', json={'items': [scan('waiting', 'AAA1111')]})
    phone.post('/scan/api/sync', json={'items': [scan('broken', 'BBB1111')]})
    Submission.query.filter_by(client_uuid='broken').one().status = 'failed'
    db.session.commit()

    body = phone.get('/scan/api/summary').get_json()

    assert body['in_flight'] == 1
    assert body['needs_attention'] == 1


def test_the_summary_never_shows_another_devices_money(phone):
    other = Device(name='Someone else')
    db.session.add(other)
    db.session.flush()
    their_submission = Submission(
        device_id=other.id, input_type='url', status='completed',
        input_data='https://verify.tra.go.tz/OTHER_010101',
    )
    db.session.add(their_submission)
    db.session.flush()
    db.session.add(Receipt(device_id=other.id, submission_id=their_submission.id,
                           receipt_date=date.today(), total_incl_tax_cents=999_00))
    db.session.commit()

    body = phone.get('/scan/api/summary').get_json()

    assert body['month'] == {'receipts': 0, 'spend_cents': 0, 'vat_cents': 0}


def test_the_summary_requires_a_session(app):
    assert app.test_client().get('/scan/api/summary').status_code == 401


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
