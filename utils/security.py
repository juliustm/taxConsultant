# utils/security.py
import pyotp
import qrcode
import base64
from io import BytesIO

def generate_totp_provisioning_uri(secret, email, issuer_name="TaxConsult AI"):
    """Generates the provisioning URI for TOTP apps."""
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=email,
        issuer_name=issuer_name
    )

def generate_qr_code_base64(uri):
    """Generates a QR code from a URI and returns it as a base64 encoded string."""
    img = qrcode.make(uri)
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{img_str}"