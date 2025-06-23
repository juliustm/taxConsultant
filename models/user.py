# models/user.py
from flask_sqlalchemy import SQLAlchemy
import uuid

db = SQLAlchemy()

class InstanceConfig(db.Model):
    """Stores the central configuration for the deployed instance."""
    id = db.Column(db.Integer, primary_key=True)
    admin_email = db.Column(db.String(120), unique=True, nullable=False)
    # Store encrypted keys in a real application
    llm_provider = db.Column(db.String(50), nullable=True)
    llm_api_key = db.Column(db.String(200), nullable=True)
    export_spreadsheet_url = db.Column(db.String(500), nullable=True)
    export_s3_bucket = db.Column(db.String(200), nullable=True)
    export_callback_url = db.Column(db.String(500), nullable=True)
    
    def is_configured(self):
        """Check if essential configuration is complete."""
        return all([self.llm_provider, self.llm_api_key])

class Device(db.Model):
    """Stores information about registered devices/interfaces."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    # The unique key used by the device to authenticate with the /receipt endpoint
    api_key = db.Column(db.String(100), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    
class AdminOTP(db.Model):
    """Temporarily stores OTPs for admin verification."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    otp = db.Column(db.String(6), nullable=False)
    # Add an expiry timestamp in a production app