import json as _json
import re as _re
from datetime import datetime, timezone
from uuid import uuid4

from flask import Blueprint, jsonify, request

from models import Customer, Order, OrderItem, Payment, Session, Staff, Table, db, order_net_amount

bp = Blueprint('session', __name__, url_prefix='/api/session')


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _active_kot_queue_id(order_id):
    """Return the id of an active (pending/printing) KOT queue row for an order,
    or None if none exists (e.g. already printed and cleared)."""
    from models import KOTQueue
    row = (
        KOTQueue.query
        .filter(
            KOTQueue.order_id == order_id,
            KOTQueue.status.in_(('pending', 'printing')),
        )
        .order_by(KOTQueue.created_at.desc())
        .first()
    )
    return row.id if row else None


def _serialize_order_item(oi):
    config_label = ''
    if oi.config_choices:
        try:
            choices = _json.loads(oi.config_choices)
            if isinstance(choices, dict):
                config_label = ' · '.join(str(v) for v in choices.values() if v)
        except Exception:
            pass
    price = (float(oi.menu_item.price) + float(oi.config_price_extra or 0)) if oi.menu_item else 0.0
    return {
        'id': oi.id,
        'name': oi.menu_item.name if oi.menu_item else '',
        'price': price,
        'quantity': oi.quantity,
        'quantity_cancelled': oi.quantity_cancelled or 0,
        'notes': oi.notes,
        'config_choices': _json.loads(oi.config_choices) if oi.config_choices else {},
        'config_label': config_label,
        'voided': oi.voided,
        'held': oi.held,
        'item_status': oi.item_status or 'placed',
    }


def _serialize_order_for_pos(order):
    items = [_serialize_order_item(oi) for oi in sorted(order.items, key=lambda x: x.id)]
    return {
        'id': order.id,
        'status': order.status,
        'created_at': _iso(order.created_at),
        'kot_printed_at': _iso(order.kot_printed_at),
        'kot_comment': order.kot_comment,
        'discount_amount': float(order.discount_amount or 0),
        'discount_note': order.discount_note,
        'order_net': order_net_amount(order),
        'items': items,
    }


def _tables_payload():
    """Table rows for a pushed table_update event.

    Deliberately the same shape /api/tables returns: the POS and captain apps
    assign this straight over their table list, so a trimmed payload silently
    dropped has_pending_cart, is_vip and the amounts until the next full
    reload — the pending-cart dot vanished right after a QR scan.
    """
    from routes.tables import _tables_list
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


def _merge_duplicate_sessions_for_table(table_number):
    """Merge all active sessions for a table into the oldest one."""
    from models import Order, OrderItem, CartItem
    sessions = Session.query.filter_by(table_number=table_number, status='active', session_type='table').order_by(Session.created_at.asc()).all()
    if len(sessions) <= 1:
        return None  # No duplicates
    
    # Keep the oldest session, merge others into it
    primary_sess = sessions[0]
    duplicate_sessions = sessions[1:]
    
    for dup_sess in duplicate_sessions:
        # Move orders from duplicate to primary
        for order in dup_sess.orders:
            order.session_id = primary_sess.id
        
        # Move cart items from duplicate to primary
        for cart_item in dup_sess.cart_items:
            cart_item.session_id = primary_sess.id
        
        # Close duplicate session
        dup_sess.status = 'merged'
        dup_sess.closed_at = datetime.now(timezone.utc)
        dup_sess.table_number = None  # Clear table reference
    
    db.session.commit()
    return primary_sess.token


