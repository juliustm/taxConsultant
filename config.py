# config.py (Final, Corrected, and Seamless Version)
import os
from datetime import timedelta

class Config:
    # Standard Flask secret key
    SECRET_KEY = os.environ.get('SECRET_KEY', 'a-super-secret-key-for-local-development')
    TASK_RUNNER_SECRET_KEY = os.environ.get('TASK_RUNNER_SECRET_KEY', 'local-secret-runner-key')

    # Session timeout configuration
    PERMANENT_SESSION_LIFETIME = timedelta(days=365)

    # The admin session cookie. This matters more now that the scanner PWA puts a
    # second kind of principal on the same origin: a device must never be able to
    # reach the dashboard, and script on any page must never be able to read this.
    # (Device sessions deliberately use a bearer header instead - see
    # utils/device_auth - so they carry no CSRF surface at all.)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    # Defaults on. Turned off only for local HTTP development, where a Secure cookie
    # would simply never be sent and login would appear to silently fail.
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'true').lower() != 'false'

    # --- THE SEAMLESS DATABASE CONFIGURATION ---
    # Define a single, conventional path inside the project's root directory.
    # The project lives at '/app' inside the container. DATA_DIR can be overridden so
    # the app can be imported outside the container - the test suite points it at a
    # temporary directory, since '/app' is not writable on a developer machine.
    DATA_DIR = os.environ.get('DATA_DIR', '/app/data')
    # Kept as 'taxconsult.db' even after the app's rename to Karani: this is the real
    # SQLite filename already sitting in every deployed instance's data volume.
    # Renaming it would point a fresh deploy at an empty database instead of the
    # existing one, with no migration in between.
    DB_FILE = 'taxconsult.db'
    DB_PATH = os.path.join(DATA_DIR, DB_FILE)
    
    # Ensure the data directory exists. This command will run as the container's user.
    # On Deploy.tz, this user has permission to create subdirectories inside /app.
    # This resolves the "Permission denied" error.
    os.makedirs(DATA_DIR, exist_ok=True)

    # Always point the database URI to our conventional path.
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DB_PATH}"
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False