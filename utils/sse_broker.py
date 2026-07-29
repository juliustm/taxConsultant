# utils/sse_broker.py
"""
The live-update channel: how a receipt changing state reaches a screen.

This used to be an in-memory fan-out - a list of queues, one per open connection, each
event pushed to whoever happened to be attached at that exact instant. Everything else
lost the event outright: a dashboard whose connection had dropped and not yet
reconnected, a phone whose screen was off, a browser that had been in a background tab,
a listener whose queue had filled and been dropped from the list. All of them stayed
silently wrong until somebody reloaded the page, which is exactly the symptom this
module was rewritten to remove.

So events are now written down first (models.EventLog) and read back by id. Every
listener carries a cursor, reconnects with it (`Last-Event-ID` for EventSource, `since`
for the scanner's fetch-based reader) and is told what it missed, rather than being
started from "now" with an unnoticed gap behind it. A cursor so old that the log no
longer reaches back that far gets an explicit `stream.resync` instead of a quiet gap -
the client reloads from the API and is correct again either way.

The in-memory part that remains is only a doorbell: a listener waits on an Event so a
publish in the same process wakes it immediately instead of waiting out its poll. If
that doorbell is never rung - another process published, the wake-up was missed - the
poll still finds the row within POLL_SECONDS. Nothing is only ever delivered by the
doorbell.
"""
import json
import threading
import time
from datetime import datetime, timedelta

from models.user import db, EventLog

# How long a listener may hear nothing at all before we send a comment line. Idle
# connections are killed by proxies (and by mobile networks) somewhere around 60s, and
# a dead-but-not-closed connection is the one failure a client cannot detect, so this
# is deliberately well inside that.
HEARTBEAT_SECONDS = 15

# The floor on how late an event can be when the doorbell does not ring - a publish
# from another worker process, say. Not tuned lower because it does not need to be: in
# the normal case the doorbell delivers in milliseconds and this never fires. What it
# does cost is a database connection per listener per tick, and on SQLite that is a
# small pool shared with every request the app is actually serving.
POLL_SECONDS = 2.0

# Handed to the browser as the EventSource reconnect delay. The default is 3s in most
# browsers anyway; sending it makes it ours to change rather than the vendor's.
RETRY_MS = 3000

# Rows per read. A phone catching up on a long absence gets them in batches rather than
# building one enormous string in memory.
BATCH_SIZE = 100

# The catch-up window. Kept in both dimensions on purpose: the age limit is what makes
# this a buffer rather than an audit trail, and the row floor is what stops a quiet
# instance from trimming away the very events a reconnecting client still needs.
TRIM_AFTER_MINUTES = 120
TRIM_KEEP_ROWS = 400
# Trimming on every publish would double the write cost of every event for no benefit.
TRIM_EVERY = 50


def format_frame(event_id, payload):
    """
    One SSE frame.

    The `id:` line is the whole point: it is what the browser sends back as
    Last-Event-ID after a reconnect, and what the scanner stores to reconnect with.
    Without it a reconnect silently means "start from now".
    """
    return f"id: {event_id}\ndata: {payload}\n\n"


def heartbeat_frame():
    """
    The idle keep-alive.

    A named event rather than the conventional `: comment` line, because a comment is
    invisible to the client: EventSource does not fire anything for one, so a page has
    no way to tell a healthy quiet connection from a connection that died silently
    somewhere between here and the browser - and that second case is not hypothetical,
    it is what a proxy timing out an idle stream looks like from the inside. Named, so
    it lands on its own listener and never reaches an `onmessage` handler that would
    have to learn to ignore it. It carries no id: it is not an event in the log, and
    must not move a cursor.
    """
    return "event: ping\ndata: {}\n\n"


def resync_frame(event_id):
    """
    Told to a listener whose cursor has fallen off the back of the log.

    Carries no data because there is nothing honest to put in it - we genuinely do not
    know what was missed. The client's job on seeing this is to reload from the API,
    which is the same thing it does on a cold start.
    """
    payload = json.dumps({'event_type': 'stream.resync', 'data': {}})
    return format_frame(event_id, payload)


