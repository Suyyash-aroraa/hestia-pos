import base64
import binascii
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from urllib import request as urllib_request

from flask import Blueprint, jsonify, request

from printing import build_kot_raw
from printing.kot_builder import _kot_beep_raw
from printing.raw_print import (
    ensure_spooler_running,
    list_printers,
    printer_exists,
    send_raw,
    spooler_state,
)
from models import Order, db

# Guards against a stuck client hammering us with multi-hundred-KB rasters.
_MAX_RASTER_BYTES = 4 * 1024 * 1024

bp = Blueprint('print_svc', __name__, url_prefix='/api/print')

_DEDUP_LOCK = threading.Lock()
_DEDUP_CACHE = {}

_DEDUP_TTL_NON_REPRINT = 120.0
_DEDUP_TTL_REPRINT = 10.0
_DEDUP_TTL_VOID = 60.0
# Only long enough to swallow two sources firing the same bill at once. Kept
# short so an operator never has to wait to reprint a bill that came out badly.
_DEDUP_TTL_BILL_AUTO = 20.0


def _dedup_cleanup_locked(now):
    expired = [k for k, (ts, _) in _DEDUP_CACHE.items() if (now - ts) > max(_DEDUP_TTL_NON_REPRINT, _DEDUP_TTL_REPRINT, _DEDUP_TTL_VOID) * 2]
    for k in expired:
        del _DEDUP_CACHE[k]


def _dedup_check(key, ttl_seconds):
    now = time.monotonic()
    with _DEDUP_LOCK:
        _dedup_cleanup_locked(now)
        entry = _DEDUP_CACHE.get(key)
        if entry is not None:
            ts, _ = entry
            if (now - ts) <= ttl_seconds:
                return True
        _DEDUP_CACHE[key] = (now, True)
        return False


def _items_hash(items):
    try:
        normalized = json.dumps(items, sort_keys=True, separators=(',', ':'))
    except Exception:
        normalized = str(items)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def _worker_is_handling_order(order_id):
    """Return True if the KOT queue worker already has an active (pending/printing)
    or very recently completed entry for this order, to avoid double-printing when
    both the worker path AND the frontend endpoint path are active."""
    try:
        from models import KOTQueue
        oid = int(order_id)
    except (TypeError, ValueError):
        return False
    active = KOTQueue.query.filter(
        KOTQueue.order_id == oid,
        KOTQueue.status.in_(('pending', 'printing')),
    ).first()
    if active is not None:
        return True
    return False


def _resolve_kot_printed_at(order_id, reprint):
    """Return the timestamp to print on the KOT ticket.

    For first-time prints, records the current time on the order row.
    For reprints, returns the previously recorded first-print time.
    """
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return None
    order = db.session.get(Order, oid)
    if order is None:
        return None
    if reprint:
        return order.kot_printed_at
    if order.kot_printed_at is None:
        order.kot_printed_at = datetime.now(timezone.utc)
        db.session.commit()
    return order.kot_printed_at


def _candidate_config_paths():
    """Return possible config.py locations for both dev and frozen (PyInstaller) builds.

    In a packaged build, __file__ lives inside the bundle's _internal folder, NOT next
    to the external config.py the operator edits. So we must also check the directory of
    the executable and the current working directory.
    """
    paths = []
    # 1) Frozen build: external config.py next to the .exe
    if getattr(sys, 'frozen', False):
        paths.append(os.path.join(os.path.dirname(sys.executable), 'config.py'))
    # 2) Dev: project root (parent of routes/)
    paths.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.py'))
    # 3) Current working directory
    paths.append(os.path.join(os.getcwd(), 'config.py'))
    return paths


def _get_config_value(key):
    """Read a value directly from config.py to avoid Python module caching."""
    for config_path in _candidate_config_paths():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue
        import ast
        for node in ast.parse(content).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == key for t in node.targets):
                try:
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        return value
                except (ValueError, TypeError):
                    pass
    print(f'[DEBUG] config.py not found / {key} missing. Tried: {_candidate_config_paths()}', file=sys.stderr)
    return ''


def _normalize_remaining_items(raw_items):
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except Exception:
            raw_items = []
    if not isinstance(raw_items, list):
        raw_items = []

    rows = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or item.get('item_name') or '').strip()
        qty = item.get('quantity', item.get('qty', 0))
        notes = str(item.get('notes') or '').strip()
        if not name:
            continue
        rows.append({
            'name': name,
            'quantity': qty,
            'notes': notes,
        })
    return rows


