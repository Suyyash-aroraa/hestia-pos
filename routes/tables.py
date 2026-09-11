import time as _time
import re as _re
from flask import Blueprint, jsonify, request, session

from models import CartItem, Session as Sess, Staff, Table, db

_TABLES_CACHE: list | None = None
_TABLES_CACHE_TS: float = 0.0
_TABLES_CACHE_TTL = 0  # pending-cart state changes too frequently to reuse stale sidebar data


def _invalidate_tables_cache():
    global _TABLES_CACHE, _TABLES_CACHE_TS
    _TABLES_CACHE = None
    _TABLES_CACHE_TS = 0.0


bp = Blueprint('tables', __name__, url_prefix='/api/tables')


def _admin_ok():
    """Check admin auth using new token-based system."""
    # Check Authorization header first
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        from models import AdminSession
        sess = AdminSession.query.filter_by(token=token).first()
        if sess and sess.is_valid():
            return True
    # Fall back to session cookie
    return bool(session.get('admin_token'))


def _tables_list():
    from routes.bills import _active_bill, _session_has_unprinted_bill_changes
    rows = Table.query.order_by(Table.number.asc()).all()
    active_sessions = {
        s.table_number: s
        for s in Sess.query.filter_by(status='active', session_type='table').all()
        if s.table_number is not None
    }
    pending_session_ids = {
        sid for (sid,) in db.session.query(CartItem.session_id).distinct().all() if sid is not None
    }
    result = []
    for t in rows:
        active_sess = active_sessions.get(t.number)
        # Derive status from active sessions so we never show 'empty' when a session exists
        # (the Table.status column can get out of sync due to races / partial failures)
        if active_sess:
            derived_status = t.status if t.status in ('paying', 'pay_requested', 'payment_confirmed') else 'occupied'
        else:
            derived_status = t.status
        active_bill = _active_bill(active_sess) if active_sess else None
        has_pending_cart = bool(active_sess and active_sess.id in pending_session_ids)
        result.append({
            'id': t.id,
            'number': t.number,
            'capacity': t.capacity,
            'status': derived_status,
            'is_vip': bool(active_sess.is_vip) if active_sess else False,
            'has_pending_cart': has_pending_cart,
            'bill_id': active_bill.id if active_bill else None,
            'bill_total': round(active_bill.amount, 2) if active_bill else None,
            'amount': round(active_bill.amount, 2) if active_bill else None,
            'has_unprinted_bill_changes': bool(
                active_sess and _session_has_unprinted_bill_changes(
                    active_sess,
                    bill=active_bill,
                    has_pending_cart=has_pending_cart,
                )
            ),
        })
    return result


def _tables_list_fresh():
    """Bypass cache — used when we know something just changed."""
    _invalidate_tables_cache()
    return _tables_list()


def _parse_allowed_tables(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    parts = [p for p in _re.split(r'[^0-9]+', s) if p]
    out = []
    seen = set()
    for p in parts:
        try:
            n = int(p)
        except Exception:
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
    return set(out)


@bp.route('', methods=['GET'])
def list_tables():
    staff_id = request.args.get('staff_id')
    if not staff_id:
        return jsonify(_tables_list())

    try:
        staff_id = int(staff_id)
    except Exception:
        return jsonify({'error': 'Invalid staff_id'}), 400

    s = db.session.get(Staff, staff_id)
    if not s or not s.active:
        return jsonify({'error': 'Staff not found'}), 404
    allowed = _parse_allowed_tables(s.allowed_tables)
    if allowed is None:
        return jsonify(_tables_list())

    tables = [t for t in _tables_list() if int(t.get('number') or 0) in allowed]
    return jsonify(tables)


@bp.route('', methods=['POST'])
def create_table():
    if not _admin_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True, silent=True) or {}
    number = data.get('number')
    capacity = data.get('capacity')
    if number is None or capacity is None:
        return jsonify({'error': 'number and capacity required'}), 400
    if Table.query.filter_by(number=int(number)).first():
        return jsonify({'error': 'Table number exists'}), 400
    t = Table(number=int(number), capacity=int(capacity), status='empty')
    db.session.add(t)
    db.session.commit()
    _invalidate_tables_cache()
    return jsonify({'id': t.id, 'number': t.number, 'capacity': t.capacity, 'status': t.status})


@bp.route('/<int:table_id>', methods=['PUT'])
def update_table(table_id):
    if not _admin_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    t = Table.query.get_or_404(table_id)
    data = request.get_json(force=True, silent=True) or {}
    if 'capacity' in data:
        t.capacity = int(data['capacity'])
    db.session.commit()
    _invalidate_tables_cache()
    return jsonify({'id': t.id, 'number': t.number, 'capacity': t.capacity, 'status': t.status})


@bp.route('/<int:table_id>', methods=['DELETE'])
def delete_table(table_id):
    if not _admin_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    t = Table.query.get_or_404(table_id)
    db.session.delete(t)
    db.session.commit()
    _invalidate_tables_cache()
    return jsonify({'success': True})


@bp.route('/<int:table_id>/hard-delete', methods=['POST'])
def hard_delete_table(table_id):
    from config import ADMIN_PASSWORD
    from models import Session as Sess, Order, OrderItem, Payment, Bill, BillModification, BillSplit, KOTQueue
    data = request.get_json(force=True, silent=True) or {}
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({'error': 'Invalid admin password'}), 401
    t = Table.query.get_or_404(table_id)
    sessions = Sess.query.filter_by(table_number=t.number, status='active').all()
    for sess in sessions:
        # Delete Bill related data
        for bill in Bill.query.filter_by(session_id=sess.id).all():
            BillModification.query.filter_by(bill_id=bill.id).delete(synchronize_session=False)
            BillSplit.query.filter_by(bill_id=bill.id).delete(synchronize_session=False)
            db.session.delete(bill)
        db.session.flush()
        
        # Delete Session related data
        Payment.query.filter_by(session_id=sess.id).delete(synchronize_session=False)
        
        for order in Order.query.filter_by(session_id=sess.id).all():
            KOTQueue.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            OrderItem.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            db.session.delete(order)
        db.session.flush()
        
        db.session.delete(sess)
    
    db.session.flush()
    t.status = 'empty'
    db.session.commit()
    _invalidate_tables_cache()
    return jsonify({'success': True})