@bp.route('/new', methods=['POST'])
def new_session():
    data = request.get_json(force=True, silent=True) or {}
    table_number = data.get('table_number')
    session_type = data.get('session_type', 'table')
    
    if session_type == 'takeout':
        # Takeout sessions don't need table_number
        token = str(uuid4())
        sess = Session(token=token, table_number=None, session_type='takeout', status='active', staff_cart=None)
        db.session.add(sess)
        db.session.commit()
        return jsonify({'token': token, 'session_type': 'takeout'})
    
    # Table session
    if table_number is None:
        return jsonify({'error': 'table_number required'}), 400
    table_number = int(table_number)
    # Auto-merge any existing duplicate sessions for this table
    merged_token = _merge_duplicate_sessions_for_table(table_number)
    if merged_token:
        # Return the merged session token
        return jsonify({'token': merged_token, 'table_number': table_number, 'session_type': 'table', 'merged': True})
    existing = Session.query.filter_by(table_number=table_number, status='active', session_type='table').first()
    if existing:
        # Auto-merge: return existing session token instead of creating new one
        return jsonify({'token': existing.token, 'table_number': table_number, 'session_type': 'table', 'merged': True})
    token = str(uuid4())
    t = Table.query.filter_by(number=table_number).first()
    if not t:
        return jsonify({'error': 'Table not found'}), 404
    staff_id = data.get('staff_id')
    if staff_id:
        try:
            staff_id_int = int(staff_id)
        except Exception:
            return jsonify({'error': 'Invalid staff_id'}), 400
        s = db.session.get(Staff, staff_id_int)
        if not s or not s.active:
            return jsonify({'error': 'Staff not found'}), 404
        allowed = _parse_allowed_tables(s.allowed_tables)
        if allowed is not None and table_number not in allowed:
            return jsonify({'error': 'Table not allowed for this staff'}), 403
    sess = Session(
        token=token, table_number=table_number, session_type='table', status='active',
        opened_by_staff_id=int(staff_id) if staff_id else None,
    )
    t.status = 'occupied'
    db.session.add(sess)
    db.session.commit()
    from routes.tables import _invalidate_tables_cache
    _invalidate_tables_cache()
    from app import push_pos_event
    push_pos_event({'type': 'table_update'})
    return jsonify({'token': token, 'table_number': table_number, 'session_type': 'table'})






@bp.route('/by-table/<int:table_number>', methods=['GET'])
def by_table(table_number):
    sess = Session.query.filter_by(table_number=table_number, status='active', session_type='table').first()
    if not sess:
        return jsonify({'error': 'No active session'}), 404
    t = Table.query.filter_by(number=table_number).first()
    table_status = t.status if t else 'empty'
    orders_out = []
    running = 0.0
    for order in sess.orders.order_by(Order.created_at.asc()):
        odict = _serialize_order_for_pos(order)
        orders_out.append(odict)
        running += odict['order_net']
    last_pay = sess.payments.order_by(Payment.id.desc()).first()
    last_payment_amount = float(last_pay.amount) if last_pay else None
    customer_phone = sess.customer.phone if sess.customer else None

    from routes.bills import _active_bill
    bill = _active_bill(sess)
    
    return jsonify({
        'token': sess.token,
        'table_number': sess.table_number,
        'session_type': sess.session_type,
        'table_status': table_status,
        'created_at': _iso(sess.created_at),
        'created_at_ts': int(sess.created_at.replace(tzinfo=timezone.utc).timestamp()),
        'customer_phone': customer_phone,
        'bill_comment': sess.bill_comment,
        'bill_id': bill.id if bill else None,
        'is_vip': sess.is_vip,
        'cash_flag': sess.cash_flag,
        'staff_cart_data': _get_session_cart_data(sess),
        'running_total': running,
        'orders': orders_out,
        'last_payment': last_payment_amount,
    })


def _get_session_cart_data(sess):
    from models import CartItem
    import json
    items = CartItem.query.filter_by(session_id=sess.id).order_by(CartItem.created_at.asc()).all()
    cart_list = []
    for i in items:
        cart_list.append({
            'id': i.id,
            'menu_item_id': i.menu_item_id,
            'name': i.name,
            'price': i.price,
            'qty': i.qty,
            'hold': i.hold,
            'notes': i.notes,
            'config_choices': json.loads(i.config_choices) if i.config_choices else {},
            'configExtra': i.config_extra,
            'configLabel': i.config_label
        })
    # KOT comment is stored in sess.staff_cart (Legacy field repurposed)
    # Ensure it's not a JSON string from old system
    kot_comment = None
    if kot_comment:
        kot_comment = kot_comment.strip()
        # If it looks like a JSON cart (starts with { or [), it's legacy garbage
        if kot_comment.startswith('{') or kot_comment.startswith('['):
            kot_comment = ''
    return {'cart': cart_list, 'kot_comment': kot_comment or ''}


