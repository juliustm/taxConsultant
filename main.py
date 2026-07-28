# main.py
import os, time, json, csv, io, pyotp, requests, gevent
from functools import wraps
from datetime import datetime, timedelta, date
from werkzeug.utils import secure_filename

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session, current_app, send_from_directory, Response

from config import Config
from models.user import db, InstanceConfig, Device, Receipt, ReceiptItem, ReceiptTaxLine, Submission, Vendor
from utils import analytics, compliance, geo, peek
from utils.security import generate_totp_provisioning_uri, generate_qr_code_base64
from utils.export import dispatch_event, format_currency
from utils.llm_processor import analyse_receipt, extract_receipt_details, LlmUnavailable
from utils.money import format_cents, from_cents, to_cents, to_decimal
from utils.sse_broker import announcer
from utils.tra import (
    fetch_receipt_html, parse_receipt_url, TraError, TraReceiptNotUploaded, TraThrottled,
    TraTransportError, TraUnexpectedResponse,
)
from utils.tra_parser import parse_receipt_html, TAX_CODES, TraParseError
from sqlalchemy import text as sa_text
from sqlalchemy.orm import joinedload, selectinload

app = Flask(__name__)
app.config.from_object(Config)

app.jinja_env.filters['currency'] = format_currency

# Configure the upload folder
app.config['UPLOAD_FOLDER'] = os.path.join(Config.DATA_DIR, 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)

# This function is correctly defined here, in main.py.
def get_instance_config():
    return InstanceConfig.query.first()

# Columns added to existing tables after their first release. db.create_all() builds
# new tables but never alters an existing one, and there is no migration tool in this
# project, so they are added by hand on boot.
PENDING_COLUMNS = {
    'instance_config': (
        ('business_name', 'VARCHAR(200)'),
        ('business_tin', 'VARCHAR(50)'),
        ('business_vrn', 'VARCHAR(50)'),
    ),
    'submission': (
        ('next_attempt_at', 'DATETIME'),
        ('claimed_at', 'DATETIME'),
        ('failure_reason', 'VARCHAR(50)'),
        ('receipt_code', 'VARCHAR(50)'),
    ),
    'receipt': (
        ('vendor_id', 'INTEGER'),
        ('tax_office', 'VARCHAR(200)'),
        ('z_number', 'VARCHAR(50)'),
        ('efd_serial', 'VARCHAR(100)'),
        ('customer_mobile', 'VARCHAR(50)'),
        ('receipt_time', 'TIME'),
        ('total_incl_tax_cents', 'BIGINT'),
        ('total_excl_tax_cents', 'BIGINT'),
        ('total_tax_cents', 'BIGINT'),
        ('discount_cents', 'BIGINT'),
        ('is_cancelled', 'BOOLEAN NOT NULL DEFAULT 0'),
        ('is_test', 'BOOLEAN NOT NULL DEFAULT 0'),
        ('extraction_source', 'VARCHAR(20)'),
        ('source_html', 'TEXT'),
        ('category', 'VARCHAR(50)'),
        ('llm_status', 'VARCHAR(20)'),
    ),
}

# Indexes on columns added above. create_all() only indexes tables it creates.
PENDING_INDEXES = (
    ('ix_submission_next_attempt_at', 'submission', 'next_attempt_at'),
    ('ix_submission_receipt_code', 'submission', 'receipt_code'),
    ('ix_receipt_receipt_date', 'receipt', 'receipt_date'),
    ('ix_receipt_vendor_id', 'receipt', 'vendor_id'),
    ('ix_receipt_vendor_tin', 'receipt', 'vendor_tin'),
    ('ix_receipt_is_cancelled', 'receipt', 'is_cancelled'),
    ('ix_receipt_is_test', 'receipt', 'is_test'),
    ('ix_receipt_category', 'receipt', 'category'),
)

def _table_columns(table):
    return {row[1] for row in db.session.execute(sa_text(f"PRAGMA table_info({table})"))}

def apply_pending_migrations():
    """
    Brings an existing database up to the current schema.

    Adds the columns and indexes listed above, then backfills the two things that
    cannot be expressed as a default: money that used to be stored as a float, and
    the vendor rows that receipts now group by.
    """
    columns_by_table = {}
    for table, columns in PENDING_COLUMNS.items():
        existing = _table_columns(table)
        if not existing:
            continue  # Fresh database; create_all() already built the current schema.
        columns_by_table[table] = existing

        for column, ddl in columns:
            if column not in existing:
                print(f"[Migration] Adding {table}.{column}")
                db.session.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

    for name, table, column in PENDING_INDEXES:
        db.session.execute(sa_text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})"))
    db.session.commit()

    _backfill_money(columns_by_table.get('receipt', set()))
    _backfill_vendors()

def _backfill_money(receipt_columns):
    """
    Copies the legacy Float amounts into their integer-cent columns.

    The old total_amount/vat_amount columns are left in place - SQLite makes dropping
    a column awkward and they are harmless once nothing writes them - but they are no
    longer on the model, so this runs once and the floats are never read again.
    """
    for legacy, cents in (('total_amount', 'total_incl_tax_cents'), ('vat_amount', 'total_tax_cents')):
        if legacy not in receipt_columns:
            continue
        result = db.session.execute(sa_text(
            f"UPDATE receipt SET {cents} = CAST(ROUND({legacy} * 100) AS INTEGER) "
            f"WHERE {cents} IS NULL AND {legacy} IS NOT NULL"
        ))
        if result.rowcount:
            print(f"[Migration] Converted {result.rowcount} receipt.{legacy} values to cents.")
    db.session.commit()

def _backfill_vendors():
    """Attaches existing receipts to a Vendor, keyed on TIN where they carry one."""
    orphans = Receipt.query.filter(Receipt.vendor_id.is_(None)).all()
    if not orphans:
        return

    attached = 0
    for receipt in orphans:
        vendor = Vendor.upsert(
            tin=receipt.vendor_tin, name=receipt.vendor_name,
            vrn=receipt.vrn, phone=receipt.vendor_phone, tax_office=receipt.tax_office,
        )
        if vendor is not None:
            receipt.vendor = vendor
            attached += 1

    db.session.commit()
    print(f"[Migration] Attached {attached} receipt(s) to {Vendor.query.count()} vendor(s).")

# Create database tables and seed with dummy data for demo
with app.app_context():
    db.create_all()
    apply_pending_migrations()

# --- JOB PROCESSING LOGIC ---

# How long a job may sit in 'processing' before another runner may reclaim it.
JOB_LEASE_MINUTES = 10
# Ceiling on jobs handled per task-runner tick, so one tick cannot monopolise the
# worker or hammer TRA; cron-job.org calls the runner again five minutes later.
MAX_JOBS_PER_RUN = 25
# Spacing between consecutive TRA fetches. The portal throttles at roughly eight
# rapid requests from one IP.
TRA_REQUEST_SPACING_SECONDS = 3

# Retry delays in minutes, indexed by attempt number. Retries are recorded on the
# submission (next_attempt_at) and picked up by a later tick - never slept through
# inside the runner that discovered the failure.
RETRY_SCHEDULE_MINUTES = {
    # Vendors upload in batches, sometimes hours after issuing the receipt.
    TraReceiptNotUploaded: [15, 60, 180, 360, 720, 1440],
    TraThrottled: [2, 5, 15, 45, 120],
    TraTransportError: [1, 5, 15, 45, 120],
    TraUnexpectedResponse: [5, 30, 120],
}

# What each typed failure means and what the admin can do about it. Defined once, on
# the server, and handed to the dashboard - the alternative is the same table written
# out again in JavaScript, where it drifts silently the first time a class is renamed.
FAILURE_GUIDANCE = {
    'TraReceiptNotUploaded': {
        'title': 'The vendor has not uploaded it yet',
        'detail': 'TRA has no record of this receipt. Vendors upload in batches, often hours '
                  'after issuing it, so this usually resolves on its own.',
        'action': 'Retry after a day. If it never appears, the vendor may not have declared the sale.',
    },
    'TraThrottled': {
        'title': 'TRA is rate-limiting us',
        'detail': 'The portal answered but refused the request as too frequent.',
        'action': 'Nothing to do; attempts are already spaced out and will resume automatically.',
    },
    'TraTransportError': {
        'title': 'Could not reach TRA',
        'detail': "The portal's TLS endpoint is intermittently unhealthy. Each attempt already "
                  'redials several times before giving up on that attempt.',
        'action': 'Retry later. Repeated failures across hours usually mean a portal outage.',
    },
    'TraUnexpectedResponse': {
        'title': 'TRA served something unrecognisable',
        'detail': 'The portal responded, but not with a page this parser knows.',
        'action': 'Worth checking the URL by hand; if the portal has changed, the parser needs updating.',
    },
    'TraWrongReceiptTime': {
        'title': 'The time in the receipt URL is wrong',
        'detail': 'The portal asked for the receipt time again, which means the six digits after '
                  'the code do not match the receipt. Retrying sends the same wrong time.',
        'action': 'Re-read the time printed on the receipt and submit the corrected URL.',
    },
    'TraRefererRejected': {
        'title': 'The portal rejected our request',
        'detail': 'TRA served its landing page instead of the receipt. This is a bug on our side, '
                  'not an outage.',
        'action': 'Report it; retrying will not help until the client is fixed.',
    },
    'TraParseError': {
        'title': 'The receipt page could not be read',
        'detail': 'TRA served a page that does not parse. Retrying fetches the same page, so this '
                  'never fixes itself - and guessing the numbers is what this pipeline exists to avoid.',
        'action': 'The stored page needs a look; the parser probably needs updating.',
    },
}

