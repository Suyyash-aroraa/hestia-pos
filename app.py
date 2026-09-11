import os
import sys

# ── PyInstaller frozen-path setup ─────────────────────────────────────────────
# FrozenImporter in sys.meta_path takes precedence over sys.path, so we must
# explicitly load external config.py (next to the .exe) via importlib and
# inject it into sys.modules before any other imports run.
if getattr(sys, 'frozen', False):
    _exe_dir = os.path.dirname(sys.executable)
    _static_dir = os.path.join(sys._MEIPASS, 'frontend')
    _ext_config = os.path.join(_exe_dir, 'config.py')
    if os.path.exists(_ext_config):
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location('config', _ext_config)
        _cfg_mod = _ilu.module_from_spec(_spec)
        sys.modules['config'] = _cfg_mod
        _spec.loader.exec_module(_cfg_mod)
else:
    _static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'frontend')
# ─────────────────────────────────────────────────────────────────────────────

import json
import queue
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory
from flask_compress import Compress
from flask_cors import CORS

import config
from config import (
    CORS_ORIGINS,
    ENABLE_TAKEOUT,
    GST_NUMBER,
    CGST_RATE,
    SGST_RATE,
    RESTAURANT_NAME,
    THEME_PRIMARY, THEME_PRIMARY_MID, THEME_SECONDARY,
    THEME_ACCENT, THEME_ACCENT_LIGHT,
    THEME_BG, THEME_BG_DARK, THEME_FOAM, THEME_MUTED,
    THEME_TEXT, THEME_BORDER, THEME_BORDER_STRONG,
    KITCHEN_PRINTER_ENABLED,
    KITCHEN_PRINTER_NAME,
    SITE_URL,
    RESTAURANT_ADDRESS,
    RESTAURANT_PHONE,
    FSSAI_NUMBER,
    RESTAURANT_WEBSITE,
    BILL_FOOTER_MSG,
    INSTAGRAM_HANDLE,
    GOOGLE_LISTING,
    BILL_JURISDICTION,
    BILL_VISIBLE_CONFIGS,
)
from models import Session, Table, db
from schema_sync import sync_schema

_ASSET_VERSION = str(int(time.time()))

app = Flask(__name__, static_folder=_static_dir, static_url_path='')
app.config.from_object(config)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)
db.init_app(app)

CORS(app, resources={r'/api/*': {'origins': CORS_ORIGINS}})
Compress(app)

from routes.admin import bp as admin_bp, _require_admin
from routes.bills import bp as bills_bp
from routes.menu import bp as menu_bp
from routes.menu_manager import bp as menu_manager_bp
from routes.orders import bp as orders_bp
from routes.payments import bp as payments_bp
from routes.session import bp as session_bp
from routes.tables import bp as tables_bp
from routes.takeout import bp as takeout_bp
from routes.kot_queue import bp as kot_queue_bp
from routes.print_svc import bp as print_svc_bp
from routes.staff import bp as staff_bp

app.register_blueprint(admin_bp)
app.register_blueprint(bills_bp)
app.register_blueprint(menu_bp)
app.register_blueprint(menu_manager_bp)
app.register_blueprint(orders_bp)
app.register_blueprint(payments_bp)
app.register_blueprint(session_bp)
app.register_blueprint(tables_bp)
app.register_blueprint(takeout_bp)
app.register_blueprint(kot_queue_bp)
app.register_blueprint(print_svc_bp)
app.register_blueprint(staff_bp)

@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    return response


@app.route('/api/config')
def get_config():
    return jsonify({
        'restaurant_name': RESTAURANT_NAME,
        'gst_number': GST_NUMBER,
        'cgst_rate': CGST_RATE,
        'sgst_rate': SGST_RATE,
        'enable_takeout': ENABLE_TAKEOUT,
        'product_name': 'Hestia POS',
        'site_url': SITE_URL,
        'address': RESTAURANT_ADDRESS,
        'phone': RESTAURANT_PHONE,
        'fssai_number': FSSAI_NUMBER,
        'restaurant_website': RESTAURANT_WEBSITE,
        'footer_msg': BILL_FOOTER_MSG,
        'instagram': INSTAGRAM_HANDLE,
        'google_listing': GOOGLE_LISTING,
        'jurisdiction': BILL_JURISDICTION,
        'bill_visible_configs': getattr(config, 'BILL_VISIBLE_CONFIGS', ''),
    })


@app.route('/')
def serve_dashboard():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/dashboard')
def dashboard_totals():
    from models import Bill
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    bills = Bill.query.filter(
        Bill.settled_at >= start.astimezone(timezone.utc),
        Bill.settled_at < end.astimezone(timezone.utc),
        Bill.is_cancelled.is_(False),
        Bill.is_split_child.is_(False),
    ).all()
    return jsonify({
        'open_tables': Session.query.filter_by(status='active', session_type='table').count(),
        'open_takeouts': Session.query.filter_by(status='active', session_type='takeout').count(),
        'settled_bills': len(bills),
        'sales': round(sum(float(b.amount or 0) for b in bills if not b.is_complementary), 2),
    })