@bp.route('/cart/add', methods=['POST'])
def cart_add():
    from app import push_pos_event
    import json
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    item_data = data.get('item')
    
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess: return jsonify({'error': 'Active session not found'}), 404
    
    from models import CartItem
    # Check if identical item exists to increment qty
    choices_str = json.dumps(item_data.get('config_choices', {}), sort_keys=True)
    existing = CartItem.query.filter_by(
        session_id=sess.id,
        menu_item_id=item_data['menu_item_id'],
        notes=None,
        config_choices=choices_str
    ).first()
    
    if existing:
        existing.qty += item_data.get('qty', 1)
    else:
        new_item = CartItem(
            session_id=sess.id,
            menu_item_id=item_data['menu_item_id'],
            name=item_data['name'],
            price=item_data['price'],
            qty=item_data.get('qty', 1),
            hold=item_data.get('hold', False),
            notes=None,
            config_choices=choices_str,
            config_extra=item_data.get('configExtra', 0),
            config_label=item_data.get('configLabel')
        )
        db.session.add(new_item)
    
    db.session.commit()
    result_id = existing.id if existing else new_item.id
    push_pos_event({
        'type': 'cart_update',
        'session_token': token,
        'staff_cart_data': _get_session_cart_data(sess)
    })
    return jsonify({'success': True, 'id': result_id})


@bp.route('/cart/update-qty', methods=['POST'])
def cart_update_qty():
    from app import push_pos_event
    data = request.get_json(force=True, silent=True) or {}
    cart_item_id = data.get('cart_item_id')
    new_qty = data.get('qty')
    
    try:
        cart_item_id = int(cart_item_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'Item not found'}), 404
    
    from models import CartItem
    item = CartItem.query.get(cart_item_id)
    if not item: return jsonify({'error': 'Item not found'}), 404
    
    if new_qty <= 0:
        db.session.delete(item)
    else:
        item.qty = new_qty
    
    db.session.commit()
    push_pos_event({
        'type': 'cart_update',
        'session_token': item.session.token,
        'staff_cart_data': _get_session_cart_data(item.session)
    })
    return jsonify({'success': True})


@bp.route('/cart/delete', methods=['POST'])
def cart_delete():
    from app import push_pos_event
    data = request.get_json(force=True, silent=True) or {}
    cart_item_id = data.get('cart_item_id')
    
    try:
        cart_item_id = int(cart_item_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'Item not found'}), 404
    
    from models import CartItem
    item = CartItem.query.get(cart_item_id)
    if not item: return jsonify({'error': 'Item not found'}), 404
    
    token = item.session.token
    sess = item.session
    db.session.delete(item)
    db.session.commit()
    push_pos_event({
        'type': 'cart_update',
        'session_token': token,
        'staff_cart_data': _get_session_cart_data(sess)
    })
    return jsonify({'success': True})




@bp.route('/cart/update-item', methods=['POST'])
def cart_update_item():
    from app import push_pos_event
    import json
    data = request.get_json(force=True, silent=True) or {}
    cart_item_id = data.get('cart_item_id')
    updates = data.get('updates', {})
    
    try:
        cart_item_id = int(cart_item_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'Item not found'}), 404
    
    from models import CartItem
    item = CartItem.query.get(cart_item_id)
    if not item: return jsonify({'error': 'Item not found'}), 404
    
    if 'hold' in updates: item.hold = updates['hold']
    item.notes = None
    if 'config_choices' in updates:
        choices = updates.get('config_choices') or {}
        if isinstance(choices, str):
            try:
                choices = json.loads(choices)
            except Exception:
                choices = {}
        if not isinstance(choices, dict):
            choices = {}
        item.config_choices = json.dumps(choices, sort_keys=True)
    if 'configExtra' in updates:
        try:
            item.config_extra = float(updates.get('configExtra') or 0)
        except Exception:
            item.config_extra = 0.0
    if 'configLabel' in updates:
        item.config_label = updates.get('configLabel') or ''

    if 'config_choices' in updates or 'notes' in updates:
        choices_str = item.config_choices or json.dumps({}, sort_keys=True)
        existing = (
            CartItem.query
            .filter_by(
                session_id=item.session_id,
                menu_item_id=item.menu_item_id,
                notes=item.notes,
                config_choices=choices_str,
                hold=item.hold,
            )
            .first()
        )
        if existing and existing.id != item.id:
            existing.qty = int(existing.qty or 0) + int(item.qty or 0)
            existing.config_extra = float(item.config_extra or 0)
            existing.config_label = item.config_label
            token = item.session.token
            sess = item.session
            db.session.delete(item)
            db.session.commit()
            push_pos_event({
                'type': 'cart_update',
                'session_token': token,
                'staff_cart_data': _get_session_cart_data(sess)
            })
            return jsonify({'success': True})
    
    db.session.commit()
    push_pos_event({
        'type': 'cart_update',
        'session_token': item.session.token,
        'staff_cart_data': _get_session_cart_data(item.session)
    })
    return jsonify({'success': True})


