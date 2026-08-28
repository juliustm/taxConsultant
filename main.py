# main.py
import os, re, time, json, csv, io, uuid, pyotp, requests, gevent
from functools import wraps
from datetime import datetime, timedelta, date
from werkzeug.utils import secure_filename

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session, current_app, send_from_directory, Response, g, make_response

from config import Config
from models.user import db, InstanceConfig, Device, Receipt, ReceiptItem, ReceiptTaxLine, Submission, Vendor
from utils import analytics, branding, compliance, geo, peek, qr
from utils.images import store_photo
from utils.device_auth import (
    consume_enrolment_token, device_required, end_session, issue_enrolment_token,
    REJECTION_MESSAGES,
)
from utils.security import generate_totp_provisioning_uri, generate_qr_code_base64
from utils.export import dispatch_event, format_currency
from utils.llm_processor import (
    analyse_receipt, extract_receipt_details, reconstructed_receipt_url, LlmUnavailable,
)
from utils.money import format_cents, from_cents, to_cents, to_decimal
from utils import sse_broker
from utils.tra import (
    build_receipt_url, fetch_receipt_html, parse_receipt_url, TraError, TraReceiptNotUploaded,
    TraThrottled, TraTransportError, TraUnexpectedResponse,
)
from utils.tra_parser import normalise_vrn, parse_receipt_html, TAX_CODES, TraParseError
from sqlalchemy import text as sa_text
from sqlalchemy.orm import joinedload, selectinload

app = Flask(__name__)
app.config.from_object(Config)

app.jinja_env.filters['currency'] = format_currency


@app.errorhandler(413)
def too_large(_error):
    """
    An upload past Config.MAX_CONTENT_LENGTH, answered in the same shape as every other
    refusal on these routes.

    Both routes that take a file are JSON APIs, and the callers are a phone's outbox and
    a bot - neither of which can do anything with Werkzeug's HTML page. The scanner reads
    the status rather than this body (see httpReason in pwa.js) and now treats it as
    final instead of retrying the same bytes eight times, but the message is what a
    direct API integrator gets, and it should say the limit rather than make them guess.
    """
    limit_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return jsonify({
        'error': f'That file is larger than the {limit_mb}MB limit.',
        'max_bytes': app.config['MAX_CONTENT_LENGTH'],
    }), 413


# UPLOAD_FOLDER comes from Config along with everything else on the persistence
# volume - see config.py. Nothing here may name a data path of its own.

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
        ('landing_mode', 'VARCHAR(20)'),
        ('brand_name', 'VARCHAR(120)'),
        ('brand_tagline', 'VARCHAR(200)'),
        ('brand_accent', 'VARCHAR(20)'),
        ('brand_logo_url', 'VARCHAR(500)'),
        ('landing_cta_label', 'VARCHAR(60)'),
        ('landing_cta_url', 'VARCHAR(500)'),
        ('llm_text_model', 'VARCHAR(100)'),
        ('llm_vision_model', 'VARCHAR(100)'),
        ('rebuild_url_from_text', 'BOOLEAN'),
    ),
    'submission': (
        ('next_attempt_at', 'DATETIME'),
        ('claimed_at', 'DATETIME'),
        ('failure_reason', 'VARCHAR(50)'),
        ('receipt_code', 'VARCHAR(50)'),
        ('recovered_url', 'VARCHAR(200)'),
        ('qr_scan', 'TEXT'),
        ('client_uuid', 'VARCHAR(64)'),
        ('captured_at', 'DATETIME'),
        ('corrected_at', 'DATETIME'),
        ('photo_filename', 'VARCHAR(255)'),
        ('llm_draft', 'TEXT'),
        ('rebuild_declined', 'BOOLEAN NOT NULL DEFAULT 0'),
    ),
    'device': (
        ('created_at', 'DATETIME'),
        ('enrolment_token', 'VARCHAR(80)'),
        ('enrolment_issued_at', 'DATETIME'),
        ('activated_at', 'DATETIME'),
        ('session_token_hash', 'VARCHAR(64)'),
        ('session_started_at', 'DATETIME'),
        ('session_user_agent', 'VARCHAR(255)'),
        ('last_seen_at', 'DATETIME'),
        ('revoked_at', 'DATETIME'),
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
        ('document_type', 'VARCHAR(30)'),
        ('source_html', 'TEXT'),
        ('category', 'VARCHAR(50)'),
        ('llm_status', 'VARCHAR(20)'),
        ('corrected_at', 'DATETIME'),
        ('corrected_fields', 'TEXT'),
    ),
}

# Indexes on columns added above. create_all() only indexes tables it creates.
PENDING_INDEXES = (
    ('ix_submission_next_attempt_at', 'submission', 'next_attempt_at'),
    ('ix_submission_receipt_code', 'submission', 'receipt_code'),
    # Covers the scanner's own history lookup: WHERE device_id = ? ORDER BY id DESC.
    # Without it that query is a full table scan of every device's submissions.
    ('ix_submission_device_id_id', 'submission', 'device_id, id'),
    ('ix_receipt_receipt_date', 'receipt', 'receipt_date'),
    ('ix_receipt_vendor_id', 'receipt', 'vendor_id'),
    ('ix_receipt_vendor_tin', 'receipt', 'vendor_tin'),
    ('ix_receipt_is_cancelled', 'receipt', 'is_cancelled'),
    ('ix_receipt_is_test', 'receipt', 'is_test'),
    ('ix_receipt_category', 'receipt', 'category'),
    ('ix_device_last_seen_at', 'device', 'last_seen_at'),
)

# Kept apart from PENDING_INDEXES because SQLite cannot add a UNIQUE column with
# ALTER TABLE - uniqueness on a column added after the fact has to arrive as its own
# index. NULLs are distinct in a SQLite unique index, so existing rows are unaffected.
PENDING_UNIQUE_INDEXES = (
    ('uq_submission_client_uuid', 'submission', 'client_uuid'),
    ('uq_device_enrolment_token', 'device', 'enrolment_token'),
)

def _table_columns(table):
    return {row[1] for row in db.session.execute(sa_text(f"PRAGMA table_info({table})"))}

def apply_pending_migrations():
    """
    Brings an existing database up to the current schema.

    Adds the columns and indexes listed above, then runs the backfills - the changes
    that cannot be expressed as a column default, like money that used to be stored
    as a float or the vendor rows that receipts now group by.
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
    for name, table, column in PENDING_UNIQUE_INDEXES:
        db.session.execute(sa_text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({column})"))
    db.session.commit()

    _backfill_money(columns_by_table.get('receipt', set()))
    _backfill_vrn_placeholders()
    _backfill_vendors()
    _backfill_device_tokens()
    _backfill_photo_paths()

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

def _backfill_vrn_placeholders():
    """
    Clears VRNs that were stored as TRA's 'NOT REGISTERED' placeholder.

    Those rows read as VAT registered everywhere the VRN is tested for presence, so
    un-registered suppliers were shown with the green badge and scored as if input
    VAT on their receipts were recoverable. Runs before _backfill_vendors so the
    placeholder cannot be copied from a receipt onto a freshly created vendor.
    """
    cleared = 0
    for model in (Receipt, Vendor):
        for row in model.query.filter(model.vrn.isnot(None)).all():
            if normalise_vrn(row.vrn) is None:
                row.vrn = None
                cleared += 1

    if not cleared:
        return

    db.session.commit()
    print(f"[Migration] Cleared {cleared} placeholder VRN(s).")

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

def _backfill_device_tokens():
    """
    Gives devices that predate enrolment an activation token.

    Without this, every device already in the database would show as un-activatable on
    the new admin page and an admin would have to recreate them - losing the link
    between a device and the receipts it has already submitted.
    """
    stale = Device.query.filter(
        Device.enrolment_token.is_(None), Device.session_token_hash.is_(None)
    ).all()
    if not stale:
        return

    for device in stale:
        if device.created_at is None:
            device.created_at = datetime.utcnow()
        issue_enrolment_token(device)

    db.session.commit()
    print(f"[Migration] Issued activation tokens to {len(stale)} pre-existing device(s).")

def _backfill_photo_paths():
    """
    Reduces stored photo paths to bare filenames.

    Photo submissions used to record the absolute path they were saved at, which tied
    every row to wherever the persistence volume was mounted at the time. Moving the
    volume - which is the whole point of keeping data outside the code directory -
    would leave those rows pointing at a directory that no longer exists.
    """
    absolute = Submission.query.filter(
        Submission.input_type == 'photo', Submission.input_data.like('%/%'),
    ).all()
    if not absolute:
        return

    for submission in absolute:
        submission.input_data = os.path.basename(submission.input_data)

    db.session.commit()
    print(f"[Migration] Reduced {len(absolute)} photo path(s) to filenames.")

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
        'action': 'Read the time off the photograph and correct it above - the portal is asked '
                  'again as soon as you do.',
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

# The fields a human may key in off the photograph, and how each one is read back.
#
# Deliberately short. Karani exists so that nobody types receipts in, so this is not a
# data-entry form - it is the list of values that quietly break something downstream
# when a model misreads them, next to a photograph of the paper they are printed on.
# Everything here is a printed fact; nothing computed, and nothing the model was asked
# to judge. Declared once and used by both the form and the route that saves it, so the
# page can never offer a field the route ignores.
#
# `hint` says what the field costs when it is wrong, not what it is - the label already
# says what it is.
CORRECTABLE_FIELDS = (
    # (attribute, label, kind, hint)
    ('vendor_name', 'Vendor', 'text',
     'The trading name printed at the top.'),
    ('vendor_tin', 'Vendor TIN', 'text',
     'Every receipt from this supplier is grouped on it. A digit out files this one under a supplier of its own.'),
    ('vrn', 'VRN', 'text',
     'Leave blank when the paper says NOT REGISTERED. No VRN means no input VAT may be claimed.'),
    ('receipt_date', 'Receipt date', 'date',
     'Every report keys off it, and it opens the six-month VAT claim window.'),
    ('receipt_time', 'Receipt time', 'time',
     'The six digits after the code in the TRA address.'),
    ('receipt_verification_code', 'Verification code', 'text',
     "The receipt's identity, and what a second submission of it is caught on."),
    ('receipt_number', 'Receipt no.', 'text', None),
    ('total_incl_tax_cents', 'Total incl. tax', 'money', None),
    ('total_excl_tax_cents', 'Total excl. tax', 'money', None),
    ('total_tax_cents', 'Tax', 'money',
     'The VAT charged. The ledger claims from this figure.'),
)

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
        "document_type": receipt.document_type,
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

# How many suppliers the filter panel offers as one-click selections, and how many
# the insights column names. The panel is a picker and wants breadth; the insights
# column is a finding and wants the few that matter.
FACET_VENDOR_LIMIT = 12
TOP_VENDOR_LIMIT = 6
# Categories named individually in a breakdown before the tail is collapsed into one
# 'other' row. Past this the bars are too short to compare and the list is a legend.
BREAKDOWN_LIMIT = 8


def _read_list(args, key, cast=None):
    """
    A repeatable query parameter, read as a list of distinct values.

    Accepted both ways round - `category=fuel&category=meals` and `category=fuel,meals`
    - because the first is what this page emits and the second is what somebody types by
    hand or pastes into a chat. A value that will not cast is ignored rather than
    rejected, the same way an unparseable date already is: a mangled link should show
    the reader a table, not a 500, and the chips above it say what is actually applied.
    """
    values, seen = [], set()
    for raw in args.getlist(key):
        for part in str(raw).split(','):
            part = part.strip()
            if not part:
                continue
            if cast is not None:
                try:
                    part = cast(part)
                except (TypeError, ValueError):
                    continue
            if part in seen:
                continue
            seen.add(part)
            values.append(part)
    return values


def _selection(filters, key):
    """
    One filter's selected values, tolerant of a single value written as itself.

    The multi-value filters are lists, but a filter dict can also be assembled by hand
    - in a test, or by a caller that only ever meant one category - and a bare string
    handed to an IN clause matches its own characters rather than itself.
    """
    value = filters.get(key)
    if not value:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _read_filters(args):
    """
    The table's filter state, read from a query string and normalised.

    Three of these are lists, and that is most of what lets the table answer a real
    question. 'What did the two vans spend on fuel and repairs last quarter' is one
    query with three selections in it; asked one value at a time it is six separate
    looks and a total the reader has to add up themselves - which is the point at
    which people stop asking.
    """
    tab = args.get('tab', 'processed')
    sort = args.get('sort', 'received_at')
    return {
        'tab': tab if tab in TAB_FILTERS else 'processed',
        'search': (args.get('search') or '').strip(),
        # Set by clicking a category anywhere it is shown, so 'what else is fuel?' is
        # one click rather than a search that would also match a vendor called Fuel.
        # Repeatable now, and every single-value link ever shared still means what it did.
        'category': _read_list(args, 'category'),
        # Which phone or bot the receipt came in through. Same click-to-filter contract
        # as the category chip, because 'show me everything off that device' is the
        # question an admin asks the moment one of them starts producing bad scans.
        'device': _read_list(args, 'device', cast=int),
        # Vendor lookup keys ('tin:100147181'), the same key /vendors/<key> is addressed
        # by - never the printed name, which one supplier spells three ways.
        'vendor': _read_list(args, 'vendor'),
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

    categories = _selection(filters, 'category')
    if categories:
        query = query.filter(Receipt.category.in_(categories))

    devices = _selection(filters, 'device')
    if devices:
        query = query.filter(Submission.device_id.in_(devices))

    vendor_keys = _selection(filters, 'vendor')
    if vendor_keys:
        # Matched on the vendor row, which is keyed on the TIN TRA issued, so selecting
        # a supplier selects every spelling of them. The printed TIN is an OR rather
        # than a fallback because a receipt read off a photograph can carry a TIN and
        # still have no Vendor row behind it, and it is the same supplier either way.
        matched = db.session.query(Vendor.id).filter(Vendor.lookup_key.in_(vendor_keys))
        conditions = [Receipt.vendor_id.in_(matched)]
        tins = [key.split(':', 1)[1] for key in vendor_keys if key.startswith('tin:')]
        if tins:
            conditions.append(Receipt.vendor_tin.in_(tins))
        query = query.filter(db.or_(*conditions))

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
        # What else could be selected, so the pickers and the chips that stand for an
        # applied filter can both be drawn without a second round trip - and so a
        # device or supplier that has been filtered down to nothing still has a name.
        'facets': _filter_facets(filters),
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

def _device_names():
    """Every device's name by id. One query against a table with a handful of rows."""
    return {device.id: device.name for device in Device.query.all()}


