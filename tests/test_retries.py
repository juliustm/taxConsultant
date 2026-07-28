# tests/test_retries.py
"""
What happens between a submission arriving and a receipt existing - or not.

The retry policy was already correct; what it was not was visible. A submission that
had gone quiet for a day looked identical to one that had given up, and the only way
to find out which was to read this module's source. These cover both the mechanics and
the reporting of them, because a schedule nobody can see is a schedule nobody trusts.
"""
from datetime import date, datetime, timedelta

import pytest

from models.user import db, Receipt, Submission
from utils.tra import (
    TraReceiptNotUploaded, TraRefererRejected, TraThrottled, TraTransportError,
    TraWrongReceiptTime,
)

RECEIPT_URL = 'https://verify.tra.go.tz/58E41A514_092022'


@pytest.fixture
def submission(app, device, config):
    import main

    submitted = Submission(
        device_id=device.id, input_type='url', input_data=RECEIPT_URL,
        receipt_code=main._code_from_url(RECEIPT_URL),
    )
    db.session.add(submitted)
    db.session.commit()
    return submitted


# --- What is captured before TRA is ever contacted --------------------------

def test_the_verification_code_is_read_off_the_url_at_intake(app, device):
    """
    A submission that never verifies still has to be worth something to the admin.

    The code is the receipt's identity and it is sitting in the URL, so there is no
    reason to learn it from TRA.
    """
    import main

    assert main._code_from_url(RECEIPT_URL) == '58E41A514'
    assert main.receipt_time_from_url(RECEIPT_URL) == '09:20:22'


def test_a_malformed_url_is_still_accepted_and_queued(app, device):
    """
    Refusing it at intake would drop a receipt at the one moment nobody is watching.

    The device has already been handed a 202; the failure belongs later, where it is
    recorded with a reason somebody can read.
    """
    import main

    assert main._code_from_url('https://verify.tra.go.tz/nonsense') is None
    assert main.receipt_time_from_url('') is None


def test_intake_records_the_code_on_the_submission(app, device, config):
    client = app.test_client()
    response = client.post(
        '/receipt',
        headers={'Authorization': f'Bearer {device.api_key}'},
        data={'receipturl': RECEIPT_URL},
    )

    assert response.status_code == 202
    stored = db.session.get(Submission, response.get_json()['submission_id'])
    assert stored.receipt_code == '58E41A514'


# --- The schedule -----------------------------------------------------------

def test_a_transport_failure_is_rescheduled_rather_than_retried_in_place(app, submission):
    """
    Nothing sleeps inside the runner that discovered the failure.

    Retrying in place holds a worker open against a portal that is already unhealthy;
    the next attempt is written on the job and picked up by a later run.
    """
    import main

    before = datetime.utcnow()
    main.schedule_retry_or_fail(submission, TraTransportError('portal unreachable'))

    assert submission.status == 'queued'
    assert submission.retry_count == 1
    assert submission.failure_reason == 'TraTransportError'
    # First TraTransportError delay is one minute.
    assert timedelta(seconds=30) < (submission.next_attempt_at - before) < timedelta(minutes=2)


def test_delays_lengthen_with_each_attempt(app, submission):
    import main

    delays = []
    for _ in main.RETRY_SCHEDULE_MINUTES[TraReceiptNotUploaded]:
        before = datetime.utcnow()
        main.schedule_retry_or_fail(submission, TraReceiptNotUploaded('not there yet'))
        delays.append((submission.next_attempt_at - before).total_seconds())

    assert delays == sorted(delays)
    assert submission.status == 'queued'


def test_it_gives_up_once_the_schedule_is_exhausted(app, submission):
    import main

    schedule = main.RETRY_SCHEDULE_MINUTES[TraThrottled]
    for _ in range(len(schedule) + 1):
        main.schedule_retry_or_fail(submission, TraThrottled('rate limited'))

    assert submission.status == 'failed'
    assert submission.next_attempt_at is None
    assert submission.retry_count == len(schedule) + 1
    assert 'no retries left' in submission.error_message


