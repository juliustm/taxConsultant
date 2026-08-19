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

    # --- PERSISTENCE CONFIGURATION ---
    # Everything that has to survive a deploy lives under this one directory, and
    # nothing outside this class is allowed to name a path inside it. In production
    # it is the platform's persistent-folder mount at '/karani'; it is deliberately
    # NOT under '/app', which is the code directory a deploy replaces wholesale.
    # A relative value (the local-development default) is resolved against the
    # working directory, so importing the app on a developer machine writes to
    # ./karani instead of trying to create a directory at the filesystem root.
    DATA_DIR = os.environ.get('DATA_DIR', 'karani')
    if not os.path.isabs(DATA_DIR):
        DATA_DIR = os.path.abspath(DATA_DIR)

    # Kept as 'taxconsult.db' even after the app's rename to Karani: this is the real
    # SQLite filename already sitting in every deployed instance's data volume.
    # Renaming it would point a fresh deploy at an empty database instead of the
    # existing one, with no migration in between.
    DB_FILE = 'taxconsult.db'
    DB_PATH = os.path.join(DATA_DIR, DB_FILE)

    # The one upload category this app has: receipt photos, served back out through
    # the @login_required /uploads/<filename> route and read by the vision model.
    UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')

    # The largest request body this app will take, which in practice means the largest
    # receipt photo.
    #
    # Set explicitly because the alternative is not "no limit", it is "whatever the
    # proxy in front of gunicorn happens to default to" - and phones in the field found
    # that the hard way. An upload past that limit is refused by the ingress before
    # Flask is ever reached, so the app answered nothing at all and the scanner showed a
    # bare 'HTTP 413' under a receipt that then sat in its outbox forever. A limit named
    # here is one the app can enforce, explain, and be tested against.
    #
    # 20MB rather than something snug: a phone photograph is the input, an unmodified
    # 12MP HEIC-to-JPEG conversion clears 8MB without trying, and the direct API path
    # takes whatever a bot sends. Nothing is stored at this size regardless - see
    # utils/images.store_photo, which caps the pixels on the way in - so the number only
    # has to be generous enough that a real receipt is never turned away at the door.
    #
    # NOTE: this is a ceiling, not a floor. The ingress in front of this app has its own
    # body limit and the lower of the two wins, so raising this without raising that
    # (nginx: client_max_body_size 20m) changes nothing.
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024

    # Created at import so a brand-new empty volume bootstraps itself on first boot.
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # Always point the database URI to our conventional path.
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DB_PATH}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False