def _share(cents, total_cents):
    return round(cents * 100 / total_cents, 1) if total_cents else 0.0


def _filtered_insights(filters):
    """
    Everything the filter matches, added up - not just the page on screen.

    Summed by the database, so the figures do not change when you turn the page. Only
    completed, non-void receipts count: a queued submission is not yet money and a
    cancelled one never was.

    The breakdowns underneath the totals are what make a multi-part filter worth
    building. A total answers 'how much'; the split answers 'made of what, collected by
    whom', which is the question that was actually being asked and previously took an
    export and a pivot table to get at.
    """
    base = _filtered_submissions(filters, ordered=False)
    spending = _spending_only(base)

    count, total_cents, vat_cents, largest_cents, first_date, last_date = spending.with_entities(
        db.func.count(Receipt.id),
        db.func.sum(Receipt.total_incl_tax_cents),
        db.func.sum(Receipt.total_tax_cents),
        db.func.max(Receipt.total_incl_tax_cents),
        db.func.min(Receipt.receipt_date),
        db.func.max(Receipt.receipt_date),
    ).one()
    count, total_cents, vat_cents = count or 0, total_cents or 0, vat_cents or 0

    top_vendors = (
        spending
        .with_entities(
            Receipt.vendor_name, Receipt.vendor_tin,
            db.func.count(Receipt.id), db.func.sum(Receipt.total_incl_tax_cents),
        )
        # Grouped on the vendor row, which is keyed on the TIN TRA issued, so one
        # supplier spelled three ways stays one supplier.
        .filter(Receipt.vendor_id.isnot(None))
        .group_by(Receipt.vendor_id)
        .order_by(db.func.sum(Receipt.total_incl_tax_cents).desc())
        .limit(TOP_VENDOR_LIMIT).all()
    )

    by_category = (
        spending
        .with_entities(
            Receipt.category, db.func.count(Receipt.id),
            db.func.sum(Receipt.total_incl_tax_cents),
        )
        .group_by(Receipt.category)
        .order_by(db.func.sum(Receipt.total_incl_tax_cents).desc()).all()
    )
    by_device = (
        spending
        .with_entities(
            Submission.device_id, db.func.count(Receipt.id),
            db.func.sum(Receipt.total_incl_tax_cents),
        )
        .group_by(Submission.device_id)
        .order_by(db.func.sum(Receipt.total_incl_tax_cents).desc()).all()
    )
    names = _device_names()

    return {
        'count': count,
        'total_cents': total_cents,
        'vat_cents': vat_cents,
        'average_cents': int(total_cents / count) if count else 0,
        'largest_cents': largest_cents or 0,
        # The stretch of calendar the figures cover, which is the denominator a reader
        # needs before a total means anything - and is not the same as the dates asked
        # for, since a range can be wider than the receipts inside it.
        'first_date': first_date.isoformat() if first_date else None,
        'last_date': last_date.isoformat() if last_date else None,
        'top_vendors': [
            {
                'name': name or 'Unnamed vendor', 'tin': tin,
                'count': vendor_count, 'cents': cents or 0,
                'share_pct': _share(cents or 0, total_cents),
                # The same key /vendors/<key>, the hover cards and the vendor filter
                # use, built by the rule that owns it rather than reassembled here.
                'key': Vendor.make_lookup_key(tin, name),
            }
            for name, tin, vendor_count, cents in top_vendors
        ],
        'by_category': _capped_breakdown([
            {
                'key': category, 'label': (category or 'uncategorised').replace('_', ' '),
                'count': category_count, 'cents': cents or 0,
                'share_pct': _share(cents or 0, total_cents),
            }
            for category, category_count, cents in by_category
        ], total_cents),
        'by_device': [
            {
                'key': device_id, 'label': names.get(device_id, 'Unknown device'),
                'count': device_count, 'cents': cents or 0,
                'share_pct': _share(cents or 0, total_cents),
            }
            for device_id, device_count, cents in by_device
        ],
    }


def _capped_breakdown(rows, total_cents):
    """
    The largest few slices, with the tail collapsed into one honest 'other' row.

    Collapsed rather than truncated: a list that silently stops at eight does not add
    up to the total printed above it, and a reader who notices that has to distrust
    every other figure on the panel to work out why.
    """
    if len(rows) <= BREAKDOWN_LIMIT:
        return rows

    head, tail = rows[:BREAKDOWN_LIMIT], rows[BREAKDOWN_LIMIT:]
    rest_cents = sum(row['cents'] for row in tail)
    return head + [{
        'key': None, 'label': f'{len(tail)} more', 'count': sum(row['count'] for row in tail),
        'cents': rest_cents, 'share_pct': _share(rest_cents, total_cents),
    }]


