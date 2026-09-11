import os
import sys
import tempfile
import secrets
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_test_data = tempfile.TemporaryDirectory(prefix='hestia-tests-')
os.environ['HESTIA_DATABASE_URL'] = 'sqlite:///' + (Path(_test_data.name) / 'test.db').as_posix()
os.environ['HESTIA_TESTING'] = '1'
os.environ['HESTIA_DATA_DIR'] = _test_data.name
os.environ['HESTIA_ADMIN_PASSWORD'] = secrets.token_urlsafe(32)

from app import app, _seed_tables
from models import db, MenuItem


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_tables()
        db.session.add(MenuItem(name='Coffee', category='Drinks', price=100, available=True))
        db.session.commit()
        from routes.tables import _invalidate_tables_cache
        from routes.menu import _invalidate_menu_cache
        _invalidate_tables_cache()
        _invalidate_menu_cache()
        yield app.test_client()
        db.session.remove()


def pytest_sessionfinish(session, exitstatus):
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    _test_data.cleanup()
