# models/user.py
from flask_sqlalchemy import SQLAlchemy
import uuid
from datetime import datetime, timedelta # <-- Add this import

db = SQLAlchemy()

class InstanceConfig(db.Model):
    """Stores the central configuration for the deployed instance."""
    id = db.Column(db.Integer, primary_key=True)
    admin_email = db.Column(db.String(120), unique=True, nullable=False)
    llm_provider = db.Column(db.String(50), nullable=True)
    llm_api_key = db.Column(db.String(200), nullable=True)
    export_spreadsheet_url = db.Column(db.String(500), nullable=True)
    export_s3_bucket = db.Column(db.String(200), nullable=True)
    export_callback_url = db.Column(db.String(500), nullable=True)
    
    def is_configured(self):
        return all([self.llm_provider, self.llm_api_key])

class Device(db.Model):
    """Stores information about registered devices/interfaces."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    api_key = db.Column(db.String(100), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

class AdminOTP(db.Model):
    """Temporarily stores OTPs for admin verification."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    otp = db.Column(db.String(6), nullable=False)
    # This line also needs 'datetime', which is now fixed by the import above
    expires_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.utcnow() + timedelta(minutes=10))

class Receipt(db.Model):
    """Stores processed receipt data."""
    id = db.Column(db.Integer, primary_key=True)
    vendor_name = db.Column(db.String(200), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    vat_amount = db.Column(db.Float, nullable=True)
    receipt_date = db.Column(db.Date, nullable=False)
    # This line was the source of the error, now fixed by the import
    processed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    
    device = db.relationship('Device', backref=db.backref('receipts', lazy=True))