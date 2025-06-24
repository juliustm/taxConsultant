# main.py
import os
import pyotp
from functools import wraps
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session

from config import Config
from models.user import db, InstanceConfig, Device, Receipt
from utils.security import generate_totp_provisioning_uri, generate_qr_code_base64

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

with app.app_context():
    db.create_all()
    # Dummy data creation remains the same
    if Receipt.query.count() == 0:
        if Device.query.count() == 0:
            dummy_device = Device(name='Default Interface')
            db.session.add(dummy_device)
            db.session.commit()
        else:
            dummy_device = Device.query.first()
        
        receipts_data = [
            {'vendor_name': 'Shoprite', 'total_amount': 55000, 'receipt_date': date.today() - timedelta(days=1), 'processed_at': datetime.utcnow() - timedelta(days=1)},
            {'vendor_name': 'Total Petrol', 'total_amount': 70000, 'receipt_date': date.today() - timedelta(days=6), 'processed_at': datetime.utcnow() - timedelta(days=6)},
            {'vendor_name': 'CJs Restaurant', 'total_amount': 35000, 'receipt_date': date.today() - timedelta(days=20), 'processed_at': datetime.utcnow() - timedelta(days=20)},
        ]
        
        for data in receipts_data:
            new_receipt = Receipt(
                vendor_name=data['vendor_name'],
                total_amount=data['total_amount'],
                receipt_date=data['receipt_date'],
                processed_at=data['processed_at'],
                device_id=dummy_device.id
            )
            db.session.add(new_receipt)
        db.session.commit()

def get_instance_config():
    return InstanceConfig.query.first()

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
    now = datetime.utcnow()
    periods = {
        '24h': now - timedelta(hours=24), '7d': now - timedelta(days=7), '4w': now - timedelta(weeks=4),
        '3m': now - timedelta(days=90), '6m': now - timedelta(days=180), '1y': now - timedelta(days=365)
    }
    stats = {
        name: db.session.query(db.func.count(Receipt.id), db.func.sum(Receipt.total_amount)).filter(Receipt.processed_at >= start_time).one()
        for name, start_time in periods.items()
    }
    recent_receipts = Receipt.query.order_by(Receipt.processed_at.desc()).limit(5).all()
    return render_template('index.html', stats=stats, recent_receipts=recent_receipts)

# --- NEW TOTP SETUP FLOW ---

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

# --- NEW TOTP LOGIN FLOW ---

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


# --- UPDATED CONFIGURATION ROUTE ---
@app.route('/admin/configure', methods=['GET', 'POST'])
@login_required
def configure_instance():
    config = get_instance_config()
    if request.method == 'POST':
        # LLM Settings
        config.llm_provider = request.form.get('llm_provider')
        config.llm_api_key = request.form.get('llm_api_key')
        
        # Export & Backup Settings
        config.export_spreadsheet_url = request.form.get('export_spreadsheet_url')
        config.post_callback_url = request.form.get('post_callback_url')
        config.s3_bucket_name = request.form.get('s3_bucket_name')
        config.s3_access_key_id = request.form.get('s3_access_key_id')
        config.s3_secret_access_key = request.form.get('s3_secret_access_key')
        config.s3_region = request.form.get('s3_region')
        
        db.session.commit()
        flash('Configuration saved successfully!', 'success')
        return redirect(url_for('configure_instance'))

    devices = Device.query.all()
    return render_template('admin/configure.html', config=config, devices=devices)


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


@app.route('/receipt', methods=['POST'])
def receipt_endpoint():
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization header is missing or invalid'}), 401
    device_key = auth_header.split(' ')[1]
    device = Device.query.filter_by(api_key=device_key).first()
    if not device:
        return jsonify({'error': 'Invalid device API key'}), 403
    config = get_instance_config()
    if not config or not config.is_configured():
        return jsonify({'error': 'Instance is not configured by the admin'}), 503
    description = request.form.get('description', '')
    receipt_photo = request.files.get('receiptphoto')
    receipt_url = request.form.get('receipturl')
    if not receipt_photo and not receipt_url:
        return jsonify({'error': '`receiptphoto` (file) or `receipturl` (form field) is required'}), 400
    
    # Placeholder for future LLM processing
    print("--- RECEIPT RECEIVED ---")
    print(f"Device: {device.name} ({device.id})")
    
    return jsonify({ "message": "Receipt received and is being processed." }), 202