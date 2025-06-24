# models/user.py
from flask_sqlalchemy import SQLAlchemy
import uuid
from datetime import datetime

db = SQLAlchemy()

class InstanceConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_email = db.Column(db.String(120), unique=True, nullable=False)
    
    # TOTP Secret
    totp_secret = db.Column(db.String(100), unique=True, nullable=False)

    # LLM Configuration
    llm_provider = db.Column(db.String(50), nullable=True)
    llm_api_key = db.Column(db.String(200), nullable=True)
    
    # Export / Backup Configuration
    export_spreadsheet_url = db.Column(db.String(500), nullable=True)
    post_callback_url = db.Column(db.String(500), nullable=True)
    s3_bucket_name = db.Column(db.String(200), nullable=True)
    s3_access_key_id = db.Column(db.String(200), nullable=True)
    s3_secret_access_key = db.Column(db.String(200), nullable=True)
    s3_region = db.Column(db.String(50), nullable=True)

    def is_configured(self):
        return all([self.llm_provider, self.llm_api_key])

class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    api_key = db.Column(db.String(100), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

class Receipt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vendor_name = db.Column(db.String(200), nullable=True)
    total_amount = db.Column(db.Float, nullable=True)
    vat_amount = db.Column(db.Float, nullable=True)
    receipt_date = db.Column(db.Date, nullable=True)
    processed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    device = db.relationship('Device', backref=db.backref('receipts', lazy=True))