def safe_serialize(obj):
    """Safely serialize SQLAlchemy objects for JSON, handling dates."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)

def _as_float(cents):
    """Display value for the browser. Sums are done on the *_cents fields, not these."""
    amount = from_cents(cents)
    return None if amount is None else float(amount)

def assess_receipt(receipt, config=None):
    """
    Runs the compliance checks for a receipt against this instance's own registration.

    Computed on read rather than stored: the checks are pure and cheap, and two of them
    (the claim window, and whether the buyer TIN matches) change answer as the calendar
    moves or as the business's own TIN is filled in. A stored verdict would be stale
    the day after it was written.
    """
    if receipt is None:
        return None

    config = config if config is not None else get_instance_config()
    return compliance.evaluate(
        receipt,
        business_tin=getattr(config, 'business_tin', None),
        business_vrn=getattr(config, 'business_vrn', None),
    )

def find_possible_duplicates(receipt, limit=5):
    """
    Other receipts that look like the same purchase recorded twice.

    Dedup on the verification code catches the same receipt submitted twice, because
    the code is the receipt's primary key. It cannot catch the same purchase submitted
    once as a photograph and once as a TRA link: the photo carries no code to match on,
    so both are stored and the expense is counted twice.

    What that actually looks like is the same supplier, the same day, the same total -
    which is a query, not a guess. Two genuinely separate purchases can match, so this
    reports candidates and never merges anything.
    """
    if receipt.receipt_date is None or receipt.total_incl_tax_cents is None:
        return []

    query = Receipt.query.filter(
        Receipt.id != receipt.id,
        Receipt.receipt_date == receipt.receipt_date,
        Receipt.total_incl_tax_cents == receipt.total_incl_tax_cents,
    )
    # Matched on the vendor row where there is one, and on the printed TIN otherwise,
    # so a photographed receipt that never got a Vendor still finds its twin.
    if receipt.vendor_id:
        query = query.filter(Receipt.vendor_id == receipt.vendor_id)
    elif receipt.vendor_tin:
        query = query.filter(Receipt.vendor_tin == receipt.vendor_tin)
    else:
        return []

    return query.order_by(Receipt.id.asc()).limit(limit).all()

def receipt_to_dict(receipt, config=None, detailed=True):
    """
    The canonical JSON view of a stored receipt.

    Shared by the dashboard bootstrap, the SSE payload and the webhook/sheet exports,
    so every consumer sees the same shape. Amounts appear twice: as cents, which is
    what anything adding them up must use, and as a float for display only.
    """
    if receipt is None:
        return {}

    judgment = json.loads(receipt.raw_llm_response) if receipt.raw_llm_response else {}
    assessment = assess_receipt(receipt, config)
    return {
        # The receipt's own id, so anything holding this payload can link straight to
        # /receipts/<id> without a second lookup.
        "receipt_id": receipt.id,
        "vendor_name": receipt.vendor_name,
        "vendor_tin": receipt.vendor_tin,
        "vendor_key": receipt.vendor.lookup_key if receipt.vendor else None,
        "vendor_phone": receipt.vendor_phone,
        "vrn": receipt.vrn,
        "tax_office": receipt.tax_office,
        "efd_serial": receipt.efd_serial,
        "z_number": receipt.z_number,
        "uin": receipt.uin,
        "receipt_number": receipt.receipt_number,
        "receipt_verification_code": receipt.receipt_verification_code,
        "customer_name": receipt.customer_name,
        "customer_id_type": receipt.customer_id_type,
        "customer_id": receipt.customer_id,
        "receipt_date": receipt.receipt_date.strftime('%Y-%m-%d') if receipt.receipt_date else None,
        "receipt_time": receipt.receipt_time.strftime('%H:%M:%S') if receipt.receipt_time else None,
        "total_amount": _as_float(receipt.total_incl_tax_cents),
        "total_amount_cents": receipt.total_incl_tax_cents,
        "total_excl_tax": _as_float(receipt.total_excl_tax_cents),
        "vat_amount": _as_float(receipt.total_tax_cents),
        "vat_amount_cents": receipt.total_tax_cents,
        "discount": _as_float(receipt.discount_cents),
        "is_cancelled": receipt.is_cancelled,
        "is_test": receipt.is_test,
        "is_expense": receipt.is_expense,
        "category": receipt.category,
        "extraction_source": receipt.extraction_source,
        "llm_status": receipt.llm_status,
        "items": [
            {
                "line_number": item.line_number, "description": item.description,
                "quantity": float(item.quantity) if item.quantity is not None else None,
                "amount": _as_float(item.amount_cents), "tax_code": item.tax_code,
            }
            for item in receipt.items
        ],
        "tax_lines": [
            {
                "code": line.code,
                "rate": float(line.rate) if line.rate is not None else None,
                "amount": _as_float(line.amount_cents),
            }
            for line in receipt.tax_lines
        ],
        "llm_extracted_description": judgment.get('llm_extracted_description'),
        "llm_tax_analysis": judgment.get('llm_tax_analysis'),
        "raw_llm_response": judgment,
        # The deterministic verdict: what is claimable, what is wrong with the receipt
        # and how long is left to act on it. See utils/compliance.
        "assessment": assessment.as_dict(detailed=detailed) if assessment else None,
    }

# How many rows the table asks for at a time, and the ceiling a caller may request.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# What each tab means, as a filter on the submission's status. Defined once and used
# by the table, the counts above it and the CSV export, so a receipt cannot appear
# under 'Processed' and be missing from the processed export.
TAB_FILTERS = {
    'processed': lambda: (Submission.status == 'completed',),
    'with_vat': lambda: (Submission.status == 'completed', Receipt.total_tax_cents > 0),
    'queued': lambda: (Submission.status.in_(('queued', 'processing')),),
    'failed': lambda: (Submission.status == 'failed',),
    'duplicates': lambda: (Submission.status == 'duplicate',),
    'all': lambda: (),
}

# Sortable columns, mapped to what they actually order by. Anything not in here is
# ignored rather than interpolated into the query.
SORT_COLUMNS = {
    'received_at': Submission.received_at,
    'status': Submission.status,
    'vendor_name': Receipt.vendor_name,
    'total_amount': Receipt.total_incl_tax_cents,
    'receipt_date': Receipt.receipt_date,
}

def _read_filters(args):
    """The table's filter state, read from a query string and normalised."""
    tab = args.get('tab', 'processed')
    sort = args.get('sort', 'received_at')
    return {
        'tab': tab if tab in TAB_FILTERS else 'processed',
        'search': (args.get('search') or '').strip(),
        # Set by clicking a category anywhere it is shown, so 'what else is fuel?' is
        # one click rather than a search that would also match a vendor called Fuel.
        'category': (args.get('category') or '').strip(),
        'start_date': args.get('start_date', ''),
        'end_date': args.get('end_date', ''),
        'sort': sort if sort in SORT_COLUMNS else 'received_at',
        'direction': 'asc' if args.get('direction') == 'asc' else 'desc',
    }

def _read_page(args):
    try:
        return max(1, int(args.get('page', 1)))
    except (TypeError, ValueError):
        return 1

def _read_page_size(args):
    try:
        return min(MAX_PAGE_SIZE, max(1, int(args.get('per_page', DEFAULT_PAGE_SIZE))))
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE

def _parse_date_arg(value):
    """A YYYY-MM-DD string from the query string, or None if it is neither."""
    try:
        return datetime.strptime(value, '%Y-%m-%d').date() if value else None
    except (TypeError, ValueError):
        return None

def _filtered_submissions(filters, ordered=True):
    """
    The submission query behind the table, before paging.

    Joined to Receipt rather than filtered in Python: the table is sorted by vendor and
    by amount, which live on the receipt, and searched across both. `ordered` is turned
    off by the callers that aggregate, where an ORDER BY on a column outside the
    GROUP BY is at best wasted work.
    """
    query = Submission.query.outerjoin(Receipt, Receipt.submission_id == Submission.id)

    for condition in TAB_FILTERS[filters['tab']]():
        query = query.filter(condition)

    search = filters['search']
    if search:
        # Matched against what someone would actually type: who it was from, what it
        # was for, or an identifier off the receipt itself.
        pattern = f'%{search}%'
        query = query.filter(db.or_(
            Receipt.vendor_name.ilike(pattern),
            Receipt.vendor_tin.ilike(pattern),
            Receipt.receipt_verification_code.ilike(pattern),
            Submission.description.ilike(pattern),
        ))

    if filters.get('category'):
        query = query.filter(Receipt.category == filters['category'])

    start_date = _parse_date_arg(filters['start_date'])
    end_date = _parse_date_arg(filters['end_date'])
    if start_date:
        query = query.filter(Receipt.receipt_date >= start_date)
    if end_date:
        query = query.filter(Receipt.receipt_date <= end_date)

    if not ordered:
        return query

    column = SORT_COLUMNS[filters['sort']]
    ordering = column.asc() if filters['direction'] == 'asc' else column.desc()
    # Tie-broken on the primary key so paging is stable: without it, rows with equal
    # sort values can reappear on the next page or be skipped entirely.
    return query.order_by(ordering, Submission.id.desc())

def _submissions_page(filters, page, per_page=DEFAULT_PAGE_SIZE):
    """One page of the table, plus the totals for everything the filter matches."""
    query = _filtered_submissions(filters).options(
        joinedload(Submission.receipt).selectinload(Receipt.items),
        joinedload(Submission.receipt).selectinload(Receipt.tax_lines),
        joinedload(Submission.receipt).joinedload(Receipt.vendor),
        joinedload(Submission.device),
    )
    paged = query.paginate(page=page, per_page=per_page, error_out=False)

    return {
        # The table shows a score and the ids of what failed; the wording behind each
        # check lives on /receipts/<id>, which renders it server-side.
        'submissions': prepare_submissions_for_frontend(paged.items, detailed=False),
        'page': paged.page,
        'pages': paged.pages,
        'per_page': paged.per_page,
        'total': paged.total,
        'has_next': paged.has_next,
        'has_prev': paged.has_prev,
        'insights': _filtered_insights(filters),
        'tab_counts': _tab_counts(filters),
        'filters': filters,
    }

def _spending_only(query):
    """Narrows a submission query to receipts that represent money actually spent."""
    return query.filter(
        Submission.status == 'completed',
        Receipt.is_cancelled.is_(False),
        Receipt.is_test.is_(False),
    )

def _filtered_insights(filters):
    """
    Totals across everything the filter matches, not just the page on screen.

    Summed by the database, so the figure does not change when you turn the page. Only
    completed, non-void receipts count: a queued submission is not yet money and a
    cancelled one never was.
    """
    base = _filtered_submissions(filters, ordered=False)

    count, total_cents, vat_cents = _spending_only(base).with_entities(
        db.func.count(Receipt.id),
        db.func.sum(Receipt.total_incl_tax_cents),
        db.func.sum(Receipt.total_tax_cents),
    ).one()

    top_vendors = (
        _spending_only(base)
        .with_entities(
            Receipt.vendor_name, Receipt.vendor_tin,
            db.func.count(Receipt.id), db.func.sum(Receipt.total_incl_tax_cents),
        )
        # Grouped on the vendor row, which is keyed on the TIN TRA issued, so one
        # supplier spelled three ways stays one supplier.
        .filter(Receipt.vendor_id.isnot(None))
        .group_by(Receipt.vendor_id)
        .order_by(db.func.sum(Receipt.total_incl_tax_cents).desc())
        .limit(3).all()
    )

    return {
        'count': count or 0,
        'total_cents': total_cents or 0,
        'vat_cents': vat_cents or 0,
        'top_vendors': [
            {
                'name': name or 'Unnamed vendor', 'tin': tin,
                'count': vendor_count, 'cents': cents or 0,
                # The same key /vendors/<key> and the hover cards use, built by the
                # rule that owns it rather than reassembled by the browser.
                'key': Vendor.make_lookup_key(tin, name),
            }
            for name, tin, vendor_count, cents in top_vendors
        ],
    }