@bp.route('/cart/bulk-update', methods=['POST'])
def cart_bulk_update():
    from app import push_pos_event
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    updates = data.get('updates', {}) # e.g. {"hold": true}
    
    if not token: return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess: return jsonify({'error': 'Active session not found'}), 404
    
    from models import CartItem
    # Batch all matching cart rows into a single UPDATE instead of one per row.
    update_values = {}
    if 'hold' in updates: update_values[CartItem.hold] = updates['hold']
    update_values[CartItem.notes] = None
    if update_values:
        CartItem.query.filter_by(session_id=sess.id).update(
            update_values, synchronize_session=False
        )
    db.session.commit()
    push_pos_event({
        'type': 'cart_update',
        'session_token': token,
        'staff_cart_data': _get_session_cart_data(sess)
    })
    return jsonify({'success': True})


@bp.route('/cart/fire', methods=['POST'])
def cart_fire():
    from app import push_pos_event
    from routes.bills import invalidate_bill

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    staff_id = data.get('staff_id')
    kot_comment = None
    skip_kitchen_push = bool(data.get('skip_kitchen_push', False))
    fire_id = data.get('fire_id')
    fire_id = str(fire_id).strip() if fire_id else None

    if not token:
        return jsonify({'error': 'token required'}), 400

    # Idempotency: if this exact fire was already processed, return the same
    # result instead of firing again (handles lost responses / client retries).
    if fire_id:
        prior = Order.query.filter_by(fire_id=fire_id).first()
        if prior:
            from routes.orders import _new_order_event
            qid = _active_kot_queue_id(prior.id)
            return jsonify({
                'order_id': prior.id,
                'status': prior.status,
                'message': 'Order already placed',
                'orders': [_new_order_event(prior)],
                'kot_queue_id': qid,
                'kot_printed': qid is None,
                'idempotent': True,
            })

    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400

    if sess.session_type == 'table':
        if Payment.query.filter(
            Payment.session_id == sess.id,
            Payment.status.in_(('pending', 'confirmed')),
        ).first():
            return jsonify({'error': 'Ordering is locked after payment selection'}), 400
        initial_status = 'placed'
    else:
        initial_status = 'placed'

    if kot_comment:
        kot_comment = str(kot_comment).strip()
        if kot_comment.startswith('{') or kot_comment.startswith('['):
            kot_comment = ''

    from models import CartItem
    from routes.orders import _deduct_stock, _new_order_event, _normalize_config_choices
    from models import MenuItem, OrderItem

    # Lock the rows we are about to fire to avoid double-fire across devices
    fire_items = (
        CartItem.query
        .filter_by(session_id=sess.id, hold=False)
        .with_for_update()
        .all()
    )
    if not fire_items:
        return jsonify({'error': 'No items to fire'}), 400

    order = Order(
        session_id=sess.id,
        status=initial_status,
        created_at=datetime.now(timezone.utc),
        placed_by_staff_id=int(staff_id) if staff_id else None,
        kot_comment=kot_comment,
        fire_id=fire_id,
    )
    db.session.add(order)
    db.session.flush()

    for ci in fire_items:
        mi = db.session.get(MenuItem, int(ci.menu_item_id))
        if not mi or not mi.available:
            return jsonify({'error': 'Invalid or unavailable menu item'}), 400

        raw_choices = {}
        if ci.config_choices:
            try:
                raw_choices = _json.loads(ci.config_choices) or {}
            except Exception:
                raw_choices = {}

        norm_choices = _normalize_config_choices(raw_choices)
        config_json = _json.dumps(norm_choices) if norm_choices else None

        oi = OrderItem(
            order_id=order.id,
            menu_item_id=mi.id,
            quantity=int(ci.qty or 1),
            notes=None,
            held=False,
            config_choices=config_json,
            config_price_extra=float(ci.config_extra or 0),
        )
        db.session.add(oi)

    db.session.flush()

    if order.status == 'placed':
        for oi in order.items:
            _deduct_stock(oi)

    if sess.last_order_at is None:
        sess.last_order_at = datetime.now(timezone.utc)

    # Remove only fired items from cart; held items stay
    for ci in fire_items:
        db.session.delete(ci)

    # Clear KOT comment after firing (comment is per-KOT)
    sess.staff_cart = None

    db.session.commit()
    db.session.refresh(order)

    invalidate_bill(sess)

    ev = _new_order_event(order)
    if skip_kitchen_push:
        ev['skip_auto_print'] = True

    if initial_status != 'processing' and not skip_kitchen_push:
        if ev.get('items'):
            pass

    # Enqueue KOT for head-pos printing (skip if direct printer handles it)
    kot_queue_id = None
    if not skip_kitchen_push:
        try:
            from kot_worker import enqueue_kot
            kot_queue_id = enqueue_kot(order, ev)
        except Exception:
            pass
    else:
        # Direct printer handled it — mark items so reconciliation sweep
        # doesn't re-enqueue them for kitchen printing.
        for oi in order.items:
            if not oi.held and not oi.voided:
                oi.kot_queued = True
        db.session.commit()

    push_pos_event(ev)
    push_pos_event({
        'type': 'cart_update',
        'session_token': token,
        'staff_cart_data': _get_session_cart_data(sess),
    })
    pass

    return jsonify({
        'order_id': order.id,
        'status': order.status,
        'message': 'Order placed',
        'orders': [ev],
        'kot_queue_id': kot_queue_id,
        'kot_printed': skip_kitchen_push,
    })




