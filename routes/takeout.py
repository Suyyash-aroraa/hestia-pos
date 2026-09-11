import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request

from config import CGST_RATE, SGST_RATE
from models import CartItem, Customer, Order, Payment, Session, db, session_billable_subtotal

bp = Blueprint('takeout', __name__, url_prefix='/api/takeout')


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _pickup_slot_number(code):
    s = str(code or '')
    last4 = s[-4:]
    if not last4.isdigit():
        return None
    slot = int(last4)
    return slot if 1 <= slot <= 9999 else None


def _generate_pickup_code(slot_number=None):
    if slot_number is not None:
        try:
            slot_number = int(slot_number)
        except (TypeError, ValueError):
            raise ValueError('Invalid slot number')
        if slot_number < 1 or slot_number > 9999:
            raise ValueError('Invalid slot number')

        # Keep the last four digits bound to the slot number while making the
        # full 16-digit code unique across all sessions.
        candidate_time = datetime.now().astimezone().replace(microsecond=0)
        for _ in range(120):
            candidate = candidate_time.strftime('%y%m%d%H%M%S') + f'{slot_number:04d}'
            exists = Session.query.filter_by(pickup_code=candidate).first()
            if not exists:
                return candidate
            candidate_time += timedelta(seconds=1)
        raise RuntimeError('Could not generate unique pickup code for slot')

    now_local = datetime.now().astimezone()
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(timezone.utc)
    day_end_utc = day_end_local.astimezone(timezone.utc)

    used = set()
    rows = (
        Session.query
        .filter(
            Session.session_type == 'takeout',
            Session.pickup_code.isnot(None),
            Session.created_at >= day_start_utc,
            Session.created_at < day_end_utc,
        )
        .with_entities(Session.pickup_code)
        .all()
    )
    for (code,) in rows:
        s = str(code or '')
        last4 = s[-4:]
        if last4.isdigit():
            used.add(int(last4))

    # Unassigned counter orders use numbers above the fixed parcel slots.
    slot = None
    for n in range(101, 10000):
        if n not in used:
            slot = n
            break
    if slot is None:
        raise RuntimeError('Pickup code limit reached for the day')

    prefix = now_local.strftime('%y%m%d%H%M%S')
    return prefix + f'{slot:04d}'
















@bp.route('/collect', methods=['POST'])
def collect_order():
    """Mark a takeout session as collected (customer picked up). Closes the session."""
    from app import push_pos_event
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess or sess.session_type != 'takeout':
        return jsonify({'error': 'Invalid takeout session'}), 400
    for order in sess.orders:
        if order.status not in ('cancelled', 'collected'):
            order.status = 'collected'
    sess.status = 'closed'
    sess.closed_at = datetime.now(timezone.utc)
    db.session.commit()
    push_pos_event({'type': 'takeout_collected', 'token': token, 'pickup_code': sess.pickup_code})
    return jsonify({'message': 'Order collected', 'pickup_code': sess.pickup_code})


@bp.route('/sessions', methods=['GET'])
def list_takeout_sessions():
    """List all active takeout sessions."""
    from routes.bills import _active_bill, _session_has_unprinted_bill_changes
    sessions = Session.query.filter_by(session_type='takeout', status='active').all()
    pending_session_ids = {
        sid for (sid,) in db.session.query(CartItem.session_id).distinct().all() if sid is not None
    }
    out = []
    for s in sessions:
        # Check if paid
        is_paid = s.payments.filter_by(status='confirmed').first() is not None
        bill = _active_bill(s)
        has_pending_cart = s.id in pending_session_ids
        # What the customer owes right now. A printed bill is authoritative
        # (it carries tip, discounts and any previous due); before that, the
        # running subtotal plus GST, which is what the preview would show.
        if bill is not None and bill.amount is not None:
            payable = round(float(bill.amount), 2)
        else:
            sub = float(session_billable_subtotal(s))
            payable = round(sub * (1 + CGST_RATE + SGST_RATE), 2)
        sess_source = s.source or 'offline'
        latest_order = s.orders.order_by(Order.created_at.desc()).first()
        can_mark_ready = s.orders.filter(
            Order.status.in_(('placed', 'preparing', 'served'))
        ).first() is not None
        out.append({
            'token': s.token,
            'pickup_code': s.pickup_code,
            'slot_number': _pickup_slot_number(s.pickup_code) if sess_source == 'offline' else None,
            'source': sess_source,
            'customer_phone': s.customer_phone,
            'created_at': _iso(s.created_at),
            'is_paid': is_paid,
            'bill_id': bill.id if bill else None,
            'bill_total': round(bill.amount, 2) if bill else None,
            'payable': payable,
            'status': latest_order.status if latest_order else None,
            'can_mark_ready': can_mark_ready,
            'has_pending_cart': has_pending_cart,
            'cash_flag': s.cash_flag,
            'has_unprinted_bill_changes': _session_has_unprinted_bill_changes(
                s,
                bill=bill,
                has_pending_cart=has_pending_cart,
            ),
        })
    return jsonify({'sessions': out})


