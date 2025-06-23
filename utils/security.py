# utils/security.py
import jwt
import random
from datetime import datetime, timedelta, timezone
from flask import current_app

def generate_otp():
    """Generates a 6-digit OTP."""
    return str(random.randint(100000, 999999))

def generate_auth_token(email):
    """Generates a JWT for an authenticated admin."""
    payload = {
        'exp': datetime.now(timezone.utc) + timedelta(hours=24),
        'iat': datetime.now(timezone.utc),
        'sub': email
    }
    return jwt.encode(
        payload,
        current_app.config['SECRET_KEY'],
        algorithm='HS256'
    )

def verify_auth_token(token):
    """Verifies a JWT and returns the email if valid."""
    try:
        payload = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        return payload['sub']
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None