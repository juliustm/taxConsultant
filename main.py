# main.py
import os
import re
import time
import json
import pyotp
import requests
import threading
from functools import wraps
from datetime import datetime, timedelta, date
from werkzeug.utils import secure_filename
from bs4 import BeautifulSoup

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session, current_app

from config import Config
from models.user import db, InstanceConfig, Device, Receipt, Submission
from utils.security import generate_totp_provisioning_uri, generate_qr_code_base64
from utils.export import dispatch_event
from utils.llm_processor import extract_receipt_details

app = Flask(__name__)
app.config.from_object(Config)

# Configure the upload folder
app.config['UPLOAD_FOLDER'] = os.path.join(Config.DATA_DIR, 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)

# This function is correctly defined here, in main.py.
def get_instance_config():
    return InstanceConfig.query.first()

# Create database tables and seed with dummy data for demo
with app.app_context():
    db.create_all()

# --- JOB PROCESSING LOGIC ---

MAX_RETRIES = 9
RETRY_DELAY_SECONDS = 60 # 1 minute

def safe_serialize(obj):
    """Safely serialize SQLAlchemy objects for JSON, handling dates."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)

def prepare_submissions_for_frontend(submissions):
    """Converts Submission objects into a JSON-serializable list of dictionaries."""
    output = []
    for sub in submissions:
        # Check if a receipt is linked to this submission
        receipt_data = {}
        if sub.receipt:
            receipt_data = {
                "vendor_name": sub.receipt.vendor_name,
                "total_amount": sub.receipt.total_amount,
                "receipt_date": sub.receipt.receipt_date.strftime('%Y-%m-%d') if sub.receipt.receipt_date else None
            }

        data = {
            "id": sub.id,
            "status": sub.status,
            "received_at": sub.received_at,
            "input_type": sub.input_type,
            "input_data": sub.input_data,
            "description": sub.description,
            "location": sub.location,
            "error_message": sub.error_message,
            "is_duplicate": sub.status == 'duplicate',
            "receipt": receipt_data
        }
        if sub.receipt:
            data["receipt"] = {
                "vendor_name": sub.receipt.vendor_name,
                "total_amount": sub.receipt.total_amount,
                "vat_amount": sub.receipt.vat_amount,
                "receipt_date": sub.receipt.receipt_date,
                "receipt_verification_code": sub.receipt.receipt_verification_code,
                "raw_llm_response": json.loads(sub.receipt.raw_llm_response) if sub.receipt.raw_llm_response else None
            }
        output.append(data)
    return json.dumps(output, default=safe_serialize)

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


def fetch_receipt_html_from_tra(submission):
    """
    Fetches receipt data, and now cleans the HTML before returning it.
    """
    url = submission.input_data
    match = re.search(r'_(\d{2})(\d{2})(\d{2})$', url)
    if not match:
        raise ValueError("Invalid receipt URL format: Time suffix not found.")
    
    secret_time = f"{match.group(1)}:{match.group(2)}:{match.group(3)}"
    verify_url_base = 'https://verify.tra.go.tz'
    verify_url_with_secret = f"{verify_url_base}/Verify/Verified?Secret={secret_time}"

    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 TaxConsultAI/1.0'})

    for i in range(submission.retry_count, MAX_RETRIES + 1):
        try:
            print(f"[Fetch] Attempt {i+1}/{MAX_RETRIES+1} for {url}")
            initial_response = session.get(url, timeout=15)
            initial_response.raise_for_status()
            
            html_response = session.get(verify_url_with_secret, timeout=15)
            html_response.raise_for_status()

            if "Receipt not found" in html_response.text or html_response.status_code != 200:
                 raise ValueError("Receipt not yet available on TRA portal.")

            raw_html = html_response.text
            print(f"[FetchSuccess] Successfully retrieved HTML (length: {len(raw_html)}).")

            # ---- NEW CLEANING STEP ----
            cleaned_text = clean_html_for_llm(raw_html)
            print(f"[Clean] HTML cleaned. New length: {len(cleaned_text)}.")
            print(f"[Clean] Sample of cleaned text being sent to LLM:\n---\n{cleaned_text[:500]}...\n---")

            return cleaned_text

        except (requests.exceptions.RequestException, ValueError) as e:
            # ... (error handling remains the same) ...
            print(f"[FetchAttemptFailed] Attempt {i+1} failed: {e}")
            submission.retry_count = i + 1
            db.session.commit()
            if i < MAX_RETRIES:
                print(f"[FetchRetry] Waiting {RETRY_DELAY_SECONDS}s before next retry...")
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                print(f"[FetchFailed] Max retries reached for submission {submission.id}.")
                submission.status = 'failed'
                submission.error_message = f"Failed after {MAX_RETRIES+1} attempts: {e}"
                db.session.commit()
                return None

def trigger_url_in_background(url_to_trigger):
    """
    Waits for a short period and then calls a given URL.
    This runs in a separate thread to not block the main request.
    """
    print(f"[Trigger] Background trigger initiated for URL: {url_to_trigger}. Waiting 10 seconds...")
    time.sleep(10)
    
    try:
        print(f"[Trigger] Making internal request to process the queue...")
        # We don't need to wait long for a response, just to kick it off.
        requests.get(url_to_trigger, timeout=5)
        print("[Trigger] Internal request sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"[Trigger Error] Could not trigger task runner internally: {e}")

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
            content_for_llm = fetch_receipt_html_from_tra(submission)
        elif submission.input_type == 'photo':
            content_for_llm = submission.input_data
            is_image = True
        
        if content_for_llm is None: return

        # --- Call LLM Processor ---
        extracted_data = extract_receipt_details(content_for_llm, is_image, config)
        
        # --- Deduplication Logic ---
        verification_code = extracted_data.get('receipt_verification_code')
        if verification_code:
            existing_receipt = Receipt.query.filter_by(receipt_verification_code=verification_code).first()
            if existing_receipt:
                print(f"[TaskSkip] Duplicate receipt found. Original sub ID: {existing_receipt.submission_id}")
                submission.status = 'duplicate'
                submission.error_message = f"Duplicate of submission ID {existing_receipt.submission_id}"
                db.session.commit()
                return
        
        # --- Update Description from LLM ---
        llm_desc = extracted_data.get('llm_extracted_description')
        if llm_desc:
            submission.description = llm_desc # Overwrite user description with more accurate LLM summary
        
        receipt_date_obj = None
        if extracted_data.get('receipt_date'):
            try:
                receipt_date_obj = date.fromisoformat(extracted_data['receipt_date'])
            except (ValueError, TypeError):
                print(f"Warning: Could not parse date '{extracted_data['receipt_date']}'")

        new_receipt = Receipt(
            vendor_name=extracted_data.get('vendor_name'), vendor_tin=extracted_data.get('vendor_tin'),
            vendor_phone=extracted_data.get('vendor_phone'), vrn=extracted_data.get('vrn'),
            receipt_verification_code=verification_code, receipt_number=extracted_data.get('receipt_number'),
            uin=extracted_data.get('uin'), customer_name=extracted_data.get('customer_name'),
            customer_id_type=extracted_data.get('customer_id_type'), customer_id=extracted_data.get('customer_id'),
            total_amount=extracted_data.get('total_amount'), vat_amount=extracted_data.get('vat_amount'),
            receipt_date=receipt_date_obj, raw_llm_response=json.dumps(extracted_data),
            device_id=submission.device_id, submission_id=submission.id
        )
        db.session.add(new_receipt)
        submission.status = 'completed'
        db.session.commit()
        
        payload = {"submission_id": submission.id, "status": submission.status, "processed_at": new_receipt.processed_at.isoformat(), "data": extracted_data}
        dispatch_event('submission.processed', payload, config)
        print(f"[TaskSuccess] Submission {submission.id} completed.")

    except Exception as e:
        print(f"[TaskError] Unhandled exception in process_submission {submission.id}: {e}")
        submission.status = 'failed'
        submission.error_message = str(e)
        db.session.commit()

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
    submissions = Submission.query.order_by(Submission.received_at.desc()).all()
    devices = Device.query.all()
    
    now = datetime.utcnow()
    periods = {
        '24h': now - timedelta(hours=24), '7d': now - timedelta(days=7), '4w': now - timedelta(weeks=4),
        '3m': now - timedelta(days=90), '6m': now - timedelta(days=180), '1y': now - timedelta(days=365)
    }
    stats = {
        name: db.session.query(db.func.count(Receipt.id), db.func.sum(Receipt.total_amount))
                        .filter(Receipt.processed_at >= start_time).one()
        for name, start_time in periods.items()
    }
    
    # Prepare data as a JSON string for Alpine.js
    submissions_json = prepare_submissions_for_frontend(submissions)
    
    return render_template('index.html', 
                           submissions=submissions, 
                           submissions_json=submissions_json,
                           devices=devices, 
                           stats=stats)

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

    input_type, input_data = ('', '')
    if receipt_photo:
        input_type = 'photo'
        filename = secure_filename(f"{datetime.utcnow().timestamp()}_{receipt_photo.filename}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        receipt_photo.save(filepath)
        input_data = filepath
    elif receipt_url:
        input_type = 'url'
        input_data = receipt_url

    new_submission = Submission(
        device_id=device.id, input_type=input_type, input_data=input_data,
        description=description, location=location
    )
    db.session.add(new_submission)
    db.session.commit()
    
    config = get_instance_config()
    payload = {
        "id": new_submission.id, "device_name": device.name, "status": new_submission.status,
        "received_at": new_submission.received_at.isoformat(), "input_type": new_submission.input_type,
        "description": new_submission.description, "location": new_submission.location
    }
    dispatch_event('submission.queued', payload, config)
    
    # Dynamically generate the full, external URL for the task runner.
    #    `_external=True` uses the request's host and scheme (http/https).
    runner_secret = current_app.config['TASK_RUNNER_SECRET_KEY']
    runner_url = url_for('run_tasks', secret=runner_secret, _external=True)
    
    # Start the background thread, passing the complete URL.
    thread = threading.Thread(target=trigger_url_in_background, args=(runner_url,))
    thread.daemon = True
    thread.start()
    
    return jsonify({ "message": "Receipt accepted and queued for processing.", "submission_id": new_submission.id }), 202

@app.route('/tasks/run', methods=['GET'])
def run_tasks():
    secret = request.args.get('secret')
    if secret != app.config['TASK_RUNNER_SECRET_KEY']:
        return jsonify({"error": "Unauthorized"}), 403

    # --- NEW: Self-healing logic for stuck jobs ---
    # Find jobs stuck in 'processing' for more than 10 minutes and requeue them.
    ten_minutes_ago = datetime.utcnow() - timedelta(minutes=10)
    stuck_jobs = Submission.query.filter(
        Submission.status == 'processing',
        Submission.received_at < ten_minutes_ago # Using received_at as a proxy for start time
    ).all()

    for job in stuck_jobs:
        print(f"[Heal] Found stuck job {job.id}. Re-queueing.")
        job.status = 'queued'
        job.error_message = "Rescued from stuck 'processing' state."
    
    if stuck_jobs:
        db.session.commit()

    processed_jobs = []
    # Process all queued jobs
    while True:
        # Find one pending job (this will now include any rescued jobs)
        job = Submission.query.filter_by(status='queued').order_by(Submission.received_at.asc()).first()
        if not job:
            break

        job.status = 'processing'
        # Clear any previous error messages before processing
        job.error_message = None 
        db.session.commit()
        
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