@bp.route('/session/<token>', methods=['GET'])
def get_takeout_session(token):
    """Get details for a specific takeout session."""
    sess = Session.query.filter_by(token=token).first()
    if not sess or sess.session_type != 'takeout':
        return jsonify({'error': 'Invalid takeout session'}), 400
    
    is_paid = sess.payments.filter_by(status='confirmed').first() is not None
    
    from models import session_billable_subtotal
    running_total = session_billable_subtotal(sess)
    
    orders_out = []
    for order in sess.orders.order_by(Order.created_at.desc()):
        items = []
        for oi in order.items:
            items.append({
                'id': oi.id,
                'name': oi.menu_item.name if oi.menu_item else '',
                'quantity': oi.quantity,
                'quantity_cancelled': oi.quantity_cancelled or 0,
                'price': float(oi.menu_item.price) if oi.menu_item else 0.0,
                'config_price_extra': float(oi.config_price_extra or 0),
                'voided': oi.voided,
                'held': oi.held,
                'item_status': oi.item_status,
                'config_label': ' · '.join(
                    str(v) for v in (json.loads(oi.config_choices).values() if oi.config_choices else []) if v
                ) if oi.config_choices else '',
                'config_choices': json.loads(oi.config_choices) if oi.config_choices else {},
                'notes': oi.notes,
            })
        orders_out.append({
            'id': order.id,
            'status': order.status,
            'created_at': _iso(order.created_at),
            'kot_comment': order.kot_comment,
            'items': items,
            'discount_amount': float(order.discount_amount or 0),
        })

    from routes.bills import _active_bill
    bill = _active_bill(sess)

    # How this bill was settled, for the POS bill preview. The bill is the
    # authoritative record — it is what carries the split breakdown and the
    # 'cash + card' style combined method that settle_bill writes.
    settlement = None
    if bill is not None and bill.settled_at:
        splits = []
        if bill.split_payments:
            try:
                for row in json.loads(bill.split_payments) or []:
                    splits.append({
                        'method': row.get('method'),
                        'amount': float(row.get('amount') or 0),
                        'comment': (row.get('comment') or '').strip(),
                    })
            except (ValueError, TypeError):
                splits = []
        settlement = {
            'method': bill.payment_method,
            'amount': round(float(bill.amount or 0), 2),
            'settled_at': _iso(bill.settled_at),
            'settled_by': bill.settled_by,
            'splits': splits,
        }

    # The customer record carries the name; a counter order may only have
    # captures a phone, so look the rest up from it.
    customer = None
    if sess.customer_id:
        customer = Customer.query.get(sess.customer_id)
    elif sess.customer_phone:
        customer = Customer.query.filter_by(phone=sess.customer_phone).first()

    from routes.session import _get_session_cart_data
    return jsonify({
        'token': sess.token,
        'pickup_code': sess.pickup_code,
        'slot_number': _pickup_slot_number(sess.pickup_code) if (sess.source or 'offline') == 'offline' else None,
        'source': sess.source or 'offline',
        'customer_phone': sess.customer_phone,
        'customer_name': (customer.name or None) if customer else None,
        'created_at': _iso(sess.created_at),
        'is_paid': is_paid,
        'settlement': settlement,
        'bill_comment': sess.bill_comment,
        'bill_id': bill.id if bill else None,
        'is_vip': bool(sess.is_vip),
        'cash_flag': sess.cash_flag,
        'running_total': float(running_total),
        'orders': orders_out,
        'staff_cart_data': _get_session_cart_data(sess),
    })