@bp.route('/by-pickup/<pickup_code>', methods=['GET'])
def by_pickup(pickup_code):
    sess = Session.query.filter_by(pickup_code=pickup_code, status='active', session_type='takeout').first()
    if not sess:
        return jsonify({'error': 'No active takeout session'}), 404
    orders_out = []
    running = 0.0
    for order in sess.orders.order_by(Order.created_at.asc()):
        odict = _serialize_order_for_pos(order)
        orders_out.append(odict)
        running += odict['order_net']
    last_pay = sess.payments.order_by(Payment.id.desc()).first()
    last_payment_amount = float(last_pay.amount) if last_pay else None
    return jsonify({
        'token': sess.token,
        'pickup_code': sess.pickup_code,
        'session_type': sess.session_type,
        'customer_phone': sess.customer_phone,
        'bill_comment': sess.bill_comment,
        'is_vip': sess.is_vip,
        'cash_flag': sess.cash_flag,
        'staff_cart_data': _get_session_cart_data(sess),
        'created_at': _iso(sess.created_at),
        'running_total': running,
        'orders': orders_out,
        'last_payment': last_payment_amount,
    })


@bp.route('/bill-comment', methods=['POST'])
def update_bill_comment():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    comment = data.get('comment', '').strip()
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400
    sess.bill_comment = comment
    db.session.commit()
    return jsonify({'message': 'ok', 'bill_comment': sess.bill_comment})


@bp.route('/toggle-vip', methods=['POST'])
def toggle_vip():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400
    sess.is_vip = not sess.is_vip
    db.session.commit()
    from app import push_pos_event
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'ok', 'is_vip': sess.is_vip})


@bp.route('/toggle-cash', methods=['POST'])
def toggle_cash():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400
    is_paid = sess.payments.filter_by(status='confirmed').first() is not None
    if is_paid:
        return jsonify({'error': 'Cannot toggle cash flag on paid session'}), 400
    sess.cash_flag = not sess.cash_flag
    db.session.commit()
    from app import push_pos_event
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'ok', 'cash_flag': sess.cash_flag})




@bp.route('/dismiss-call', methods=['POST'])
def dismiss_call():
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    table_number = data.get('table_number')
    push_pos_event({
        'type': 'dismiss_call',
        'table_number': table_number,
    })
    return jsonify({'message': 'Call dismissed'})


@bp.route('/shift-table', methods=['POST'])
def shift_table():
    from app import push_pos_event
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    new_table_num = data.get('new_table')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 404
    if sess.session_type != 'table':
        return jsonify({'error': 'Only table sessions can be shifted'}), 400
    new_t = Table.query.filter_by(number=new_table_num).first()
    if not new_t:
        return jsonify({'error': 'Table not found'}), 404
    if new_t.status != 'empty':
        return jsonify({'error': 'Target table is not empty'}), 400
    old_table_num = sess.table_number
    old_t = Table.query.filter_by(number=old_table_num).first()
    if old_t:
        old_t.status = 'empty'
    new_t.status = 'occupied'
    sess.table_number = new_table_num
    sess.shifts_count = (sess.shifts_count or 0) + 1
    # Update active bill's table reference if any
    from routes.bills import _active_bill
    bill = _active_bill(sess)
    if bill:
        bill.table_number = new_table_num
    db.session.commit()
    from routes.tables import _invalidate_tables_cache
    _invalidate_tables_cache()
    pass
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'Table shifted', 'old_table': old_table_num, 'new_table': new_table_num})