def _tab_counts(filters):
    """
    How many submissions sit behind each tab under the current search and dates.

    Without these the tabs are unlabelled guesses - you cannot see that eleven
    submissions failed without opening Failed and finding out. Counted in two queries
    rather than one per tab, since every tab but 'With VAT' is a slice of the same
    grouping by status.
    """
    # Counted across every tab, so the search and dates apply but the tab currently
    # open does not - otherwise standing on 'Processed' would report zero failures.
    base = _filtered_submissions({**filters, 'tab': 'all'}, ordered=False)
    by_status = dict(
        base.with_entities(Submission.status, db.func.count(Submission.id))
        .group_by(Submission.status).all()
    )

    counts = {
        'processed': by_status.get('completed', 0),
        'queued': by_status.get('queued', 0) + by_status.get('processing', 0),
        'failed': by_status.get('failed', 0),
        'duplicates': by_status.get('duplicate', 0),
        'all': sum(by_status.values()),
    }
    counts['with_vat'] = base.filter(
        Submission.status == 'completed', Receipt.total_tax_cents > 0,
    ).count()
    return counts

def prepare_submissions_for_frontend(submissions, detailed=True):
    """Converts Submission objects into a JSON-serialisable list of dictionaries."""
    # Read once and passed down: every receipt is assessed against the same instance
    # config, and re-querying it per row is a query per receipt for one unchanging row.
    config = get_instance_config()

    output = []
    for sub in submissions:
        receipt_data = receipt_to_dict(sub.receipt, config, detailed=detailed)

        # Transform photo path for frontend consumption
        frontend_input_data = sub.input_data
        if sub.input_type == 'photo':
            # sub.input_data is the full path: /app/data/uploads/file.jpg
            # We create a public URL: /uploads/file.jpg
            filename = os.path.basename(sub.input_data)
            frontend_input_data = url_for('uploaded_file', filename=filename)

        data = {
            "id": sub.id, "status": sub.status, "received_at": sub.received_at.isoformat(),
            "input_type": sub.input_type, "input_data": frontend_input_data, # Use the transformed path
            "description": sub.description, "location": sub.location,
            "error_message": sub.error_message, "is_duplicate": sub.status == 'duplicate',
            "receipt": receipt_data, "device_name": sub.device.name if sub.device else 'Unknown Device',
            # What is known about a submission before - or without - a verified receipt
            # behind it, so a row that never resolves is still something to act on.
            "receipt_code": sub.receipt_code,
            "receipt_time": receipt_time_from_url(sub.input_data) if sub.input_type == 'url' else None,
            "failure_reason": sub.failure_reason,
            "retry": retry_plan(sub),
        }
        output.append(data)
    return output

def _code_from_url(url):
    """
    The verification code and time out of a submitted TRA URL, or None.

    Never raises: a malformed URL is still accepted and queued, and fails later with a
    reason the admin can read. Refusing it at intake would drop a receipt on the floor
    at the one moment nobody is watching - the device has already been handed a 202.
    """
    try:
        code, hhmmss = parse_receipt_url(url)
    except (ValueError, TypeError):
        return None
    return code


def receipt_time_from_url(url):
    """The HH:MM:SS printed on the receipt, which the submitted URL carries."""
    try:
        _, hhmmss = parse_receipt_url(url)
    except (ValueError, TypeError):
        return None
    return f'{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}'


def retry_plan(submission):
    """
    Where a submission stands in its retry schedule, in terms a human can act on.

    The policy is otherwise invisible: `next_attempt_at` is a column and the schedule
    is a dict in this module, so "when will it try again, and when does it give up"
    could only be answered by reading the source. Every field here exists to put that
    on the screen instead.
    """
    reason = getattr(submission, 'failure_reason', None)
    error_class = next(
        (klass for klass in RETRY_SCHEDULE_MINUTES if klass.__name__ == reason), None,
    )
    schedule = RETRY_SCHEDULE_MINUTES.get(error_class, []) if error_class else []
    attempts_used = submission.retry_count or 0

    plan = {
        'attempts_used': attempts_used,
        'attempts_total': len(schedule) + 1 if schedule else None,
        'attempts_left': max(0, len(schedule) - attempts_used) if schedule else None,
        'next_attempt_at': submission.next_attempt_at.isoformat() if submission.next_attempt_at else None,
        'failure_reason': reason,
        'retryable': bool(error_class),
        # The whole envelope, so the page can say "gives up about two days out" rather
        # than leaving the reader to add up a list of minutes.
        'gives_up_after_minutes': sum(schedule) if schedule else None,
    }

    if submission.next_attempt_at:
        waiting = (submission.next_attempt_at - datetime.utcnow()).total_seconds()
        # Negative means it is already due and simply waiting for a runner to be
        # triggered, which is a different thing from waiting for the clock.
        plan['seconds_until_next_attempt'] = int(waiting)
        plan['due'] = waiting <= 0
    return plan


def schedule_retry_or_fail(submission, error):
    """
    Applies the retry policy for a failed TRA fetch.

    Retryable failures go back to 'queued' with a next_attempt_at in the future;
    permanent ones (wrong time in the URL, rejected Referer) fail immediately rather
    than burning ten more requests against a rate-limited portal.
    """
    attempt = submission.retry_count or 0
    submission.retry_count = attempt + 1
    # Kept as its own field so the dashboard can say what went wrong without picking
    # the class name back out of a sentence.
    submission.failure_reason = type(error).__name__

    schedule = RETRY_SCHEDULE_MINUTES.get(type(error), []) if error.retryable else []
    delay_minutes = schedule[attempt] if attempt < len(schedule) else None

    if delay_minutes is None:
        submission.status = 'failed'
        submission.claimed_at = None
        submission.next_attempt_at = None
        reason = 'permanent' if not error.retryable else f'no retries left after {attempt + 1} attempts'
        submission.error_message = f"{type(error).__name__} ({reason}): {error}"
        print(f"[FetchFailed] Submission {submission.id} failed: {submission.error_message}")
        db.session.commit()

        payload = {"submission_id": submission.id, "status": "failed", "error_message": submission.error_message}
        dispatch_event('submission.failed', payload, get_instance_config())
        return

    submission.status = 'queued'
    submission.claimed_at = None
    submission.next_attempt_at = datetime.utcnow() + timedelta(minutes=delay_minutes)
    submission.error_message = (
        f"{type(error).__name__}: {error} Retry {attempt + 1} scheduled for "
        f"{submission.next_attempt_at.strftime('%Y-%m-%d %H:%M')} UTC."
    )
    print(f"[FetchRetry] Submission {submission.id} requeued in {delay_minutes}m: {error}")
    db.session.commit()

    payload = {
        "submission_id": submission.id, "status": submission.status,
        "error_message": submission.error_message,
        "next_attempt_at": submission.next_attempt_at.isoformat(),
    }
    dispatch_event('submission.retry_scheduled', payload, get_instance_config())

def trigger_url_in_background(url_to_trigger):
    """
    Waits for a short period and then calls a given URL.
    This now runs as a gevent greenlet.
    """
    print(f"[Trigger] Background trigger initiated for URL: {url_to_trigger}. Waiting 10 seconds...")
    gevent.sleep(10) # Use gevent's non-blocking sleep
    
    try:
        print(f"[Trigger] Making internal request to process the queue...")
        requests.get(url_to_trigger, timeout=(5, 5))
        print("[Trigger] Internal request sent successfully.")
    except requests.exceptions.ReadTimeout:
        # Expected: the runner holds the connection open until the queue is drained,
        # which normally outlasts this timeout. The trigger still did its job.
        print("[Trigger] Task runner accepted the request and is working the queue.")
    except requests.exceptions.RequestException as e:
        print(f"[Trigger Error] Could not trigger task runner internally: {e}")

# The four headline periods: (key, window length in days, sparkline buckets, days per
# bucket). Today gets a week of context behind it, because a single bar is not a trend.
STAT_PERIODS = (
    ('today', 1, 7, 1),
    ('7d', 7, 7, 1),
    ('4w', 28, 4, 7),
    ('1y', 365, 12, 30),
)

def calculate_dashboard_stats():
    """
    Spending per period, measured on the date printed on the receipt.

    Not on processed_at: that is the moment someone got round to scanning it, so a
    receipt from March scanned in July would land in "today". Expense reporting asks
    when the money was spent, which is receipt_date.

    Each period carries the one before it and the change between them, because a total
    on its own says nothing - 4.2m this month only means something next to 3.1m last
    month. Cancelled and test receipts are excluded; neither is money that left the
    business.
    """
    today = datetime.utcnow().date()
    # Two years of daily totals in one query, which is every bucket and every
    # comparison period the cards below need. At most ~730 rows.
    horizon = today - timedelta(days=2 * 365)
    daily = {
        day: (count, cents or 0)
        for day, count, cents in db.session.query(
            Receipt.receipt_date,
            db.func.count(Receipt.id),
            db.func.sum(Receipt.total_incl_tax_cents),
        ).filter(
            Receipt.receipt_date >= horizon,
            Receipt.receipt_date <= today,
            Receipt.is_cancelled.is_(False),
            Receipt.is_test.is_(False),
        ).group_by(Receipt.receipt_date).all()
        if day is not None
    }

    def window(start, end):
        """(count, cents) over an inclusive date range."""
        count = cents = 0
        for day, (day_count, day_cents) in daily.items():
            if start <= day <= end:
                count += day_count
                cents += day_cents
        return count, cents

    stats = {}
    for name, days, buckets, bucket_days in STAT_PERIODS:
        start = today - timedelta(days=days - 1)
        count, total_cents = window(start, today)
        previous_count, previous_cents = window(start - timedelta(days=days), start - timedelta(days=1))

        # Oldest bucket first, so the sparkline reads left to right.
        series = []
        for index in reversed(range(buckets)):
            bucket_end = today - timedelta(days=index * bucket_days)
            _, bucket_cents = window(bucket_end - timedelta(days=bucket_days - 1), bucket_end)
            series.append(bucket_cents)

        stats[name] = {
            'count': count,
            'total_cents': total_cents,
            'total': _as_float(total_cents),
            'previous_count': previous_count,
            'previous_total_cents': previous_cents,
            'previous_total': _as_float(previous_cents),
            # None, not 0: there is no percentage change from nothing, and rendering
            # one as '+0%' would state something the data does not support.
            'delta_pct': (
                round((total_cents - previous_cents) * 100 / previous_cents, 1)
                if previous_cents else None
            ),
            'series': series,
        }
    return stats