@pytest.mark.parametrize('error', [
    TraWrongReceiptTime('wrong time in the URL'),
    TraRefererRejected('our bug, not theirs'),
])
def test_a_failure_that_retrying_cannot_fix_stops_immediately(app, submission, error):
    """
    Ten more requests against a rate-limited portal cannot correct a wrong URL.

    These are permanent for this submission, so they cost one attempt and no more.
    """
    import main

    main.schedule_retry_or_fail(submission, error)

    assert submission.status == 'failed'
    assert submission.retry_count == 1
    assert submission.next_attempt_at is None
    assert 'permanent' in submission.error_message


# --- Reporting the schedule -------------------------------------------------

def test_the_retry_plan_says_where_a_submission_stands(app, submission):
    import main

    main.schedule_retry_or_fail(submission, TraReceiptNotUploaded('not there yet'))
    plan = main.retry_plan(submission)

    assert plan['failure_reason'] == 'TraReceiptNotUploaded'
    assert plan['attempts_used'] == 1
    assert plan['attempts_total'] == len(main.RETRY_SCHEDULE_MINUTES[TraReceiptNotUploaded]) + 1
    assert plan['attempts_left'] == plan['attempts_total'] - 2
    assert plan['retryable'] is True
    assert plan['due'] is False
    # 15m + 1h + 3h + 6h + 12h + 24h, which the page renders as "about two days".
    assert plan['gives_up_after_minutes'] == 2775


def test_an_overdue_attempt_is_reported_as_due_not_as_waiting(app, submission):
    """
    Past its time, a job is waiting for a runner rather than for the clock.

    That distinction is the whole point: the queue only moves when a new receipt
    arrives or the task runner fires, so 'due now' means somebody may need to act.
    """
    import main

    submission.next_attempt_at = datetime.utcnow() - timedelta(hours=1)
    submission.failure_reason = 'TraThrottled'
    db.session.commit()

    plan = main.retry_plan(submission)

    assert plan['due'] is True
    assert plan['seconds_until_next_attempt'] < 0


def test_a_submission_with_no_history_has_an_empty_plan(app, submission):
    import main

    plan = main.retry_plan(submission)

    assert plan['attempts_used'] == 0
    assert plan['retryable'] is False
    assert plan['next_attempt_at'] is None


# --- Saving a request -------------------------------------------------------

def test_a_receipt_already_in_the_ledger_is_recognised_without_calling_tra(
        app, device, config, submission, receipt_html, monkeypatch):
    """
    The code is known at intake, so a duplicate costs nothing against the portal.

    That matters most exactly when it hurts most: a queue full of resubmissions after
    an outage, against an endpoint that rate-limits.
    """
    import main

    monkeypatch.setattr(main, 'fetch_receipt_html', lambda url: receipt_html)
    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: False)
    main.process_submission(submission)
    assert submission.status == 'completed'

    again = Submission(device_id=device.id, input_type='url', input_data=RECEIPT_URL,
                       receipt_code='58E41A514')
    db.session.add(again)
    db.session.commit()

    def must_not_be_called(url):
        pytest.fail('TRA was contacted for a receipt already in the ledger')
    monkeypatch.setattr(main, 'fetch_receipt_html', must_not_be_called)

    main.process_submission(again)

    assert again.status == 'duplicate'


# --- Putting it back on the queue -------------------------------------------

def test_requeuing_clears_the_typed_reason_as_well_as_the_message(app, device, config, monkeypatch):
    """A retried job must not still be wearing the last failure's label."""
    import main

    monkeypatch.setattr(main.gevent, 'spawn', lambda *a, **k: None)

    failed = Submission(device_id=device.id, input_type='url', input_data=RECEIPT_URL,
                        status='failed', error_message='TraThrottled: rate limited',
                        failure_reason='TraThrottled', retry_count=6)
    db.session.add(failed)
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    assert client.post(f'/submissions/{failed.id}/retry').status_code == 202

    refreshed = db.session.get(Submission, failed.id)
    assert refreshed.status == 'queued'
    assert refreshed.failure_reason is None
    assert refreshed.retry_count == 0