@app.route('/pos')
def serve_pos():
    return send_from_directory(app.static_folder, 'pos/index.html')






@app.route('/admin')
def serve_admin():
    return send_from_directory(app.static_folder, 'admin/index.html')




@app.route('/menu-manager')
def serve_menu_manager():
    return send_from_directory(app.static_folder, 'menu-manager/index.html')













@app.route('/shared-sse.js')
def serve_shared_sse_js():
    return send_from_directory(app.static_folder, 'shared-sse.js',
                               mimetype='application/javascript')


@app.route('/print-bill.js')
def serve_print_bill_js():
    return send_from_directory(app.static_folder, 'print-bill.js',
                               mimetype='application/javascript')


@app.route('/favicon.ico')
def serve_favicon():
    return send_from_directory(app.static_folder, 'favicon.svg', mimetype='image/svg+xml')


@app.route('/pos-takeout')
def serve_pos_takeout():
    return send_from_directory(app.static_folder, 'pos-takeout/index.html')


@app.route('/history')
def serve_history():
    return send_from_directory(app.static_folder, 'history/index.html')


@app.route('/items-report')
def serve_items_report():
    return send_from_directory(app.static_folder, 'items-report/index.html')




@app.route('/brand.js')
def serve_brand():
    kot = 'true' if KITCHEN_PRINTER_ENABLED else 'false'
    pname = KITCHEN_PRINTER_NAME.replace('"', '\\"')
    siteurl = SITE_URL.rstrip('/')
    brand_obj = json.dumps({
        'name': RESTAURANT_NAME,
        'address': RESTAURANT_ADDRESS,
        'phone': RESTAURANT_PHONE,
        'gst_number': GST_NUMBER,
        'fssai_number': FSSAI_NUMBER,
        'restaurant_website': RESTAURANT_WEBSITE,
        'footer_msg': BILL_FOOTER_MSG,
        'instagram': INSTAGRAM_HANDLE,
        'google_listing': GOOGLE_LISTING,
        'jurisdiction': BILL_JURISDICTION,
        'site_url': siteurl,
        'bill_visible_configs': BILL_VISIBLE_CONFIGS,
    }, ensure_ascii=False)
    js = f"""(function(){{
var k={kot},p={json.dumps(KITCHEN_PRINTER_NAME)},su={json.dumps(siteurl)},b={brand_obj};
window.__brand=b;window.__kotEnabled=k;window.__kotPrinterName=p;window.__siteUrl=su;
var s=document.createElement('script');s.src='/print-kot.js?v={_ASSET_VERSION}';document.head.appendChild(s);
document.addEventListener('DOMContentLoaded',function(){{
  var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);var node;
  while((node=w.nextNode())){{if(node.nodeValue.indexOf('Hestia POS')!==-1)node.nodeValue=node.nodeValue.replace(/Hestia POS/g,b.name);}}
  document.title=document.title.replace(/Hestia POS/g,b.name);
}});
}})();"""
    return Response(js, mimetype='application/javascript',
                    headers={'Cache-Control': 'no-store'})


@app.route('/theme.css')
def serve_theme():
    from flask import Response
    css = f""":root {{
  --espresso:      {THEME_PRIMARY};
  --espresso-mid:  {THEME_PRIMARY_MID};
  --roast:         {THEME_SECONDARY};
  --caramel:       {THEME_ACCENT};
  --caramel-light: {THEME_ACCENT_LIGHT};
  --cream:         {THEME_BG};
  --cream-dark:    {THEME_BG_DARK};
  --foam:          {THEME_FOAM};
  --muted:         {THEME_MUTED};
  --border:        {THEME_BORDER};
  --border-strong: {THEME_BORDER_STRONG};
  --text:          {THEME_TEXT};
}}
body {{ color: var(--text); background: {THEME_BG}; }}"""
    return Response(css, mimetype='text/css',
                    headers={'Cache-Control': 'max-age=300'})






# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC PICKUP TOKENS API (for display screen)
# ═══════════════════════════════════════════════════════════════════════════════



def _seed_tables():
    if Table.query.count() == 0:
        for n in range(1, 11):
            db.session.add(Table(number=n, capacity=4, status='empty'))
        db.session.commit()





# POS events support live staff carts and table changes.
sse_clients = {'pos': []}

def push_pos_event(data):
    payload = json.dumps(data, default=str)
    for client in list(sse_clients['pos']):
        try:
            client.put_nowait(payload)
        except queue.Full:
            pass

from routes.events import bp as events_bp
app.register_blueprint(events_bp)

with app.app_context():
    db.create_all()
    sync_schema(db, logger=app.logger)
    _seed_tables()

if config.START_BACKGROUND_WORKERS and config.KITCHEN_PRINTER_ENABLED:
    from kot_worker import _kot_worker_loop
    threading.Thread(target=_kot_worker_loop, args=(app, db), daemon=True).start()

if __name__ == '__main__':
    from waitress import serve
    print(f'Hestia POS: http://127.0.0.1:{config.PORT}')
    serve(app, host=config.HOST_IP, port=config.PORT, threads=16, channel_timeout=600)