def _receipt_from_tra_url(submission, config):
    """
    Builds a Receipt from the TRA verified page. Returns None if there is nothing to
    store yet (a retry was scheduled, or this receipt is already in the ledger).
    """
    url = submission.input_data

    # Checked before the portal is touched. The verification code was read off the URL
    # at intake and it is the receipt's identity, so a receipt already in the ledger
    # can be recognised without spending a request against a rate-limited portal -
    # which matters most exactly when the queue is full of resubmissions.
    if _register_duplicate(submission, submission.receipt_code, config):
        return None

    print(f"[Fetch] Attempt {(submission.retry_count or 0) + 1} for {url}")

    try:
        html = fetch_receipt_html(url)
    except TraError as e:
        schedule_retry_or_fail(submission, e)
        return None

    print(f"[FetchSuccess] Retrieved HTML (length: {len(html)}).")
    parsed = parse_receipt_html(html)
    flags = ''.join(f' [{flag}]' for flag, on in (('CANCELLED', parsed.is_cancelled), ('TEST', parsed.is_test)) if on)
    print(
        f"[Parse] {parsed.verification_code}: {parsed.vendor_name} (TIN {parsed.vendor_tin}), "
        f"{len(parsed.items)} item(s), total {parsed.total_incl_tax}, tax {parsed.total_tax}{flags}"
    )

    if _register_duplicate(submission, parsed.verification_code, config):
        return None

    judgment, llm_status = _judge_receipt(parsed, submission, config)

    receipt = Receipt(
        vendor=Vendor.upsert(
            tin=parsed.vendor_tin, name=parsed.vendor_name, vrn=parsed.vrn,
            phone=parsed.vendor_phone, tax_office=parsed.tax_office,
        ),
        vendor_name=parsed.vendor_name, vendor_tin=parsed.vendor_tin,
        vendor_phone=parsed.vendor_phone, vrn=parsed.vrn, tax_office=parsed.tax_office,
        receipt_verification_code=parsed.verification_code,
        receipt_number=parsed.receipt_number, z_number=parsed.z_number,
        efd_serial=parsed.efd_serial, uin=parsed.uin,
        customer_name=parsed.customer_name, customer_id_type=parsed.customer_id_type,
        customer_id=parsed.customer_id, customer_mobile=parsed.customer_mobile,
        total_incl_tax_cents=to_cents(parsed.total_incl_tax),
        total_excl_tax_cents=to_cents(parsed.total_excl_tax),
        total_tax_cents=to_cents(parsed.total_tax),
        discount_cents=to_cents(parsed.discount),
        receipt_date=parsed.receipt_date, receipt_time=parsed.receipt_time,
        is_cancelled=parsed.is_cancelled, is_test=parsed.is_test,
        extraction_source='tra_html', source_html=html,
        category=judgment.get('category'), llm_status=llm_status,
        raw_llm_response=json.dumps(judgment),
        device_id=submission.device_id, submission_id=submission.id,
    )

    for item in parsed.items:
        receipt.items.append(ReceiptItem(
            line_number=item.line_number, description=item.description,
            quantity=item.quantity, amount_cents=to_cents(item.amount), tax_code=item.tax_code,
        ))
    for line in parsed.tax_lines:
        receipt.tax_lines.append(ReceiptTaxLine(
            code=line.code, rate=line.rate, amount_cents=to_cents(line.amount),
        ))

    return receipt

def _receipt_from_photo(submission, config):
    """
    Builds a Receipt from a photograph.

    The only path where the model is still trusted with facts, because there is no
    verified page behind the image. Recorded as extraction_source='llm_vision' so
    these receipts stay distinguishable from the exact ones.
    """
    if not config.is_configured():
        raise ValueError("Instance is not configured with LLM provider and API key.")

    data = extract_receipt_details(submission.input_data, True, config)

    verification_code = (data.get('receipt_verification_code') or '').strip() or None
    if _register_duplicate(submission, verification_code, config):
        return None

    category = data.get('category')
    judgment = {
        'category': category,
        'llm_extracted_description': data.get('llm_extracted_description'),
        'llm_tax_analysis': data.get('llm_tax_analysis'),
    }

    receipt = Receipt(
        vendor=Vendor.upsert(
            tin=data.get('vendor_tin'), name=data.get('vendor_name'),
            vrn=data.get('vrn'), phone=data.get('vendor_phone'),
        ),
        vendor_name=data.get('vendor_name'), vendor_tin=data.get('vendor_tin'),
        vendor_phone=data.get('vendor_phone'), vrn=data.get('vrn'),
        receipt_verification_code=verification_code,
        receipt_number=data.get('receipt_number'), z_number=data.get('z_number'),
        efd_serial=data.get('efd_serial'), uin=data.get('uin'),
        customer_name=data.get('customer_name'), customer_id_type=data.get('customer_id_type'),
        customer_id=data.get('customer_id'),
        total_incl_tax_cents=to_cents(data.get('total_amount')),
        total_excl_tax_cents=to_cents(data.get('total_excl_tax')),
        total_tax_cents=to_cents(data.get('vat_amount')),
        receipt_date=_parse_iso_date(data.get('receipt_date')),
        receipt_time=_parse_iso_time(data.get('receipt_time')),
        is_cancelled=bool(data.get('is_cancelled')),
        extraction_source='llm_vision', llm_status='ok',
        category=category, raw_llm_response=json.dumps(judgment),
        device_id=submission.device_id, submission_id=submission.id,
    )

    for index, item in enumerate(data.get('items') or [], start=1):
        if not isinstance(item, dict) or not item.get('description'):
            continue
        receipt.items.append(ReceiptItem(
            line_number=index, description=item.get('description'),
            quantity=to_decimal(item.get('quantity')),
            amount_cents=to_cents(item.get('amount')), tax_code=item.get('tax_code'),
        ))

    return receipt

def _judge_receipt(parsed, submission, config):
    """
    Asks the LLM for a category and a deductibility note. Returns (judgment, status).

    Every failure mode here is survivable: the facts are already established, so a
    missing key, an outage or a cancelled receipt just means the analysis is filled in
    locally and the receipt is stored regardless.
    """
    if not parsed.is_expense:
        reason = 'cancelled by the vendor' if parsed.is_cancelled else 'printed by an EFD in test mode'
        return {
            'category': None,
            'llm_extracted_description': f'{_describe(parsed)} - not an expense, {reason}.',
            'llm_tax_analysis': (
                f'This receipt was {reason}, so it is not deductible and no input VAT '
                'may be claimed on it. It is excluded from all spending totals.'
            ),
        }, 'skipped'

    if not config.is_configured():
        return _unanalysed(parsed, 'no LLM provider is configured'), 'skipped'

    try:
        return analyse_receipt(parsed.as_llm_facts(), config, user_note=submission.description), 'ok'
    except LlmUnavailable as e:
        print(f"[LLM] Storing submission {submission.id} without analysis: {e}")
        return _unanalysed(parsed, f'the analysis step was unavailable ({e})'), 'unavailable'

def _unanalysed(parsed, reason):
    """The judgment stand-in used when the model was not consulted."""
    return {
        'category': None,
        'llm_extracted_description': _describe(parsed),
        'llm_tax_analysis': f'Not analysed: {reason}. The receipt itself is recorded exactly as TRA verified it.',
    }

def _describe(parsed):
    """A description built from the receipt itself, with no model involved."""
    vendor = parsed.vendor_name or 'an unnamed vendor'
    if not parsed.items:
        return f'Purchase from {vendor}'

    first = parsed.items[0].description
    others = len(parsed.items) - 1
    return f'{first}{f" and {others} more item(s)" if others else ""} from {vendor}'

def _register_duplicate(submission, verification_code, config):
    """
    Marks the submission as a duplicate if this receipt is already stored.

    Runs before the LLM is called: a receipt we already hold is not worth a token.
    """
    if not verification_code or not verification_code.strip():
        return False

    existing = Receipt.query.filter_by(receipt_verification_code=verification_code).first()
    if not existing:
        return False

    print(f"[TaskSkip] Duplicate receipt {verification_code}. Original sub ID: {existing.submission_id}")
    submission.status = 'duplicate'
    submission.error_message = f"Duplicate of submission ID {existing.submission_id}"
    db.session.commit()

    payload = {"submission_id": submission.id, "status": "duplicate", "error_message": submission.error_message}
    dispatch_event('submission.duplicate', payload, config)
    return True

def _complete_submission(submission, receipt, config):
    """Stores the receipt, marks the submission done and announces it."""
    description = json.loads(receipt.raw_llm_response or '{}').get('llm_extracted_description')
    if description:
        submission.description = description

    db.session.add(receipt)
    submission.status = 'completed'
    db.session.commit()

    payload = {
        "submission_id": submission.id, "status": submission.status,
        "processed_at": receipt.processed_at.isoformat(),
        "data": receipt_to_dict(receipt),
        "stats": calculate_dashboard_stats(),
    }
    dispatch_event('submission.processed', payload, config)
    print(f"[TaskSuccess] Submission {submission.id} completed.")

def _parse_iso_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        print(f"Warning: Could not parse date '{value}'")
        return None

def _parse_iso_time(value):
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(value, fmt).time()
        except (ValueError, TypeError):
            continue
    if value:
        print(f"Warning: Could not parse time '{value}'")
    return None

