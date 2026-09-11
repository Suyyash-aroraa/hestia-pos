"""Hestia POS settings. Restart after editing. No external accounts required.
Enter your business details and tax rates before billing.
"""
import os
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
ADMIN_PASSWORD = os.environ.get('HESTIA_ADMIN_PASSWORD', '')
if not ADMIN_PASSWORD.strip():
    raise RuntimeError('No admin password configured. Run start.bat or start.py to enter one, or set HESTIA_ADMIN_PASSWORD.')
DATA_DIR = Path(os.environ.get('HESTIA_DATA_DIR') or BASE_DIR / 'data').resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
SQLALCHEMY_DATABASE_URI = os.environ.get('HESTIA_DATABASE_URL') or 'sqlite:///' + (DATA_DIR / 'hestia.db').as_posix()
SQLALCHEMY_TRACK_MODIFICATIONS = False
SQLALCHEMY_ENGINE_OPTIONS = {'connect_args': {'timeout': 30}} if SQLALCHEMY_DATABASE_URI.startswith('sqlite:') else {'pool_pre_ping': True}

HOST_IP = '127.0.0.1'
PORT = int(os.environ.get('HESTIA_PORT', '5010'))
SITE_URL = ''
CORS_ORIGINS = [f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}']
_secret_file = DATA_DIR / '.secret-key'
if not _secret_file.exists():
    try:
        with _secret_file.open('x', encoding='utf-8') as _secret:
            _secret.write(secrets.token_hex(32))
    except FileExistsError:
        pass
SECRET_KEY = _secret_file.read_text(encoding='utf-8').strip()
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
START_BACKGROUND_WORKERS = os.environ.get('HESTIA_TESTING') != '1'

RESTAURANT_NAME = 'Hestia POS'
RESTAURANT_ADDRESS = ''
RESTAURANT_PHONE = ''
GST_NUMBER = ''
FSSAI_NUMBER = ''
CGST_RATE = 0.0
SGST_RATE = 0.0
RESTAURANT_WEBSITE = ''
BILL_FOOTER_MSG = 'Thank you for your visit!'
INSTAGRAM_HANDLE = ''
GOOGLE_LISTING = ''
BILL_JURISDICTION = ''
BILL_VISIBLE_CONFIGS = 'Small, Medium, Large, Half, Full, Regular'

ENABLE_TAKEOUT = True
KITCHEN_PRINTER_ENABLED = False
KITCHEN_PRINTER_NAME = ''
BILL_PRINTER_NAME = ''

THEME_PRIMARY = '#2C1810'
THEME_PRIMARY_MID = '#4A2C1A'
THEME_SECONDARY = '#7B3F1E'
THEME_ACCENT = '#C97B2E'
THEME_ACCENT_LIGHT = '#E8A84E'
THEME_BG = '#F9F5EE'
THEME_BG_DARK = '#EDE8DC'
THEME_FOAM = '#FFFDF9'
THEME_MUTED = '#8C7B6B'
THEME_TEXT = '#2C1810'
THEME_BORDER = 'rgba(44,24,16,0.12)'
THEME_BORDER_STRONG = 'rgba(44,24,16,0.25)'
