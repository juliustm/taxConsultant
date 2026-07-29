# tests/test_device_stream.py
"""
The live-update channel.

Two things have to hold, and neither used to be testable. The first is isolation: a
device's stream must carry that device's events and nothing else, ever. The second is
that an event survives nobody being connected at the moment it happens - the bug that
made this whole channel feel broken, where a receipt processed while a dashboard was
reconnecting simply never appeared until someone reloaded the page.

Both are testable now because the channel is a log with cursors rather than a queue
that only pushes: `stream(idle_limit=...)` reads what is there and returns, instead of
blocking a synchronous test client for its full keep-alive interval.
"""
import json

from models.user import db, Device, EventLog
from utils import sse_broker


def payloads(frames):
    """The JSON envelopes out of a list of SSE frames, ignoring retry/comment lines."""
    out = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith('data: '):
                out.append(json.loads(line[len('data: '):]))
    return out


def ids(frames):
    return [
        int(line[len('id: '):])
        for frame in frames for line in frame.splitlines() if line.startswith('id: ')
    ]


# --- The log ---------------------------------------------------------------------

def test_publish_records_the_event_and_returns_its_id(app):
    first = sse_broker.event_bus.publish('submission.queued', {'submission_id': 1, 'device_id': 7})
    second = sse_broker.event_bus.publish('submission.processed', {'submission_id': 1, 'device_id': 7})

    assert second > first
    assert db.session.query(EventLog).count() == 2


def test_events_are_read_back_in_order_after_a_cursor(app):
    first = sse_broker.event_bus.publish('submission.queued', {'submission_id': 1, 'device_id': 7})
    sse_broker.event_bus.publish('submission.processed', {'submission_id': 1, 'device_id': 7})

    frames, cursor = sse_broker.read_after(first, device_id=7)

    assert [p['event_type'] for p in payloads(frames)] == ['submission.processed']
    assert cursor > first


def test_a_reconnecting_listener_is_told_what_it_missed(app):
    """The whole point of the log: nobody was connected when this happened."""
    sse_broker.event_bus.publish('submission.processed', {'submission_id': 1, 'device_id': 7})
    sse_broker.event_bus.publish('submission.failed', {'submission_id': 2, 'device_id': 7})

    frames = list(sse_broker.stream(cursor=0, device_id=7, idle_limit=0))

    assert [p['event_type'] for p in payloads(frames)] == ['submission.processed', 'submission.failed']


def test_a_fresh_listener_starts_at_the_head_and_gets_no_backlog(app):
    sse_broker.event_bus.publish('submission.processed', {'submission_id': 1, 'device_id': 7})

    frames = list(sse_broker.stream(device_id=7, idle_limit=0))

    assert payloads(frames) == []
    assert frames[0].startswith('retry: ')


def test_every_frame_carries_an_id_so_a_reconnect_can_resume(app):
    sse_broker.event_bus.publish('submission.processed', {'submission_id': 1, 'device_id': 7})

    frames = list(sse_broker.stream(cursor=0, device_id=7, idle_limit=0))

    assert ids(frames) == [db.session.query(db.func.max(EventLog.id)).scalar()]


def test_a_cursor_older_than_the_log_asks_the_client_to_resync(app):
    """
    A gap has to be admitted, not papered over. Silently resuming at the head is how a
    phone that was asleep for an hour ends up showing a receipt as still 'Checking'.
    """
    for n in (1, 2, 3):
        sse_broker.event_bus.publish('submission.processed', {'submission_id': n, 'device_id': 7})
    # Everything the client is behind has since been trimmed off the back of the log.
    db.session.query(EventLog).filter(EventLog.id < 3).delete()
    db.session.commit()

    frames = list(sse_broker.stream(cursor=1, device_id=7, idle_limit=0))

    assert [p['event_type'] for p in payloads(frames)] == ['stream.resync']


# --- Isolation -------------------------------------------------------------------

def test_another_devices_events_are_never_read(app):
    sse_broker.event_bus.publish('submission.processed', {'submission_id': 1, 'device_id': 8})

    frames, _ = sse_broker.read_after(0, device_id=7)

    assert frames == []


def test_an_event_with_no_device_fails_closed(app):
    """An event that forgets device_id must reach no device stream, not all of them."""
    sse_broker.event_bus.publish('submission.queued', {'submission_id': 1})

    frames, _ = sse_broker.read_after(0, device_id=7)

    assert frames == []


def test_a_non_integer_device_id_is_not_stored_as_a_match(app):
    sse_broker.event_bus.publish('submission.queued', {'submission_id': 1, 'device_id': 'seven'})

    assert db.session.query(EventLog).one().device_id is None


def test_the_admin_stream_is_unfiltered(app):
    """The dashboard sees every device - that is what makes it the dashboard."""
    sse_broker.event_bus.publish('submission.processed', {'submission_id': 1, 'device_id': 7})
    sse_broker.event_bus.publish('submission.processed', {'submission_id': 2, 'device_id': 8})

    frames, _ = sse_broker.read_after(0)

    assert len(payloads(frames)) == 2


# --- Cursors ---------------------------------------------------------------------

def test_the_cursor_is_read_from_either_spelling(app):
    with app.test_request_context('/stream', headers={'Last-Event-ID': '42'}):
        from flask import request
        assert sse_broker.parse_cursor(request) == 42

    with app.test_request_context('/scan/api/stream?since=42'):
        from flask import request
        assert sse_broker.parse_cursor(request) == 42


def test_a_missing_or_garbled_cursor_means_from_now(app):
    from flask import request

    with app.test_request_context('/stream'):
        assert sse_broker.parse_cursor(request) is None

    with app.test_request_context('/stream?since=tomorrow'):
        assert sse_broker.parse_cursor(request) is None


# --- Route wiring ----------------------------------------------------------------

def test_the_stream_is_not_reachable_without_a_session(app):
    client = app.test_client()
    response = client.get('/scan/api/stream')
    assert response.status_code == 401


def test_the_stream_sends_the_headers_a_proxy_needs(app, device):
    """
    Without these a buffering proxy holds the whole stream back until it ends - which
    for a stream that never ends means a connection that is open, healthy and silent.
    """
    from utils.device_auth import issue_enrolment_token, consume_enrolment_token

    token = issue_enrolment_token(device)
    db.session.commit()
    session_token, _ = consume_enrolment_token(token, user_agent='tests')
    db.session.commit()

    client = app.test_client()
    response = client.get('/scan/api/stream?since=0',
                          headers={'Authorization': f'Bearer {session_token}'})
    try:
        assert response.status_code == 200
        assert response.headers['Content-Type'].startswith('text/event-stream')
        assert 'no-cache' in response.headers['Cache-Control']
        assert response.headers['X-Accel-Buffering'] == 'no'
    finally:
        response.close()