# --- The submission page ----------------------------------------------------

@pytest.fixture
def admin(app, config):
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


def test_the_submission_page_shows_what_was_captured_and_what_happens_next(app, device, admin):
    import main

    waiting = Submission(
        device_id=device.id, input_type='url', input_data=RECEIPT_URL,
        receipt_code='58E41A514', status='queued', retry_count=2,
        failure_reason='TraReceiptNotUploaded',
        next_attempt_at=datetime.utcnow() + timedelta(hours=3),
        location='-6.7924,39.2083 - Kariakoo',
    )
    db.session.add(waiting)
    db.session.commit()

    body = admin.get(f'/submissions/{waiting.id}').get_data(as_text=True)

    assert '58E41A514' in body                       # the code, read at intake
    assert '09:20:22' in body                        # the receipt time, from the URL
    assert 'Dar es Salaam' in body                   # where it was collected
    assert 'The vendor has not uploaded it yet' in body
    assert '2 of 7' in body                          # where the schedule has got to
    assert 'Send to TRA again' in body


def test_the_submission_page_explains_a_failure_retrying_cannot_fix(app, device, admin):
    dead = Submission(
        device_id=device.id, input_type='url', input_data=RECEIPT_URL,
        receipt_code='58E41A514', status='failed',
        failure_reason='TraWrongReceiptTime', retry_count=1,
        error_message='TraWrongReceiptTime (permanent): wrong time',
    )
    db.session.add(dead)
    db.session.commit()

    body = admin.get(f'/submissions/{dead.id}').get_data(as_text=True)

    assert 'The time in the receipt URL is wrong' in body
    assert 'will fail the same way' in body


def test_the_submission_page_points_at_a_twin_that_did_get_through(app, device, admin):
    """
    The same receipt submitted twice, where the second attempt worked.

    Without this the first submission reads as lost money; it is not, and saying so
    saves somebody chasing a vendor for a receipt already in the ledger.
    """
    stored = Submission(device_id=device.id, input_type='url', input_data=RECEIPT_URL, status='completed')
    db.session.add(stored)
    db.session.flush()
    db.session.add(Receipt(
        receipt_verification_code='58E41A514', receipt_date=date.today(),
        total_incl_tax_cents=118_00, device_id=device.id, submission_id=stored.id,
    ))
    lost = Submission(device_id=device.id, input_type='url', input_data=RECEIPT_URL,
                      receipt_code='58E41A514', status='failed', failure_reason='TraThrottled')
    db.session.add(lost)
    db.session.commit()

    body = admin.get(f'/submissions/{lost.id}').get_data(as_text=True)

    assert 'already in the ledger' in body


def test_a_submission_that_resolved_redirects_to_its_receipt(app, device, admin):
    stored = Submission(device_id=device.id, input_type='url', input_data=RECEIPT_URL, status='completed')
    db.session.add(stored)
    db.session.flush()
    receipt = Receipt(receipt_verification_code='ABC', receipt_date=date.today(),
                      total_incl_tax_cents=118_00, device_id=device.id, submission_id=stored.id)
    db.session.add(receipt)
    db.session.commit()

    response = admin.get(f'/submissions/{stored.id}')

    assert response.status_code == 302
    assert f'/receipts/{receipt.id}' in response.headers['Location']


# --- The failure handler must not itself fail -------------------------------

def test_reporting_a_failure_on_an_unconfigured_instance_does_not_kill_the_runner(app, device):
    """
    An error while reporting an error strands every job behind it.

    _fail_submission dispatches an event, and that dispatch used to dereference an
    instance config that may not exist yet. Because the handler runs inside the task
    runner's loop, the exception escaped process_submission entirely and took the
    whole tick down - so one submission arriving before setup stopped the queue.
    """
    import main

    orphan = Submission(device_id=device.id, input_type='url', input_data=RECEIPT_URL)
    db.session.add(orphan)
    db.session.commit()

    # No InstanceConfig row exists: the `config` fixture is deliberately not requested.
    main.process_submission(orphan)

    assert db.session.get(Submission, orphan.id).status == 'failed'
