# main.py (Complete and Corrected Version)
import os
from functools import wraps
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session

from config import Config
from models.user import db, InstanceConfig, Device, AdminOTP, Receipt
from utils.security import generate_otp

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# Create database tables and seed with dummy data for demo
with app.app_context():
    db.create_all()
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

# --- Helper Functions & Decorators ---
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

# --- Main Routes ---
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

# --- Admin Setup and Login Flow (RESTORED) ---
@app.route('/admin/setup', methods=['GET', 'POST'])
def setup_email():
    if get_instance_config():
        return redirect(url_for('admin_login'))
    if request.method == 'POST':
        email = request.form.get('email')
        otp = generate_otp()
        new_otp = AdminOTP(email=email, otp=otp)
        db.session.add(new_otp)
        db.session.commit()
        print(f"--- SETUP OTP for {email}: {otp} ---")
        session['setup_email'] = email
        return redirect(url_for('verify_otp'))
    return render_template('admin/setup.html')

@app.route('/admin/verify', methods=['GET', 'POST'])
def verify_otp():
    email = session.get('setup_email')
    if not email:
        return redirect(url_for('setup_email'))
    if request.method == 'POST':
        submitted_otp = request.form.get('otp')
        otp_entry = AdminOTP.query.filter_by(email=email, otp=submitted_otp).first()
        if otp_entry:
            new_config = InstanceConfig(admin_email=email)
            db.session.add(new_config)
            db.session.delete(otp_entry)
            db.session.commit()
            session.pop('setup_email', None)
            flash('Email verified successfully! Please log in to continue configuration.', 'success')
            return redirect(url_for('admin_login'))
        else:
            flash('Invalid OTP. Please try again.', 'danger')
    return render_template('admin/verify_otp.html', email=email)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    config = get_instance_config()
    if not config:
        return redirect(url_for('setup_email'))
    if request.method == 'POST':
        email = request.form.get('email')
        if email == config.admin_email:
            AdminOTP.query.filter_by(email=email).delete()
            otp = generate_otp()
            new_otp = AdminOTP(email=email, otp=otp)
            db.session.add(new_otp)
            db.session.commit()
            print(f"--- LOGIN OTP for {email}: {otp} ---")
            session['login_email'] = email
            flash('An OTP has been sent to your email.', 'info')
            return redirect(url_for('verify_login_otp'))
        else:
            flash('Invalid admin email.', 'danger')
    return render_template('admin/login.html')

@app.route('/admin/login/verify', methods=['GET', 'POST'])
def verify_login_otp():
    email = session.get('login_email')
    if not email:
        return redirect(url_for('admin_login'))
    if request.method == 'POST':
        submitted_otp = request.form.get('otp')
        otp_entry = AdminOTP.query.filter_by(email=email, otp=submitted_otp).first()
        if otp_entry and otp_entry.expires_at > datetime.utcnow():
            session.clear()
            session['admin_logged_in'] = True
            session.permanent = True
            db.session.delete(otp_entry)
            db.session.commit()
            flash('Logged in successfully.', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid or expired OTP. Please try again.', 'danger')
            return redirect(url_for('admin_login'))
    return render_template('admin/login_verify.html', email=email)


@app.route('/admin/logout')
@login_required
def admin_logout():
    session.clear()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('admin_login'))

# --- Configuration & Dashboard ---
@app.route('/admin/configure', methods=['GET', 'POST'])
@login_required
def configure_instance():
    config = get_instance_config()
    if request.method == 'POST':
        config.llm_provider = request.form.get('llm_provider')
        config.llm_api_key = request.form.get('llm_api_key')
        config.export_spreadsheet_url = request.form.get('export_spreadsheet_url')
        db.session.commit()
        flash('Configuration saved successfully!', 'success')
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

# --- THE CORE RECEIPT ENDPOINT ---
@app.route('/receipt', methods=['POST'])
def receipt_endpoint():
    # ... (your existing receipt logic)
    pass

if __name__ == '__main__':
    app.run(debug=True)