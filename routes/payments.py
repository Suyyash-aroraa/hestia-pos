import hashlib
import hmac
import json
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

import config
from config import CGST_RATE, SGST_RATE
from models import Order, Payment, Session, Table, db, session_billable_subtotal

bp = Blueprint('payments', __name__, url_prefix='/api/payments')

# Methods that go through staff (no payment link)
OFFLINE_METHODS = ('cash', 'upi_offline', 'card_offline', 'due')
SPLIT_SETTLEMENT_TOLERANCE = 0.50
METHOD_ALIASES = {
    'upi_offline': 'upi',
    'card_offline': 'card',
}
STAFF_METHODS = ('cash', 'upi', 'card', 'due')


def _normalize_method(method):
    method = (method or '').strip().lower()
    return METHOD_ALIASES.get(method, method)


def _session_payment_method(sess, method):
    normalized = _normalize_method(method)
    if normalized in ('upi', 'card') and sess.session_type == 'table':
        return f'{normalized}_offline'
    return normalized


def _clear_pending_payments(sess, keep_payment_id=None):
    query = Payment.query.filter(
        Payment.session_id == sess.id,
        Payment.status == 'pending',
    )
    if keep_payment_id is not None:
        query = query.filter(Payment.id != keep_payment_id)
    query.delete(synchronize_session=False)






def _all_orders_served(sess):
    orders = sess.orders.order_by(Order.created_at.asc()).all()
    if not orders:
        return False
    for order in orders:
        if order.status in ('served', 'cancelled'):
            continue
        if not order.items or all(oi.voided for oi in order.items):
            continue
        return False
    return True


def _calc_totals(sess, tip, include_previous_due=False):
    from routes.bills import _bill_amounts

    tip = round(float(tip or 0), 2)
    amounts = _bill_amounts(sess, include_previous_due=include_previous_due, persist_customer=False)
    subtotal = round(float(amounts.get('subtotal') or 0), 2)
    cgst = round(float(amounts.get('cgst') or 0), 2)
    sgst = round(float(amounts.get('sgst') or 0), 2)
    tax = round(cgst + sgst, 2)
    previous_due = round(float(amounts.get('included_due_payable') or 0), 2)
    total = round(subtotal + tax + tip + previous_due, 2)
    return subtotal, tax, total, cgst, sgst, previous_due


def _tables_payload():
    from models import Table, Session
    rows = Table.query.order_by(Table.number.asc()).all()
    # Get all active table sessions
    active_sessions = Session.query.filter_by(status='active', session_type='table').all()
    active_table_numbers = {s.table_number for s in active_sessions}
    
    return [
        {
            'id': t.id,
            'number': t.number,
            'capacity': t.capacity,
            'status': 'occupied' if t.number in active_table_numbers else t.status
        }
        for t in rows
    ]


def _notify_offline(sess, method, total):
    """Push a staff notification for any offline payment method."""
    from app import push_pos_event
    push_pos_event({
        'type': 'cash_requested',
        'table_number': sess.table_number,
        'method': method,
        'total': total,
    })
    # Also refresh the table grid so the button flips to 'Paying'
    push_pos_event({'type': 'table_update'})


# ── Customer endpoints ──────────────────────────────────────────────────────




@bp.route('/confirm-cash', methods=['POST'])
def confirm_cash():
    """Staff confirms any offline payment (cash / upi_offline / card_offline / due)."""
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token).first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 404

    requested_method = data.get('method')
    include_previous_due = bool(data.get('include_previous_due'))
    lookup_method = _normalize_method(requested_method) if requested_method else ''
    if lookup_method and lookup_method not in STAFF_METHODS:
        return jsonify({'error': 'Invalid payment method'}), 400
    stored_method = _session_payment_method(sess, lookup_method) if lookup_method else None

    if lookup_method == 'due':
        from routes.bills import _parse_bill_comment
        info = _parse_bill_comment(sess.bill_comment)
        if not info.get('phone'):
            return jsonify({'error': 'Phone number is required in Bill Notes for due payments'}), 400

    pay = (
        Payment.query
        .filter(Payment.session_id == sess.id,
                Payment.status == 'pending')
        .order_by(Payment.id.desc())
        .first()
    )
    if not pay:
        return jsonify({'error': 'No pending offline payment'}), 400

    payment_comment = (data.get('comment') or '').strip()
    tip = round(float(pay.tip or 0), 2)
    subtotal, tax, total, cgst, sgst, previous_due = _calc_totals(
        sess,
        tip,
        include_previous_due=include_previous_due,
    )
    if total <= 0:
        return jsonify({'error': 'Bill total must be greater than 0 before settlement'}), 400
    if stored_method:
        pay.method = stored_method
    pay.amount = total
    pay.tax = tax
    pay.tip = tip
    pay.status = 'confirmed'
    pay.confirmed_at = datetime.now(timezone.utc)
    _clear_pending_payments(sess, keep_payment_id=pay.id)
    # Only update table status for table sessions
    if sess.session_type == 'table':
        t = Table.query.filter_by(number=sess.table_number).first()
        if t:
            t.status = 'payment_confirmed'
    db.session.commit()

    from routes.bills import settle_bill
    settle_bill(
        sess,
        pay.method,
        pay.amount,
        payment_comment=payment_comment,
        settled_by=data.get('settled_by'),
        include_previous_due=include_previous_due,
    )


    push_pos_event({
        'type': 'payment_confirmed',
        'table_number': sess.table_number,
        'pickup_code': sess.pickup_code,
        'session_type': sess.session_type,
        'method': pay.method,
        'amount': pay.amount,
    })
    # Refresh the table grid so the button colour updates without a reload
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'Payment confirmed'})