@bp.route('/merge-tables', methods=['POST'])
def merge_tables():
    from app import push_pos_event
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    from_table_num = data.get('from_table')
    to_table_num = data.get('to_table')
    if not to_table_num and not token:
        return jsonify({'error': 'token or to_table required'}), 400
    if to_table_num is not None:
        dest_sess = Session.query.filter_by(table_number=to_table_num, status='active', session_type='table').first()
    else:
        dest_sess = Session.query.filter_by(token=token, status='active').first()
    if not dest_sess:
        return jsonify({'error': 'Destination table has no active session'}), 404
    if dest_sess.session_type != 'table':
        return jsonify({'error': 'Only table sessions can be merged'}), 400
    src_sess = Session.query.filter_by(table_number=from_table_num, status='active', session_type='table').first()
    if not src_sess:
        return jsonify({'error': 'Source table has no active session'}), 404
    if src_sess.id == dest_sess.id:
        return jsonify({'error': 'Cannot merge a table with itself'}), 400
    from models import Order as OrderModel, Bill, BillModification, Payment
    for order in src_sess.orders.all():
        order.session_id = dest_sess.id
    db.session.flush()
    for bill in Bill.query.filter_by(session_id=src_sess.id).all():
        BillModification.query.filter_by(bill_id=bill.id).delete(synchronize_session=False)
        db.session.delete(bill)
    db.session.flush()
    Payment.query.filter_by(session_id=src_sess.id).delete(synchronize_session=False)
    db.session.flush()
    db.session.delete(src_sess)
    src_t = Table.query.filter_by(number=from_table_num).first()
    if src_t:
        src_t.status = 'empty'
    db.session.commit()
    db.session.refresh(dest_sess)
    from routes.bills import invalidate_bill
    invalidate_bill(dest_sess)
    from routes.tables import _invalidate_tables_cache
    _invalidate_tables_cache()
    pass
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'Tables merged', 'from_table': from_table_num, 'to_table': dest_sess.table_number})


@bp.route('/split-to-table', methods=['POST'])
def split_to_table():
    from app import push_pos_event
    data = request.get_json(force=True, silent=True) or {}
    source_token = data.get('source_token')
    target_table_num = data.get('target_table')
    # items should be a list of { order_item_id: ID, quantity: QTY }
    items_to_move = data.get('items', [])
    
    if not source_token or not target_table_num or not items_to_move:
        return jsonify({'error': 'source_token, target_table, and items are required'}), 400

    # 1. Get source session
    src_sess = Session.query.filter_by(token=source_token, status='active').first()
    if not src_sess:
        return jsonify({'error': 'Source session not found'}), 404
    if src_sess.session_type != 'table':
        return jsonify({'error': 'Source session is not a dine-in table session'}), 400

    # 2. Get/Create target session
    target_sess = Session.query.filter_by(table_number=target_table_num, status='active', session_type='table').first()
    if not target_sess:
        # Create new session for target table
        t = Table.query.filter_by(number=target_table_num).first()
        if not t:
            return jsonify({'error': 'Target table not found'}), 404
        target_token = str(uuid4())
        target_sess = Session(
            token=target_token, table_number=target_table_num, session_type='table', status='active',
            opened_by_staff_id=src_sess.opened_by_staff_id,
        )
        t.status = 'occupied'
        db.session.add(target_sess)
        db.session.flush() # Get ID
    else:
        # Auto-merge into existing session
        target_token = target_sess.token
    
    # 3. Create a new Order in the target session
    from models import OrderItem, Order as OrderModel
    new_order = OrderModel(
        session_id=target_sess.id, 
        status='placed', 
        kot_comment=f"Split from Table {src_sess.table_number}",
        placed_by_staff_id=src_sess.opened_by_staff_id
    )
    db.session.add(new_order)
    db.session.flush()
    
    moved_count = 0
    for move_info in items_to_move:
        oi_id = move_info.get('order_item_id')
        qty_to_move = int(move_info.get('quantity', 0))
        
        if qty_to_move <= 0:
            continue
            
        oi = OrderItem.query.get(oi_id)
        if not oi or oi.order.session_id != src_sess.id or oi.voided:
            continue
            
        if qty_to_move >= oi.quantity:
            # Move the entire item
            oi.order_id = new_order.id
            moved_count += 1
        else:
            # Split the item: reduce original, create new copy
            new_oi = OrderItem(
                order_id=new_order.id,
                menu_item_id=oi.menu_item_id,
                quantity=qty_to_move,
                notes=oi.notes,
                config_choices=oi.config_choices,
                config_price_extra=oi.config_price_extra,
                item_status=oi.item_status,
                fired_at=oi.fired_at,
                held=oi.held,
                kot_queued=oi.kot_queued,
            )
            oi.quantity -= qty_to_move
            db.session.add(new_oi)
            moved_count += 1
            
    if moved_count == 0:
        db.session.delete(new_order)
        return jsonify({'error': 'No valid items moved'}), 400
        
    # 4. Commit and invalidate
    db.session.commit()
    
    from routes.bills import invalidate_bill
    invalidate_bill(src_sess)
    invalidate_bill(target_sess)
    from routes.tables import _invalidate_tables_cache
    _invalidate_tables_cache()
    
    pass
    push_pos_event({'type': 'table_update'})
    
    return jsonify({'message': f'Split {moved_count} items to Table {target_table_num}', 'target_token': target_sess.token})


