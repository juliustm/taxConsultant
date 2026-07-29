# tests/test_device_stream.py
"""
The scanner's push channel.

/scan/api/stream reuses the same global announcer the admin dashboard's /stream runs
on, which broadcasts identically to every listener - so the one thing this endpoint
must never get wrong is _sse_line_for_device, the filter that keeps a device to only
its own events. That is what is under test here, directly and exhaustively: the route
itself is a thin, untestable-by-design wrapper (it holds a live connection open on a
blocking Queue.get(), the same as the admin /stream, which is why neither has ever
been driven through the synchronous test client - doing so blocks for the announcer's
full 30s keep-alive interval before returning anything).
"""
import json

from main import _sse_line_for_device


def data_line(**fields):
    return f"data: {json.dumps(fields)}\n\n"


def test_a_matching_device_id_passes_through():
    line = data_line(event_type='submission.processed', data={'device_id': 7, 'status': 'completed'})
    assert _sse_line_for_device(line, 7) == line


def test_a_different_device_id_is_dropped():
    line = data_line(event_type='submission.processed', data={'device_id': 7, 'status': 'completed'})
    assert _sse_line_for_device(line, 8) is None


def test_a_keepalive_comment_always_passes_through():
    assert _sse_line_for_device(': keep-alive\n\n', 7) == ': keep-alive\n\n'
    assert _sse_line_for_device(': keep-alive\n\n', None) == ': keep-alive\n\n'


def test_a_line_with_no_device_id_is_dropped_not_broadcast():
    """An event that predates this filter (or a bug that forgets device_id) must fail
    closed - silently dropped, never shown to a device it does not belong to."""
    line = data_line(event_type='submission.queued', data={'status': 'queued'})
    assert _sse_line_for_device(line, 7) is None


def test_malformed_json_is_dropped_without_raising():
    assert _sse_line_for_device('data: not json\n\n', 7) is None


def test_a_line_with_no_data_key_is_dropped():
    line = 'data: {"event_type": "submission.queued"}\n\n'
    assert _sse_line_for_device(line, 7) is None


# --- Route wiring ---------------------------------------------------------------

def test_the_stream_is_not_reachable_without_a_session(app):
    """
    device_required must reject before the view ever opens a connection to the
    announcer - checked here rather than with the full 200 path, because a genuinely
    open stream blocks the synchronous test client on the announcer's keep-alive
    timeout (see the module docstring).
    """
    client = app.test_client()
    response = client.get('/scan/api/stream')
    assert response.status_code == 401