# ── Staff endpoints ─────────────────────────────────────────────────────────

@bp.route('/staff-offline', methods=['POST'])
def staff_offline():
    """Staff initiates an offline payment (cash / upi_offline / card_offline)."""
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    method = _normalize_method(data.get('method'))
    tip = float(data.get('tip') or 0)
    payment_comment = (data.get('comment') or '').strip()
    if not token or method not in STAFF_METHODS:
        return jsonify({'error': 'token and valid method required'}), 400
    sess = Session.query.filter_by(token=token).first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 400

    include_previous_due = bool(data.get('include_previous_due'))
    subtotal, tax, total, cgst, sgst, previous_due = _calc_totals(sess, tip, include_previous_due=include_previous_due)
    if total <= 0:
        return jsonify({'error': 'Bill total must be greater than 0 before settlement'}), 400
    payment_method = _session_payment_method(sess, method)

    _clear_pending_payments(sess)
    pay = Payment(session_id=sess.id, method=payment_method, amount=total, tip=tip, tax=tax, status='pending')
    db.session.add(pay)
    # Only update table status for table sessions
    if sess.session_type == 'table':
        t = Table.query.filter_by(number=sess.table_number).first()
        if t:
            t.status = 'paying'
    db.session.commit()

    _notify_offline(sess, payment_method, total)
    return jsonify({'message': 'Payment requested', 'comment': payment_comment, 'total': total, 'previous_due': previous_due})



@bp.route('/staff-split-confirm', methods=['POST'])
def staff_split_confirm():
    """Staff confirms a split payment (multiple methods/amounts in one go)."""
    from app import push_pos_event
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    split = data.get('split_payments') or []   # [{method, amount, comment}, ...]
    tip = float(data.get('tip') or 0)
    settled_by = data.get('settled_by')
    include_previous_due = bool(data.get('include_previous_due'))
    if not token or not split:
        return jsonify({'error': 'token and split_payments required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 400
    normalized_split = []
    for s in split:
        method = _normalize_method(s.get('method'))
        amt = float(s.get('amount') or 0)
        if amt <= 0:
            continue
        if method not in ('cash', 'upi', 'card'):
            return jsonify({'error': 'Invalid payment method in split_payments'}), 400
        normalized_split.append({
            'method': _session_payment_method(sess, method),
            'amount': amt,
            'comment': (s.get('comment') or '').strip(),
        })
    if not normalized_split:
        return jsonify({'error': 'split_payments required'}), 400
    total = round(sum(float(s['amount']) for s in normalized_split), 2)
    subtotal, tax, _, cgst, sgst, previous_due = _calc_totals(sess, tip, include_previous_due=include_previous_due)
    expected_total = round(float(subtotal + tax + tip + previous_due), 2)
    if expected_total <= 0 or total <= 0:
        return jsonify({'error': 'Bill total must be greater than 0 before settlement'}), 400
    delta = round(expected_total - total, 2)
    if abs(delta) > SPLIT_SETTLEMENT_TOLERANCE:
        return jsonify({'error': f'Split total must match payable amount of ₹{expected_total:.2f}'}), 400

    if abs(delta) > 0:
        # Allow small cashier-side rounding, but keep stored payment/bill math exact.
        normalized_split[-1]['amount'] = round(float(normalized_split[-1]['amount']) + delta, 2)
        if normalized_split[-1]['amount'] <= 0:
            return jsonify({'error': f'Split total must stay within ₹{SPLIT_SETTLEMENT_TOLERANCE:.2f} of payable amount'}), 400
        total = expected_total
    now = datetime.now(timezone.utc)
    _clear_pending_payments(sess)
    
    # Generate descriptive payment method: "cash + card"
    methods = []
    for s in normalized_split:
        m = s.get('method', '').replace('_offline', '')
        if m not in methods:
            methods.append(m)
    desc_method = ' + '.join(methods) if methods else 'split'

    pay = Payment(
        session_id=sess.id, method=desc_method, amount=total,
        tip=tip, tax=tax, status='confirmed', confirmed_at=now,
    )
    db.session.add(pay)
    if sess.session_type == 'table':
        t = Table.query.filter_by(number=sess.table_number).first()
        if t:
            t.status = 'payment_confirmed'
    db.session.commit()
    from routes.bills import settle_bill
    settle_bill(
        sess,
        desc_method,
        total,
        split_payments=normalized_split,
        settled_by=settled_by,
        include_previous_due=include_previous_due,
    )
    push_pos_event({
        'type': 'payment_confirmed',
        'table_number': sess.table_number,
        'session_type': sess.session_type,
        'method': desc_method,
        'amount': total,
    })
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'Split payment confirmed', 'total': total})