@bp.route('/split-to-takeout', methods=['POST'])
def split_to_takeout():
    from app import push_pos_event
    from routes.takeout import _generate_pickup_code, _pickup_slot_number
    data = request.get_json(force=True, silent=True) or {}
    source_token = data.get('source_token')
    target_slot = data.get('target_slot')
    # items should be a list of { order_item_id: ID, quantity: QTY }
    items_to_move = data.get('items', [])

    if not source_token or target_slot in (None, '') or not items_to_move:
        return jsonify({'error': 'source_token, target_slot, and items are required'}), 400

    try:
        target_slot = int(target_slot)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid slot number'}), 400
    if target_slot < 1 or target_slot > 9999:
        return jsonify({'error': 'Invalid slot number'}), 400

    # 1. Get source session
    src_sess = Session.query.filter_by(token=source_token, status='active').first()
    if not src_sess:
        return jsonify({'error': 'Source session not found'}), 404

    # 2. Get/Create target takeout session for the slot
    target_sess = next((
        sess for sess in Session.query.filter_by(session_type='takeout', status='active').all()
        if (sess.source or 'offline') == 'offline' and _pickup_slot_number(sess.pickup_code) == target_slot
    ), None)
    if not target_sess:
        try:
            pickup_code = _generate_pickup_code(slot_number=target_slot)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        target_sess = Session(
            token=str(uuid4()),
            session_type='takeout',
            table_number=None,
            pickup_code=pickup_code,
            status='active',
            source='offline',
            opened_by_staff_id=src_sess.opened_by_staff_id,
        )
        db.session.add(target_sess)
        db.session.flush()

    # 3. Create a new Order in the target session
    from models import OrderItem, Order as OrderModel
    new_order = OrderModel(
        session_id=target_sess.id,
        status='placed',
        kot_comment=f"Split from Takeout #{src_sess.pickup_code or ''}",
        placed_by_staff_id=src_sess.opened_by_staff_id
    )
    db.session.add(new_order)
    db.session.flush()

    moved_count = 0
    for move_info in items_to_move:
        oi_id = move_info.get('order_item_id')
        qty_to_move = int(move_info.get('quantity', 0))

        if qty_to_move <= 0:
            continue

        oi = OrderItem.query.get(oi_id)
        if not oi or oi.order.session_id != src_sess.id or oi.voided:
            continue

        if qty_to_move >= oi.quantity:
            oi.order_id = new_order.id
            moved_count += 1
        else:
            new_oi = OrderItem(
                order_id=new_order.id,
                menu_item_id=oi.menu_item_id,
                quantity=qty_to_move,
                notes=oi.notes,
                config_choices=oi.config_choices,
                config_price_extra=oi.config_price_extra,
                item_status=oi.item_status,
                fired_at=oi.fired_at,
                held=oi.held,
                kot_queued=oi.kot_queued,
            )
            oi.quantity -= qty_to_move
            db.session.add(new_oi)
            moved_count += 1

    if moved_count == 0:
        db.session.delete(new_order)
        return jsonify({'error': 'No valid items moved'}), 400

    db.session.commit()

    from routes.bills import invalidate_bill
    invalidate_bill(src_sess)
    invalidate_bill(target_sess)

    pass
    push_pos_event({'type': 'table_update'})

    return jsonify({
        'message': f'Split {moved_count} items to Takeout slot {target_slot}',
        'target_token': target_sess.token,
        'target_pickup_code': target_sess.pickup_code,
    })


