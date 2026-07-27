# main.py
import os, time, json, csv, io, pyotp, requests, gevent
from functools import wraps
from datetime import datetime, timedelta, date
from werkzeug.utils import secure_filename

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session, current_app, send_from_directory, Response

from config import Config
from models.user import db, InstanceConfig, Device, Receipt, ReceiptItem, ReceiptTaxLine, Submission, Vendor
from utils.security import generate_totp_provisioning_uri, generate_qr_code_base64
from utils.export import dispatch_event, format_currency
from utils.llm_processor import analyse_receipt, extract_receipt_details, LlmUnavailable
from utils.money import format_cents, from_cents, to_cents, to_decimal
from utils.sse_broker import announcer
from utils.tra import (
    fetch_receipt_html, TraError, TraReceiptNotUploaded, TraThrottled,
    TraTransportError, TraUnexpectedResponse,
)
from utils.tra_parser import parse_receipt_html, TAX_CODES, TraParseError
from sqlalchemy import text as sa_text
from sqlalchemy.orm import joinedload

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
    'submission': (
        ('next_attempt_at', 'DATETIME'),
        ('claimed_at', 'DATETIME'),
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

def safe_serialize(obj):
    """Safely serialize SQLAlchemy objects for JSON, handling dates."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)

def _as_float(cents):
    """Display value for the browser. Sums are done on the *_cents fields, not these."""
    amount = from_cents(cents)
    return None if amount is None else float(amount)

def receipt_to_dict(receipt):
    """
    The canonical JSON view of a stored receipt.

    Shared by the dashboard bootstrap, the SSE payload and the webhook/sheet exports,
    so every consumer sees the same shape. Amounts appear twice: as cents, which is
    what anything adding them up must use, and as a float for display only.
    """
    if receipt is None:
        return {}

    judgment = json.loads(receipt.raw_llm_response) if receipt.raw_llm_response else {}
    return {
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
    }

def prepare_submissions_for_frontend(submissions):
    """Converts Submission objects into a JSON-serializable list of dictionaries."""
    output = []
    for sub in submissions:
        receipt_data = receipt_to_dict(sub.receipt)

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
            "receipt": receipt_data, "device_name": sub.device.name if sub.device else 'Unknown Device'
        }
        output.append(data)
    return json.dumps(output)

def schedule_retry_or_fail(submission, error):
    """
    Applies the retry policy for a failed TRA fetch.

    Retryable failures go back to 'queued' with a next_attempt_at in the future;
    permanent ones (wrong time in the URL, rejected Referer) fail immediately rather
    than burning ten more requests against a rate-limited portal.
    """
    attempt = submission.retry_count or 0
    submission.retry_count = attempt + 1

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

def calculate_dashboard_stats():
    """
    Spending per period, measured on the date printed on the receipt.

    Not on processed_at: that is the moment someone got round to scanning it, so a
    receipt from March scanned in July would land in "today". Expense reporting asks
    when the money was spent, which is receipt_date.

    Cancelled and test receipts are excluded - neither is money that left the business.
    """
    today = datetime.utcnow().date()
    periods = {
        'today': today,
        '7d': today - timedelta(days=6),
        '4w': today - timedelta(days=27),
        '1y': today - timedelta(days=364),
    }

    stats = {}
    for name, start_date in periods.items():
        count, total_cents = (
            db.session.query(db.func.count(Receipt.id), db.func.sum(Receipt.total_incl_tax_cents))
            .filter(
                Receipt.receipt_date >= start_date,
                Receipt.receipt_date <= today,
                Receipt.is_cancelled.is_(False),
                Receipt.is_test.is_(False),
            ).one()
        )
        stats[name] = {
            'count': count or 0,
            'total_cents': total_cents or 0,
            'total': _as_float(total_cents or 0),
        }
    return stats

def _receipt_from_tra_url(submission, config):
    """
    Builds a Receipt from the TRA verified page. Returns None if there is nothing to
    store yet (a retry was scheduled, or this receipt is already in the ledger).
    """
    url = submission.input_data
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
        _fail_submission(submission.id, f"Could not parse the TRA receipt page: {e}")

    except Exception as e:
        # --- FIX #2: Resilient Error Handling ---
        # This block ensures a single failed job doesn't kill the whole queue runner.
        print(f"[TaskError] Unhandled exception in process_submission {submission.id}: {e}")
        _fail_submission(submission.id, str(e))

def _fail_submission(submission_id, message):
    """Rolls back, marks the submission failed and announces it."""
    db.session.rollback()  # IMPORTANT: clean the session before touching it again.

    # The session was rolled back, so the submission has to be re-fetched.
    submission = Submission.query.get(submission_id)
    if not submission:
        return

    submission.status = 'failed'
    submission.error_message = message
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
    # Check for the 'story' parameter to auto-launch the story mode
    start_in_story_mode = request.args.get('story', 'false').lower() == 'true'
    # Handle incoming filter parameters from the URL
    search_query = request.args.get('search', '')
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    stats = calculate_dashboard_stats()
    
    # Alpine.js handles all filtering.
    submissions = Submission.query.order_by(Submission.received_at.desc()).all()
    submissions_json = prepare_submissions_for_frontend(submissions)
    
    return render_template('index.html', 
                           stats=stats, 
                           submissions_json=submissions_json,
                           # Pass URL params to the template for initialization
                           search_query=search_query,
                           start_date=start_date_str,
                           end_date=end_date_str,
                           start_in_story_mode=start_in_story_mode)

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
        
        # Save all the form data
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
        description=description, location=location
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
        ]
        writer.writerow(header)
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)

        # This will now work because receipt.submission was pre-loaded.
        for receipt in receipts:
            raw_response = json.loads(receipt.raw_llm_response or '{}')
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
                raw_response.get('llm_tax_analysis', ''), receipt.customer_name, receipt.customer_id
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