def process_submission(submission):
    """
    Turns one submission into a stored Receipt.

    A receipt submitted as a TRA URL has its facts *parsed* from the verified page,
    never inferred: see utils/tra_parser. The LLM is asked only to categorise the
    purchase and comment on it, so an LLM outage costs the analysis and nothing else.
    Photographed receipts have no machine-readable source and still go through vision.
    """
    print(f"[TaskStart] Processing submission {submission.id} (Type: {submission.input_type})")
    try:
        config = get_instance_config()
        if not config:
            raise ValueError("Instance has not been set up yet.")

        if submission.input_type == 'url':
            receipt = _receipt_from_tra_url(submission, config)
        elif submission.input_type == 'photo':
            receipt = _receipt_from_photo(submission, config)
        else:
            raise ValueError(f"Unsupported submission type '{submission.input_type}'.")

        # The submission was a duplicate, or a retry has been scheduled. Either way it
        # has already been committed and announced.
        if receipt is None:
            return

        _complete_submission(submission, receipt, config)

    except TraParseError as e:
        # TRA served a page we could not read. Retrying gets the same HTML, and the
        # alternative - handing it to the LLM to guess the numbers from - is exactly
        # what this pipeline exists to avoid. Fail it and fix the parser.
        print(f"[ParseError] Submission {submission.id}: {e}")
        _fail_submission(submission.id, f"Could not parse the TRA receipt page: {e}", 'TraParseError')

    except Exception as e:
        # --- FIX #2: Resilient Error Handling ---
        # This block ensures a single failed job doesn't kill the whole queue runner.
        print(f"[TaskError] Unhandled exception in process_submission {submission.id}: {e}")
        _fail_submission(submission.id, str(e))

def _fail_submission(submission_id, message, reason=None):
    """Rolls back, marks the submission failed and announces it."""
    db.session.rollback()  # IMPORTANT: clean the session before touching it again.

    # The session was rolled back, so the submission has to be re-fetched.
    submission = Submission.query.get(submission_id)
    if not submission:
        return

    submission.status = 'failed'
    submission.error_message = message
    submission.failure_reason = reason
    submission.next_attempt_at = None
    submission.claimed_at = None
    db.session.commit()

    payload = {"submission_id": submission_id, "status": "failed", "error_message": message}
    dispatch_event('submission.failed', payload, get_instance_config())