@bp.route('/merge-duplicates', methods=['POST'])
def merge_duplicate_sessions():
    """Manually merge all duplicate active sessions for tables."""
    from app import push_pos_event
    from routes.tables import _invalidate_tables_cache
    
    data = request.get_json(force=True, silent=True) or {}
    table_number = data.get('table_number')
    
    if table_number is not None:
        # Merge specific table
        table_number = int(table_number)
        merged_token = _merge_duplicate_sessions_for_table(table_number)
        if merged_token:
            _invalidate_tables_cache()
            push_pos_event({'type': 'table_update'})
            return jsonify({'merged': True, 'token': merged_token})
        return jsonify({'merged': False, 'message': 'No duplicates found'})
    else:
        # Merge all tables with duplicates
        from sqlalchemy import func
        duplicate_tables = db.session.query(
            Session.table_number
        ).filter(
            Session.status == 'active',
            Session.session_type == 'table',
            Session.table_number.isnot(None)
        ).group_by(
            Session.table_number
        ).having(
            func.count(Session.id) > 1
        ).all()
        
        merged_count = 0
        for (table_num,) in duplicate_tables:
            if _merge_duplicate_sessions_for_table(table_num):
                merged_count += 1
        
        if merged_count > 0:
            _invalidate_tables_cache()
            push_pos_event({'type': 'table_update'})
            return jsonify({'merged': True, 'count': merged_count})
        return jsonify({'merged': False, 'message': 'No duplicates found'})


@bp.route('/close', methods=['POST'])
def close_session():
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 400
    sess.status = 'closed'
    sess.closed_by = 'staff'
    sess.closed_at = datetime.now(timezone.utc)
    # Only update table status for table sessions
    if sess.session_type == 'table':
        t = Table.query.filter_by(number=sess.table_number).first()
        if t:
            t.status = 'empty'
    db.session.commit()
    from routes.tables import _invalidate_tables_cache
    _invalidate_tables_cache()
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'Session closed'})



@bp.route('/force-close', methods=['POST'])
def force_close():
    from app import push_pos_event
    from werkzeug.security import check_password_hash
    from models import Staff

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    pin = str(data.get('pin') or '').strip()
    if not token:
        return jsonify({'error': 'token required'}), 400
    if not pin:
        return jsonify({'error': 'Manager PIN required'}), 401
    if not pin.isdigit() or len(pin) != 6:
        return jsonify({'error': 'PIN must be exactly 6 digits'}), 400

    # Check against active manager staff accounts
    managers = Staff.query.filter_by(role='manager', active=True).all()
    pin_valid = any(check_password_hash(m.pin_hash, pin) for m in managers)
    if not pin_valid:
        return jsonify({'error': 'Invalid manager PIN'}), 401
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 404

    now = datetime.now(timezone.utc)
    voided_item_ids = []
    cancelled_order_ids = []

    for order in sess.orders.all():
        if order.status in ('cancelled', 'closed'):
            continue
        for item in order.items:
            if not item.voided:
                item.voided = True
                item.voided_at = now
                voided_item_ids.append(item.id)
        order.status = 'cancelled'
        cancelled_order_ids.append(order.id)

    # Remove any queued KOTs for cancelled orders so they don't print.
    if cancelled_order_ids:
        from models import KOTQueue
        KOTQueue.query.filter(
            KOTQueue.order_id.in_(cancelled_order_ids),
            KOTQueue.status.in_(('pending', 'printing')),
        ).delete(synchronize_session=False)

    sess.status = 'force_closed'
    sess.closed_by = 'manager'
    sess.closed_at = now
    # Only update table status for table sessions
    if sess.session_type == 'table':
        t = Table.query.filter_by(number=sess.table_number).first()
        if t:
            t.status = 'empty'
    db.session.commit()
    from routes.bills import invalidate_bill
    from routes.tables import _invalidate_tables_cache
    invalidate_bill(sess)
    _invalidate_tables_cache()

    pass
    push_pos_event({'type': 'table_update'})
    return jsonify({'message': 'Session force closed'})