@bp.route('/kot', methods=['POST'])
def print_kot():
    KITCHEN_PRINTER_NAME = _get_config_value('KITCHEN_PRINTER_NAME')
    print(f'[DEBUG] KOT printer name from config: "{KITCHEN_PRINTER_NAME}"', file=sys.stderr)
    if not KITCHEN_PRINTER_NAME:
        return jsonify({'error': 'KITCHEN_PRINTER_NAME not set in config'}), 400

    data     = request.get_json(force=True, silent=True) or {}
    order_id = data.get('order_id', '')
    location = data.get('location', '')
    items    = data.get('items', [])
    kot_comment = data.get('kot_comment')
    token    = data.get('token')

    reprint  = data.get('reprint', False)

    if not reprint and _worker_is_handling_order(order_id):
        print(f'[DEBUG] KOT order {order_id} already handled by worker queue, skipping endpoint print', file=sys.stderr)
        return jsonify({'ok': True, 'worker_queued': True})

    ttl = _DEDUP_TTL_REPRINT if reprint else _DEDUP_TTL_NON_REPRINT
    dedup_key = f"kot:{order_id}:{reprint}:kitchen:{location}:{_items_hash(items)}:{str(kot_comment or '')}"
    if _dedup_check(dedup_key, ttl):
        print(f'[DEBUG] KOT dedup hit for order {order_id} reprint={reprint}, skipping print', file=sys.stderr)
        return jsonify({'ok': True, 'dedup': True})

    kot_printed_at = _resolve_kot_printed_at(order_id, reprint)
    raw = build_kot_raw(order_id, location, items, reprint=reprint, kot_comment=kot_comment, token=token, kot_printed_at=kot_printed_at)

    try:
        send_raw(KITCHEN_PRINTER_NAME, raw, 'KOT')
        print(f'[DEBUG] KOT printed successfully', file=sys.stderr)
        return jsonify({'ok': True})
    except ImportError:
        msg = 'pywin32 not installed — run: pip install pywin32'
        print(msg, file=sys.stderr)
        return jsonify({'error': msg}), 500
    except Exception as e:
        print(f'[DEBUG] KOT print error: {e}', file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': str(e)}), 500


@bp.route('/bill', methods=['POST'])
def print_bill():
    """Forward bill print request to local Electron app on port 5002 for silent printing."""
    data = request.get_json(force=True, silent=True) or {}
    html = data.get('html')
    if not html:
        return jsonify({'error': 'html is required'}), 400

    # Read BILL_PRINTER_NAME directly from config.py to avoid module caching
    bill_printer_name = _get_config_value('BILL_PRINTER_NAME')

    # Forward to local Electron bill printer agent
    # Printer name is read by Electron app from its own config.py — no need to forward it
    bill_id = data.get('bill_id')
    payload = json.dumps({
        'html': html,
        'bill_id': bill_id
    }).encode('utf-8')

    try:
        req = urllib_request.Request(
            'http://127.0.0.1:5002/print-bill',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib_request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            return jsonify(result)
    except Exception as e:
        print(f'Bill print agent error (falling back to browser): {e}', file=sys.stderr)
        return jsonify({'error': 'Bill print agent unavailable', 'fallback': True}), 503


@bp.route('/printers', methods=['GET'])
def printers():
    """Printers on the machine running this server, for the bill printer picker."""
    try:
        return jsonify({
            'ok': True,
            'configured': _get_config_value('BILL_PRINTER_NAME'),
            'kitchen': _get_config_value('KITCHEN_PRINTER_NAME'),
            'spooler_running': spooler_state() == 4,
            'printers': list_printers(),
        })
    except Exception as e:
        print(f'Printer enumeration failed: {e}', file=sys.stderr)
        return jsonify({'error': str(e)}), 500


@bp.route('/bill-raw', methods=['POST'])
def print_bill_raw():
    """Print a bill the browser already rasterized into ESC/POS bytes.

    The client renders and thresholds the bill itself, so the printer driver is
    never asked to render anything — which is what used to take the whole print
    spooler down mid-service. This is the same delivery path KOTs use.
    """
    data = request.get_json(force=True, silent=True) or {}
    raster_b64 = data.get('raster')
    if not raster_b64:
        return jsonify({'error': 'raster is required'}), 400

    printer_name = (data.get('printer_name') or '').strip() or _get_config_value('BILL_PRINTER_NAME')
    if not printer_name:
        return jsonify({'error': 'BILL_PRINTER_NAME not set in config'}), 400

    try:
        raw = base64.b64decode(raster_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        return jsonify({'error': f'raster is not valid base64: {e}'}), 400

    if not raw:
        return jsonify({'error': 'raster decoded to zero bytes'}), 400
    if len(raw) > _MAX_RASTER_BYTES:
        return jsonify({'error': f'raster too large ({len(raw)} bytes)'}), 413

    bill_id = data.get('bill_id')
    reprint = bool(data.get('reprint', False))
    job_id = data.get('job_id')

    # Duplicate detection has to stop the same bill printing twice on its own
    # while never standing in the way of an operator who wants another copy —
    # a roll running out mid-print means they need one immediately.
    #
    # job_id is unique per print attempt, so a retried HTTP request is dropped
    # but a second press of the button is not.
    if job_id and _dedup_check(f'billjob:{job_id}', _DEDUP_TTL_NON_REPRINT):
        print(f'[DEBUG] Bill job {job_id} already printed, skipping duplicate request', file=sys.stderr)
        return jsonify({'ok': True, 'dedup': True})

    # Automatic prints additionally dedup on the bill itself, because a single
    # bill can be fired by more than one source (the POS page and a head-pos
    # tab on the SSE feed). Reprints are always explicit, so they skip this.
    if bill_id is not None and not reprint:
        if _dedup_check(f'billraw:{bill_id}:{printer_name}', _DEDUP_TTL_BILL_AUTO):
            print(f'[DEBUG] Bill raster dedup hit for bill {bill_id}, skipping print', file=sys.stderr)
            return jsonify({'ok': True, 'dedup': True})

    if not printer_exists(printer_name):
        # Could be a genuinely missing printer, or the spooler having died and
        # taken every printer with it. Tell them apart before failing.
        if spooler_state() != 4:
            ok, msg = ensure_spooler_running()
            if not ok:
                return jsonify({'error': msg, 'spooler_down': True}), 503
        if not printer_exists(printer_name):
            return jsonify({'error': f'Printer not found: {printer_name}'}), 400

    try:
        send_raw(printer_name, raw, f'Bill {bill_id}' if bill_id else 'Bill')
        print(f'[DEBUG] Bill {bill_id} printed raw ({len(raw)} bytes) to {printer_name}', file=sys.stderr)
        return jsonify({'ok': True, 'bytes': len(raw), 'printer': printer_name})
    except ImportError:
        msg = 'pywin32 not installed — run: pip install pywin32'
        print(msg, file=sys.stderr)
        return jsonify({'error': msg}), 500
    except Exception as e:
        print(f'Bill raw print error: {e}', file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': str(e), 'spooler_running': spooler_state() == 4}), 500


@bp.route('/kot-at-printer', methods=['POST'])
def print_kot_at_printer():
    """Print KOT at a specified printer (e.g., bill printer for 'Print here' option)."""
    BILL_PRINTER_NAME = _get_config_value('BILL_PRINTER_NAME')

    if not BILL_PRINTER_NAME:
        return jsonify({'error': 'BILL_PRINTER_NAME not set in config'}), 400

    data     = request.get_json(force=True, silent=True) or {}
    order_id = data.get('order_id', '')
    location = data.get('location', '')
    items    = data.get('items', [])
    kot_comment = data.get('kot_comment')
    token    = data.get('token')

    reprint  = data.get('reprint', False)

    if not reprint and _worker_is_handling_order(order_id):
        print(f'[DEBUG] KOT-at-printer order {order_id} already handled by worker queue, skipping endpoint print', file=sys.stderr)
        return jsonify({'ok': True, 'worker_queued': True})

    ttl = _DEDUP_TTL_REPRINT if reprint else _DEDUP_TTL_NON_REPRINT
    dedup_key = f"kot:{order_id}:{reprint}:billprinter:{location}:{_items_hash(items)}:{str(kot_comment or '')}"
    if _dedup_check(dedup_key, ttl):
        print(f'[DEBUG] KOT-at-printer dedup hit for order {order_id} reprint={reprint}, skipping print', file=sys.stderr)
        return jsonify({'ok': True, 'dedup': True})

    kot_printed_at = _resolve_kot_printed_at(order_id, reprint)
    raw = build_kot_raw(order_id, location, items, reprint=reprint, kot_comment=kot_comment, token=token, kot_printed_at=kot_printed_at)

    try:
        send_raw(BILL_PRINTER_NAME, raw, 'KOT')
        return jsonify({'ok': True})
    except ImportError:
        msg = 'pywin32 not installed — run: pip install pywin32'
        print(msg, file=sys.stderr)
        return jsonify({'error': msg}), 500
    except Exception as e:
        print(f'KOT print error: {e}', file=sys.stderr)
        return jsonify({'error': str(e)}), 500


@bp.route('/void', methods=['POST'])
def print_void():
    KITCHEN_PRINTER_NAME = _get_config_value('KITCHEN_PRINTER_NAME')
    if not KITCHEN_PRINTER_NAME:
        return jsonify({'error': 'KITCHEN_PRINTER_NAME not set in config'}), 400

    data      = request.get_json(force=True, silent=True) or {}
    order_id  = data.get('order_id', '')
    location  = data.get('location', '')
    item_name = data.get('item_name', '')
    qty       = data.get('qty', 1)
    config_choices = data.get('config_choices', {})
    notes     = data.get('notes', '')

    dedup_key = f"void:{order_id}:{location}:{item_name}:{qty}:{_items_hash(config_choices)}:{str(notes or '')}"
    if _dedup_check(dedup_key, _DEDUP_TTL_VOID):
        print(f'[DEBUG] Void KOT dedup hit for order {order_id} item {item_name}, skipping print', file=sys.stderr)
        return jsonify({'ok': True, 'dedup': True})

    esc = b'\x1b'
    gs = b'\x1d'
    now = datetime.now()
    months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    date_str = f"{now.day}-{months[now.month-1]}-{now.year}"
    hour = now.hour % 12 or 12
    ampm = 'PM' if now.hour >= 12 else 'AM'
    time_str = f"{hour}:{now.minute:02d} {ampm}"
    solid = b'=' * 48 + b'\n'
    dash = b'-' * 48 + b'\n'

    out = []
    out.append(esc + b'@')
    out.append(_kot_beep_raw())
    out.append(esc + b'@')
    out.append(solid)
    out.append(esc + b'a\x01')
    out.append(esc + b'E\x01')
    out.append(b'VOID KOT\n')
    out.append(esc + b'E\x00')
    out.append(gs + b'!\x11')
    out.append((f"* {location} *\n").encode('cp437', errors='replace'))
    out.append(gs + b'!\x00')
    out.append(esc + b'a\x00')
    out.append(solid)
    out.append((f"KOT #{order_id} | {date_str} | {time_str}\n").encode('cp437', errors='replace'))
    out.append(dash)
    out.append(esc + b'a\x01')
    out.append(gs + b'!\x11')
    out.append(esc + b'E\x01')
    out.append(b'CANCELLED\n')
    out.append(b'\n')
    out.append(gs + b'!\x00')
    out.append(esc + b'E\x00')
    out.append(esc + b'a\x00')
    out.append(esc + b'M\x01')
    out.append(gs + b'!\x11')
    out.append((f"{qty}x    {item_name}\n").encode('cp437', errors='replace'))
    out.append(gs + b'!\x00')
    out.append(esc + b'M\x00')

    # Print config label and notes in same format as normal KOT
    config_label = ''
    if config_choices:
        if isinstance(config_choices, dict):
            config_label = ' · '.join(str(v) for v in config_choices.values() if v)
    
    if config_label:
        config_line = f"  {config_label}\n"
        out.append(config_line.encode('cp437', errors='replace'))
    if notes:
        notes_line = f"  {notes}\n"
        out.append(notes_line.encode('cp437', errors='replace'))

    out.append(dash)
    out.append(esc + b'a\x01')
    out.append(b'END OF KOT\n')
    out.append(esc + b'a\x00')
    out.append(b'\n\n\n')
    out.append(gs + b'V\x01')
    raw = b''.join(out)

    try:
        send_raw(KITCHEN_PRINTER_NAME, raw, 'VOID KOT')
        return jsonify({'ok': True})
    except ImportError:
        msg = 'pywin32 not installed'
        print(msg, file=sys.stderr)
        return jsonify({'error': msg}), 500
    except Exception as e:
        print(f'Void print error: {e}', file=sys.stderr)
        return jsonify({'error': str(e)}), 500