# --- WEB ROUTES & AUTH ---

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            flash('You must be logged in to view this page.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
@login_required
def index():
    """
    The dashboard shell, bootstrapped with the first page of submissions.

    Only a page is rendered into the HTML. The whole table used to be serialised into
    the document on every load, which is survivable at a hundred receipts and is not
    at five thousand; filtering, sorting and paging are now the database's job and the
    browser asks for a page at a time through /api/submissions.
    """
    filters = _read_filters(request.args)

    return render_template(
        'index.html',
        stats=calculate_dashboard_stats(),
        # Handed over as data, not as a pre-rendered string: the template serialises it
        # with |tojson, which escapes the characters that would otherwise let a vendor
        # name lifted off a hostile page close the <script> tag it is embedded in.
        initial_page=_submissions_page(
            filters, _read_page(request.args), _read_page_size(request.args),
        ),
        filters=filters,
        # One table, defined on the server, so the dashboard and /submissions/<id>
        # cannot disagree about what a failure means.
        failure_guidance=FAILURE_GUIDANCE,
    )

@app.route('/api/submissions')
@login_required
def api_submissions():
    """One page of submissions, filtered and sorted by the database."""
    return jsonify(_submissions_page(
        _read_filters(request.args), _read_page(request.args), _read_page_size(request.args),
    ))

@app.route('/api/peek/<kind>/<path:key>')
@login_required
def api_peek(kind, key):
    """
    One hover card: everything we hold about a single value printed on a receipt.

    Answers the question the table cannot fit - is this vendor always like this, is
    this price normal, what does this score actually object to - without navigating
    away from the row that raised it. The payload shape is the same for every kind,
    so the browser has one renderer and this endpoint is the only place a new
    hoverable field has to be taught about. See utils/peek.

    A miss is a 404 rather than an empty card: the key came from our own markup, so
    nothing behind it means the receipt was deleted or the link is stale, and a card
    that renders 'Unnamed vendor · 0 receipts' hides that.
    """
    card = peek.build(kind, key, business=get_instance_config())
    if card is None:
        return jsonify({'error': f'Nothing to show for {kind}.'}), 404
    return jsonify(card)

@app.route('/receipts/<int:receipt_id>')
@login_required
def receipt_detail(receipt_id):
    """
    One receipt, rendered as a receipt, with every computed check beside it.

    Addressable so it can be linked to from an email, a ticket or a spreadsheet - which
    a modal never could.
    """
    receipt = db.session.get(Receipt, receipt_id)
    if receipt is None:
        flash('That receipt does not exist.', 'danger')
        return redirect(url_for('index'))

    config = get_instance_config()
    # Other receipts from the same supplier, so the vendor's pattern is one click away.
    siblings = []
    if receipt.vendor_id:
        siblings = (
            Receipt.query.filter(Receipt.vendor_id == receipt.vendor_id, Receipt.id != receipt.id)
            .order_by(Receipt.receipt_date.desc().nullslast())
            .limit(8).all()
        )

    judgment = json.loads(receipt.raw_llm_response or '{}')
    return render_template(
        'receipt_detail.html',
        receipt=receipt,
        submission=receipt.submission,
        assessment=assess_receipt(receipt, config),
        llm_analysis=judgment.get('llm_tax_analysis'),
        duplicates=find_possible_duplicates(receipt),
        region=geo.region_for(geo.parse_location(receipt.submission.location)) if receipt.submission else None,
        siblings=siblings,
        business=config,
        photo_url=(
            url_for('uploaded_file', filename=os.path.basename(receipt.submission.input_data))
            if receipt.submission and receipt.submission.input_type == 'photo' else None
        ),
    )

# Windows the insights page can be run over. Bounded on purpose: every analysis there
# needs the line items loaded, and "what is happening lately" is the question being
# asked - a five-year sweep answers a different one, more slowly.
INSIGHT_WINDOWS = {'30': 30, '90': 90, '365': 365}
DEFAULT_INSIGHT_WINDOW = '90'

@app.route('/insights')
@login_required
def insights():
    """
    Everything the receipts say once you stop looking at them one at a time.

    Ordered by what it costs to ignore: money that cannot be reclaimed first, then
    money about to become unreclaimable, then where it all went, then what has changed
    in what things cost. Rendered server-side - there is no state to keep here, and a
    page that reports figures should not be able to fail to a blank screen.
    """
    window = request.args.get('window', DEFAULT_INSIGHT_WINDOW)
    days = INSIGHT_WINDOWS.get(window, INSIGHT_WINDOWS[DEFAULT_INSIGHT_WINDOW])
    today = datetime.utcnow().date()
    start = today - timedelta(days=days - 1)

    config = get_instance_config()
    receipts = (
        Receipt.query.join(Submission, Receipt.submission_id == Submission.id)
        .filter(
            Submission.status == 'completed',
            Receipt.receipt_date >= start,
            Receipt.receipt_date <= today,
        )
        .options(
            selectinload(Receipt.items), selectinload(Receipt.tax_lines),
            joinedload(Receipt.submission),
        )
        .order_by(Receipt.receipt_date.asc())
        .all()
    )

    # Every submission in the window, not just the ones that produced a receipt: a
    # verification failure rate needs the attempts that failed as its numerator and
    # the ones that worked as its denominator.
    submissions = Submission.query.filter(Submission.received_at >= datetime.combine(start, datetime.min.time())).all()

    return render_template(
        'insights.html',
        reliability=analytics.verification_reliability(receipts, submissions),
        failure_titles={reason: guidance['title'] for reason, guidance in FAILURE_GUIDANCE.items()},
        window=window if window in INSIGHT_WINDOWS else DEFAULT_INSIGHT_WINDOW,
        windows=INSIGHT_WINDOWS,
        days=days, start=start, today=today,
        receipt_count=len(receipts),
        attention=_attention(receipts, config, today),
        categories=analytics.category_breakdown(receipts),
        regions=analytics.region_breakdown(receipts),
        months=analytics.monthly_totals(receipts, months=min(12, max(2, days // 30)), today=today),
        price_movements=analytics.unit_price_movements(receipts),
        cheaper=analytics.cheaper_elsewhere(receipts),
        anomalies=analytics.spend_anomalies(receipts),
        outliers=analytics.location_outliers(receipts),
        uploaders=analytics.vendor_upload_behaviour(receipts),
        map_points=_map_points(receipts),
        map_reference=_map_reference_points(),
        business=config,
    )

def _attention(receipts, config, today):
    """
    The money-at-risk summary, assembled from each receipt's own assessment.

    Three separate things get confused with each other constantly, so they are counted
    separately: VAT already lost, VAT about to be lost, and tax that should have been
    withheld and was not.
    """
    blocked, expiring, restricted = [], [], []
    blocked_cents = recoverable_cents = wht_cents = 0

    for receipt in receipts:
        assessment = assess_receipt(receipt, config)

        recoverable_cents += assessment.recoverable_vat_cents
        wht_cents += assessment.wht_total_cents

        if assessment.input_vat_cents > 0 and assessment.recoverable_vat_cents == 0:
            blocked_cents += assessment.input_vat_cents
            blocked.append({'receipt': receipt, 'assessment': assessment})

        days_left = assessment.claim_days_left
        if assessment.recoverable_vat_cents > 0 and days_left is not None and 0 <= days_left <= 30:
            expiring.append({'receipt': receipt, 'assessment': assessment})

        if assessment.restrictions:
            restricted.append({'receipt': receipt, 'assessment': assessment})

    blocked.sort(key=lambda entry: entry['assessment'].input_vat_cents, reverse=True)
    expiring.sort(key=lambda entry: entry['assessment'].claim_days_left)

    # Why the blocked VAT is blocked, most expensive reason first: one bad habit is
    # usually behind most of it, and naming it is what makes the number actionable.
    reasons = {}
    for entry in blocked:
        for blocker in entry['assessment'].recovery_blockers:
            summary = reasons.setdefault(blocker, {'reason': blocker, 'cents': 0, 'count': 0})
            summary['cents'] += entry['assessment'].input_vat_cents
            summary['count'] += 1

    return {
        'blocked': blocked[:8],
        'blocked_total': len(blocked),
        'blocked_cents': blocked_cents,
        'reasons': sorted(reasons.values(), key=lambda entry: entry['cents'], reverse=True),
        'expiring': expiring[:8],
        'expiring_cents': sum(entry['assessment'].recoverable_vat_cents for entry in expiring),
        'recoverable_cents': recoverable_cents,
        'wht_cents': wht_cents,
        'restricted': restricted[:8],
        'restricted_total': len(restricted),
    }

def _map_points(receipts, limit=400):
    """
    Receipt positions, projected onto the plain SVG canvas in utils/geo.

    No tile server and no API key. A tile map would be prettier, but it would fetch
    from a third party on every view - telling them which part of the country this
    business operates in - and it would break the moment the browser is offline or the
    tile host rate-limits. The question here is where the money goes, and a scatter
    answers that with nothing between the dashboard and its own data.

    Position alone is unreadable without landmarks, so the country's 31 regional
    centres are drawn behind the receipts as faint reference points. They are exact,
    they are already in utils/geo, and Tanzania's shape emerges from them well enough
    to place a dot - without a hand-drawn border pretending to a precision it lacks.

    Dots are sized by spend, so one large purchase does not read the same as fifty
    small ones in the same town.
    """
    points = []
    largest = 1
    for receipt in receipts:
        if not receipt.is_expense or receipt.submission is None:
            continue
        position = geo.parse_location(receipt.submission.location)
        if position is None:
            continue

        placed = geo.to_svg_xy(position)
        cents = receipt.total_incl_tax_cents or 0
        largest = max(largest, cents)
        points.append({
            'x': placed[0], 'y': placed[1],
            'receipt_id': receipt.id,
            'vendor_name': receipt.vendor_name or 'Unnamed vendor',
            'region': geo.region_for(position) or 'outside Tanzania',
            'cents': cents,
            'on': receipt.receipt_date.isoformat() if receipt.receipt_date else None,
        })
        if len(points) >= limit:
            break

    # Radius by square root of value, so area tracks spend rather than radius doing -
    # a dot twice as wide is otherwise read as four times the money.
    for point in points:
        share = (point['cents'] / largest) ** 0.5 if largest else 0
        point['r'] = round(1.1 + share * 2.6, 2)
    return points

def _map_reference_points():
    """The regional centres, as faint landmarks behind the receipt scatter."""
    return [
        {'name': name, 'x': geo.to_svg_xy(centre)[0], 'y': geo.to_svg_xy(centre)[1]}
        for name, centre in geo.REGION_CENTRES.items()
    ]

@app.route('/vendors')
@login_required
def vendors():
    """
    Who the money goes to.

    Grouped on the vendor row, which is keyed on TIN, so one supplier spelled three
    different ways across its receipts appears once. Multiple EFD serials against one
    vendor means multiple tills or branches, which is worth seeing.
    """
    rows = (
        db.session.query(
            Vendor,
            db.func.count(Receipt.id),
            db.func.sum(Receipt.total_incl_tax_cents),
            db.func.sum(Receipt.total_tax_cents),
            db.func.min(Receipt.receipt_date),
            db.func.max(Receipt.receipt_date),
            db.func.count(db.distinct(Receipt.efd_serial)),
        )
        .join(Receipt, Receipt.vendor_id == Vendor.id)
        .filter(Receipt.is_cancelled.is_(False), Receipt.is_test.is_(False))
        .group_by(Vendor.id)
        .order_by(db.func.sum(Receipt.total_incl_tax_cents).desc())
        .all()
    )

    profiles = [
        {
            'vendor': vendor,
            'count': count,
            'total_cents': total_cents or 0,
            'vat_cents': vat_cents or 0,
            # The average ticket is what makes an outlier receipt visible.
            'average_cents': int((total_cents or 0) / count) if count else 0,
            'first_seen': first_seen,
            'last_seen': last_seen,
            'tills': tills,
        }
        for vendor, count, total_cents, vat_cents, first_seen, last_seen, tills in rows
    ]

    return render_template(
        'vendors.html',
        profiles=profiles,
        total_cents=sum(profile['total_cents'] for profile in profiles),
    )

@app.route('/vendors/<path:key>')
@login_required
def vendor_detail(key):
    """
    One supplier, everything at once.

    The hover card answers 'is this vendor always like this?' in six lines; this is
    where the answer is shown its working. Two things live here that exist nowhere
    else in the app: which checks this supplier fails *repeatedly* - one receipt made
    out to a walk-in customer is an annoyance, nine out of twelve is a conversation to
    have with them - and what their prices have done over the receipts we hold.

    Keyed on the vendor's lookup key rather than a row id, so the address survives a
    supplier who had no Vendor row when the receipt was filed, and so every link to
    here can be built from a receipt without a second query.
    """
    vendor, query = peek.vendor_query(key)
    if query is None:
        flash('That vendor does not exist.', 'danger')
        return redirect(url_for('vendors'))

    receipts = (
        query.options(
            selectinload(Receipt.items), selectinload(Receipt.tax_lines),
            joinedload(Receipt.submission),
        )
        .order_by(Receipt.receipt_date.desc().nullslast())
        .limit(peek.MAX_ASSESSED).all()
    )
    if not receipts and vendor is None:
        flash('That vendor does not exist.', 'danger')
        return redirect(url_for('vendors'))

    config = get_instance_config()
    today = datetime.utcnow().date()

    # The compliance record: which checks this supplier fails, how often, and what it
    # costs. Counted across every check rather than only the failures, so 'passes 12
    # of 12' is visible too - a supplier who never gets it wrong is worth knowing.
    tally, entries = {}, []
    totals = {'charged': 0, 'recoverable': 0, 'blocked': 0, 'wht': 0}
    for receipt in receipts:
        assessment = assess_receipt(receipt, config)
        entries.append({'receipt': receipt, 'assessment': assessment})

        totals['charged'] += assessment.input_vat_cents
        totals['recoverable'] += assessment.recoverable_vat_cents
        totals['wht'] += assessment.wht_total_cents
        if assessment.input_vat_cents > 0 and assessment.recoverable_vat_cents == 0:
            totals['blocked'] += assessment.input_vat_cents

        for check in assessment.checks:
            record = tally.setdefault(check.id, {
                'id': check.id, 'label': check.label, 'pass': 0, 'warn': 0, 'fail': 0,
                'detail': check.detail,
            })
            if check.status in ('pass', 'warn', 'fail'):
                record[check.status] += 1
            # The wording kept is the most recent failure's, because that is the one
            # that says what went wrong rather than what went right.
            if check.status == 'fail':
                record['detail'] = check.detail

    failing = sorted(
        (record for record in tally.values() if record['fail']),
        key=lambda record: record['fail'], reverse=True,
    )

    tills = (
        query.with_entities(
            Receipt.efd_serial, db.func.count(Receipt.id),
            db.func.sum(Receipt.total_incl_tax_cents),
            db.func.min(Receipt.receipt_date), db.func.max(Receipt.receipt_date),
        )
        .group_by(Receipt.efd_serial)
        .order_by(db.func.sum(Receipt.total_incl_tax_cents).desc()).all()
    )

    # Cheaper-elsewhere needs the other suppliers to compare against, so it is the one
    # analysis here that cannot run on this vendor's receipts alone.
    window_start = today - timedelta(days=365)
    everyone = (
        Receipt.query.join(Submission, Receipt.submission_id == Submission.id)
        .filter(Submission.status == 'completed', Receipt.receipt_date >= window_start)
        .options(selectinload(Receipt.items)).all()
    )
    name = (vendor.name if vendor else None) or (receipts[0].vendor_name if receipts else None)
    cheaper = [
        finding for finding in analytics.cheaper_elsewhere(everyone)
        if finding['current_vendor'] == name
    ]

    return render_template(
        'vendor_detail.html',
        key=key, vendor=vendor, name=name or 'Unnamed vendor',
        receipt=receipts[0] if receipts else None,
        entries=entries, totals=totals, tills=tills,
        failing=failing, checks=sorted(tally.values(), key=lambda record: record['label']),
        assessed=len(receipts),
        spend=sum(receipt.total_incl_tax_cents or 0 for receipt in receipts),
        categories=analytics.category_breakdown(receipts),
        months=analytics.monthly_totals(receipts, months=12, today=today),
        price_movements=analytics.unit_price_movements(receipts),
        cheaper=cheaper,
        uploads=next(iter(analytics.vendor_upload_behaviour(receipts)), None),
        business=config,
        today=today,
    )

@app.route('/vat-ledger')
@login_required
def vat_ledger():
    """
    Input VAT for one period, with every receipt that is not claimable named.

    This is the view that has to be right: it is what a return is filed from. Every
    receipt in the period is assessed individually rather than summed in SQL, because
    'the VAT charged' and 'the VAT you may claim' are different figures and only the
    checks know which is which.
    """
    today = datetime.utcnow().date()
    period = request.args.get('period') or today.strftime('%Y-%m')
    try:
        start = datetime.strptime(period, '%Y-%m').date()
    except ValueError:
        start = today.replace(day=1)
        period = start.strftime('%Y-%m')
    end = compliance.add_months(start, 1) - timedelta(days=1)

    config = get_instance_config()
    receipts = (
        Receipt.query.join(Submission, Receipt.submission_id == Submission.id)
        .filter(
            Submission.status == 'completed',
            Receipt.receipt_date >= start,
            Receipt.receipt_date <= end,
        )
        # The vendor row comes along because every supplier on the ledger links to
        # their profile, and a lazy load there is one query per line of the return.
        .options(selectinload(Receipt.items), selectinload(Receipt.tax_lines),
                 joinedload(Receipt.vendor))
        .order_by(Receipt.receipt_date.asc())
        .all()
    )

    entries, blocked = [], []
    totals = {'charged': 0, 'recoverable': 0, 'standard_excl': 0, 'exempt': 0, 'gross': 0}
    for receipt in receipts:
        assessment = assess_receipt(receipt, config)
        entry = {'receipt': receipt, 'assessment': assessment}

        totals['charged'] += assessment.input_vat_cents
        totals['recoverable'] += assessment.recoverable_vat_cents
        totals['standard_excl'] += assessment.standard_rated_excl_cents
        totals['exempt'] += assessment.zero_or_exempt_cents
        if receipt.is_expense:
            totals['gross'] += receipt.total_incl_tax_cents or 0

        entries.append(entry)
        if assessment.input_vat_cents > 0 and assessment.recoverable_vat_cents == 0:
            blocked.append(entry)

    # The return for a period is due on the 20th of the month after it.
    due = compliance.add_months(start, 1).replace(day=20)

    return render_template(
        'vat_ledger.html',
        period=period, start=start, end=end,
        entries=entries, blocked=blocked, totals=totals,
        due=due, days_to_due=(due - today).days,
        business=config,
        periods=[compliance.add_months(today.replace(day=1), -offset).strftime('%Y-%m') for offset in range(12)],
    )

@app.route('/submissions/<int:submission_id>')
@login_required
def submission_detail(submission_id):
    """
    A submission that has no verified receipt behind it - yet, or ever.

    /receipts/<id> covers the ones that worked. This covers the ones still waiting and
    the ones that gave up, which until now were a status chip and a stack trace. It
    shows what was captured before TRA was ever contacted, exactly where the retry
    schedule has got to, and the button that puts it back on the queue.
    """
    submission = db.session.get(Submission, submission_id)
    if submission is None:
        flash('That submission does not exist.', 'danger')
        return redirect(url_for('index'))

    # A submission that did resolve belongs on the receipt page, which says more.
    if submission.receipt is not None:
        return redirect(url_for('receipt_detail', receipt_id=submission.receipt.id))

    # The same receipt may have been submitted again and got through the second time.
    twin = None
    if submission.receipt_code:
        twin = Receipt.query.filter_by(receipt_verification_code=submission.receipt_code).first()

    return render_template(
        'submission_detail.html',
        submission=submission,
        plan=retry_plan(submission),
        reason=FAILURE_GUIDANCE.get(submission.failure_reason),
        receipt_time=receipt_time_from_url(submission.input_data) if submission.input_type == 'url' else None,
        region=geo.region_for(geo.parse_location(submission.location)),
        twin=twin,
        photo_url=(
            url_for('uploaded_file', filename=os.path.basename(submission.input_data))
            if submission.input_type == 'photo' else None
        ),
    )

@app.route('/submissions/<int:submission_id>/retry', methods=['POST'])
@login_required
def retry_submission(submission_id):
    """
    Puts a failed submission back on the queue.

    Most failures here are a vendor who has not uploaded the receipt to TRA yet, so the
    retry count is cleared as well: this is a fresh attempt at a job whose circumstances
    have probably changed, not the next tick of an exhausted schedule.
    """
    submission = db.session.get(Submission, submission_id)
    if submission is None:
        return jsonify({'error': 'No such submission.'}), 404

    if submission.status not in ('failed', 'queued'):
        return jsonify({'error': f"A submission that is '{submission.status}' cannot be retried."}), 409

    submission.status = 'queued'
    submission.retry_count = 0
    submission.next_attempt_at = None
    submission.claimed_at = None
    submission.error_message = None
    submission.failure_reason = None
    db.session.commit()

    dispatch_event('submission.queued', {
        'id': submission.id, 'submission_id': submission.id, 'status': submission.status,
        'received_at': submission.received_at.isoformat(), 'input_type': submission.input_type,
        'input_data': submission.input_data, 'description': submission.description,
        'location': submission.location,
        'device_name': submission.device.name if submission.device else 'Unknown Device',
    }, get_instance_config())

    runner_url = url_for('run_tasks', secret=current_app.config['TASK_RUNNER_SECRET_KEY'], _external=True)
    gevent.spawn(trigger_url_in_background, runner_url)

    return jsonify({'submission_id': submission.id, 'status': submission.status}), 202

@app.route('/receipts/<int:receipt_id>/reanalyse', methods=['POST'])
@login_required
def reanalyse_receipt(receipt_id):
    """
    Asks the model for a fresh judgment on a receipt already in the ledger.

    The facts are never re-read: they were parsed from the verified page and are not
    the model's to revise. Only the category and the narrative analysis are replaced,
    which is what is worth redoing after a prompt change or an LLM outage. The stored
    page is re-parsed rather than re-fetched, so this costs TRA nothing.
    """
    receipt = db.session.get(Receipt, receipt_id)
    if receipt is None:
        return jsonify({'error': 'No such receipt.'}), 404

    config = get_instance_config()
    if not config or not config.is_configured():
        return jsonify({'error': 'No LLM provider is configured for this instance.'}), 409

    if not receipt.source_html:
        return jsonify({'error': 'This receipt has no stored TRA page to re-read.'}), 409

    try:
        parsed = parse_receipt_html(receipt.source_html)
    except TraParseError as e:
        return jsonify({'error': f'The stored page no longer parses: {e}'}), 500

    try:
        judgment = analyse_receipt(
            parsed.as_llm_facts(), config,
            user_note=receipt.submission.description if receipt.submission else None,
        )
    except LlmUnavailable as e:
        return jsonify({'error': f'The analysis step is unavailable: {e}'}), 503

    receipt.category = judgment.get('category')
    receipt.llm_status = 'ok'
    receipt.raw_llm_response = json.dumps(judgment)
    db.session.commit()

    return jsonify({'receipt_id': receipt.id, 'receipt': receipt_to_dict(receipt, config)}), 200

@app.route('/admin/setup', methods=['GET', 'POST'])
def setup():
    if get_instance_config():
        return redirect(url_for('admin_login'))
    if request.method == 'POST':
        email = request.form.get('email')
        if InstanceConfig.query.filter_by(admin_email=email).first():
            flash('This email is already registered. Please login.', 'danger')
            return redirect(url_for('admin_login'))
        
        totp_secret = pyotp.random_base32()
        provisioning_uri = generate_totp_provisioning_uri(totp_secret, email)
        qr_code_b64 = generate_qr_code_base64(provisioning_uri)

        session['setup_email'] = email
        session['setup_totp_secret'] = totp_secret
        session['setup_qr_code'] = qr_code_b64
        
        return redirect(url_for('setup_verify'))
    return render_template('admin/setup.html')

@app.route('/admin/setup/verify', methods=['GET', 'POST'])
def setup_verify():
    email = session.get('setup_email')
    totp_secret = session.get('setup_totp_secret')
    qr_code_b64 = session.get('setup_qr_code')

    if not all([email, totp_secret, qr_code_b64]):
        return redirect(url_for('setup'))

    if request.method == 'POST':
        submitted_code = request.form.get('totp_code')
        totp = pyotp.TOTP(totp_secret)

        if totp.verify(submitted_code):
            new_config = InstanceConfig(admin_email=email, totp_secret=totp_secret)
            db.session.add(new_config)
            db.session.commit()
            
            session.pop('setup_email', None)
            session.pop('setup_totp_secret', None)
            session.pop('setup_qr_code', None)
            
            flash('Setup complete! Please log in using your authenticator app.', 'success')
            return redirect(url_for('admin_login'))
        else:
            flash('Invalid code. Please try again.', 'danger')
            
    return render_template('admin/setup_verify.html', email=email, qr_code_b64=qr_code_b64)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    config = get_instance_config()
    if not config:
        return redirect(url_for('setup'))
    if request.method == 'POST':
        email = request.form.get('email')
        if email == config.admin_email:
            session['login_email'] = email
            return redirect(url_for('login_verify'))
        else:
            flash('Invalid admin email.', 'danger')
    return render_template('admin/login.html')

@app.route('/admin/login/verify', methods=['GET', 'POST'])
def login_verify():
    email = session.get('login_email')
    if not email:
        return redirect(url_for('admin_login'))

    config = get_instance_config()
    if not config or config.admin_email != email:
        flash('Authentication error. Please start over.', 'danger')
        session.pop('login_email', None)
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        submitted_code = request.form.get('totp_code')
        totp = pyotp.TOTP(config.totp_secret)
        
        if totp.verify(submitted_code):
            session.clear()
            session['admin_logged_in'] = True
            session.permanent = True
            flash('Logged in successfully.', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid or expired authenticator code. Please try again.', 'danger')
            return redirect(url_for('admin_login'))

    return render_template('admin/login_verify.html', email=email)

@app.route('/admin/logout')
@login_required
def admin_logout():
    session.clear()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('admin_login'))

@app.route('/admin/configure', methods=['GET', 'POST'])
@login_required
def configure_instance():
    config = get_instance_config()
    
    if request.method == 'POST':
        # Get the active tab from a hidden input in the form
        active_tab = request.form.get('active_tab', 'general-settings')
        
        # Save all the form data. The business identity is blanked to NULL rather than
        # stored as '', because the compliance checks read "no TIN set" from it.
        def optional(field):
            return (request.form.get(field) or '').strip() or None

        config.business_name = optional('business_name')
        config.business_tin = optional('business_tin')
        config.business_vrn = optional('business_vrn')
        config.llm_provider = request.form.get('llm_provider')
        config.llm_api_key = request.form.get('llm_api_key')
        config.google_sheet_id = request.form.get('google_sheet_id')
        config.google_service_account_json = request.form.get('google_service_account_json')
        config.post_callback_url = request.form.get('post_callback_url')
        config.s3_bucket_name = request.form.get('s3_bucket_name')
        config.s3_access_key_id = request.form.get('s3_access_key_id')
        config.s3_secret_access_key = request.form.get('s3_secret_access_key')
        config.s3_region = request.form.get('s3_region')
        
        db.session.commit()
        flash('Configuration saved successfully!', 'success')
        
        # Redirect back to the configuration page, passing the active tab as a URL parameter
        return redirect(url_for('configure_instance', tab=active_tab))

    # For GET requests, get the active tab from the URL, defaulting to 'general-settings'
    active_tab = request.args.get('tab', 'general-settings')
    devices = Device.query.all()
    
    # Pass the active_tab variable to the template
    return render_template('admin/configure.html', config=config, devices=devices, active_tab=active_tab)

@app.route('/admin/devices', methods=['POST'])
@login_required
def add_device():
    device_name = request.form.get('device_name')
    if not device_name:
        flash('Device name cannot be empty.', 'danger')
        return redirect(url_for('configure_instance'))
    new_device = Device(name=device_name)
    db.session.add(new_device)
    db.session.commit()
    flash(f'Device "{device_name}" added successfully.', 'success')
    return redirect(url_for('configure_instance'))


# --- INTAKE & TASK RUNNER ENDPOINTS ---

@app.route('/admin/queue')
@login_required
def queue_status():
    """Displays pending jobs and provides a manual trigger."""
    pending_jobs = Submission.query.filter_by(status='queued').order_by(Submission.received_at.asc()).all()
    # Pass the secret key to the template so the button URL can be built securely
    runner_secret = current_app.config['TASK_RUNNER_SECRET_KEY']
    return render_template('admin/queue.html', jobs=pending_jobs, runner_secret=runner_secret)

@app.route('/receipt', methods=['POST'])
def receipt_endpoint():
    """
    ### MODIFIED ###
    Handles new submissions. Saves the full filesystem path for photos to the DB
    for the backend, but sends a public URL in the SSE payload for the frontend.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization header is missing or invalid'}), 401
    
    device_key = auth_header.split(' ')[1]
    device = Device.query.filter_by(api_key=device_key).first()
    if not device:
        return jsonify({'error': 'Invalid device API key'}), 403

    receipt_photo = request.files.get('receiptphoto')
    receipt_url = request.form.get('receipturl')
    if not receipt_photo and not receipt_url:
        return jsonify({'error': '`receiptphoto` (file) or `receipturl` (form field) is required'}), 400

    description = request.form.get('description')
    location = request.form.get('location')

    input_type = ''
    # This will be the path saved to the database.
    db_input_data = ''
    # This will be the path sent to the frontend via SSE.
    frontend_input_data = ''

    if receipt_photo:
        input_type = 'photo'
        filename = secure_filename(f"{datetime.utcnow().timestamp()}_{receipt_photo.filename}")
        
        # The full, absolute path for backend processing.
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        receipt_photo.save(filepath)
        
        # Set the two different paths for their specific purposes.
        db_input_data = filepath
        frontend_input_data = url_for('uploaded_file', filename=filename)

    elif receipt_url:
        input_type = 'url'
        # For URLs, the path is the same for both backend and frontend.
        db_input_data = receipt_url
        frontend_input_data = receipt_url

    new_submission = Submission(
        device_id=device.id, input_type=input_type,
        input_data=db_input_data, # Save the full filesystem path to the DB
        description=description, location=location,
        # Read before anything is queued. A submission that never verifies still has
        # its receipt's identity on it, which is what the admin needs to chase it.
        receipt_code=_code_from_url(db_input_data) if input_type == 'url' else None,
    )
    db.session.add(new_submission)
    db.session.commit()
    
    config = get_instance_config()
    payload = {
        "id": new_submission.id, "device_name": device.name, "status": new_submission.status,
        "received_at": new_submission.received_at.isoformat(),
        "input_type": new_submission.input_type,
        "input_data": frontend_input_data, # Send the public URL to the frontend
        "description": new_submission.description, "location": new_submission.location
    }
    dispatch_event('submission.queued', payload, config)
    
    runner_secret = current_app.config['TASK_RUNNER_SECRET_KEY']
    runner_url = url_for('run_tasks', secret=runner_secret, _external=True)
    gevent.spawn(trigger_url_in_background, runner_url)
    
    return jsonify({ "message": "Receipt accepted and queued for processing.", "submission_id": new_submission.id }), 202

@app.route('/tasks/run', methods=['GET'])
def run_tasks():
    secret = request.args.get('secret')
    if secret != app.config['TASK_RUNNER_SECRET_KEY']:
        return jsonify({"error": "Unauthorized"}), 403

    # --- Self-healing logic for stuck jobs ---
    # Reclaim jobs whose runner died mid-flight, i.e. whose lease has expired.
    lease_cutoff = datetime.utcnow() - timedelta(minutes=JOB_LEASE_MINUTES)
    stuck_jobs = Submission.query.filter(
        Submission.status == 'processing',
        db.or_(
            Submission.claimed_at < lease_cutoff,
            # Rows claimed before claimed_at existed, or by an older release.
            db.and_(Submission.claimed_at.is_(None), Submission.received_at < lease_cutoff),
        )
    ).all()

    for job in stuck_jobs:
        print(f"[Heal] Found stuck job {job.id}. Re-queueing.")
        job.status = 'queued'
        job.claimed_at = None
        job.next_attempt_at = None
        job.error_message = "Rescued from stuck 'processing' state."

    if stuck_jobs:
        db.session.commit()

    processed_jobs = []
    tra_fetches = 0
    # Process due jobs, up to this tick's budget. The loop is bounded by attempts, not
    # by completions, so contention with another runner can never spin it forever.
    for _ in range(MAX_JOBS_PER_RUN * 2):
        if len(processed_jobs) >= MAX_JOBS_PER_RUN:
            break
        now = datetime.utcnow()
        # Find one due job (this will now include any rescued jobs). Jobs waiting on a
        # scheduled retry are skipped until their next_attempt_at comes round.
        job = Submission.query.filter(
            Submission.status == 'queued',
            db.or_(Submission.next_attempt_at.is_(None), Submission.next_attempt_at <= now)
        ).order_by(Submission.received_at.asc()).first()
        if not job:
            break

        # Atomically claim the job: the UPDATE only matches while the row is still
        # queued, so a second, unsynchronised runner cannot process it as well.
        claimed = db.session.query(Submission).filter(
            Submission.id == job.id, Submission.status == 'queued'
        ).update(
            {'status': 'processing', 'claimed_at': now, 'error_message': None},
            synchronize_session=False
        )
        db.session.commit()

        if not claimed:
            print(f"[Claim] Job {job.id} was taken by another runner. Skipping.")
            continue

        db.session.refresh(job)

        # Space out portal requests; TRA throttles bursts from a single IP.
        if job.input_type == 'url':
            if tra_fetches:
                time.sleep(TRA_REQUEST_SPACING_SECONDS)
            tra_fetches += 1

        process_submission(job)

        final_status = Submission.query.get(job.id)
        processed_jobs.append({
            "id": job.id,
            "final_status": final_status.status,
            "error_message": final_status.error_message
        })

    if not processed_jobs and not stuck_jobs:
        return jsonify({"message": "No pending or stuck jobs to process."}), 200

    return jsonify({
        "message": f"Processed {len(processed_jobs)} job(s). Rescued {len(stuck_jobs)} stuck job(s).",
        "processed_details": processed_jobs
    }), 200

@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    """Serves a file from the upload folder."""
    return send_from_directory(
        app.config['UPLOAD_FOLDER'],
        filename,
        as_attachment=False # Display in browser instead of downloading
    )

@app.route('/export/csv')
@login_required
def export_csv():
    search_query = request.args.get('search', '').lower()
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    query = Receipt.query.join(Submission).filter(Submission.status == 'completed')

    try:
        if start_date_str:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            query = query.filter(Receipt.receipt_date >= start_date)
        if end_date_str:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            query = query.filter(Receipt.receipt_date <= end_date)
    except ValueError:
        flash('Invalid date format provided for export.', 'danger')
        return redirect(url_for('index'))
    
    # --- MODIFIED: Added .options() for eager loading of the 'submission' relationship ---
    query = query.options(joinedload(Receipt.submission), joinedload(Receipt.items), joinedload(Receipt.tax_lines))

    receipts = query.order_by(Receipt.receipt_date.desc()).all()
    
    if search_query:
        filtered_receipts = []
        for receipt in receipts:
            vendor_match = receipt.vendor_name and search_query in receipt.vendor_name.lower()
            desc_match = receipt.submission.description and search_query in receipt.submission.description.lower()
            if vendor_match or desc_match:
                filtered_receipts.append(receipt)
        receipts = filtered_receipts

    # Read before the response starts streaming: generate() is consumed after the
    # request context has gone, so everything it touches has to be loaded by now.
    config = get_instance_config()

    def generate():
        data = io.StringIO()
        writer = csv.writer(data)
        header = [
            'ID', 'Status', 'Received At', 'Processed At', 'Vendor', 'Vendor TIN', 'VRN',
            'Tax Office', 'EFD Serial', 'Receipt No', 'Z Number', 'Verification Code',
            'Receipt Date', 'Receipt Time', 'Total Excl Tax', 'Total Tax', 'Total Incl Tax',
            'Discount', *[f'Tax {code}' for code in TAX_CODES], 'Cancelled', 'Test',
            'Category', 'Source', 'Items', 'LLM Description', 'Tax Analysis',
            'Customer Name', 'Customer ID',
            # Computed by utils/compliance, so a spreadsheet can be filtered on what is
            # actually claimable rather than on what was merely spent.
            'Compliance Score', 'Input VAT Charged', 'Input VAT Recoverable',
            'Standard Rated Excl', 'Zero Rated Or Exempt', 'Claim Deadline',
            'Days Left To Claim', 'Failed Checks', 'Recovery Blockers',
            'Computed Category', 'WHT Estimate',
        ]
        writer.writerow(header)
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)

        # This will now work because receipt.submission was pre-loaded.
        for receipt in receipts:
            raw_response = json.loads(receipt.raw_llm_response or '{}')
            assessment = assess_receipt(receipt, config)
            by_code = {line.code: format_cents(line.amount_cents) for line in receipt.tax_lines}
            items = '; '.join(
                f"{item.description} x{item.quantity or 1} @ {format_cents(item.amount_cents)}"
                f"{f' [{item.tax_code}]' if item.tax_code else ''}"
                for item in receipt.items
            )
            row = [
                receipt.submission_id, 'completed', receipt.submission.received_at.strftime('%Y-%m-%d %H:%M:%S'),
                receipt.processed_at.strftime('%Y-%m-%d %H:%M:%S'), receipt.vendor_name, receipt.vendor_tin,
                receipt.vrn, receipt.tax_office, receipt.efd_serial, receipt.receipt_number,
                receipt.z_number, receipt.receipt_verification_code, receipt.receipt_date,
                receipt.receipt_time, format_cents(receipt.total_excl_tax_cents),
                format_cents(receipt.total_tax_cents), format_cents(receipt.total_incl_tax_cents),
                format_cents(receipt.discount_cents), *[by_code.get(code, '') for code in TAX_CODES],
                'yes' if receipt.is_cancelled else '', 'yes' if receipt.is_test else '',
                receipt.category, receipt.extraction_source, items, receipt.submission.description,
                raw_response.get('llm_tax_analysis', ''), receipt.customer_name, receipt.customer_id,
                assessment.score, format_cents(assessment.input_vat_cents),
                format_cents(assessment.recoverable_vat_cents),
                format_cents(assessment.standard_rated_excl_cents),
                format_cents(assessment.zero_or_exempt_cents),
                assessment.claim_deadline.isoformat() if assessment.claim_deadline else '',
                assessment.claim_days_left if assessment.claim_days_left is not None else '',
                '; '.join(check.id for check in assessment.failed_checks),
                '; '.join(assessment.recovery_blockers),
                assessment.computed_category or '',
                format_cents(assessment.wht_total_cents),
            ]
            writer.writerow(row)
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    response.headers["Content-Disposition"] = f'attachment; filename="receipts_export_{timestamp}.csv"'
    
    return response

@app.route('/stream')
@login_required
def stream():
    """This endpoint holds open a connection and streams updates."""
    def event_stream():
        # Listen to the announcer and yield messages
        messages = announcer.listen()
        while True:
            msg = next(messages)
            yield msg
    
    return app.response_class(event_stream(), mimetype='text/event-stream')