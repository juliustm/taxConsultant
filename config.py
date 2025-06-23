# config.py (updated line)
import os
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY')
    # Point the database to the 'instance' folder, which will be mounted as a volume
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'instance', 'taxconsult.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False