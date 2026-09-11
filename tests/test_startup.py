import os
from pathlib import Path
import secrets
import subprocess
import sys

from start import configure_admin_password


def test_startup_prompts_without_storing_password(monkeypatch, tmp_path):
    password = secrets.token_urlsafe(24)
    monkeypatch.delenv('HESTIA_ADMIN_PASSWORD', raising=False)
    monkeypatch.chdir(tmp_path)
    answers = iter(['', password, 'mismatch', password, password])
    monkeypatch.setattr('getpass.getpass', lambda prompt: next(answers))
    configure_admin_password()
    assert os.environ['HESTIA_ADMIN_PASSWORD'] == password
    assert list(tmp_path.iterdir()) == []


def test_supplied_password_skips_prompt(monkeypatch):
    password = secrets.token_urlsafe(24)
    monkeypatch.setenv('HESTIA_ADMIN_PASSWORD', password)
    monkeypatch.setattr('getpass.getpass', lambda prompt: (_ for _ in ()).throw(AssertionError('Unexpected prompt')))
    configure_admin_password()
    assert os.environ['HESTIA_ADMIN_PASSWORD'] == password


def test_blank_password_prevents_direct_backend_start(tmp_path):
    env = dict(os.environ, HESTIA_ADMIN_PASSWORD=' ', HESTIA_DATA_DIR=str(tmp_path), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', 'import config'], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'No admin password configured' in result.stderr
    assert list(tmp_path.iterdir()) == []
