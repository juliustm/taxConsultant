# models/user.py
from flask_sqlalchemy import SQLAlchemy
import uuid
from datetime import datetime

db = SQLAlchemy()

class InstanceConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_email = db.Column(db.String(120), unique=True, nullable=False)
    totp_secret = db.Column(db.String(100), unique=True, nullable=False)
    llm_provider = db.Column(db.String(50), nullable=True)
    llm_api_key = db.Column(db.String(200), nullable=True)
    
    post_callback_url = db.Column(db.String(500), nullable=True)
    s3_bucket_name = db.Column(db.String(200), nullable=True)
    s3_access_key_id = db.Column(db.String(200), nullable=True)
    s3_secret_access_key = db.Column(db.String(200), nullable=True)
    s3_region = db.Column(db.String(50), nullable=True)

    google_sheet_id = db.Column(db.String(200), nullable=True)
    google_service_account_json = db.Column(db.Text, nullable=True)

    def is_configured(self):
        return all([self.llm_provider, self.llm_api_key])

class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    api_key = db.Column(db.String(100), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    received_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=False, default='queued', index=True)
    input_type = db.Column(db.String(10), nullable=False)
    input_data = db.Column(db.String(1024), nullable=False)
    description = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(255), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    device = db.relationship('Device', backref=db.backref('submissions', lazy=True))

class Receipt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    
    # --- Core Extracted Fields ---
    vendor_name = db.Column(db.String(200), nullable=True)
    vendor_tin = db.Column(db.String(50), nullable=True)
    vendor_phone = db.Column(db.String(50), nullable=True)
    vrn = db.Column(db.String(50), nullable=True)  # <-- THIS LINE FIXES THE ERROR
    receipt_verification_code = db.Column(db.String(50), nullable=True, index=True, unique=True)
    receipt_number = db.Column(db.String(100), nullable=True)
    uin = db.Column(db.String(200), nullable=True)
    
    # --- Customer Fields ---
    customer_name = db.Column(db.String(200), nullable=True)
    customer_id_type = db.Column(db.String(100), nullable=True)
    customer_id = db.Column(db.String(100), nullable=True)
    
    total_amount = db.Column(db.Float, nullable=True)
    vat_amount = db.Column(db.Float, nullable=True)
    receipt_date = db.Column(db.Date, nullable=True)
    
    # --- System & Audit Fields ---
    processed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    raw_llm_response = db.Column(db.Text, nullable=True)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    device = db.relationship('Device', backref=db.backref('receipts', lazy=True))
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'), unique=True, nullable=False)
    submission = db.relationship('Submission', backref=db.backref('receipt', uselist=False, lazy=True))