def _filter_facets(filters):
    """
    What there is to filter by, counted under everything except the pickers themselves.

    That exclusion is what makes a second selection possible. Counted under the current
    selection, choosing 'fuel' would empty the category list of every other category and
    there would be no way to add 'meals' to it - the filter would be a series of
    single choices wearing a multi-select's clothes. So the tab, the search and the
    dates narrow the options; the three multi-value pickers do not narrow themselves.

    Counted over submissions rather than over spending, so the device picker still
    works on the Failed and Queued tabs, where by definition no money was recorded.
    """
    scope = {**filters, 'category': [], 'device': [], 'vendor': []}
    base = _filtered_submissions(scope, ordered=False)

    categories = (
        base.with_entities(Receipt.category, db.func.count(Submission.id))
        .filter(Receipt.category.isnot(None))
        .group_by(Receipt.category)
        .order_by(db.func.count(Submission.id).desc()).all()
    )
    device_counts = dict(
        base.with_entities(Submission.device_id, db.func.count(Submission.id))
        .group_by(Submission.device_id).all()
    )
    vendors = (
        base.with_entities(
            Receipt.vendor_name, Receipt.vendor_tin, db.func.count(Submission.id),
            db.func.sum(Receipt.total_incl_tax_cents),
        )
        .filter(Receipt.vendor_id.isnot(None))
        .group_by(Receipt.vendor_id)
        .order_by(db.func.sum(Receipt.total_incl_tax_cents).desc())
        .limit(FACET_VENDOR_LIMIT).all()
    )

    vendor_options = [
        {
            'key': Vendor.make_lookup_key(tin, name), 'label': name or 'Unnamed vendor',
            'tin': tin, 'count': vendor_count,
        }
        for name, tin, vendor_count, _cents in vendors
    ]
    # A supplier chosen from a wider range, or from a vendor page, must stay listed
    # once the dates move past their last receipt - otherwise the only way to remove
    # a filter is to guess that it is still applied.
    listed = {option['key'] for option in vendor_options}
    missing = [key for key in _selection(filters, 'vendor') if key not in listed]
    if missing:
        for vendor in Vendor.query.filter(Vendor.lookup_key.in_(missing)).all():
            vendor_options.append({
                'key': vendor.lookup_key, 'label': vendor.name or 'Unnamed vendor',
                'tin': vendor.tin, 'count': 0,
            })
            listed.add(vendor.lookup_key)
        for key in missing:
            if key not in listed:
                vendor_options.append({
                    'key': key, 'label': key.split(':', 1)[-1], 'tin': None, 'count': 0,
                })

    return {
        'categories': [
            {'key': category, 'label': category.replace('_', ' '), 'count': category_count}
            for category, category_count in categories
        ],
        # Every device, including the ones with nothing in range: an admin looking for
        # what a particular phone collected needs to see that the answer is none,
        # which a list that quietly omits it cannot say.
        'devices': sorted(
            [
                {'key': device.id, 'label': device.name,
                 'count': device_counts.get(device.id, 0), 'status': device.status}
                for device in Device.query.all()
            ],
            key=lambda option: (-option['count'], option['label'].lower()),
        ),
        'vendors': vendor_options,
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

def submission_photo_name(submission):
    """
    The stored filename of this submission's photograph, or None if it has none.

    The rule itself lives on the row - see Submission.photo_name - because the hover
    cards need the same answer and cannot import this module. This stays as the name
    every caller here already uses, and tolerates a None submission.
    """
    return submission.photo_name if submission is not None else None

def submission_photo_path(submission):
    """
    Where a submission's photograph actually is, right now.

    The stored name is a bare filename, but rows written before the persistence volume
    moved hold an absolute path into a directory that no longer exists. Resolving by
    basename against the current UPLOAD_FOLDER reads both, and keeps the database
    free of any dependency on where the volume happens to be mounted.
    """
    name = submission_photo_name(submission)
    if not name:
        return None
    return os.path.join(current_app.config['UPLOAD_FOLDER'], os.path.basename(name))

def submission_photo_url(submission):
    """The public URL for a submission's photograph, or None if it has none."""
    name = submission_photo_name(submission)
    if not name:
        return None
    return url_for('uploaded_file', filename=os.path.basename(name))

def prepare_submissions_for_frontend(submissions, detailed=True):
    """Converts Submission objects into a JSON-serialisable list of dictionaries."""
    # Read once and passed down: every receipt is assessed against the same instance
    # config, and re-querying it per row is a query per receipt for one unchanging row.
    config = get_instance_config()

    output = []
    for sub in submissions:
        receipt_data = receipt_to_dict(sub.receipt, config, detailed=detailed)

        # A photo's stored filename becomes a public /uploads/... URL; a URL
        # submission is already the thing the frontend should show.
        #
        # Kept apart from photo_url below, which a URL submission can now also have.
        # Collapsing the two - which this did, while a photograph and a URL were still
        # mutually exclusive - would put an image path in input_data on a row whose
        # input_type says 'url', and every reader of that pair would then be wrong.
        photo_url = submission_photo_url(sub)
        frontend_input_data = (photo_url or sub.input_data) if sub.input_type == 'photo' \
            else sub.input_data

        data = {
            "id": sub.id, "status": sub.status, "received_at": sub.received_at.isoformat(),
            "input_type": sub.input_type, "input_data": frontend_input_data, # Use the transformed path
            # The photograph, whatever the input was. A scan whose QR code the phone read
            # now files the picture it read it from alongside the code, so a verified
            # receipt has the paper behind it too.
            "photo_url": photo_url,
            "description": sub.description, "location": sub.location,
            "error_message": sub.error_message, "is_duplicate": sub.status == 'duplicate',
            "receipt": receipt_data, "device_name": sub.device.name if sub.device else 'Unknown Device',
            # The id as well as the name: the row's device chip is a filter link and a
            # hover card, and both are addressed by id. A name is not an identity -
            # two devices can be called 'Front desk' a year apart.
            "device_id": sub.device_id,
            # What is known about a submission before - or without - a verified receipt
            # behind it, so a row that never resolves is still something to act on.
            "receipt_code": sub.receipt_code,
            "receipt_time": receipt_time_from_url(sub.input_data) if sub.input_type == 'url' else None,
            "failure_reason": sub.failure_reason,
            "retry": retry_plan(sub),
            # How a scanner reconciles what it sent against what the server has, and
            # when the person actually stood in front of the receipt.
            "client_uuid": sub.client_uuid,
            "captured_at": sub.captured_at.isoformat() if sub.captured_at else None,
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


def schedule_retry_or_fail(submission, error, fail_permanently=True):
    """
    Applies the retry policy for a failed TRA fetch. Returns True if a retry was
    scheduled, False if this submission is out of attempts.

    Retryable failures go back to 'queued' with a next_attempt_at in the future;
    permanent ones (wrong time in the URL, rejected Referer) fail immediately rather
    than burning ten more requests against a rate-limited portal.

    `fail_permanently=False` keeps the attempt on the record but leaves the submission
    alone when the retries run out, for a caller that has a fallback of its own. Only
    the photo path uses it: a photograph whose recovered code the portal will not
    confirm is still a photograph we can read.
    """
    attempt = submission.retry_count or 0
    submission.retry_count = attempt + 1
    # Kept as its own field so the dashboard can say what went wrong without picking
    # the class name back out of a sentence.
    submission.failure_reason = type(error).__name__

    schedule = RETRY_SCHEDULE_MINUTES.get(type(error), []) if error.retryable else []
    delay_minutes = schedule[attempt] if attempt < len(schedule) else None

    if delay_minutes is None:
        reason = 'permanent' if not error.retryable else f'no retries left after {attempt + 1} attempts'

        if not fail_permanently:
            # The caller carries on from here, so the row keeps its 'processing' status
            # and its own code writes the outcome. Committed anyway: the attempt count
            # and the reason are real, and are what explains a photo that was recorded
            # unverified.
            submission.failure_reason = type(error).__name__
            db.session.commit()
            print(f"[FetchFallback] Submission {submission.id} unverified ({reason}): {error}")
            return False

        submission.status = 'failed'
        submission.claimed_at = None
        submission.next_attempt_at = None
        submission.error_message = f"{type(error).__name__} ({reason}): {error}"
        print(f"[FetchFailed] Submission {submission.id} failed: {submission.error_message}")
        db.session.commit()

        payload = {
            "submission_id": submission.id, "status": "failed",
            "error_message": submission.error_message, "device_id": submission.device_id,
        }
        dispatch_event('submission.failed', payload, get_instance_config())
        return False

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
        "device_id": submission.device_id,
    }
    dispatch_event('submission.retry_scheduled', payload, get_instance_config())
    return True

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

def _receipt_from_tra_url(submission, config, url=None, on_exhausted=None):
    """
    Builds a Receipt from the TRA verified page. Returns None if there is nothing to
    store yet (a retry was scheduled, or this receipt is already in the ledger).

    `url` overrides the submission's own input_data, which is how a photograph gets
    verified: its QR code, or the verification code the vision model read off it, names
    a receipt on the portal even though the submission itself carries a filename.

    `on_exhausted`, given, is called instead of failing the submission when the portal
    cannot be reached and no retries remain. A photograph has somewhere else to go - the
    image is still in hand - and 'we could not reach TRA' is not a reason to throw away
    a receipt we can still read.
    """
    # recovered_url outranks input_data because it is always the more considered answer:
    # a QR code decoded off the image, or an address an admin corrected by hand. Without
    # this, a hand-corrected URL submission would be re-broken by its next scheduled
    # retry, which would go back to reading the address that was already failing.
    url = url or submission.recovered_url or submission.input_data

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
        if not schedule_retry_or_fail(submission, e, fail_permanently=on_exhausted is None):
            if on_exhausted is not None:
                return on_exhausted(e)
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

    return _receipt_from_parsed_page(submission, config, parsed, html)


def _receipt_from_parsed_page(submission, config, parsed, html):
    """
    Builds the Receipt a verified TRA page describes. Not stored - the caller decides.

    Split out of _receipt_from_tra_url so the interactive path - an admin correcting the
    code by hand and asking the portal there and then - writes exactly the receipt the
    queue would have written, rather than a second mapping of the same page that drifts
    from this one the next time a field is added.
    """
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

def _scan_photo_qr(submission, photo_path):
    """
    Runs the server-side decoder over the photograph and records what it saw.

    The recording is the point of this wrapper. A photo read by the vision model has
    walked past the decoder on the way, and until the report was stored there was no
    way to tell from the outside whether it walked past a decoder that found nothing, a
    decoder that could not open the upload, or no decoder at all - three states that
    look identical on the submission page and want three different things done about
    them. Committed immediately, because everything after this can raise.
    """
    report = qr.scan(photo_path)
    submission.qr_scan = json.dumps(report)
    db.session.commit()
    return report.get('url')


def _may_rebuild_url(submission, config):
    """
    Whether a portal address may be guessed from text the model read off this photo.

    Two switches, deliberately at different altitudes. The instance setting is the
    default for every photograph nobody has looked at; the submission flag is one
    admin's verdict on the document in front of them, and it wins - a person who has
    read the picture and said "this is not a TRA receipt" knows something the pipeline
    does not, and a later retry must not overrule them.
    """
    if submission.rebuild_declined:
        return False
    return config is None or config.rebuilds_urls_from_text()


def _store_llm_draft(submission, data):
    """
    Keeps what the vision model read, so no outcome below can throw it away.

    Committed on its own, before the layers that act on it, for the same reason
    _scan_photo_qr commits its report: everything after this can raise or reschedule,
    and the reading is worth having either way. A draft that cannot be serialised is
    simply not stored - this is a convenience for the admin page, never a reason to
    fail a receipt that is otherwise fine.
    """
    try:
        submission.llm_draft = json.dumps(data)
        db.session.commit()
    except (TypeError, ValueError) as e:
        db.session.rollback()
        print(f'[Photo] Could not store the transcription for submission {submission.id}: {e}')


def _stored_llm_draft(submission):
    """
    The stored transcription for a submission, or None.

    Tolerant of junk for the same reason _stored_qr_scan is: it is read by a page whose
    job is explaining why a receipt has not landed, and it may not fail while doing it.
    """
    if submission is None or not submission.llm_draft:
        return None
    try:
        draft = json.loads(submission.llm_draft)
    except ValueError:
        return None
    return draft if isinstance(draft, dict) else None


def _receipt_from_photo(submission, config):
    """
    Builds a Receipt from a photograph, preferring TRA's numbers over the model's.

    A photo used to mean one thing: hand it to the vision model and store whatever it
    read. That is the weakest receipt this app can produce, and it was being produced
    for receipts that were never actually unverifiable - only unverified, because the
    phone had one look at the QR code in bad light and moved on.

    So the photograph is now worked through in order of how much the answer can be
    trusted, and the first layer that lands wins:

      1. The QR code, read here rather than on the phone (see utils/qr). A still, with
         the contrast stretched, the image upscaled and the code judged against its own
         corner of the frame, decodes a fair number of the codes a moving preview stream
         could not. A hit means the receipt goes down the ordinary verified path and its
         numbers come from TRA. Hit or miss, what the decoder saw is written to the
         submission, because the layers below are silent about which of them answered.

      2. The verification code and time the vision model transcribes off the paper,
         rebuilt into the same portal URL (see llm_processor.reconstructed_receipt_url).
         This is the layer that rescues a receipt whose QR is creased, torn, half under
         a thumb or simply not printed - the code beneath it is large, plain text and
         legible long after the code above it has stopped scanning.

      3. The transcription itself, recorded as extraction_source='llm_vision' exactly
         as before. Where a document is not an EFD receipt at all - a parking stub, a
         handwritten chit - this is not a fallback but the right answer, and the
         document_type the model returns says so rather than leaving it looking like an
         EFD receipt that failed verification.

    Layer 2 is a guess, and the two things that follow from that are what most of the
    care below is about.

    The first is that the guess is now *skippable*. An instance can turn it off
    (InstanceConfig.rebuild_url_from_text) and an admin can turn it off for one
    submission (Submission.rebuild_declined), because on a document that was never an
    EFD receipt it does real harm: a mobile-money SMS yields a plausible run of digits,
    those become a portal address nobody will ever confirm, and the submission then
    spends two days on the retry schedule.

    The second is that the transcription is never thrown away again. It is written to
    the submission the moment it exists, before any of it is acted on, so a photograph
    sitting in a retry schedule still has a vendor, a total and a date an admin can
    read - and accept, in one click, as the receipt. It used to be discarded the instant
    layer 2 booked a retry, which is how a receipt that had already been read and paid
    for showed an admin nothing at all.
    """
    if not config.is_configured():
        raise ValueError("Instance is not configured with LLM provider and API key.")

    photo_path = submission_photo_path(submission)

    # Layer 0: an earlier attempt already worked out which receipt this is, and only the
    # portal was unwilling. Neither the decoder nor the model can improve on that, and
    # re-running them once per retry would spend a vision call each time to arrive back
    # at the same URL.
    #
    # Not when an admin has declined the rebuild since that attempt: the address in
    # recovered_url is then exactly the guess they have just judged wrong, and going
    # back to the portal with it is the loop they asked to be let out of.
    if submission.recovered_url and not submission.rebuild_declined:
        print(f"[Photo] Retrying the code recovered earlier: {submission.recovered_url}")
        receipt, settled = _verify_photo_against_tra(
            submission, config, submission.recovered_url, source='the code recovered from it')
        if settled:
            return receipt

    # Layer 1: the code is machine-readable after all.
    qr_url = None if submission.recovered_url else _scan_photo_qr(submission, photo_path)
    if qr_url:
        print(f"[Photo] Server-side QR decode succeeded: {qr_url}")
        receipt, settled = _verify_photo_against_tra(
            submission, config, qr_url, source='its QR code')
        if settled:
            return receipt
        print("[Photo] Portal would not confirm the decoded code; reading the photo instead.")

    data = extract_receipt_details(photo_path, True, config)

    # Written before anything is done with it. Everything below this line can end in a
    # retry booked for tomorrow, and until this was stored that outcome took the whole
    # transcription with it - see the docstring.
    _store_llm_draft(submission, data)

    # Layer 2: the model read the code off the paper, so the portal can still be asked.
    # Skipped when a URL has already been recovered - by an earlier attempt or by the
    # decoder above - because that one came from the machine-readable code and the
    # portal has just declined it. Asking again with a transcription of the same code
    # spends a request to be told the same thing.
    document_type = (data.get('document_type') or '').strip() or 'tra_efd_receipt'
    if (document_type == 'tra_efd_receipt' and not submission.recovered_url
            and _may_rebuild_url(submission, config)):
        rebuilt = reconstructed_receipt_url(data)
        if rebuilt:
            print(f"[Photo] Rebuilt a verification URL from the transcription: {rebuilt}")
            receipt, settled = _verify_photo_against_tra(
                submission, config, rebuilt, source='the code printed on it')
            if settled:
                return receipt
            print("[Photo] Portal would not confirm the transcribed code; storing what was read.")

    # Layer 3: what the model read, kept as the model's reading.
    return _receipt_from_transcription(submission, data, config)


def _receipt_from_transcription(submission, data, config):
    """
    A Receipt built out of what the vision model read, or None if it is a duplicate.

    Split out of _receipt_from_photo so the admin page can build the same receipt from
    the same stored transcription (see accept_submission_extraction). One reader of one
    shape of data: a receipt an admin accepts by hand has to be indistinguishable from
    one the pipeline stored on its own, or the two paths drift and only one of them
    keeps getting the fixes.
    """
    document_type = (data.get('document_type') or '').strip() or 'tra_efd_receipt'
    verification_code = (data.get('receipt_verification_code') or '').strip() or None
    if _register_duplicate(submission, verification_code, config):
        return None

    category = data.get('category')
    judgment = {
        'category': category,
        'llm_extracted_description': data.get('llm_extracted_description'),
        'llm_tax_analysis': data.get('llm_tax_analysis'),
        'document_type': document_type,
    }

    # The paper says "VRN: NOT REGISTERED" when the supplier is not VAT registered,
    # and the LLM transcribes that faithfully. Stored as-is it reads as a VRN.
    vrn = normalise_vrn(data.get('vrn'))

    receipt = Receipt(
        vendor=Vendor.upsert(
            tin=data.get('vendor_tin'), name=data.get('vendor_name'),
            vrn=vrn, phone=data.get('vendor_phone'),
        ),
        vendor_name=data.get('vendor_name'), vendor_tin=data.get('vendor_tin'),
        vendor_phone=data.get('vendor_phone'), vrn=vrn,
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
        document_type=document_type,
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

    # Only if nothing better is already there: a code recovered from the QR came off the
    # machine-readable part of the paper, and a transcription of the same characters is
    # not an improvement on it.
    if verification_code and not submission.receipt_code:
        submission.receipt_code = verification_code
    return receipt


def _verify_photo_against_tra(submission, config, url, source):
    """
    Tries to turn a photograph into a verified receipt using a URL recovered from it.

    Returns (receipt, settled). `settled` is the important half: True means this
    submission's outcome is decided and the caller must stop - it verified, or it is a
    duplicate of one we hold, or a retry is booked for later. False means verification
    is off the table for now and the photograph itself is the remaining answer.

    The recovered URL is written to the submission before the portal is called, so a
    retry hours later goes straight back to the same receipt instead of paying for the
    decode and the vision call again, and so an admin looking at a stuck photo can see
    which receipt we think it is.
    """
    submission.recovered_url = url
    submission.receipt_code = _code_from_url(url)
    db.session.commit()

    # Appended to only when the portal has been asked as many times as it is going to
    # be. A flag of its own rather than an inference from submission.status: the status
    # belongs to the task runner and says nothing about whether this path is finished.
    exhausted = []

    try:
        receipt = _receipt_from_tra_url(
            submission, config, url=url,
            on_exhausted=lambda error: exhausted.append(error),
        )
    except TraParseError as e:
        # A URL submission fails here on purpose - guessing at numbers is what this
        # pipeline exists to avoid. A photograph is different: nobody is guessing,
        # there is a picture of the receipt and a model that can read it.
        print(f"[Photo] The verified page did not parse ({e}); reading the photo instead.")
        return None, False

    if receipt is not None:
        # The photograph is still the submission's input, and is still worth looking at,
        # but the numbers on this receipt came from the portal - which is what
        # extraction_source='tra_html', set by the URL path, already records.
        print(f"[Photo] Verified against TRA via {source}: {submission.receipt_code}")

    return receipt, not exhausted

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

    payload = {
        "submission_id": submission.id, "status": "duplicate",
        "error_message": submission.error_message, "device_id": submission.device_id,
    }
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
        "device_id": submission.device_id,
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

    payload = {
        "submission_id": submission_id, "status": "failed",
        "error_message": message, "device_id": submission.device_id,
    }
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

@app.context_processor
def inject_brand():
    """
    Who this instance says it is, available to every template that renders.

    The public pages are built entirely out of it, and the sign-in pages behind them
    share it so an admin arriving from a branded front page does not land on a stock
    form in somebody else's colours. Safe before setup, when there is no config yet.
    """
    return {'brand': branding.of(get_instance_config())}


@app.route('/')
def index():
    """
    One address, two audiences.

    A signed-in admin gets the dashboard, exactly as before. Everybody else gets the
    front door - which is a setting, not a fixture, because an instance's URL is
    shared with bookkeepers and suppliers long before its owner necessarily wants a
    sales page hanging off it. See utils/branding for the three modes.
    """
    if not session.get('admin_logged_in'):
        return _public_front_door()

    return _dashboard()


def _public_front_door():
    """What a stranger sees at '/'."""
    config = get_instance_config()
    if config is None:
        # Nothing has been set up yet, so there is no business to advertise and no
        # admin to sign in as. First run comes before any of this.
        return redirect(url_for('setup'))

    mode = branding.mode_of(config)
    if mode == branding.OFF:
        return redirect(url_for('admin_login'))
    return render_template(f'landing/{mode}.html')


@app.route('/admin/front-page/preview')
@login_required
def preview_landing():
    """
    Either public page, on demand, whatever the saved setting is.

    An admin cannot see their own front door by visiting it - '/' hands them the
    dashboard - and choosing between the two modes is a choice you have to look at.
    """
    mode = request.args.get('mode')
    if mode not in (branding.STORY, branding.SIMPLE):
        mode = branding.mode_of(get_instance_config())
    if mode == branding.OFF:
        mode = branding.SIMPLE
    return render_template(f'landing/{mode}.html')


def _dashboard():
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
        # Powers the 'Process now' button on the Queued tab - the standalone Queue page
        # this used to live on was folded into this dashboard, since it only ever
        # duplicated this tab's own list of pending submissions.
        runner_url=url_for('run_tasks', secret=current_app.config['TASK_RUNNER_SECRET_KEY']),
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
    # Same offline projection Insights draws its whole-book scatter with (utils/geo,
    # no tile server, no API key) - just the one point this receipt was collected at,
    # so a single receipt gets the same visual its aggregate view already has.
    position = geo.parse_location(receipt.submission.location) if receipt.submission else None
    scan = _stored_qr_scan(receipt.submission)
    return render_template(
        'receipt_detail.html',
        receipt=receipt,
        submission=receipt.submission,
        scan=scan, scan_summary=qr.summarise(scan),
        assessment=assess_receipt(receipt, config),
        llm_analysis=judgment.get('llm_tax_analysis'),
        duplicates=find_possible_duplicates(receipt),
        region=geo.region_for(position),
        map_point=geo.to_svg_xy(position) if position else None,
        map_reference=_map_reference_points(),
        siblings=siblings,
        business=config,
        photo_url=submission_photo_url(receipt.submission),
        # Only a receipt read off a photograph is correctable, but the form is described
        # here either way: the template decides whether to offer it, and it must not have
        # a second opinion about which fields exist. See CORRECTABLE_FIELDS.
        correctable=CORRECTABLE_FIELDS,
        correction_values=correction_form_values(receipt),
        corrected_fields=json.loads(receipt.corrected_fields or '[]'),
    )

# Windows the insights page can be run over. Kept only as an inbound alias now: the
# page speaks the dashboard's own filter language (start_date/end_date and the three
# multi-value pickers), and every link made before it did still has to mean what it
# meant. Translated to a date range on the way in, never rendered back out.
INSIGHT_WINDOWS = {'30': 30, '90': 90, '365': 365}


def _read_insight_filters(args):
    """
    The insights page reads the dashboard's filter, in exactly the same words.

    One vocabulary across both pages is the whole point of the pickers living here: a
    range narrowed on this page has to still mean that range when it is opened in the
    ledger, and a supplier pinned from a receipt row has to survive the trip back. The
    alternative - two filter languages that agree until they do not - is how a figure
    quoted off one page ends up disagreeing with the table it was supposedly read from.
    """
    filters = _read_filters(args)
    # Insights are about money that was actually recorded. Which tab a table happens to
    # be standing on is not a question this page can be asked.
    filters['tab'] = 'processed'

    if not filters['start_date'] and not filters['end_date']:
        window = args.get('window')
        if window in INSIGHT_WINDOWS:
            end = datetime.utcnow().date()
            filters['start_date'] = (end - timedelta(days=INSIGHT_WINDOWS[window] - 1)).isoformat()
            filters['end_date'] = end.isoformat()
    return filters


def _insight_linkers(filters):
    """
    The link builders the insights page draws its pickers with.

    Every control on that page is an ordinary link carrying the whole filter with one
    thing changed. No JavaScript, and - more to the point - every state the page can
    reach is a URL somebody can bookmark, paste into a message, or hand to the ledger
    and the CSV so all three are looking at the same rows. Handing the template
    functions rather than pre-built URLs is what lets a facet of fifty values cost one
    line of Jinja.
    """
    def carried(changes):
        merged = {
            'search': filters['search'],
            'category': _selection(filters, 'category'),
            'device': _selection(filters, 'device'),
            'vendor': _selection(filters, 'vendor'),
            'start_date': filters['start_date'],
            'end_date': filters['end_date'],
            **changes,
        }
        # Empty values are dropped rather than sent blank, so 'all time' is an address
        # with no dates in it instead of one carrying two empty ones.
        return {key: value for key, value in merged.items() if value not in ('', None, [])}

    def link(**changes):
        return url_for('insights', **carried(changes))

    def toggle(key, value):
        """The same list with this value added if it is missing, removed if it is not."""
        current = _selection(filters, key)
        remaining = [entry for entry in current if entry != value]
        return link(**{key: remaining if len(remaining) != len(current) else [*current, value]})

    return {
        'filter_link': link,
        'filter_toggle': toggle,
        # The same filter, in the two other places it has to mean the same thing.
        'ledger_url': url_for('index', **carried({})),
        'csv_url': url_for('export_csv', **carried({})),
    }


def _insight_date_presets(today):
    """
    The date shortcuts, computed where the page is rendered.

    The same five the dashboard's drawer used to offer, under the same names, because a
    range people already reach for should not be renamed by having moved. Server-side
    because this page deliberately has no JavaScript to compute them in - which also
    makes them UTC, as every other date on it already is.
    """
    month_start = today.replace(day=1)
    previous_end = month_start - timedelta(days=1)
    return [
        {'label': 'All time', 'start': '', 'end': ''},
        {'label': 'Last 30 days', 'start': (today - timedelta(days=29)).isoformat(),
         'end': today.isoformat()},
        {'label': 'This month', 'start': month_start.isoformat(), 'end': today.isoformat()},
        {'label': 'Last month', 'start': previous_end.replace(day=1).isoformat(),
         'end': previous_end.isoformat()},
        {'label': 'This year', 'start': today.replace(month=1, day=1).isoformat(),
         'end': today.isoformat()},
    ]

@app.route('/insights')
@login_required
def insights():
    """
    Everything the receipts say once you stop looking at them one at a time.

    Also where the filter now lives. The dashboard used to carry the pickers and a
    summary panel beside them, which meant the page you go to for a row was also the
    page you go to for a question, and did neither well. The pickers moved here, where
    the answer to changing one is the whole page rather than a column of it; the
    dashboard kept the table, the tabs and the search.

    Ordered by what it costs to ignore: money that cannot be reclaimed first, then
    money about to become unreclaimable, then where it all went, then what has changed
    in what things cost. Rendered server-side - there is no state to keep here, and a
    page that reports figures should not be able to fail to a blank screen.
    """
    filters = _read_insight_filters(request.args)
    today = datetime.utcnow().date()
    config = get_instance_config()

    # The same rows the dashboard's table and its CSV would hand back under this
    # filter, narrowed to the ones that are money. Selected through a subquery rather
    # than a list of ids so the filter stays one round trip at any size.
    matched = _spending_only(_filtered_submissions(filters, ordered=False))
    receipts = (
        Receipt.query
        .filter(Receipt.submission_id.in_(matched.with_entities(Submission.id)))
        .options(
            selectinload(Receipt.items), selectinload(Receipt.tax_lines),
            joinedload(Receipt.submission), joinedload(Receipt.vendor),
            joinedload(Receipt.device),
        )
        .order_by(Receipt.receipt_date.asc())
        .all()
    )

    # The stretch of calendar the figures actually cover. Not the same as the range
    # asked for: 'all time' names no dates at all, and a range can be far wider than
    # the receipts inside it. A total without its denominator is a number the reader
    # has to trust rather than judge.
    asked_start = _parse_date_arg(filters['start_date'])
    asked_end = _parse_date_arg(filters['end_date'])
    dated = [receipt.receipt_date for receipt in receipts if receipt.receipt_date]
    start = asked_start or (min(dated) if dated else today)
    end = asked_end or (max(dated) if dated else today)
    days = max(1, (end - start).days + 1)

    # Every submission over the same stretch, not just the ones that produced a
    # receipt: a verification failure rate needs the attempts that failed as its
    # numerator and the ones that worked as its denominator. Narrowed by device when
    # the filter names one, since 'which phone is producing bad scans' is the question
    # that section exists to answer.
    attempts = Submission.query.filter(
        Submission.received_at >= datetime.combine(start, datetime.min.time()),
    )
    if asked_end:
        attempts = attempts.filter(
            Submission.received_at < datetime.combine(end + timedelta(days=1), datetime.min.time()),
        )
    devices = _selection(filters, 'device')
    if devices:
        attempts = attempts.filter(Submission.device_id.in_(devices))

    return render_template(
        'insights.html',
        reliability=analytics.verification_reliability(receipts, attempts.all()),
        failure_titles={reason: guidance['title'] for reason, guidance in FAILURE_GUIDANCE.items()},
        days=days, start=start, end=end, today=today,
        receipt_count=len(receipts),
        # The pickers, what there is to pick, and what the picking added up to - read
        # by the same functions the dashboard reads them with, so the two pages cannot
        # report different totals for the same filter.
        filters=filters,
        facets=_filter_facets(filters),
        holds=_filtered_insights(filters),
        date_presets=_insight_date_presets(today),
        **_insight_linkers(filters),
        attention=_attention(receipts, config, today),
        categories=analytics.category_breakdown(receipts),
        regions=analytics.region_breakdown(receipts),
        vendors=analytics.vendor_breakdown(receipts),
        devices=analytics.device_breakdown(receipts),
        weekday=analytics.weekday_pattern(receipts),
        compliance_scoreboard=_compliance_scoreboard(receipts, config),
        months=analytics.monthly_totals(receipts, months=min(12, max(2, days // 30)), today=end),
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

def _tally_checks(tally, assessment):
    """
    Folds one receipt's assessment into a running pass/warn/fail count, keyed by check
    id. Shared by vendor_detail (one supplier's record) and _compliance_scoreboard
    (every receipt in a window), so what "this check failed" means cannot drift
    between the two.
    """
    for check in assessment.checks:
        record = tally.setdefault(check.id, {
            'id': check.id, 'label': check.label, 'pass': 0, 'warn': 0, 'fail': 0,
            'detail': check.detail,
        })
        if check.status in ('pass', 'warn', 'fail'):
            record[check.status] += 1
        # The wording kept is the most recent failure's, because that is the one that
        # says what went wrong rather than what went right.
        if check.status == 'fail':
            record['detail'] = check.detail

def _compliance_scoreboard(receipts, config):
    """
    Pass/warn/fail per compliance check, across every receipt in the window.

    vendor_detail keeps this same tally for one supplier; run over everything, it
    answers "how compliant are we, overall" instead of that being visible only one
    vendor at a time. Sorted worst first - the check with the most failures is the one
    worth a look.
    """
    tally = {}
    for receipt in receipts:
        _tally_checks(tally, assess_receipt(receipt, config))

    scoreboard = []
    for record in tally.values():
        total = record['pass'] + record['warn'] + record['fail']
        scoreboard.append({
            **record,
            'total': total,
            'pass_pct': round(record['pass'] * 100 / total, 1) if total else 0.0,
        })
    scoreboard.sort(key=lambda record: (record['fail'], record['warn']), reverse=True)
    return scoreboard

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

        _tally_checks(tally, assessment)

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

def _stored_qr_scan(submission):
    """
    The server-side QR report kept on a submission, or None if it was never scanned.

    Tolerant of junk on purpose: this is diagnostic detail on a page whose whole job is
    explaining a failure, and it may not fail again to explain itself.
    """
    if submission is None or not submission.qr_scan:
        return None
    try:
        return json.loads(submission.qr_scan)
    except ValueError:
        return None


# What of a stored transcription is worth putting on the submission page, in the order
# somebody checking a document reads it. Deliberately the same fields, and the same
# labels, that CORRECTABLE_FIELDS offers on the receipt page afterwards: this panel is
# the preview of that form, and two lists that disagree would show an admin a figure
# here they then cannot find there.
DRAFT_FIELDS = (
    ('vendor_name', 'Vendor', 'text'),
    ('vendor_tin', 'Vendor TIN', 'text'),
    ('vrn', 'VRN', 'text'),
    ('receipt_date', 'Receipt date', 'text'),
    ('receipt_time', 'Receipt time', 'text'),
    ('receipt_verification_code', 'Verification code', 'text'),
    ('receipt_number', 'Receipt no.', 'text'),
    ('total_amount', 'Total incl. tax', 'money'),
    ('total_excl_tax', 'Total excl. tax', 'money'),
    ('vat_amount', 'Tax', 'money'),
)


def _draft_fields(draft):
    """
    A stored transcription as (label, value) rows, or None when there is none.

    Money is formatted through the same helper the rest of the app uses rather than
    printed as whatever JSON the model returned, so a total reads as a total on a page
    whose whole purpose is letting somebody check it against the paper beside them.
    """
    if not draft:
        return None

    rows = []
    for key, label, kind in DRAFT_FIELDS:
        value = draft.get(key)
        if value is None or value == '':
            continue
        if kind == 'money':
            cents = to_cents(value)
            value = format_cents(cents) if cents is not None else value
        rows.append((label, str(value)))
    return rows


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

    scan = _stored_qr_scan(submission)
    config = get_instance_config()
    return render_template(
        'submission_detail.html',
        submission=submission,
        scan=scan, scan_summary=qr.summarise(scan),
        plan=retry_plan(submission),
        reason=FAILURE_GUIDANCE.get(submission.failure_reason),
        # What the vision model read, and whether the address being retried was built
        # out of it. Together they are the whole of the question this page could not
        # answer before: is this actually a TRA receipt, and if not, what is it?
        draft=_stored_llm_draft(submission),
        draft_fields=_draft_fields(_stored_llm_draft(submission)),
        rebuild_allowed=_may_rebuild_url(submission, config),
        rebuild_allowed_here=(config is None or config.rebuilds_urls_from_text()),
        # Whichever address we would actually ask the portal for. A photograph's lives in
        # recovered_url and a URL submission's in input_data, but the time printed on the
        # receipt is the same fact either way, and it is half of what an admin corrects.
        receipt_time=receipt_time_from_url(submission.recovered_url or submission.input_data),
        region=geo.region_for(geo.parse_location(submission.location)),
        twin=twin,
        photo_url=submission_photo_url(submission),
    )

def requeue_submission(submission_id):
    """
    Puts a failed submission back on the queue. Returns a (body, status) response.

    Most failures here are a vendor who has not uploaded the receipt to TRA yet, so the
    retry count is cleared as well: this is a fresh attempt at a job whose circumstances
    have probably changed, not the next tick of an exhausted schedule.

    Shared by the admin dashboard and the scanner, which reach it through different
    front doors but must not drift into two different definitions of "retry".
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
        # The same public URL every other event carries - never the server-side path.
        'input_data': (submission_photo_url(submission) if submission.input_type == 'photo'
                       else submission.input_data),
        'photo_url': submission_photo_url(submission),
        'description': submission.description,
        'location': submission.location, 'device_id': submission.device_id,
        'device_name': submission.device.name if submission.device else 'Unknown Device',
    }, get_instance_config())

    wake_task_runner()

    return jsonify({'submission_id': submission.id, 'status': submission.status}), 202


@app.route('/submissions/<int:submission_id>/retry', methods=['POST'])
@login_required
def retry_submission(submission_id):
    return requeue_submission(submission_id)


@app.route('/submissions/<int:submission_id>/keep-extraction', methods=['POST'])
@login_required
def keep_submission_extraction(submission_id):
    """
    Stops guessing at TRA and keeps what the vision model read off the photograph.

    The button for the case the pipeline handles worst. Layer 2 of _receipt_from_photo
    rebuilds a portal address out of a code the model transcribed, and on a document
    that is not an EFD receipt at all - a mobile-money SMS, a delivery note, a till slip
    from a shop with no EFD - it rebuilds one anyway, because a plausible run of digits
    is exactly what those documents contain. The address is then never confirmed, the
    submission spends its whole retry schedule finding that out, and the one usable
    reading of the document sits unused on the row the entire time.

    This ends that, with no new vision call and no further requests to the portal:

      * The rebuild is declined for this submission for good, so a later retry or an
        admin pressing "Send to TRA again" cannot resurrect the guessed address.
      * The address the guess produced is cleared, along with the receipt code taken
        from it - both are that guess, and leaving them would go on describing this
        submission as a receipt TRA ought to know about.
      * The stored transcription becomes the receipt, exactly as layer 3 would have
        stored it, flagged extraction_source='llm_vision' so it stays distinguishable
        from figures the portal supplied.

    What it does not do is settle the numbers. They are a model's reading and they are
    editable from the receipt page the moment this returns - which is the point of
    landing there rather than leaving them on a submission nobody can correct.
    """
    submission = db.session.get(Submission, submission_id)
    if submission is None:
        return jsonify({'error': 'No such submission.'}), 404
    if submission.receipt is not None:
        return jsonify({'error': 'This submission already has a receipt behind it.',
                        'receipt_id': submission.receipt.id}), 409

    draft = _stored_llm_draft(submission)
    if not draft:
        return jsonify({
            'error': 'Nothing was read off this photograph yet, so there is nothing to keep. '
                     'That happens when the photo has not reached the vision model - a '
                     'submission still queued for its first attempt, or one whose QR code '
                     'decoded and went straight to the portal.',
        }), 409

    # Declined first and committed with the rest: were this only set after the receipt
    # stored, a failure in between would leave the guessed address live on a submission
    # an admin has already ruled on.
    submission.rebuild_declined = True
    submission.recovered_url = None
    # Re-derived from the transcription rather than cleared, so that every branch below
    # keeps an identity for this document - including the duplicate one, which commits
    # and returns without ever building a receipt to take the code from.
    submission.receipt_code = (draft.get('receipt_verification_code') or '').strip() or None

    config = get_instance_config()
    receipt = _receipt_from_transcription(submission, draft, config)
    if receipt is None:
        # _register_duplicate has already committed the submission as a duplicate of a
        # receipt we hold, which is a real outcome and not a failure.
        db.session.commit()
        return jsonify({'submission_id': submission.id, 'status': submission.status,
                        'message': 'That receipt is already in the ledger, so this '
                                   'submission was recorded as a duplicate.'}), 200

    submission.next_attempt_at = None
    submission.claimed_at = None
    submission.error_message = None
    submission.failure_reason = None
    _complete_submission(submission, receipt, config)

    return jsonify({
        'submission_id': submission.id,
        'receipt_id': receipt.id,
        'status': submission.status,
        'message': 'Kept what was read off the photograph. Correct anything that is wrong '
                   'from here.',
    }), 200


@app.route('/submissions/<int:submission_id>/rebuild-policy', methods=['POST'])
@login_required
def set_submission_rebuild_policy(submission_id):
    """
    Turns the address-guessing on or off for one submission, without settling it.

    Separate from keep-extraction because the two answer different questions. That one
    says "this document is not a TRA receipt, file what we read"; this one says "stop
    rebuilding the address, but keep trying" - which is what an admin wants when the
    photograph *is* a receipt and they intend to type the code in by hand, or when the
    QR is worth another rescan first.

    Declining clears the guessed address so the next attempt starts from the photograph
    again rather than from the digits that failed. Re-allowing it deliberately does not
    put the old address back: the guess is cheap to make again and the stale one was
    wrong often enough to be worth re-deriving.
    """
    submission = db.session.get(Submission, submission_id)
    if submission is None:
        return jsonify({'error': 'No such submission.'}), 404

    declined = (request.form.get('declined') or '').lower() in ('1', 'true', 'on', 'yes')
    submission.rebuild_declined = declined
    if declined and submission.recovered_url and not submission.corrected_at:
        # Only a guessed address is dropped. One an admin typed by hand carries
        # corrected_at, and throwing that away would delete somebody's work.
        submission.recovered_url = None
        submission.receipt_code = None
    db.session.commit()

    return jsonify({
        'submission_id': submission.id,
        'rebuild_declined': submission.rebuild_declined,
        'message': ('Addresses will no longer be rebuilt from text read off this photo.'
                    if declined else
                    'Addresses may be rebuilt from text read off this photo again.'),
    }), 200


@app.route('/submissions/<int:submission_id>/rescan', methods=['POST'])
@login_required
def rescan_submission_photo(submission_id):
    """
    Runs the server-side QR decoder over a stored photograph again, on demand.

    The queue scans a photo once, on the way past, and whatever it found then is what
    the submission has carried ever since. That is the wrong number of times for two
    reasons, and both of them are ordinary rather than exotic.

    The first is that the decoder changes. Its ladder is preprocessing, and preprocessing
    gets better - the sharpening passes in utils/qr read codes the version before them
    could not. Every photo scanned before that improvement holds a 'no code found' that
    is now merely out of date, and there is otherwise no way to ask again short of
    re-uploading a receipt the photographer has long since thrown away.

    The second is that a scan can be run against pixels nobody would choose. A phone
    that uploaded a viewfinder frame instead of a photograph produces an unreadable code
    and a truthful report saying so; once the capture is fixed the old submissions are
    still there, and re-running the decoder is what tells you which of them were the
    camera's fault rather than the receipt's.

    A decode is not left as a nicer diagnostic panel. If a TRA address comes back it is
    put straight to the portal, exactly as a correction typed by hand would be, because
    the entire value of reading the code is the verified receipt on the other side of
    it - see verify_submission_now.
    """
    submission = db.session.get(Submission, submission_id)
    if submission is None:
        return jsonify({'error': 'No such submission.'}), 404

    # Asked of the photograph, not of the input type. A scan whose QR code the phone
    # read now files the picture alongside the code, so a URL submission can have one -
    # and running the decoder over those is the only way to find out what the server
    # side actually reads on ordinary receipts, rather than only on the ones a phone had
    # already failed to decode.
    photo_path = submission_photo_path(submission)
    if not photo_path:
        return jsonify({'error': 'This submission has no photograph to scan.'}), 409
    if not os.path.exists(photo_path):
        return jsonify({'error': 'The photograph for this submission is no longer on disk.'}), 409

    report = qr.scan(photo_path)
    submission.qr_scan = json.dumps(report)
    db.session.commit()

    payload = {
        'submission_id': submission.id,
        'scan': report,
        'summary': qr.summarise(report),
        'verified': False,
    }

    url = report.get('url')
    if not url:
        return jsonify(payload), 200

    # The decode is only worth having if it is acted on. Anything the portal says about
    # it - confirmed, unreachable, already in the ledger - is verify_submission_now's to
    # decide and to record, so that a code recovered here lands in exactly the state a
    # code recovered any other way would.
    config = get_instance_config()
    if not config or not config.is_configured():
        return jsonify({**payload, 'error': 'The code was read, but this instance is not '
                                            'configured to contact TRA.'}), 200

    verification, _status = verify_submission_now(submission, config, url)
    return jsonify({**payload, 'verified': bool(verification.get('verified')),
                    'verification': verification}), 200


# --- CORRECTING BY HAND ---
#
# Everything below exists for the same moment: the automation has done all it can, the
# photograph is on the screen, and a person can read off it what no decoder and no model
# could. Two things can be fixed from there, and they are different in kind.
#
# The first is the address. A receipt is fetched from TRA at <code>_<HHMMSS>, and both
# halves are guesses when the QR code would not decode - so a receipt that exists on the
# portal can sit failing forever behind one misread digit. Correcting those two fields
# is worth more than every other edit combined, because it does not enter data at all:
# it hands the pipeline the right address and the portal supplies the facts, exactly as
# if the code had scanned. See verify_submission_now.
#
# The second is the receipt itself, and only ever one read off a photograph. Those
# numbers are a model's reading of a crumpled thermal print, and some misreadings are
# expensive - see CORRECTABLE_FIELDS. A receipt parsed from TRA's own verified page is
# never editable here: those numbers are the portal's, not ours to revise.


def _read_correction(kind, raw):
    """
    Reads one typed field. Returns (value, problem), where problem is None if it read.

    Blank is a value, not an omission: someone deleting a VRN the model invented is
    saying the supplier has none, and that answer has to be storable.
    """
    text = (raw or '').strip()
    if not text:
        return None, None

    if kind == 'money':
        # Typed off paper, so it arrives as it is printed: '76,000', '76000.00 TZS'.
        amount = to_decimal(re.sub(r'(?i)[,\s]|tzs|/=', '', text))
        if amount is None:
            return None, 'is not a number'
        if amount < 0:
            return None, 'cannot be negative'
        return to_cents(amount), None

    if kind == 'date':
        value = _parse_iso_date(text)
        return (value, None) if value else (None, 'should be a date, as YYYY-MM-DD')

    if kind == 'time':
        value = _parse_iso_time(text)
        return (value, None) if value else (None, 'should be a time, as HH:MM:SS')

    return text, None


def correction_form_values(receipt):
    """This receipt's current values, as the correction form needs to show them."""
    values = {}
    for attribute, _label, kind, _hint in CORRECTABLE_FIELDS:
        value = getattr(receipt, attribute, None)
        if value is None:
            values[attribute] = ''
        elif kind == 'money':
            values[attribute] = format_cents(value)
        elif kind == 'date':
            values[attribute] = value.isoformat()
        elif kind == 'time':
            values[attribute] = value.strftime('%H:%M:%S')
        else:
            values[attribute] = str(value)
    return values


def _apply_corrections(receipt, form):
    """
    Writes hand-read values onto a receipt. Returns (changed labels, problems).

    A field the form did not post is left alone rather than blanked, so a caller may
    correct two fields without having to send back the other eight unchanged.
    """
    changed, problems = [], []

    for attribute, label, kind, _hint in CORRECTABLE_FIELDS:
        if attribute not in form:
            continue

        value, problem = _read_correction(kind, form.get(attribute))
        if problem:
            problems.append(f'{label} {problem}.')
            continue

        # 'VRN: NOT REGISTERED' is printed on the paper and gets typed in as faithfully
        # as the model transcribed it. Stored as-is it reads as a VRN.
        if attribute == 'vrn':
            value = normalise_vrn(value)

        if value != getattr(receipt, attribute):
            setattr(receipt, attribute, value)
            changed.append(label)

    return changed, problems


def _rekey_vendor(receipt, changed):
    """
    Re-files a receipt under the supplier its corrected details now name.

    Suppliers are grouped on TIN rather than on the printed name (see
    Vendor.make_lookup_key), so a corrected TIN is not a cosmetic edit - it moves this
    receipt, and its share of every per-supplier total, from one vendor to another.
    Without this the row would go on pointing at the vendor the wrong TIN created.
    """
    vendor = Vendor.upsert(
        tin=receipt.vendor_tin, name=receipt.vendor_name, vrn=receipt.vrn,
        phone=receipt.vendor_phone, tax_office=receipt.tax_office,
    )
    receipt.vendor = vendor

    # upsert only ever fills a blank in, so that details survive a later receipt printed
    # without them. A hand correction is the opposite case - someone saying this value is
    # wrong - and it has to be able to clear one.
    if vendor is not None and 'VRN' in changed:
        vendor.vrn = receipt.vrn
    return vendor


def _note_correction(record, changed):
    """Records that a human overwrote these fields, cumulatively across corrections."""
    if not changed:
        return
    already = json.loads(record.corrected_fields or '[]')
    record.corrected_fields = json.dumps(sorted(set(already) | set(changed)))
    record.corrected_at = datetime.utcnow()


def verify_submission_now(submission, config, url):
    """
    Asks TRA about this submission right now, and stores whatever the portal says.
    Returns a (payload, status) response for the caller to hand back as JSON.

    The queue's job is to be patient. This one's job is to answer the person who just
    typed a correction and is watching the button, so there is no retry schedule here and
    no ten-second wake-up: one request to the portal, one outcome, reported straight back.

    What becomes of the submission afterwards depends on what it already had behind it. A
    submission with no receipt goes back on the queue when the portal was merely
    unreachable, so the corrected address keeps being tried with nobody watching. One that
    already holds a receipt read off its photograph keeps it either way: a portal we
    cannot reach is not a reason to throw away a receipt we can already read.
    """
    existing = submission.receipt

    submission.recovered_url = url
    submission.receipt_code = _code_from_url(url)
    submission.corrected_at = datetime.utcnow()
    # Claimed before the portal is called, so a runner tick landing mid-request cannot
    # work the same submission alongside us - its claim only ever matches a queued row.
    if existing is None:
        submission.status = 'processing'
        submission.claimed_at = datetime.utcnow()
    db.session.commit()

    print(f"[Correction] Submission {submission.id} re-pointed at {url}")

    try:
        html = fetch_receipt_html(url)
        parsed = parse_receipt_html(html)
    except (TraError, TraParseError) as error:
        return _correction_unverified(submission, existing, error)

    # The corrected address may name a receipt that is already in the ledger under
    # another submission - which is a real answer, not a failure: this photograph is a
    # second copy of a receipt we already hold.
    clash = Receipt.query.filter(
        Receipt.receipt_verification_code == parsed.verification_code,
        Receipt.submission_id != submission.id,
    ).first()
    if clash is not None:
        # Only a submission with nothing of its own becomes the duplicate. One already
        # holding a receipt read off its photograph stays completed and keeps it - it is
        # not a duplicate, it is a receipt whose corrected code turned out to name
        # somebody else's row, and saying otherwise would take it out of the ledger.
        if existing is None:
            submission.status = 'duplicate'
            submission.error_message = f'Duplicate of submission ID {clash.submission_id}'
            db.session.commit()
            dispatch_event('submission.duplicate', {
                'submission_id': submission.id, 'status': submission.status,
                'error_message': submission.error_message, 'device_id': submission.device_id,
            }, config)
        else:
            db.session.commit()

        return {
            'verified': False, 'status': submission.status, 'url': url,
            'receipt_id': clash.id,
            'message': (
                f'That code belongs to receipt #{clash.id}, which is already in the ledger. '
                'Nothing was stored twice.'
            ),
        }, 200

    # The portal answered, so its numbers replace the reading of the photograph. Flushed
    # rather than left to the session's own ordering: receipt.submission_id is unique and
    # SQLAlchemy issues inserts before deletes, so the new row would collide with the old.
    if existing is not None:
        print(f"[Correction] Replacing receipt {existing.id}, read from the photo, with TRA's own page.")
        db.session.delete(existing)
        db.session.flush()

    receipt = _receipt_from_parsed_page(submission, config, parsed, html)
    submission.failure_reason = None
    submission.error_message = None
    submission.next_attempt_at = None
    submission.claimed_at = None
    _complete_submission(submission, receipt, config)

    return {
        'verified': True, 'status': submission.status, 'url': url,
        'receipt_id': receipt.id,
        'message': "TRA confirmed it. This receipt now carries the portal's own numbers.",
    }, 200


def _correction_unverified(submission, existing, error):
    """The portal did not confirm the corrected address. Decides what happens next."""
    reason = type(error).__name__
    guidance = FAILURE_GUIDANCE.get(reason) or {}
    retryable = getattr(error, 'retryable', False)

    submission.failure_reason = reason
    submission.error_message = f'{reason}: {error}'

    if existing is not None:
        # The reading of the photograph is still the best answer we have, and it is still
        # in the ledger. Only the upgrade to TRA's own numbers did not happen.
        submission.status = 'completed'
        tail = 'The reading from the photograph stays in the ledger.'
    elif retryable:
        # The address has changed, so the schedule starts again rather than resuming: the
        # attempts already spent were spent on a different address.
        submission.status = 'queued'
        submission.retry_count = 0
        submission.next_attempt_at = None
        submission.claimed_at = None
        tail = 'The corrected address is saved and back on the queue, so it will keep being tried.'
    else:
        submission.status = 'failed'
        submission.next_attempt_at = None
        submission.claimed_at = None
        tail = 'The corrected address is saved, but nothing will retry on its own.'

    db.session.commit()
    print(f"[Correction] Submission {submission.id} still unverified ({reason}): {error}")

    return {
        'verified': False, 'status': submission.status, 'url': submission.recovered_url,
        'reason': reason, 'guidance': guidance or None,
        'message': f"{guidance.get('title') or 'TRA would not confirm it'}. {tail}",
    }, 200


@app.route('/submissions/<int:submission_id>/correct', methods=['POST'])
@login_required
def correct_submission_url(submission_id):
    """
    Re-points a submission at the receipt its photograph actually names, and asks TRA.

    The two fields this takes - the verification code and the printed time - are the
    whole of the address, and both are legible on the paper long after the QR square
    above them has stopped scanning. Correcting them is the cheapest repair in the
    system: it enters no data at all, it just tells the pipeline where to look.
    """
    submission = db.session.get(Submission, submission_id)
    if submission is None:
        return jsonify({'error': 'No such submission.'}), 404

    config = get_instance_config()
    if config is None:
        return jsonify({'error': 'This instance has not been set up yet.'}), 409

    existing = submission.receipt
    if existing is not None and existing.extraction_source == 'tra_html':
        return jsonify({
            'error': "This receipt was already fetched from TRA's verified page, so its address is "
                     'the one the portal answered to. There is nothing to correct.',
        }), 409

    try:
        url = build_receipt_url(
            request.form.get('receipt_code'), request.form.get('receipt_time'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    if url == submission.recovered_url and submission.status == 'processing':
        return jsonify({'error': 'That address is being checked right now.'}), 409

    payload, status = verify_submission_now(submission, config, url)
    return jsonify(payload), status


@app.route('/receipts/<int:receipt_id>/correct', methods=['POST'])
@login_required
def correct_receipt(receipt_id):
    """
    Overwrites what the model read off a photograph with what a human reads off it.

    Refused outright for a receipt parsed from TRA's verified page. Those numbers are the
    portal's own record of the sale and the whole pipeline is built on preferring them to
    anybody's reading, this one included - a TRA receipt that looks wrong is a parser
    problem, not a typing problem.

    With `verify` set, the corrected code and time are then put to the portal, and if it
    answers, everything typed here is superseded by TRA's own page. That is the intended
    ending: hand-read values are the fallback, never the goal.
    """
    receipt = db.session.get(Receipt, receipt_id)
    if receipt is None:
        return jsonify({'error': 'No such receipt.'}), 404

    if receipt.extraction_source == 'tra_html':
        return jsonify({
            'error': "These numbers were parsed from TRA's own verified page, so they are not edited "
                     'by hand. If they are wrong, the page has changed and the parser needs a look.',
        }), 409

    code = (request.form.get('receipt_verification_code') or '').strip()
    if code:
        clash = Receipt.query.filter(
            Receipt.receipt_verification_code == code, Receipt.id != receipt.id,
        ).first()
        if clash is not None:
            return jsonify({
                'error': f'Receipt #{clash.id} already carries that verification code. Two receipts '
                         'cannot share one - check whether this is the same purchase twice.',
            }), 409

    changed, problems = _apply_corrections(receipt, request.form)
    if problems:
        db.session.rollback()
        return jsonify({'error': ' '.join(problems)}), 400

    _rekey_vendor(receipt, changed)
    _note_correction(receipt, changed)
    db.session.commit()

    if changed:
        print(f"[Correction] Receipt {receipt.id} corrected by hand: {', '.join(changed)}")

    saved = (
        f"Saved. {len(changed)} field{'' if len(changed) == 1 else 's'} now read as you entered "
        f"{'it' if len(changed) == 1 else 'them'}." if changed else 'Nothing was changed.'
    )

    if request.form.get('verify') not in ('1', 'true', 'on'):
        return jsonify({
            'verified': False, 'changed': changed, 'receipt_id': receipt.id, 'message': saved,
        }), 200

    config = get_instance_config()
    if config is None:
        return jsonify({'error': 'This instance has not been set up yet.'}), 409

    try:
        url = build_receipt_url(receipt.receipt_verification_code, receipt.receipt_time)
    except ValueError as e:
        return jsonify({
            'verified': False, 'changed': changed, 'receipt_id': receipt.id,
            'message': f'{saved} TRA could not be asked, though: {e}',
        }), 200

    payload, status = verify_submission_now(receipt.submission, config, url)
    payload['changed'] = changed
    payload['message'] = f"{saved} {payload['message']}"
    return jsonify(payload), status

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
        active_tab = request.form.get('active_tab', 'business')
        
        # Save all the form data. The business identity is blanked to NULL rather than
        # stored as '', because the compliance checks read "no TIN set" from it.
        def optional(field):
            return (request.form.get(field) or '').strip() or None

        config.business_name = optional('business_name')
        config.business_tin = optional('business_tin')
        config.business_vrn = optional('business_vrn')

        # The public page. Everything here is optional and every blank falls back to
        # something sensible in utils/branding, so a half-filled form still renders a
        # page rather than an empty one. Guarded as a group: a post that does not carry
        # the front-page fields at all is one that should leave the front page alone,
        # not one that should quietly reset it to the default.
        if 'landing_mode' in request.form:
            mode = (request.form.get('landing_mode') or '').strip().lower()
            config.landing_mode = mode if mode in branding.MODES else branding.DEFAULT_MODE
            config.brand_name = optional('brand_name')
            config.brand_tagline = optional('brand_tagline')
            config.brand_accent = optional('brand_accent')
            config.brand_logo_url = optional('brand_logo_url')
            config.landing_cta_label = optional('landing_cta_label')
            config.landing_cta_url = optional('landing_cta_url')
        config.llm_provider = request.form.get('llm_provider')
        config.llm_api_key = request.form.get('llm_api_key')
        # Blank means "use the built-in candidates", which is what almost every
        # instance wants - so an empty box has to store NULL, not ''.
        config.llm_text_model = optional('llm_text_model')
        config.llm_vision_model = optional('llm_vision_model')
        # A checkbox posts nothing at all when it is unticked, so the tab it lives on
        # has to be identified some other way before its absence can mean 'off'. Without
        # this guard, saving any other tab would silently switch the rebuild off.
        if 'llm_settings' in request.form:
            config.rebuild_url_from_text = 'rebuild_url_from_text' in request.form
        config.google_sheet_id = request.form.get('google_sheet_id')
        config.google_service_account_json = request.form.get('google_service_account_json')
        config.post_callback_url = request.form.get('post_callback_url')
        config.s3_bucket_name = request.form.get('s3_bucket_name')
        config.s3_access_key_id = request.form.get('s3_access_key_id')
        config.s3_secret_access_key = request.form.get('s3_secret_access_key')
        config.s3_region = request.form.get('s3_region')
        
        db.session.commit()
        flash('Settings saved successfully!', 'success')

        # Redirect back to the configuration page, passing the active tab as a URL parameter
        return redirect(url_for('configure_instance', tab=active_tab))

    # For GET requests, get the active tab from the URL, defaulting to 'business'
    active_tab = request.args.get('tab', 'business')
    devices = Device.query.all()
    
    # Pass the active_tab variable to the template
    return render_template(
        'admin/configure.html', config=config, devices=devices, active_tab=active_tab,
        # Photographed receipts degrade quietly without it - they are read by the model
        # instead of verified against TRA - so the one place it can be noticed is here.
        qr_decoder_problem=qr.unavailable_reason(),
    )

# --- DEVICE MANAGEMENT ---

def _activation_url(device):
    """The link an admin hands out. Also what the activation QR encodes."""
    if not device.enrolment_token:
        return None
    return url_for('scan_activate', token=device.enrolment_token, _external=True)


def _device_rows():
    """
    Devices with everything the admin list needs, counted in two queries rather than
    two per device.
    """
    submission_counts = dict(
        db.session.query(Submission.device_id, db.func.count(Submission.id))
        .group_by(Submission.device_id).all()
    )
    receipt_counts = dict(
        db.session.query(Receipt.device_id, db.func.count(Receipt.id))
        .group_by(Receipt.device_id).all()
    )

    rows = []
    for device in Device.query.order_by(Device.id.asc()).all():
        submissions = submission_counts.get(device.id, 0)
        receipts = receipt_counts.get(device.id, 0)
        activation = _activation_url(device)
        rows.append({
            'device': device,
            'submission_count': submissions,
            'receipt_count': receipts,
            'activation_url': activation,
            'activation_qr': generate_qr_code_base64(activation) if activation else None,
            # A device with history cannot be deleted without orphaning rows whose
            # device_id is NOT NULL, so the UI offers revoking instead.
            'deletable': submissions == 0 and receipts == 0,
        })
    return rows


def _get_device_or_404(device_id):
    device = db.session.get(Device, device_id)
    if device is None:
        flash('No such device.', 'danger')
    return device


@app.route('/admin/devices')
@login_required
def manage_devices():
    return render_template(
        'admin/devices.html',
        rows=_device_rows(),
        just_created=request.args.get('new', type=int),
    )


@app.route('/admin/devices', methods=['POST'])
@login_required
def add_device():
    device_name = (request.form.get('device_name') or '').strip()
    if not device_name:
        flash('Device name cannot be empty.', 'danger')
        return redirect(url_for('manage_devices'))

    new_device = Device(name=device_name, created_at=datetime.utcnow())
    db.session.add(new_device)
    # Flushed so the device has an id, which the token embeds - see utils/device_auth.
    db.session.flush()
    issue_enrolment_token(new_device)
    db.session.commit()

    flash(f'Device "{device_name}" added. Scan or send its activation link below.', 'success')
    return redirect(url_for('manage_devices', new=new_device.id))


@app.route('/admin/devices/<int:device_id>/rename', methods=['POST'])
@login_required
def rename_device(device_id):
    device = _get_device_or_404(device_id)
    if device is None:
        return redirect(url_for('manage_devices'))

    name = (request.form.get('device_name') or '').strip()
    if not name:
        flash('Device name cannot be empty.', 'danger')
    else:
        device.name = name
        db.session.commit()
        flash(f'Device renamed to "{name}".', 'success')
    return redirect(url_for('manage_devices'))


@app.route('/admin/devices/<int:device_id>/issue-link', methods=['POST'])
@login_required
def issue_device_link(device_id):
    """
    Mints a new activation link, invalidating any outstanding one.

    This is the routine path, not an exceptional one: activation tokens are single
    use, so replacing a phone means issuing another. The device keeps its identity and
    therefore its receipt history.
    """
    device = _get_device_or_404(device_id)
    if device is None:
        return redirect(url_for('manage_devices'))
    if device.is_revoked:
        flash('Restore this device before issuing a new activation link.', 'danger')
        return redirect(url_for('manage_devices'))

    issue_enrolment_token(device)
    db.session.commit()
    flash(f'New activation link issued for "{device.name}". Any earlier link is now dead.', 'success')
    return redirect(url_for('manage_devices', new=device.id))


@app.route('/admin/devices/<int:device_id>/sign-out', methods=['POST'])
@login_required
def sign_out_device(device_id):
    device = _get_device_or_404(device_id)
    if device is None:
        return redirect(url_for('manage_devices'))

    end_session(device)
    db.session.commit()
    flash(f'"{device.name}" has been signed out. Issue an activation link to bring it back.', 'success')
    return redirect(url_for('manage_devices'))


@app.route('/admin/devices/<int:device_id>/revoke', methods=['POST'])
@login_required
def revoke_device(device_id):
    """Takes a device out of service without touching the receipts it submitted."""
    device = _get_device_or_404(device_id)
    if device is None:
        return redirect(url_for('manage_devices'))

    device.revoked_at = datetime.utcnow()
    device.enrolment_token = None
    device.enrolment_issued_at = None
    end_session(device)
    db.session.commit()
    flash(f'"{device.name}" revoked. Its API key and session no longer work.', 'success')
    return redirect(url_for('manage_devices'))


@app.route('/admin/devices/<int:device_id>/restore', methods=['POST'])
@login_required
def restore_device(device_id):
    device = _get_device_or_404(device_id)
    if device is None:
        return redirect(url_for('manage_devices'))

    device.revoked_at = None
    issue_enrolment_token(device)
    db.session.commit()
    flash(f'"{device.name}" restored with a fresh activation link.', 'success')
    return redirect(url_for('manage_devices', new=device.id))


@app.route('/admin/devices/<int:device_id>/rotate-key', methods=['POST'])
@login_required
def rotate_device_key(device_id):
    """Issues a new API key for a server-side integration. Breaks the old one."""
    device = _get_device_or_404(device_id)
    if device is None:
        return redirect(url_for('manage_devices'))

    device.api_key = str(uuid.uuid4())
    db.session.commit()
    flash(f'API key rotated for "{device.name}". Update any integration using the old key.', 'success')
    return redirect(url_for('manage_devices'))


@app.route('/admin/devices/<int:device_id>/delete', methods=['POST'])
@login_required
def delete_device(device_id):
    """
    Removes a device outright, but only one that never submitted anything.

    Submission.device_id and Receipt.device_id are NOT NULL with no ON DELETE, so
    deleting a device with history would leave rows pointing at nothing. Revoking is
    the answer for those, and the UI only ever offers delete where it is safe.
    """
    device = _get_device_or_404(device_id)
    if device is None:
        return redirect(url_for('manage_devices'))

    has_history = (
        Submission.query.filter_by(device_id=device.id).first() is not None
        or Receipt.query.filter_by(device_id=device.id).first() is not None
    )
    if has_history:
        flash(
            f'"{device.name}" has submitted receipts and cannot be deleted without '
            'orphaning them. Revoke it instead.', 'danger'
        )
        return redirect(url_for('manage_devices'))

    name = device.name
    db.session.delete(device)
    db.session.commit()
    flash(f'Device "{name}" deleted.', 'success')
    return redirect(url_for('manage_devices'))


# --- SCANNER PWA ---
#
# Everything the field app needs lives under /scan/, and its service worker is scoped
# to that prefix. The admin dashboard, /stream and /api/* are therefore not reachable
# by the worker at all, so no scanner bug can serve a stale financial view or sit on
# an open event stream. It also means no Service-Worker-Allowed header is needed.
#
# The three shells are deliberately unauthenticated. A service worker has to be able
# to precache them and hand them to a phone that is offline, or offline and signed
# out; they carry no data, and every byte of data behind them is on /scan/api/*.

SCAN_HISTORY_PAGE_SIZE = 50


# All three scan routes render the same document.
#
# The field app is one page: which of Scan, History and Diagnostics is showing is
# decided in the browser from the URL, by static/js/router.js, and moving between them
# never reloads. The routes stay because the URLs are real - a deep link, a bookmark, a
# refresh and the back button all still work exactly as they did when these were three
# templates - but serving a different document per route would defeat the point.
#
# What that buys is in router.js; the headline is that a navigation destroys the camera,
# and on WebKit getting it back costs the user a permission prompt every single time.
SCAN_SHELL = 'scan/shell.html'


@app.route('/scan/')
def scan_home():
    """The scanner. This is the PWA's start_url and the whole point of the app."""
    return render_template(SCAN_SHELL)


@app.route('/scan/history')
def scan_history():
    return render_template(SCAN_SHELL)


@app.route('/scan/diagnostics')
def scan_diagnostics():
    return render_template(SCAN_SHELL)


@app.route('/scan/a/<token>')
def scan_activate(token):
    """
    Where an activation link lands.

    Renders; does not activate. Spending a single-use credential on a mere GET would
    let a link preview or a mail scanner burn it before the field user ever sees it,
    so the token is only consumed by the explicit POST the page makes.
    """
    return render_template('scan/activate.html', token=token)


@app.route('/scan/sw.js')
def scan_service_worker():
    """
    Served from a route rather than /static so the version bump inside it can never be
    masked by an HTTP cache. A service worker nobody can update is a bricked app.
    """
    response = make_response(send_from_directory(app.static_folder, 'js/service-worker.js'))
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@app.route('/scan/manifest.json')
def scan_manifest():
    """
    The install manifest, optionally carrying an activation token.

    `?t=<token>` puts the token in `start_url`, so a phone that adds the app to its
    home screen from an activation link launches straight into activating itself. That
    is the only thing that makes this work on iOS, where a home-screen app does not
    share storage with Safari: activating in the browser first leaves the installed app
    signed out, and the single-use token already spent. Carrying it through the install
    means the token is spent once, in the storage the app will actually run in.

    `id` is fixed so the browser still recognises every one of these as the same
    installed app, however the start_url differs.

    Everything a person sees comes from the instance's own branding rather than from
    this file. An installed app is an icon and a caption on somebody's home screen,
    sitting next to their bank and their WhatsApp, and "Receipts" is the caption of an
    app that could belong to anyone - which is exactly wrong for a tool a business hands
    to its drivers and its shop staff. It is that business's app; it says so.
    """
    brand = branding.of(get_instance_config())

    token = (request.args.get('t') or '').strip()
    start_url = url_for('scan_home', t=token) if token else '/scan/'

    return jsonify({
        'name': f'{brand.name} Receipts',
        # What fits under an icon. See Brand.short_name - a long business name is cut at
        # a word rather than left to the operating system's ellipsis.
        'short_name': brand.short_name,
        'description': f'Scan and submit EFD receipts for {brand.name}, online or off.',
        'id': '/scan/',
        'start_url': start_url,
        'scope': '/scan/',
        'display': 'standalone',
        'orientation': 'portrait',
        'background_color': '#000000',
        'theme_color': '#000000',
        'icons': [
            {'src': url_for('static', filename='icons/icon-192.png'),
             'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': url_for('static', filename='icons/icon-512.png'),
             'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': url_for('static', filename='icons/icon-maskable-512.png'),
             'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    })


# --- Scanner API ---

@app.route('/scan/api/activate', methods=['POST'])
def scan_api_activate():
    """
    Spends an activation token and returns the session token the phone will hold.

    Activating necessarily signs out whichever phone held this device before; see
    utils/device_auth.start_session.
    """
    payload = request.get_json(silent=True) or {}
    token = (payload.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'No activation token supplied.', 'reason': 'unknown'}), 400

    session_token, result = consume_enrolment_token(token, user_agent=request.headers.get('User-Agent'))
    if session_token is None:
        reason = result
        return jsonify({
            'error': REJECTION_MESSAGES.get(reason, REJECTION_MESSAGES['unknown']),
            'reason': reason,
        }), 401

    device = result
    return jsonify({
        'session_token': session_token,
        'device': {'id': device.id, 'name': device.name},
    }), 200


@app.route('/scan/api/me')
@device_required
def scan_api_me():
    device = g.device
    return jsonify({
        'device': {'id': device.id, 'name': device.name},
        'server_time': datetime.utcnow().isoformat(),
    })


def _parse_captured_at(value):
    """A client clock is not to be trusted with anything but its own timeline."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


@app.route('/scan/api/sync', methods=['POST'])
@device_required
def scan_api_sync():
    """
    Takes a batch of scanned receipt URLs from a device's outbox.

    Every item carries the client_uuid the phone minted for it, and the response maps
    each one to its submission so the phone knows exactly what to clear. Items are
    handled independently: one malformed row does not cost the other twenty-nine.
    """
    payload = request.get_json(silent=True) or {}
    items = payload.get('items')
    if not isinstance(items, list):
        return jsonify({'error': '`items` must be a list.'}), 400
    if len(items) > SCAN_HISTORY_PAGE_SIZE:
        return jsonify({'error': f'At most {SCAN_HISTORY_PAGE_SIZE} items per request.'}), 413

    results = []
    accepted = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        client_uuid = (item.get('client_uuid') or '').strip()
        receipt_url = (item.get('receipturl') or '').strip()
        if not client_uuid or not receipt_url:
            results.append({'client_uuid': client_uuid or None, 'status': 'rejected',
                            'error': 'client_uuid and receipturl are both required.'})
            continue

        submission, created = ingest_submission(
            g.device,
            url=receipt_url,
            description=item.get('description'),
            location=item.get('location'),
            client_uuid=client_uuid,
            captured_at=_parse_captured_at(item.get('captured_at')),
        )
        accepted += 1
        results.append({
            'client_uuid': client_uuid,
            'submission_id': submission.id,
            # 'duplicate' means we already had this exact scan, so the phone can drop
            # it from the outbox just as confidently as if it were new.
            'status': 'accepted' if created else 'duplicate',
        })

    # One wake-up for the whole batch. A device back from a day offline should not
    # spawn thirty runner triggers against a rate-limited portal.
    if accepted:
        wake_task_runner()

    return jsonify({'results': results}), 200


@app.route('/scan/api/sync/photo', methods=['POST'])
@device_required
def scan_api_sync_photo():
    """
    Takes one photo. Deliberately not batched: a multi-megabyte blob that fails
    should not take the rest of the queue down with it.

    `receipturl` is optional and is what makes this the endpoint for a scan the phone
    *did* decode, as well as one it did not. The two used to be different submissions
    through different doors - a decoded code went up in the JSON batch and the picture
    it was read from was dropped on the phone - which left every verified receipt with
    no image behind it. Sending both here files them as one submission: processed as the
    URL, with the photograph kept beside it.
    """
    photo = request.files.get('receiptphoto')
    client_uuid = (request.form.get('client_uuid') or '').strip()
    receipt_url = (request.form.get('receipturl') or '').strip() or None
    if not photo:
        return jsonify({'error': '`receiptphoto` is required.'}), 400
    if not client_uuid:
        return jsonify({'error': '`client_uuid` is required.'}), 400

    submission, created = ingest_submission(
        g.device,
        photo=photo,
        url=receipt_url,
        description=request.form.get('description'),
        location=request.form.get('location'),
        client_uuid=client_uuid,
        captured_at=_parse_captured_at(request.form.get('captured_at')),
    )
    wake_task_runner()

    return jsonify({
        'client_uuid': client_uuid,
        'submission_id': submission.id,
        'status': 'accepted' if created else 'duplicate',
    }), 200


@app.route('/scan/api/submissions')
@device_required
def scan_api_submissions():
    """
    This device's own history, for the phone to cache and read back offline.

    Two cursors, two purposes: `since` catches up on anything new (what the regular
    poll/boot sync asks for), `before_id` pages backwards into older history (what the
    history screen's infinite scroll asks for). Ordered by id rather than received_at -
    equivalent for this single-writer-per-row app, but an integer PK range is what
    the covering index and the before_id cursor are both built on.

    `q`, given, searches vendor, receipt code and line items - still scoped to this
    device, still paginated, so a match past the requested page simply is not returned
    yet, same as any other row.
    """
    query = Submission.query.options(
        # All three chains load through the same Submission.receipt hop, so it must use
        # one consistent strategy - mixing joinedload and selectinload on the same path
        # is a SQLAlchemy error, not just wasteful.
        selectinload(Submission.receipt).selectinload(Receipt.items),
        selectinload(Submission.receipt).selectinload(Receipt.tax_lines),
        selectinload(Submission.receipt).joinedload(Receipt.vendor),
        joinedload(Submission.device),
    ).filter(Submission.device_id == g.device.id)

    since = _parse_captured_at(request.args.get('since'))
    if since is not None:
        query = query.filter(Submission.received_at >= since)

    before_id = request.args.get('before_id', type=int)
    if before_id is not None:
        query = query.filter(Submission.id < before_id)

    q = (request.args.get('q') or '').strip()
    if q:
        like = f'%{q}%'
        query = query.outerjoin(Submission.receipt).outerjoin(Receipt.items).filter(
            db.or_(
                Submission.receipt_code.ilike(like),
                Submission.description.ilike(like),
                Receipt.vendor_name.ilike(like),
                Receipt.vendor_tin.ilike(like),
                ReceiptItem.description.ilike(like),
            )
        ).distinct()

    limit = request.args.get('limit', type=int) or SCAN_HISTORY_PAGE_SIZE
    limit = max(1, min(limit, SCAN_HISTORY_PAGE_SIZE))

    rows = query.order_by(Submission.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    return jsonify({
        'submissions': prepare_submissions_for_frontend(rows, detailed=False),
        'has_more': has_more,
        'server_time': datetime.utcnow().isoformat(),
    })


@app.route('/scan/api/submissions/<int:submission_id>/retry', methods=['POST'])
@device_required
def scan_api_retry(submission_id):
    """Re-queues a failed submission, but only one this device sent."""
    submission = db.session.get(Submission, submission_id)
    if submission is None or submission.device_id != g.device.id:
        return jsonify({'error': 'No such submission.'}), 404
    return requeue_submission(submission_id)


def _device_summary(device_id):
    """
    The handful of numbers the field app puts above its list.

    Deliberately small. This is the top of a phone screen belonging to someone standing
    in a shop, not a report: what they have captured this month, what tax that carries,
    and whether anything needs them to do something about it. Everything else is a tap
    away in the list below it.

    Money is summed in SQL over cents, never assembled in Python from floats - see
    utils/money. Cancelled and test receipts are excluded here exactly as they are on
    the dashboard, because a voided receipt is not money anybody spent.

    Counted by when this device captured the receipt, not by the date printed on it -
    the opposite of the admin dashboard, deliberately. The dashboard reports spending,
    so it keys off the receipt date. This screen reports a person's own work, and
    somebody who spends an afternoon clearing a shoebox of last quarter's receipts has
    to see that afternoon's work, not a month that reads zero.
    """
    today = date.today()
    month_start = today.replace(day=1)
    captured_on = db.func.date(Receipt.processed_at)

    def totals_since(start):
        row = db.session.query(
            db.func.count(Receipt.id),
            db.func.coalesce(db.func.sum(Receipt.total_incl_tax_cents), 0),
            db.func.coalesce(db.func.sum(Receipt.total_tax_cents), 0),
        ).filter(
            Receipt.device_id == device_id,
            Receipt.is_cancelled.is_(False),
            Receipt.is_test.is_(False),
            captured_on >= start,
        ).one()
        return {'receipts': row[0] or 0, 'spend_cents': int(row[1] or 0), 'vat_cents': int(row[2] or 0)}

    in_flight = db.session.query(db.func.count(Submission.id)).filter(
        Submission.device_id == device_id,
        Submission.status.in_(('queued', 'processing')),
    ).scalar() or 0

    # Bounded to a month: a failure from six weeks ago is history, and a permanent
    # badge counting it is a badge nobody can ever clear.
    needs_attention = db.session.query(db.func.count(Submission.id)).filter(
        Submission.device_id == device_id,
        Submission.status == 'failed',
        Submission.received_at >= datetime.utcnow() - timedelta(days=30),
    ).scalar() or 0

    return {
        'month': totals_since(month_start),
        'today': totals_since(today),
        'month_label': today.strftime('%B'),
        'in_flight': in_flight,
        'needs_attention': needs_attention,
        'server_time': datetime.utcnow().isoformat(),
    }


@app.route('/scan/api/summary')
@device_required
def scan_api_summary():
    """This device's own numbers, for the top of the field app."""
    return jsonify(_device_summary(g.device.id))


def sse_response(cursor, device_id=None):
    """
    One open event stream, with the headers that decide whether it works at all.

    The generator runs long after the request that started it has been torn down, so it
    pushes its own app context: it needs a database session of its own to read the
    event log through, and reaching for the request's would be reading through a
    session somebody else is entitled to close.

    The headers are not boilerplate. Without `X-Accel-Buffering: no` an nginx-style
    proxy buffers the response and hands it over only once it is complete - which for a
    stream that never completes means the browser sits on an open connection receiving
    nothing, forever, which is indistinguishable from the feature being broken. Without
    `no-transform` an intermediary is free to compress it and buffer to do so.
    """
    def frames():
        with app.app_context():
            try:
                yield from sse_broker.stream(cursor, device_id=device_id)
            finally:
                db.session.remove()

    response = app.response_class(frames(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache, no-store, no-transform, must-revalidate'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response


@app.route('/scan/api/stream')
@device_required
def scan_api_stream():
    """
    Pushes this device's own submission events as they happen - queued, processing,
    processed, failed, duplicate, retry_scheduled - so the app can update a receipt's
    status without waiting for the next poll.

    Reads the same event log the admin dashboard's /stream reads, filtered to this
    device by an indexed WHERE clause rather than by inspecting payloads: the one thing
    this endpoint must never do is show one device another's receipts, and an exact
    match on a column cannot half-work the way string matching on a payload can.

    Not built on EventSource, deliberately: device_required reads the session from an
    Authorization header (see its own docstring on why - never a cookie), and
    EventSource cannot set one. The client instead reads this with fetch() + a stream
    reader, sending the same header every other /scan/api/* call already does, and
    passes its cursor as ?since= because it has no Last-Event-ID to resend.
    """
    # Captured before the generator: the request context is not guaranteed to still be
    # meaningful once the response is being streamed out, so nothing below touches g.
    return sse_response(sse_broker.parse_cursor(request), device_id=g.device.id)


# --- INTAKE & TASK RUNNER ENDPOINTS ---

def ingest_submission(device, photo=None, url=None, description=None, location=None,
                      client_uuid=None, captured_at=None):
    """
    Puts one receipt on the queue. Returns (submission, created).

    The single place a submission is born, shared by the bot endpoint and the
    scanner's sync. Photos are stored as a bare filename for the backend and announced
    as a public URL for the dashboard - two different strings for two different readers,
    which is the one subtlety here.

    `photo` and `url` are not exclusive. A phone that decodes a receipt's QR code is
    holding a photograph of that receipt at the same instant, and sending both is what
    keeps the paper behind the verified figures - see Submission.photo_filename.

    Idempotent on client_uuid: an offline device retries a scan until it is
    acknowledged, and a response lost on the way back must not leave a second copy
    behind. Re-ingesting a known uuid returns the original submission untouched.

    Does *not* trigger the task runner. That is the caller's job, because a device
    coming back from a day offline syncs thirty receipts in one request and should
    wake the runner once, not thirty times.
    """
    if client_uuid:
        existing = Submission.query.filter_by(client_uuid=client_uuid).first()
        if existing is not None:
            return existing, False

    input_type = ''
    # What gets saved to the database: a bare filename for photos, the URL itself
    # for URL submissions.
    db_input_data = ''
    # This will be the path sent to the frontend via SSE.
    frontend_input_data = ''
    # The photograph filed beside a URL, when the scan carried both.
    photo_filename = None

    if photo:
        filename = secure_filename(f"{datetime.utcnow().timestamp()}_{photo.filename}")

        # Not photo.save(). What arrives can be a 12MP frame straight off a sensor, and
        # every pixel past utils.images.STORED_MAX_EDGE is one the decoder discards on
        # every pass, the vision model is billed for base64-encoding, and the
        # persistence volume then carries for good. store_photo bounds it - and leaves
        # it alone when it already is, which is what the scanner's own uploads are.
        #
        # Reassigned, because a re-encode also settles the extension: what is stored is
        # the name of the file that actually exists, not the one that was uploaded.
        photo_filename = store_photo(photo, app.config['UPLOAD_FOLDER'], filename)

    if url:
        # A URL wins the input_type even when a photograph came with it, and that
        # ordering is the whole point of accepting both. The code is the stronger claim
        # about which receipt this is - it goes to TRA and comes back with the portal's
        # own figures - so the submission is processed as the URL it is, and the picture
        # rides along as evidence rather than as a second thing to read. Only when the
        # code is absent is the photograph the input.
        input_type = 'url'
        # For URLs, the path is the same for both backend and frontend.
        db_input_data = url
        frontend_input_data = url

    elif photo_filename:
        input_type = 'photo'
        # Only the filename is stored. Writing the absolute path here would tie every
        # row to wherever the persistence volume happened to be mounted that day, and
        # moving the volume would orphan every photo already in the database.
        db_input_data = photo_filename
        frontend_input_data = url_for('uploaded_file', filename=photo_filename)
        # Already the input; a second copy of the name in photo_filename would leave two
        # columns to keep in step for no gain. submission_photo_name reads both.
        photo_filename = None

    new_submission = Submission(
        device_id=device.id, input_type=input_type,
        input_data=db_input_data, # Save the full filesystem path to the DB
        photo_filename=photo_filename,
        description=description, location=location,
        client_uuid=client_uuid, captured_at=captured_at,
        # Read before anything is queued. A submission that never verifies still has
        # its receipt's identity on it, which is what the admin needs to chase it.
        receipt_code=_code_from_url(db_input_data) if input_type == 'url' else None,
    )
    db.session.add(new_submission)
    db.session.commit()

    payload = {
        "id": new_submission.id, "device_name": device.name, "status": new_submission.status,
        "received_at": new_submission.received_at.isoformat(),
        "input_type": new_submission.input_type,
        "input_data": frontend_input_data, # Send the public URL to the frontend
        "photo_url": (url_for('uploaded_file', filename=photo_filename)
                      if photo_filename else (frontend_input_data if input_type == 'photo' else None)),
        "description": new_submission.description, "location": new_submission.location,
        "device_id": device.id,
    }
    dispatch_event('submission.queued', payload, get_instance_config())
    return new_submission, True


def wake_task_runner():
    """Nudges the in-app cron so a fresh submission is not left waiting for a tick."""
    runner_secret = current_app.config['TASK_RUNNER_SECRET_KEY']
    runner_url = url_for('run_tasks', secret=runner_secret, _external=True)
    gevent.spawn(trigger_url_in_background, runner_url)


@app.route('/receipt', methods=['POST'])
def receipt_endpoint():
    """
    Handles new submissions from a device holding a long-lived API key.

    This is the original integration contract - a WhatsApp bot and anything else built
    against it hold these keys - so it is kept exactly as it was.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization header is missing or invalid'}), 401

    device_key = auth_header.split(' ')[1]
    device = Device.query.filter_by(api_key=device_key).first()
    if not device:
        return jsonify({'error': 'Invalid device API key'}), 403
    if device.is_revoked:
        return jsonify({'error': 'This device has been revoked.'}), 403

    receipt_photo = request.files.get('receiptphoto')
    receipt_url = request.form.get('receipturl')
    if not receipt_photo and not receipt_url:
        return jsonify({'error': '`receiptphoto` (file) or `receipturl` (form field) is required'}), 400

    submission, _created = ingest_submission(
        device,
        photo=receipt_photo, url=receipt_url,
        description=request.form.get('description'),
        location=request.form.get('location'),
    )
    wake_task_runner()

    return jsonify({ "message": "Receipt accepted and queued for processing.", "submission_id": submission.id }), 202

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
    # Read once and reused for every job claimed this tick, rather than a query per
    # claim - it does not change mid-run.
    config = get_instance_config()
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

        # The one place a submission visibly starts being worked on - without this,
        # a receipt sits on "Checking..." with no event ever having said so.
        dispatch_event('submission.processing', {
            'submission_id': job.id, 'status': 'processing', 'device_id': job.device_id,
        }, config)

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
    """
    Serves a receipt photograph from the upload folder.

    Cached hard, and safely: a stored filename carries the timestamp it was written at
    and the file underneath it is never rewritten, so the only thing that can change at
    one of these addresses is the file being deleted. Without this the dashboard
    re-downloads every photograph on every visit - each one up to
    utils.images.STORED_MAX_EDGE, on a list where a dozen of them are on screen at once.

    `private` because these are one business's receipts behind @login_required: shared
    caches, including any proxy in front of this app, must not keep a copy that a
    different session could be handed.
    """
    response = send_from_directory(
        app.config['UPLOAD_FOLDER'],
        filename,
        as_attachment=False # Display in browser instead of downloading
    )
    response.headers['Cache-Control'] = 'private, max-age=31536000, immutable'
    return response

# The spreadsheet's columns, named once. A row is built against this list rather than
# in step with it, so a submission that never produced a receipt can be padded to the
# same width and still line up under the columns it has nothing to say about.
EXPORT_HEADER = [
    'ID', 'Status', 'Device', 'Received At', 'Processed At', 'Vendor', 'Vendor TIN', 'VRN',
    'Tax Office', 'EFD Serial', 'Receipt No', 'Z Number', 'Verification Code',
    'Receipt Date', 'Receipt Time', 'Total Excl Tax', 'Total Tax', 'Total Incl Tax',
    'Discount', *[f'Tax {code}' for code in TAX_CODES], 'Cancelled', 'Test',
    'Category', 'Source', 'Document Type', 'Items', 'LLM Description', 'Tax Analysis',
    'Customer Name', 'Customer ID',
    # Computed by utils/compliance, so a spreadsheet can be filtered on what is
    # actually claimable rather than on what was merely spent.
    'Compliance Score', 'Input VAT Charged', 'Input VAT Recoverable',
    'Standard Rated Excl', 'Zero Rated Or Exempt', 'Claim Deadline',
    'Days Left To Claim', 'Failed Checks', 'Recovery Blockers',
    'Computed Category', 'WHT Estimate',
]


@app.route('/export/csv')
@login_required
def export_csv():
    """
    Everything the current filter matches, as a spreadsheet.

    The same filter the table is showing - the tab, the search, the dates, and every
    selected category, device and supplier - read by the same function the table reads
    it with. An export that quietly means something broader than the screen it was
    started from is worse than no export at all: the difference only surfaces after
    somebody has filed the number.

    Rows the filter matches that have no receipt behind them (a failure, a submission
    still queued) are exported too, with their receipt columns empty. Filtering to
    'Failed' and downloading an empty file would be a straight lie about what is there.
    """
    filters = _read_filters(request.args)
    submissions = _filtered_submissions(filters).options(
        joinedload(Submission.receipt).selectinload(Receipt.items),
        joinedload(Submission.receipt).selectinload(Receipt.tax_lines),
        joinedload(Submission.device),
    ).all()

    # Read before the response starts streaming: generate() is consumed after the
    # request context has gone, so everything it touches has to be loaded by now.
    config = get_instance_config()

    def generate():
        data = io.StringIO()
        writer = csv.writer(data)
        writer.writerow(EXPORT_HEADER)
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)

        for submission in submissions:
            writer.writerow(_export_row(submission, config))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    response.headers["Content-Disposition"] = f'attachment; filename="receipts_export_{timestamp}.csv"'

    return response


def _export_row(submission, config):
    """One submission as a spreadsheet row, receipt or no receipt."""
    device_name = submission.device.name if submission.device else ''
    received = submission.received_at.strftime('%Y-%m-%d %H:%M:%S') if submission.received_at else ''
    receipt = submission.receipt

    if receipt is None:
        # What is known about a submission with no receipt behind it, in the columns
        # that are about the submission rather than about the receipt.
        row = [''] * len(EXPORT_HEADER)
        row[0], row[1], row[2], row[3] = submission.id, submission.status, device_name, received
        row[EXPORT_HEADER.index('LLM Description')] = submission.description or ''
        return row

    raw_response = json.loads(receipt.raw_llm_response or '{}')
    assessment = assess_receipt(receipt, config)
    by_code = {line.code: format_cents(line.amount_cents) for line in receipt.tax_lines}
    items = '; '.join(
        f"{item.description} x{item.quantity or 1} @ {format_cents(item.amount_cents)}"
        f"{f' [{item.tax_code}]' if item.tax_code else ''}"
        for item in receipt.items
    )
    return [
        submission.id, submission.status, device_name, received,
        receipt.processed_at.strftime('%Y-%m-%d %H:%M:%S'), receipt.vendor_name, receipt.vendor_tin,
        receipt.vrn, receipt.tax_office, receipt.efd_serial, receipt.receipt_number,
        receipt.z_number, receipt.receipt_verification_code, receipt.receipt_date,
        receipt.receipt_time, format_cents(receipt.total_excl_tax_cents),
        format_cents(receipt.total_tax_cents), format_cents(receipt.total_incl_tax_cents),
        format_cents(receipt.discount_cents), *[by_code.get(code, '') for code in TAX_CODES],
        'yes' if receipt.is_cancelled else '', 'yes' if receipt.is_test else '',
        receipt.category, receipt.extraction_source,
        # Blank on everything TRA verified, which is by far the common case:
        # only a photograph can be anything other than an EFD receipt.
        receipt.document_type or '', items, submission.description,
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


@app.route('/stream')
@login_required
def stream():
    """
    Every event on the instance, live, for the dashboard.

    Unfiltered on purpose - this is the admin's view of every device - and cursored, so
    a dashboard that reconnects (a laptop lid closed, a wifi handover, a proxy timing
    the connection out) is told what happened while it was away instead of resuming at
    "now" with a hole behind it. EventSource resends the last id it saw as
    Last-Event-ID by itself; see utils/sse_broker.parse_cursor.
    """
    return sse_response(sse_broker.parse_cursor(request))