class EventBus:
    """
    Writes events to the log and wakes anyone waiting on one.

    One instance, module-level. It holds no per-listener state beyond the wake-up
    Events themselves, so a listener that dies without cleaning up costs nothing - the
    thing that used to leak (a queue per connection, removed only on a tidy
    GeneratorExit) no longer exists.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._waiters = set()
        self._since_trim = 0

    # --- publishing -------------------------------------------------------------

    def publish(self, event_type, payload):
        """
        Records one event and returns its id, or None if it could not be recorded.

        Never raises. This is called from the task runner's failure handler among other
        places, and an exception here would be an error raised while reporting an error
        - taking down the tick, and with it every job queued behind it.

        Every caller commits its own work before announcing, so committing here cannot
        publish somebody's half-finished transaction. That ordering is deliberate:
        an event that names a submission the database does not yet hold would be read
        by a client that then fetches nothing.
        """
        payload = payload or {}
        envelope = json.dumps({'event_type': event_type, 'data': payload}, default=str)

        device_id = payload.get('device_id')
        if not isinstance(device_id, int):
            device_id = None

        row = EventLog(event_type=event_type, device_id=device_id, payload=envelope)
        try:
            db.session.add(row)
            db.session.commit()
        except Exception as exc:   # pragma: no cover - a broken DB fails the caller elsewhere
            db.session.rollback()
            print(f"[SSE] Could not record {event_type}: {exc}")
            return None

        self._maybe_trim()
        self.wake()
        return row.id

    def _maybe_trim(self):
        self._since_trim += 1
        if self._since_trim < TRIM_EVERY:
            return
        self._since_trim = 0
        try:
            floor = db.session.query(db.func.max(EventLog.id)).scalar()
            if floor is None:
                return
            db.session.query(EventLog).filter(
                EventLog.id <= floor - TRIM_KEEP_ROWS,
                EventLog.created_at < datetime.utcnow() - timedelta(minutes=TRIM_AFTER_MINUTES),
            ).delete(synchronize_session=False)
            db.session.commit()
        except Exception as exc:   # pragma: no cover
            db.session.rollback()
            print(f"[SSE] Could not trim the event log: {exc}")

    # --- the doorbell -----------------------------------------------------------

    def wake(self):
        with self._lock:
            waiters = list(self._waiters)
        for waiter in waiters:
            waiter.set()

    def wait(self, timeout):
        """Blocks until the next publish in this process, or `timeout` seconds."""
        waiter = threading.Event()
        with self._lock:
            self._waiters.add(waiter)
        try:
            return waiter.wait(timeout)
        finally:
            with self._lock:
                self._waiters.discard(waiter)


event_bus = EventBus()


# --- reading ---------------------------------------------------------------------

def head():
    """The newest event id, or 0 on an empty log. Where a fresh listener starts."""
    # Rolled back first for the same reason read_after does - see there.
    db.session.rollback()
    return db.session.query(db.func.max(EventLog.id)).scalar() or 0


def oldest():
    """The oldest id still held, or 0. Anything at or below it has been trimmed."""
    db.session.rollback()
    return db.session.query(db.func.min(EventLog.id)).scalar() or 0


def read_after(cursor, device_id=None, limit=BATCH_SIZE):
    """
    Events after `cursor`, oldest first, as (frames, new_cursor).

    `device_id` is an exact match, never a "no device set means everyone" fallback: a
    device stream must show one device its own receipts and nothing else, and an event
    that forgot to carry a device_id has to fail closed rather than be broadcast to
    every phone in the field.

    The rollback is not decoration. A long-lived generator reads through one session
    for hours, and on SQLite that session's first SELECT opens a read transaction whose
    snapshot never advances - so without this the second poll, and every poll after it,
    would keep returning the same rows the first one saw and no event would ever be
    delivered.
    """
    db.session.rollback()
    query = db.session.query(EventLog).filter(EventLog.id > cursor)
    if device_id is not None:
        query = query.filter(EventLog.device_id == device_id)
    rows = query.order_by(EventLog.id.asc()).limit(limit).all()
    if not rows:
        return [], cursor
    return [format_frame(row.id, row.payload) for row in rows], rows[-1].id


def stream(cursor=None, device_id=None, heartbeat=HEARTBEAT_SECONDS,
           poll=POLL_SECONDS, idle_limit=None):
    """
    An open SSE connection, as a generator of frames.

    Yields for as long as the client keeps reading; the caller is the WSGI server,
    which closes the generator when the socket goes away. `idle_limit` bounds the
    number of idle waits before returning, which is what makes this testable - in
    production it is None and the loop is the connection's whole lifetime.

    A cursor pointing at events the log no longer holds gets a resync rather than a
    silent jump to the present: see resync_frame.
    """
    yield f"retry: {RETRY_MS}\n\n"

    if cursor is None:
        cursor = head()
    elif cursor < oldest() - 1:
        cursor = head()
        yield resync_frame(cursor)

    idle_waits = 0
    last_frame_at = time.monotonic()

    while True:
        frames, cursor = read_after(cursor, device_id)
        if frames:
            idle_waits = 0
            last_frame_at = time.monotonic()
            for frame in frames:
                yield frame
            # A full batch means there is probably more waiting; go straight back for
            # it rather than sleeping between pages of a catch-up.
            if len(frames) == BATCH_SIZE:
                continue

        if idle_limit is not None and idle_waits >= idle_limit:
            return

        idle_waits += 1
        event_bus.wait(poll)

        if time.monotonic() - last_frame_at >= heartbeat:
            last_frame_at = time.monotonic()
            yield heartbeat_frame()


def parse_cursor(request):
    """
    Where this listener wants to start, or None for "from now".

    Two spellings for the same thing because the two clients cannot share one: the
    dashboard uses EventSource, which resends its own Last-Event-ID header and cannot
    be given a query string on reconnect; the scanner reads the stream with fetch() and
    has to pass ?since= itself. A value that is not a number is treated as absent -
    a garbled cursor should cost the events since it was issued, not the connection.
    """
    raw = request.headers.get('Last-Event-ID') or request.args.get('since')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(value, 0)