@bp.route('/hard-delete', methods=['POST'])
def hard_delete_session():
    """Hard delete a session and all associated data. Requires admin password."""
    from config import ADMIN_PASSWORD
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    password = data.get('password')

    if not token or not password:
        return jsonify({'error': 'token and password required'}), 400
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Invalid admin password'}), 401

    sess = Session.query.filter_by(token=token).first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 404

    from models import Bill, BillModification, OrderItem
    for bill in Bill.query.filter_by(session_id=sess.id).all():
        BillModification.query.filter_by(bill_id=bill.id).delete(synchronize_session=False)
        db.session.delete(bill)
    db.session.flush()
    sess.payments.delete(synchronize_session=False)
    db.session.flush()
    for order in sess.orders.all():
        OrderItem.query.filter_by(order_id=order.id).delete(synchronize_session=False)
    sess.orders.delete(synchronize_session=False)
    db.session.flush()
    db.session.delete(sess)
    db.session.commit()

    from app import push_pos_event
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'Session hard deleted'})


@bp.route('/merge', methods=['POST'])
def merge_takeout_sessions():
    """Merge source session into target session. No PIN needed."""
    data = request.get_json(force=True, silent=True) or {}
    src_token = data.get('src_token')
    dest_token = data.get('dest_token')

    if not src_token or not dest_token:
        return jsonify({'error': 'src_token and dest_token required'}), 400
    if src_token == dest_token:
        return jsonify({'error': 'Cannot merge a session with itself'}), 400

    src_sess = Session.query.filter_by(token=src_token, status='active').first()
    dest_sess = Session.query.filter_by(token=dest_token, status='active').first()

    if not src_sess or not dest_sess:
        return jsonify({'error': 'Active session(s) not found'}), 404

    # Move all orders from source to destination
    for order in src_sess.orders:
        order.session_id = dest_sess.id
    
    from models import Bill, BillModification
    for bill in Bill.query.filter_by(session_id=src_sess.id).all():
        BillModification.query.filter_by(bill_id=bill.id).delete(synchronize_session=False)
        db.session.delete(bill)
    db.session.flush()
    src_sess.payments.delete(synchronize_session=False)
    db.session.flush()
    db.session.delete(src_sess)
    db.session.commit()

    from app import push_pos_event
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'Takeout sessions merged', 'dest_token': dest_token})


@bp.route('/offline-session', methods=['POST'])
def create_offline_session():
    """Staff creates an offline takeout session with pickup code."""
    data = request.get_json(force=True, silent=True) or {}
    phone = data.get('phone')
    phone = str(phone).strip() if phone else None
    slot_number = data.get('slot_number')
    if slot_number not in (None, ''):
        try:
            slot_number = int(slot_number)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid slot number'}), 400
        if slot_number < 1 or slot_number > 9999:
            return jsonify({'error': 'Invalid slot number'}), 400
    else:
        slot_number = None

    if slot_number is not None:
        existing = next((
            sess for sess in Session.query.filter_by(session_type='takeout', status='active').all()
            if (sess.source or 'offline') == 'offline' and _pickup_slot_number(sess.pickup_code) == slot_number
        ), None)
        if existing:
            return jsonify({
                'token': existing.token,
                'pickup_code': existing.pickup_code,
                'slot_number': slot_number,
                'source': existing.source or 'offline',
                'phone': existing.customer_phone,
                'message': 'Existing slot session found'
            })

    # Get or create customer if phone provided
    cust = None
    if phone:
        cust = Customer.query.filter_by(phone=phone).first()
        if cust:
            cust.total_visits = (cust.total_visits or 0) + 1
            cust.last_seen = datetime.now(timezone.utc)
        else:
            cust = Customer(phone=phone, total_visits=1, last_seen=datetime.now(timezone.utc))
            db.session.add(cust)
            db.session.flush()

    # Generate pickup code
    try:
        pickup_code = _generate_pickup_code(slot_number=slot_number)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    # Create takeout session
    token = str(uuid4())
    sess = Session(
        token=token,
        session_type='takeout',
        table_number=None,
        pickup_code=pickup_code,
        customer_phone=phone,
        customer_id=cust.id if cust else None,
        status='active',
        source='offline',
        phone_submitted_at=datetime.now(timezone.utc) if phone else None,
    )
    db.session.add(sess)
    db.session.commit()

    return jsonify({
        'token': token,
        'pickup_code': pickup_code,
        'slot_number': _pickup_slot_number(pickup_code) if slot_number is not None else None,
        'source': 'offline',
        'phone': phone,
        'message': 'Offline takeout session created'
    })




