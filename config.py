# config.py (updated line)
import os
from dotenv import load_dotenv
from datetime import timedelta


class Config:
    # Standard Flask secret key
    SECRET_KEY = os.environ.get('SECRET_KEY', 'a-super-secret-key-for-local-development')

    # Session timeout configuration
    PERMANENT_SESSION_LIFETIME = timedelta(hours=1)

    # --- THE SEAMLESS DATABASE CONFIGURATION ---
    # Define a single, conventional path for the application's data.
    DATA_DIR = '/data'
    DB_FILE = 'taxconsult.db'
    
    # Ensure the data directory exists. This is critical for the first run.
    os.makedirs(DATA_DIR, exist_ok=True)

    # Always point the database URI to the conventional path.
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(DATA_DIR, DB_FILE)}"
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False