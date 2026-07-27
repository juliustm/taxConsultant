# main.py
import os, re, time, json, csv, io, pyotp, requests, gevent
from functools import wraps
from datetime import datetime, timedelta, date
from werkzeug.utils import secure_filename
from bs4 import BeautifulSoup

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session, current_app, send_from_directory, Response

from config import Config
from models.user import db, InstanceConfig, Device, Receipt, Submission
from utils.security import generate_totp_provisioning_uri, generate_qr_code_base64
from utils.export import dispatch_event, format_currency
from utils.llm_processor import extract_receipt_details
from utils.sse_broker import announcer
from utils.tra import (
    fetch_receipt_html, TraError, TraReceiptNotUploaded, TraThrottled,
    TraTransportError, TraUnexpectedResponse,
)
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

def apply_pending_migrations():
    """
    Adds columns introduced after a database was first created.

    There is no migration tool in this project and db.create_all() skips tables that
    already exist, so an existing deployment would otherwise keep an old 'submission'
    table and fail on the scheduling columns.
    """
    existing_columns = {row[1] for row in db.session.execute(sa_text("PRAGMA table_info(submission)"))}
    if not existing_columns:
        return  # Fresh database; create_all() already built the current schema.

    for column, ddl_type in (('next_attempt_at', 'DATETIME'), ('claimed_at', 'DATETIME')):
        if column not in existing_columns:
            print(f"[Migration] Adding submission.{column}")
            db.session.execute(sa_text(f"ALTER TABLE submission ADD COLUMN {column} {ddl_type}"))

    db.session.execute(sa_text(
        "CREATE INDEX IF NOT EXISTS ix_submission_next_attempt_at ON submission (next_attempt_at)"
    ))
    db.session.commit()

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

