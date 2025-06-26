# config.py (Final, Corrected, and Seamless Version)
import os
from datetime import timedelta

class Config:
    # Standard Flask secret key
    SECRET_KEY = os.environ.get('SECRET_KEY', 'a-super-secret-key-for-local-development')
    TASK_RUNNER_SECRET_KEY = os.environ.get('TASK_RUNNER_SECRET_KEY', 'local-secret-runner-key')

    # Session timeout configuration
    PERMANENT_SESSION_LIFETIME = timedelta(hours=1)

    # --- THE SEAMLESS DATABASE CONFIGURATION ---
    # Define a single, conventional path inside the project's root directory.
    # The project lives at '/app' inside the container.
    DATA_DIR = '/app/data'
    DB_FILE = 'taxconsult.db'
    DB_PATH = os.path.join(DATA_DIR, DB_FILE)
    
    # Ensure the data directory exists. This command will run as the container's user.
    # On Deploy.tz, this user has permission to create subdirectories inside /app.
    # This resolves the "Permission denied" error.
    os.makedirs(DATA_DIR, exist_ok=True)

    # Always point the database URI to our conventional path.
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DB_PATH}"
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False