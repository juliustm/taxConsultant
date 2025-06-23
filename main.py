# main.py
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session
from config import Config
from models.user import db, InstanceConfig, Device, AdminOTP
from utils.security import generate_otp
import os

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# Create database tables if they don't exist
with app.app_context():
    db.create_all()

# --- Helper Functions ---
def get_instance_config():
    return InstanceConfig.query.first()

# --- Main Routes ---
@app.route('/')
def index():
    config = get_instance_config()
    if not config:
        return redirect(url_for('setup_email'))
    if not config.is_configured():
        flash("Your instance is not fully configured. Please complete the setup.", "warning")
        return redirect(url_for('admin_login'))
    return render_template('index.html')

# --- Admin Setup and Login Flow ---
@app.route('/admin/setup', methods=['GET', 'POST'])
def setup_email():
    if get_instance_config():
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        email = request.form.get('email')
        otp = generate_otp()
        
        # In a real app, send the OTP via email
        print(f"--- OTP for {email}: {otp} ---")
        
        new_otp = AdminOTP(email=email, otp=otp)
        db.session.add(new_otp)
        db.session.commit()

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
            # OTP is correct, create the admin user
            new_config = InstanceConfig(admin_email=email)
            db.session.add(new_config)
            db.session.delete(otp_entry) # OTP is used, delete it
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
        # For this MVP, we will just check if the email matches.
        # A full implementation would send another OTP for login.
        email = request.form.get('email')
        if email == config.admin_email:
            session['admin_logged_in'] = True
            flash('Logged in successfully.', 'success')
            return redirect(url_for('configure_instance'))
        else:
            flash('Invalid admin email.', 'danger')
    
    return render_template('admin/login.html', admin_email=config.admin_email)


# --- Configuration & Dashboard ---
@app.route('/admin/configure', methods=['GET', 'POST'])
def configure_instance():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    
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
def add_device():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    
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
    # 1. Authenticate the device
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization header is missing or invalid'}), 401
    
    device_key = auth_header.split(' ')[1]
    device = Device.query.filter_by(api_key=device_key).first()
    if not device:
        return jsonify({'error': 'Invalid device API key'}), 403

    # 2. Get the instance configuration
    config = get_instance_config()
    if not config or not config.is_configured():
        return jsonify({'error': 'Instance is not configured by the admin'}), 503

    # 3. Process the request data
    description = request.form.get('description', '')
    receipt_photo = request.files.get('receiptphoto')
    receipt_url = request.form.get('receipturl')

    if not receipt_photo and not receipt_url:
        return jsonify({'error': '`receiptphoto` (file) or `receipturl` (form field) is required'}), 400

    # 4. --- Placeholder for Core Logic ---
    # Here you would call your processing library.
    # from core.receipt import process_receipt
    # result = process_receipt(config, device, photo=receipt_photo, url=receipt_url, description=description)

    print("--- RECEIPT RECEIVED ---")
    print(f"Device: {device.name} ({device.id})")
    print(f"Description: {description}")
    if receipt_photo:
        print(f"Photo Filename: {receipt_photo.filename}")
    if receipt_url:
        print(f"Receipt URL: {receipt_url}")
    print("------------------------")
    
    # Simulate a successful processing
    mock_llm_response = {
        "vendor": "Mega Supermarket",
        "date": "2024-05-23",
        "total_amount": "55,000 TZS",
        "vat_amount": "8,389.83 TZS"
    }

    return jsonify({
        "message": "Receipt received and is being processed.",
        "device": device.name,
        "data_extracted": mock_llm_response
    }), 202 # 202 Accepted

if __name__ == '__main__':
    app.run(debug=True)