@bp.route('/history', methods=['GET'])
def order_history():
    """Get order history for all sessions (dine-in and takeout) with optional filters."""
    # Get filter parameters
    session_type = request.args.get('type')  # 'table', 'takeout', or None for all
    date_from = request.args.get('from')  # ISO date string
    date_to = request.args.get('to')  # ISO date string
    
    # Build query
    query = Session.query.filter(Session.status != 'active')
    
    # Filter by session type
    if session_type in ('table', 'takeout'):
        query = query.filter(Session.session_type == session_type)
    
    # Filter by date range
    if date_from:
        try:
            from_date = datetime.fromisoformat(date_from)
            query = query.filter(Session.created_at >= from_date)
        except ValueError:
            pass
    
    if date_to:
        try:
            # Include the entire day by adding one day
            to_date = datetime.fromisoformat(date_to)
            to_date = to_date.replace(hour=23, minute=59, second=59)
            query = query.filter(Session.created_at <= to_date)
        except ValueError:
            pass
    
    sessions = query.order_by(Session.created_at.desc()).all()
    
    history_out = []
    for sess in sessions:
        # Calculate total
        total = 0.0
        for order in sess.orders:
            for item in order.items:
                if not item.voided:
                    total += (item.menu_item.price or 0) * item.quantity
        
        # Calculate SGST and CGST (2.5% each on base total)
        sgst = round(total * 0.025, 2)
        cgst = round(total * 0.025, 2)
        total_with_tax = round(total + sgst + cgst, 2)
        
        # Get payment method
        payment_method = None
        for payment in sess.payments:
            if payment.status == 'confirmed':
                payment_method = payment.method
                break
        
        # Skip sessions with no orders or no total
        if total == 0 or not sess.orders:
            continue
        
        history_out.append({
            'token': sess.token,  # Full token for fetching details
            'session_id': sess.token[:8],  # Shortened token for display
            'session_type': sess.session_type,
            'table_number': sess.table_number,
            'pickup_code': sess.pickup_code,
            'created_at': _iso(sess.created_at),
            'subtotal': round(total, 2),
            'sgst': sgst,
            'cgst': cgst,
            'total': total_with_tax,
            'payment_method': payment_method,
            'customer_phone': sess.customer_phone,
        })
    
    return jsonify({'history': history_out})


@bp.route('/history/<token>', methods=['GET'])
def order_history_detail(token):
    """Get detailed order history for a specific session including items."""
    sess = Session.query.filter_by(token=token).first_or_404()

    # Build items list
    items_out = []
    for order in sess.orders.all():
        for item in order.items:
            if not item.voided:
                price = item.menu_item.price or 0 if item.menu_item else 0
                items_out.append({
                    'name': item.menu_item.name if item.menu_item else 'Unknown',
                    'price': price,
                    'quantity': item.quantity,
                    'subtotal': price * item.quantity,
                })
    confirmed_payment = Payment.query.filter_by(session_id=sess.id, status='confirmed').first()
    payment_method = confirmed_payment.method if confirmed_payment else None

    # Calculate totals
    subtotal = sum(item['subtotal'] for item in items_out)
    sgst = round(subtotal * 0.025, 2)
    cgst = round(subtotal * 0.025, 2)
    total = round(subtotal + sgst + cgst, 2)

    
    return jsonify({
        'session_id': sess.token[:8],
        'session_type': sess.session_type,
        'table_number': sess.table_number,
        'pickup_code': sess.pickup_code,
        'created_at': _iso(sess.created_at),
        'customer_phone': sess.customer_phone,
        'items': items_out,
        'subtotal': subtotal,
        'sgst': sgst,
        'cgst': cgst,
        'total': total,
        'payment_method': payment_method,
    })