def clean_amount(value):
    """
    Cleans a string amount (e.g. '10,000.00', 'TZS 5000') into a float.
    Returns None if conversion fails.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    
    # Remove everything except digits and dots
    # This handles '10,000', 'TZS 1000', etc.
    # We assume standard decimal notation.
    try:
        # Remove commas
        cleaned = str(value).replace(',', '')
        # Extract the first valid number found (simple approach)
        # Or just strip non-numeric chars except dot
        cleaned = re.sub(r'[^\d.]', '', cleaned)
        return float(cleaned)
    except (ValueError, TypeError):
        return None

def prepare_submissions_for_frontend(submissions):
    """Converts Submission objects into a JSON-serializable list of dictionaries."""
    output = []
    for sub in submissions:
        receipt_data = {}
        if sub.receipt:
            receipt_data = {
                "vendor_name": sub.receipt.vendor_name, "total_amount": sub.receipt.total_amount,
                "vat_amount": sub.receipt.vat_amount, "receipt_date": sub.receipt.receipt_date.strftime('%Y-%m-%d') if sub.receipt.receipt_date else None,
                "raw_llm_response": json.loads(sub.receipt.raw_llm_response) if sub.receipt.raw_llm_response else {}
            }

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

def clean_html_for_llm(html_content: str) -> str:
    """
    Parses raw HTML and extracts clean text from the main receipt section.
    """
    soup = BeautifulSoup(html_content, 'lxml')
    
    # Target the specific <section> tag that contains the receipt details
    invoice_section = soup.find('section', class_='invoice')
    
    if invoice_section:
        # Get text from the specific section for a cleaner result
        text = invoice_section.get_text(separator='\n', strip=True)
    else:
        # Fallback to the whole body if the specific section isn't found
        print("Warning: Specific invoice section not found, falling back to full body text.")
        text = soup.body.get_text(separator='\n', strip=True)
        
    # Replace multiple newlines with a single one for cleaner formatting
    return re.sub(r'\n\s*\n', '\n', text)


def fetch_receipt_text_from_tra(submission):
    """
    Fetches the receipt from the TRA portal and returns it as clean text for the LLM.

    One request, no retry loop: retries are scheduled on the submission by the caller.
    Raises a TraError subclass when the portal did not hand us a receipt.
    """
    url = submission.input_data
    print(f"[Fetch] Attempt {(submission.retry_count or 0) + 1} for {url}")

    raw_html = fetch_receipt_html(url)
    print(f"[FetchSuccess] Successfully retrieved HTML (length: {len(raw_html)}).")

    cleaned_text = clean_html_for_llm(raw_html)
    print(f"[Clean] HTML cleaned. New length: {len(cleaned_text)}.")
    print(f"[Clean] Sample of cleaned text being sent to LLM:\n---\n{cleaned_text[:500]}...\n---")

    return cleaned_text

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
    """Calculates and returns the dashboard stats dictionary."""
    now = datetime.utcnow()
    periods = {
        '24h': now - timedelta(hours=24), '7d': now - timedelta(days=7),
        '4w': now - timedelta(weeks=4), '1y': now - timedelta(days=365)
    }
    stats = {
        name: db.session.query(db.func.count(Receipt.id), db.func.sum(Receipt.total_amount))
                        .filter(Receipt.processed_at >= start_time).one()
        for name, start_time in periods.items()
    }
    return {key: {'count': value[0] or 0, 'total': value[1] or 0.0} for key, value in stats.items()}

def process_submission(submission):
    """
    Processes a single submission with deduplication logic and updates description from LLM.
    """
    print(f"[TaskStart] Processing submission {submission.id} (Type: {submission.input_type})")
    try:
        config = get_instance_config()
        if not config or not config.is_configured():
            raise ValueError("Instance is not configured with LLM provider and API key.")

        content_for_llm, is_image = (None, False)
        if submission.input_type == 'url':
            try:
                content_for_llm = fetch_receipt_text_from_tra(submission)
            except TraError as e:
                schedule_retry_or_fail(submission, e)
                return
        elif submission.input_type == 'photo':
            content_for_llm = submission.input_data
            is_image = True
        
        if content_for_llm is None: return

        # --- Call LLM Processor ---
        extracted_data = extract_receipt_details(content_for_llm, is_image, config)
        
        # --- Deduplication Logic ---
        verification_code = extracted_data.get('receipt_verification_code')
        # Only check for duplicates if the code is a meaningful, non-empty string.
        if verification_code and verification_code.strip():
            existing_receipt = Receipt.query.filter_by(receipt_verification_code=verification_code).first()
            if existing_receipt:
                print(f"[TaskSkip] Duplicate receipt found with code {verification_code}. Original sub ID: {existing_receipt.submission_id}")
                submission.status = 'duplicate'
                submission.error_message = f"Duplicate of submission ID {existing_receipt.submission_id}"
                db.session.commit()
                # Dispatch duplicate event and exit cleanly
                payload = {"submission_id": submission.id, "status": "duplicate", "error_message": submission.error_message}
                dispatch_event('submission.duplicate', payload, config)
                return
        
       # --- Update Description, Parse Date, etc. ---
        llm_desc = extracted_data.get('llm_extracted_description')
        if llm_desc:
            submission.description = llm_desc
        
        receipt_date_obj = None
        if extracted_data.get('receipt_date'):
            try:
                receipt_date_obj = date.fromisoformat(extracted_data['receipt_date'])
            except (ValueError, TypeError):
                print(f"Warning: Could not parse date '{extracted_data.get('receipt_date')}'")

        # Convert empty verification code string to None to avoid UNIQUE constraint violation on ""
        db_verification_code = verification_code if (verification_code and verification_code.strip()) else None

        new_receipt = Receipt(
            vendor_name=extracted_data.get('vendor_name'), vendor_tin=extracted_data.get('vendor_tin'),
            vendor_phone=extracted_data.get('vendor_phone'), vrn=extracted_data.get('vrn'),
            receipt_verification_code=db_verification_code, receipt_number=extracted_data.get('receipt_number'),
            uin=extracted_data.get('uin'), customer_name=extracted_data.get('customer_name'),
            customer_id_type=extracted_data.get('customer_id_type'), customer_id=extracted_data.get('customer_id'),
            total_amount=clean_amount(extracted_data.get('total_amount')), vat_amount=clean_amount(extracted_data.get('vat_amount')),
            receipt_date=receipt_date_obj, raw_llm_response=json.dumps(extracted_data),
            device_id=submission.device_id, submission_id=submission.id
        )
        db.session.add(new_receipt)
        submission.status = 'completed'
        db.session.commit()
        # --- Dispatch COMPLETED event ---
        updated_stats = calculate_dashboard_stats()
        payload = {
            "submission_id": submission.id, "status": submission.status, 
            "processed_at": new_receipt.processed_at.isoformat(), "data": extracted_data,
            "stats": updated_stats
        }
        dispatch_event('submission.processed', payload, config)

        print(f"[TaskSuccess] Submission {submission.id} completed.")

    except Exception as e:
        # --- FIX #2: Resilient Error Handling ---
        # This block ensures a single failed job doesn't kill the whole queue runner.
        print(f"[TaskError] Unhandled exception in process_submission {submission.id}: {e}")
        db.session.rollback()  # IMPORTANT: Rollback the failed transaction to clean the session
        
        # We need to re-fetch the submission object as the session was rolled back
        submission_to_update = Submission.query.get(submission.id)
        if submission_to_update:
            submission_to_update.status = 'failed'
            submission_to_update.error_message = str(e)
            db.session.commit()
            
            # Dispatch failed event
            payload = {"submission_id": submission.id, "status": "failed", "error_message": submission_to_update.error_message}
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

@app.route('/admin/receipt/<int:submission_id>/reanalyze', methods=['POST'])
@login_required
def reanalyze_receipt(submission_id):
    """
    Re-analyzes a receipt by re-fetching from TRA (if URL) or re-using photo,
    then sending to LLM again and updating the receipt data.
    """
    submission = Submission.query.get(submission_id)
    if not submission:
        return jsonify({'error': 'Submission not found'}), 404
    
    try:
        config = get_instance_config()
        if not config or not config.is_configured():
            return jsonify({'error': 'Instance is not configured with LLM provider and API key'}), 500

        # Fetch content based on input type
        content_for_llm, is_image = (None, False)
        if submission.input_type == 'url':
            print(f"[Reanalyze] Re-fetching URL for submission {submission_id}")
            content_for_llm = fetch_receipt_html_from_tra(submission)
            if content_for_llm is None:
                return jsonify({'error': 'Could not fetch receipt from TRA. It may be temporarily unavailable.'}), 500
        elif submission.input_type == 'photo':
            print(f"[Reanalyze] Re-using photo for submission {submission_id}")
            content_for_llm = submission.input_data
            is_image = True
        
        # Call LLM to extract fresh data
        print(f"[Reanalyze] Sending to LLM for analysis...")
        extracted_data = extract_receipt_details(content_for_llm, is_image, config)
        
        # Update submission description
        llm_desc = extracted_data.get('llm_extracted_description')
        if llm_desc:
            submission.description = llm_desc
        
        # Parse receipt date
        receipt_date_obj = None
        if extracted_data.get('receipt_date'):
            try:
                receipt_date_obj = date.fromisoformat(extracted_data['receipt_date'])
            except (ValueError, TypeError):
                print(f"Warning: Could not parse date '{extracted_data.get('receipt_date')}'")
        
        # Convert empty verification code string to None
        verification_code = extracted_data.get('receipt_verification_code')
        db_verification_code = verification_code if (verification_code and verification_code.strip()) else None
        
        # Update or create receipt
        existing_receipt = Receipt.query.filter_by(submission_id=submission_id).first()
        
        if existing_receipt:
            # Update existing receipt
            print(f"[Reanalyze] Updating existing receipt for submission {submission_id}")
            existing_receipt.vendor_name = extracted_data.get('vendor_name')
            existing_receipt.vendor_tin = extracted_data.get('vendor_tin')
            existing_receipt.vendor_phone = extracted_data.get('vendor_phone')
            existing_receipt.vrn = extracted_data.get('vrn')
            existing_receipt.receipt_verification_code = db_verification_code
            existing_receipt.receipt_number = extracted_data.get('receipt_number')
            existing_receipt.uin = extracted_data.get('uin')
            existing_receipt.customer_name = extracted_data.get('customer_name')
            existing_receipt.customer_id_type = extracted_data.get('customer_id_type')
            existing_receipt.customer_id = extracted_data.get('customer_id')
            existing_receipt.total_amount = clean_amount(extracted_data.get('total_amount'))
            existing_receipt.vat_amount = clean_amount(extracted_data.get('vat_amount'))
            existing_receipt.receipt_date = receipt_date_obj
            existing_receipt.raw_llm_response = json.dumps(extracted_data)
            existing_receipt.processed_at = datetime.utcnow()
        else:
            # Create new receipt
            print(f"[Reanalyze] Creating new receipt for submission {submission_id}")
            new_receipt = Receipt(
                vendor_name=extracted_data.get('vendor_name'),
                vendor_tin=extracted_data.get('vendor_tin'),
                vendor_phone=extracted_data.get('vendor_phone'),
                vrn=extracted_data.get('vrn'),
                receipt_verification_code=db_verification_code,
                receipt_number=extracted_data.get('receipt_number'),
                uin=extracted_data.get('uin'),
                customer_name=extracted_data.get('customer_name'),
                customer_id_type=extracted_data.get('customer_id_type'),
                customer_id=extracted_data.get('customer_id'),
                total_amount=clean_amount(extracted_data.get('total_amount')),
                vat_amount=clean_amount(extracted_data.get('vat_amount')),
                receipt_date=receipt_date_obj,
                raw_llm_response=json.dumps(extracted_data),
                device_id=submission.device_id,
                submission_id=submission.id
            )
            db.session.add(new_receipt)
        
        # Update submission status
        submission.status = 'completed'
        submission.error_message = None
        db.session.commit()
        
        # Dispatch event
        updated_stats = calculate_dashboard_stats()
        payload = {
            "submission_id": submission.id,
            "status": submission.status,
            "processed_at": datetime.utcnow().isoformat(),
            "data": extracted_data,
            "stats": updated_stats
        }
        dispatch_event('submission.processed', payload, config)
        
        print(f"[Reanalyze] Successfully re-analyzed submission {submission_id}")
        return jsonify({
            'message': 'Receipt re-analyzed successfully',
            'data': extracted_data
        }), 200
        
    except Exception as e:
        print(f"[Reanalyze Error] Failed to re-analyze submission {submission_id}: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


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
    query = query.options(joinedload(Receipt.submission))

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
            'Receipt No', 'Verification Code', 'Receipt Date', 'Total Amount', 'VAT Amount', 
            'LLM Description', 'Tax Analysis', 'Customer Name', 'Customer ID'
        ]
        writer.writerow(header)
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)

        # This will now work because receipt.submission was pre-loaded.
        for receipt in receipts:
            raw_response = json.loads(receipt.raw_llm_response or '{}')
            row = [
                receipt.submission_id, 'completed', receipt.submission.received_at.strftime('%Y-%m-%d %H:%M:%S'),
                receipt.processed_at.strftime('%Y-%m-%d %H:%M:%S'), receipt.vendor_name, receipt.vendor_tin,
                receipt.vrn, receipt.receipt_number, receipt.receipt_verification_code, receipt.receipt_date,
                receipt.total_amount, receipt.vat_amount, receipt.submission.description,
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