# tests/conftest.py
"""
Test setup.

DATA_DIR is redirected before anything imports config, because importing the app
creates the directory and opens the SQLite file inside it, and the container default
('/app/data') is not writable on a developer machine.
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='taxconsult-tests-'))

import pytest

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


@pytest.fixture(scope='session')
def receipt_html():
    """A real verified-receipt page, saved from verify.tra.go.tz."""
    return (FIXTURES / 'receipt_58E41A514.html').read_text()


@pytest.fixture
def app():
    """The Flask app with an empty database."""
    import main
    from models.user import db

    with main.app.app_context():
        db.drop_all()
        db.create_all()
        yield main.app
        db.session.remove()


@pytest.fixture
def device(app):
    from models.user import db, Device

    device = Device(name='Test device', api_key='test-key')
    db.session.add(device)
    db.session.commit()
    return device


@pytest.fixture
def config(app):
    """An instance config with no LLM credentials, i.e. the model is unavailable."""
    from models.user import db, InstanceConfig

    config = InstanceConfig(admin_email='admin@example.com', totp_secret='SECRET')
    db.session.add(config)
    db.session.commit()
    return config
