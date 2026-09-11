import json as _json
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.orm import joinedload

from models import MenuItem, Order, OrderItem, Payment, Session, db, Inventory, RecipeIngredient
from models import order_lines_subtotal, order_net_amount

bp = Blueprint('orders', __name__, url_prefix='/api/orders')


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _config_extra(oi):
    """Total price addition from config choices stored on an OrderItem."""
    return float(oi.config_price_extra or 0)


def _extract_config_extra_from_choices(choices):
    """Sum the 'extra' values from a structured config_choices dict sent by the frontend."""
    if not choices:
        return 0.0
    total = 0.0
    for v in choices.values():
        if isinstance(v, dict):
            total += float(v.get('extra') or 0)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    total += float(item.get('extra') or 0)
    return total


def _normalize_config_choices(choices):
    """Reduce {group: {label,extra}} → {group: label} for compact storage."""
    if not choices:
        return {}
    result = {}
    for k, v in choices.items():
        if isinstance(v, dict):
            result[k] = v.get('label', v)
        elif isinstance(v, list):
            result[k] = [item.get('label', item) if isinstance(item, dict) else item for item in v]
        else:
            result[k] = v
    return result


def _config_label(oi):
    """Return a short readable string of config choices for display, e.g. 'Strong · Large'."""
    if not oi.config_choices:
        return ''
    try:
        choices = _json.loads(oi.config_choices)
        if isinstance(choices, dict):
            return ' · '.join(str(v) for v in choices.values() if v)
    except Exception:
        pass
    return ''


def _kitchen_items_payload(order):
    """Only include non-held, non-voided items for kitchen display."""
    out = []
    for oi in sorted(order.items, key=lambda x: x.id):
        if oi.voided:
            continue
        if oi.held:
            continue
        label = _config_label(oi)
        out.append({
            'id': oi.id,
            'name': oi.menu_item.name if oi.menu_item else '',
            'quantity': oi.quantity,
            'notes': (label + (' — ' + oi.notes if oi.notes else '')) if label else (oi.notes or ''),
            'status': oi.item_status or 'placed',
        })
    return out


def _all_items_payload(order):
    """Full per-item payload for POS + customer, including held/voided state."""
    out = []
    for oi in sorted(order.items, key=lambda x: x.id):
        p = float(oi.menu_item.price) + _config_extra(oi) if oi.menu_item else 0.0
        out.append({
            'id': oi.id,
            'name': oi.menu_item.name if oi.menu_item else '',
            'quantity': oi.quantity,
            'quantity_cancelled': oi.quantity_cancelled or 0,
            'notes': oi.notes,
            'config_label': _config_label(oi),
            'config_choices': _json.loads(oi.config_choices) if oi.config_choices else {},
            'price': p,
            'voided': oi.voided,
            'held': oi.held,
            'item_status': oi.item_status or 'placed',
        })
    return out


def _new_order_event(order):
    return {
        'type': 'new_order',
        'order_id': order.id,
        'table_number': order.session.table_number,
        'session_type': order.session.session_type,
        'pickup_code': order.session.pickup_code,
        'session_token': order.session.token,
        'status': order.status,
        'created_at': _iso(order.created_at),
        'kot_printed_at': _iso(order.kot_printed_at),
        'items': _kitchen_items_payload(order),
        'all_items': _all_items_payload(order),
        'kot_comment': order.kot_comment,
    }


def _order_update_event(order, fired_item_id=None):
    ev = {
        'type': 'order_update',
        'order_id': order.id,
        'status': order.status,
        'table_number': order.session.table_number,
        'session_type': order.session.session_type,
        'pickup_code': order.session.pickup_code,
        'session_token': order.session.token,
        'items': _kitchen_items_payload(order),
        'all_items': _all_items_payload(order),
        'discount_amount': float(order.discount_amount or 0),
        'kot_comment': order.kot_comment,
        'kot_printed_at': _iso(order.kot_printed_at),
    }
    if fired_item_id is not None:
        ev['fired_item_id'] = fired_item_id
    return ev


def _session_order_item_payload(oi):
    """Full item payload for POS session view, including held state and item_status."""
    p = float(oi.menu_item.price) + _config_extra(oi) if oi.menu_item else 0.0
    return {
        'id': oi.id,
        'name': oi.menu_item.name if oi.menu_item else '',
        'quantity': oi.quantity,
        'quantity_cancelled': oi.quantity_cancelled or 0,
        'notes': oi.notes,
        'config_choices': _json.loads(oi.config_choices) if oi.config_choices else {},
        'config_label': _config_label(oi),
        'price': p,
        'voided': oi.voided,
        'held': oi.held,
        'item_status': oi.item_status or 'placed',
    }


def _derive_order_status(order):
    """
    Derive order-level status from active (non-voided, non-held) item statuses.
    Held items don't count toward order status until fired.
    """
    active = [i for i in order.items if not i.voided and not i.held]
    if not active:
        # All items are held or voided — keep existing status
        return order.status
    statuses = {i.item_status or 'placed' for i in active}
    if statuses <= {'served'}:
        return 'served'
    if statuses <= {'ready', 'served'}:
        return 'ready'
    if statuses <= {'preparing', 'ready', 'served'}:
        return 'preparing'
    return 'placed'


def _deduct_stock(order_item):
    """Deduct inventory stock based on the menu item recipe and selected configs."""
    if order_item.stock_deducted or order_item.voided or order_item.held:
        return
    
    # Ensure mi is loaded
    mi = order_item.menu_item
    if not mi and order_item.menu_item_id:
        mi = db.session.get(MenuItem, order_item.menu_item_id)
        
    if not mi:
        return
    
    choices = _json.loads(order_item.config_choices) if order_item.config_choices else {}
    
    # 1. Deduct from direct inventory link if it exists
    if mi.inventory_id:
        inv = db.session.get(Inventory, mi.inventory_id)
        if inv:
            inv.stock_level = max(0.0, inv.stock_level - order_item.quantity)
    
    # 2. Deduct from recipe ingredients (supporting varying configs)
    recipe = RecipeIngredient.query.filter_by(menu_item_id=mi.id).all()
    
    for ri in recipe:
        # Check if this ingredient is conditional on a config
        if ri.config_group:
            selected_option = choices.get(ri.config_group)
            if ri.config_option:
                if selected_option != ri.config_option:
                    continue
            else:
                continue
        
        # Deduct stock
        if ri.ingredient:
            ri.ingredient.stock_level = max(0.0, ri.ingredient.stock_level - (ri.quantity * order_item.quantity))
            
    order_item.stock_deducted = True
    # NOTE: commit is handled by the caller to maintain transaction integrity


@bp.route('', methods=['POST'])
def place_order():
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    items = data.get('items') or []
    staff_id = data.get('staff_id')
    kot_comment = None
    skip_kitchen_push = data.get('skip_kitchen_push', False)
    if not token or not items:
        return jsonify({'error': 'token and items required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400
    
    if sess.session_type == 'table':
        # Table: block ordering once payment is in progress
        if Payment.query.filter(
            Payment.session_id == sess.id,
            Payment.status.in_(('pending', 'confirmed')),
        ).first():
            return jsonify({'error': 'Ordering is locked after payment selection'}), 400
        initial_status = 'placed'
    else:
        # Offline takeout (staff-created)
        initial_status = 'placed'

    resolved = []
    for line in items:
        mid = line.get('menu_item_id')
        qty = line.get('quantity')
        if mid is None or qty is None:
            return jsonify({'error': 'Invalid item'}), 400
        mi = db.session.get(MenuItem, int(mid))
        if not mi or not mi.available:
            return jsonify({'error': 'Invalid or unavailable menu item'}), 400
        raw_choices = line.get('config_choices') or {}
        price_extra = _extract_config_extra_from_choices(raw_choices)
        norm_choices = _normalize_config_choices(raw_choices)
        config_json = _json.dumps(norm_choices) if norm_choices else None
        resolved.append((mi, int(qty), None, bool(line.get('held', False)), config_json, price_extra))

    order = Order(
        session_id=sess.id, status=initial_status, created_at=datetime.now(timezone.utc),
        placed_by_staff_id=int(staff_id) if staff_id else None,
        kot_comment=kot_comment,
    )
    db.session.add(order)
    db.session.flush()

    for mi, qty, notes, held, config_json, price_extra in resolved:
        oi = OrderItem(
            order_id=order.id,
            menu_item_id=mi.id,
            quantity=qty,
            notes=notes,
            held=held,
            config_choices=config_json,
            config_price_extra=price_extra,
        )
        db.session.add(oi)
    
    # Flush to ensure order items have IDs and relationships are established
    db.session.flush()

    # Deduct stock for placed items
    if order.status == 'placed':
        for oi in order.items:
            _deduct_stock(oi)

    if sess.last_order_at is None:
        sess.last_order_at = datetime.now(timezone.utc)
    db.session.commit()
    db.session.refresh(order)

    from routes.bills import invalidate_bill
    invalidate_bill(sess)

    ev = _new_order_event(order)
    if skip_kitchen_push:
        ev['skip_auto_print'] = True

    if initial_status != 'processing' and not skip_kitchen_push:
        if ev['items']:
            pass

    # Enqueue KOT for head-pos printing only if order is placed (not processing)
    kot_queue_id = None
    if initial_status != 'processing':
        try:
            from kot_worker import enqueue_kot
            kot_queue_id = enqueue_kot(order, ev)
        except Exception:
            pass

    push_pos_event(ev)
    from app import push_pos_event as app_push_pos_event
    app_push_pos_event({'type': 'table_update'})
    pass

    # Return full order info for 'Print here' support
    return jsonify({
        'order_id': order.id,
        'status': order.status,
        'message': 'Order placed',
        'orders': [_new_order_event(order)],
        'kot_queue_id': kot_queue_id,
    })


@bp.route('/fire-item', methods=['POST'])
def fire_item():
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    order_item_id = data.get('order_item_id')
    if not token or order_item_id is None:
        return jsonify({'error': 'token and order_item_id required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400
    oi = db.get_or_404(OrderItem, int(order_item_id))
    if oi.order.session_id != sess.id:
        return jsonify({'error': 'Item not in session'}), 400
    if oi.voided:
        return jsonify({'error': 'Item is voided'}), 400
    if not oi.held:
        return jsonify({'error': 'Item is not held'}), 400

    oi.held = False
    oi.fired_at = datetime.now(timezone.utc)
    _deduct_stock(oi)
    db.session.commit()
    db.session.refresh(oi)

    order = oi.order
    ev = _order_update_event(order, fired_item_id=oi.id)

    # Enqueue KOT for the fired item
    kot_queue_id = None
    try:
        from kot_worker import enqueue_kot
        kot_queue_id = enqueue_kot(order, ev)
    except Exception:
        pass

    pass
    push_pos_event(ev)
    pass
    return jsonify({'message': 'Item fired', 'order_item_id': oi.id, 'kot_queue_id': kot_queue_id})


@bp.route('/fire-items', methods=['POST'])
def fire_items_bulk():
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    order_item_ids = data.get('order_item_ids') or []
    skip_kitchen_push = bool(data.get('skip_kitchen_push', False))
    if not token or not order_item_ids:
        return jsonify({'error': 'token and order_item_ids required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400

    now = datetime.now(timezone.utc)
    fired_payload = []
    changed_orders = {}

    for oi_id in order_item_ids:
        oi = db.session.get(OrderItem, int(oi_id))
        if not oi or oi.order.session_id != sess.id:
            continue
        if oi.voided or not oi.held:
            continue
        oi.held = False
        oi.fired_at = now
        _deduct_stock(oi)
        changed_orders[oi.order.id] = oi.order
        label = _config_label(oi)
        fired_payload.append({
            'id': oi.id,
            'name': oi.menu_item.name if oi.menu_item else '',
            'quantity': oi.quantity,
            'notes': (label + (' — ' + oi.notes if oi.notes else '')) if label else (oi.notes or ''),
            'order_id': oi.order.id,
        })

    if not fired_payload:
        return jsonify({'error': 'No valid held items to fire'}), 400

    db.session.commit()

    # Per-order update events for kitchen screen and POS order list
    for order in changed_orders.values():
        db.session.refresh(order)
        ev = _order_update_event(order)
        pass
        push_pos_event(ev)
        pass

    # One consolidated event so head-pos.js prints a single KOT (only if not skipped)
    kot_queue_id = None
    if not skip_kitchen_push:
        loc = ('Takeout #' + (sess.pickup_code or '')) if sess.session_type == 'takeout' else ('Table ' + str(sess.table_number or ''))
        # Grab kot_comment from the first changed order; usually all fired items belong to one order
        first_order = next(iter(changed_orders.values()), None)
        kot_comment = first_order.kot_comment if first_order else None
        order_type = 'Takeout' if sess.session_type == 'takeout' else 'Dine-In'
        pos_ev = {
            'type': 'fired_items',
            'location': loc,
            'session_type': sess.session_type,
            'table_number': sess.table_number,
            'pickup_code': sess.pickup_code,
            'session_token': sess.token,
            'fired_items': fired_payload,
            'kot_comment': kot_comment or '',
            'orderType': order_type,
        }
        push_pos_event(pos_ev)

        # Enqueue consolidated KOT
        try:
            from kot_worker import enqueue_kot
            # Use the first order for enqueue
            if first_order:
                kot_queue_id = enqueue_kot(first_order, pos_ev)
        except Exception:
            pass

    return jsonify({'message': 'Items fired', 'fired_count': len(fired_payload), 'kot_queue_id': kot_queue_id})


@bp.route('/session', methods=['GET'])
def session_orders():
    token = request.args.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token).first()
    if not sess:
        return jsonify({'error': 'Not found'}), 404
    existing_payment = (
        Payment.query.filter(
            Payment.session_id == sess.id,
            Payment.status.in_(('pending', 'confirmed')),
        )
        .order_by(Payment.id.desc())
        .first()
    )
    payment_locked = existing_payment is not None
    orders_out = []
    billable = 0.0
    orders_list = (
        Order.query
        .filter_by(session_id=sess.id)
        .order_by(Order.created_at.asc())
        .options(joinedload(Order.items).joinedload(OrderItem.menu_item))
        .all()
    )
    for order in orders_list:
        items = []
        line_sum = 0.0
        for oi in sorted(order.items, key=lambda x: x.id):
            p = float(oi.menu_item.price) + _config_extra(oi) if oi.menu_item else 0.0
            if not oi.voided:
                line_sum += p * oi.quantity
            items.append(_session_order_item_payload(oi))
        disc = float(order.discount_amount or 0)
        if disc > line_sum:
            disc = line_sum
        net = max(0.0, line_sum - disc)
        billable += net
        orders_out.append({
            'id': order.id,
            'status': order.status,
            'created_at': _iso(order.created_at),
            'kot_printed_at': _iso(order.kot_printed_at),
            'kot_comment': order.kot_comment,
            'items': items,
            'discount_amount': disc,
            'order_net': net,
        })
    return jsonify({
        'orders': orders_out,
        'billable_subtotal': billable,
        'payment_locked': payment_locked,
        'bill_comment': sess.bill_comment,
    })


@bp.route('/status', methods=['POST'])
def order_status():
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get('order_id')
    status = data.get('status')
    if order_id is None or status is None:
        return jsonify({'error': 'order_id and status required'}), 400
    # For takeout orders, only allow 'ready' status (no 'served')
    order = db.get_or_404(Order, int(order_id))
    if order.session.session_type == 'takeout' and status == 'served':
        return jsonify({'error': 'Takeout orders cannot be marked as served, only ready'}), 400
    if status not in ('preparing', 'ready', 'served'):
        return jsonify({'error': 'Invalid status'}), 400
    order.status = status
    db.session.commit()
    db.session.refresh(order)
    token = order.session.token
    ev = _order_update_event(order)
    pass
    push_pos_event(ev)
    pass
    return jsonify({'message': 'ok', 'order_id': order.id, 'status': order.status})


@bp.route('/apply-discount', methods=['POST'])
def apply_discount():
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    discount_amount = data.get('discount_amount')
    pin = data.get('pin')
    order_id = data.get('order_id')
    if not token or discount_amount is None or pin is None:
        return jsonify({'error': 'token, discount_amount, pin required'}), 400
    from werkzeug.security import check_password_hash
    from models import Staff as _Staff
    managers = _Staff.query.filter_by(role='manager', active=True).all()
    if not any(check_password_hash(s.pin_hash, str(pin)) for s in managers):
        return jsonify({'error': 'Invalid PIN'}), 401
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400

    session_orders = (
        sess.orders
        .order_by(Order.created_at.asc(), Order.id.asc())
        .all()
    )
    if not session_orders:
        return jsonify({'error': 'No billable orders in session'}), 400

    anchor_order = None
    if order_id is not None:
        try:
            requested_order_id = int(order_id)
        except Exception:
            return jsonify({'error': 'Invalid order_id'}), 400
        anchor_order = next((o for o in session_orders if o.id == requested_order_id), None)
        if anchor_order is None:
            return jsonify({'error': 'Order not in session'}), 400
    else:
        anchor_order = session_orders[0]

    lines = sum(order_lines_subtotal(order) for order in session_orders)
    disc = max(0.0, float(discount_amount))
    if disc > lines:
        disc = lines

    changed_orders = []
    if disc <= 0:
        # Removing discount — clear all orders
        for order in session_orders:
            old_discount = float(order.discount_amount or 0)
            if old_discount != 0.0 or order.discount_note:
                order.discount_amount = 0.0
                order.discount_note = None
                changed_orders.append(order)
    else:
        # Distribute discount proportionally across all orders
        for order in session_orders:
            old_discount = float(order.discount_amount or 0)
            old_note = order.discount_note

            order_sub = order_lines_subtotal(order)
            order_disc = disc * (order_sub / lines) if lines > 0 else 0.0
            # Safety cap (shouldn't trigger with proportional distribution, but protects against fp edge cases)
            if order_disc > order_sub:
                order_disc = order_sub

            new_note = data.get('discount_note') if (order.id == anchor_order.id and data.get('discount_note') is not None) else None

            if old_discount != order_disc or old_note != new_note:
                order.discount_amount = order_disc
                order.discount_note = new_note
                changed_orders.append(order)

    db.session.commit()
    for order in changed_orders:
        db.session.refresh(order)
        ev = _order_update_event(order)
        pass
        push_pos_event(ev)
        pass
    return jsonify({
        'message': 'Discount removed' if disc <= 0 else 'Discount applied',
        'order_id': anchor_order.id if disc > 0 else None,
        'discount_amount': float(disc),
        'order_net': order_net_amount(anchor_order) if disc > 0 else 0.0,
        'session_total_discount': float(disc),
    })


@bp.route('/reduce-item', methods=['POST'])
def reduce_item():
    from app import push_pos_event
    from routes.bills import invalidate_bill
    from werkzeug.security import check_password_hash
    from models import Staff
    from config import ADMIN_PASSWORD

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    order_item_id = data.get('order_item_id')
    pin = str(data.get('pin') or '').strip()
    password = str(data.get('password') or '').strip()
    staff_id = data.get('staff_id')
    new_qty = data.get('new_quantity')
    if not token or order_item_id is None or new_qty is None:
        return jsonify({'error': 'token, order_item_id and new_quantity required'}), 400

    new_qty = int(new_qty)
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400

    from routes.bills import _active_bill
    bill = _active_bill(sess)
    bill_printed = bill is not None and bill.printed_at is not None

    def _resolve_staff_id():
        if staff_id is None:
            return None
        try:
            staff_id_int = int(staff_id)
        except Exception:
            return jsonify({'error': 'Invalid staff_id'}), 400
        s = db.session.get(Staff, staff_id_int)
        if not s or not s.active:
            return jsonify({'error': 'Staff not found'}), 404
        if sess.session_type == 'table':
            import re as _re
            raw = getattr(s, 'allowed_tables', None)
            allowed = None
            if raw is not None:
                st = str(raw).strip()
                if st:
                    parts = [p for p in _re.split(r'[^0-9]+', st) if p]
                    allowed = set()
                    for p in parts:
                        try:
                            allowed.add(int(p))
                        except Exception:
                            pass
                    if not allowed:
                        allowed = None
            if allowed is not None and int(sess.table_number or 0) not in allowed:
                return jsonify({'error': 'Table not allowed for this staff'}), 403
        return s.id

    actor_staff_id = None
    if bill_printed:
        if password != ADMIN_PASSWORD:
            return jsonify({'error': 'Invalid admin password', 'needs_admin': True}), 401
        resolved = _resolve_staff_id()
        if isinstance(resolved, tuple):
            return resolved
        actor_staff_id = resolved
    elif staff_id is not None:
        resolved = _resolve_staff_id()
        if isinstance(resolved, tuple):
            return resolved
        actor_staff_id = resolved
    elif pin:
        if not pin.isdigit() or len(pin) != 6:
            return jsonify({'error': 'PIN must be exactly 6 digits'}), 400
        all_staff = Staff.query.filter_by(active=True).all()
        pin_valid = any(check_password_hash(s.pin_hash, pin) for s in all_staff)
        if not pin_valid:
            return jsonify({'error': 'Invalid PIN'}), 401
        actor_staff_id = next((s.id for s in all_staff if check_password_hash(s.pin_hash, pin)), None)
    elif bill_printed:
        return jsonify({'error': 'Admin password required after bill is printed', 'needs_admin': True}), 401

    oi = db.get_or_404(OrderItem, int(order_item_id))
    order = oi.order
    if order.session_id != sess.id:
        return jsonify({'error': 'Item not in session'}), 400
    if oi.voided:
        return jsonify({'error': 'Item already voided'}), 400
    if new_qty >= oi.quantity:
        return jsonify({'error': 'New quantity must be less than current'}), 400

    cancelled_qty = oi.quantity - new_qty
    item_name = oi.menu_item.name if oi.menu_item else ''
    now = datetime.now(timezone.utc)

    if new_qty <= 0:
        oi.voided = True
        oi.voided_at = now
        # Track which manager voided this item
        oi.voided_by_staff_id = actor_staff_id
    oi.quantity = new_qty
    oi.quantity_cancelled = (oi.quantity_cancelled or 0) + cancelled_qty
    oi.reduced_at = now
    # Track which manager reduced this item
    oi.reduced_by_staff_id = actor_staff_id
    lines_after = order_lines_subtotal(order)
    if float(order.discount_amount or 0) > lines_after:
        order.discount_amount = lines_after
    order.status = _derive_order_status(order)
    db.session.commit()
    db.session.refresh(order)
    invalidate_bill(sess)

    pass
    remaining_items = _kitchen_items_payload(order)
    loc = ('Takeout #' + (sess.pickup_code or '')) if sess.session_type == 'takeout' else ('Table ' + str(sess.table_number or ''))
    push_pos_event({
        'type': 'void_kot',
        'order_id': order.id,
        'order_item_id': oi.id,
        'location': loc,
        'name': item_name,
        'quantity': cancelled_qty,
        'remaining_items': remaining_items,
        'table_number': sess.table_number,
        'session_type': sess.session_type,
        'pickup_code': sess.pickup_code,
        'session_token': sess.token,
    })

    # Enqueue void KOT for the cancelled quantity so the main server KOT worker prints it
    kot_queue_id = None
    try:
        from kot_worker import enqueue_void_kot
        # Include config choices and notes from the original order item
        config_choices = _json.loads(oi.config_choices) if oi.config_choices else None
        kot_queue_id = enqueue_void_kot(order, item_name, cancelled_qty, config_choices, oi.notes)
    except Exception:
        pass

    ev = _order_update_event(order)
    pass
    push_pos_event(ev)
    pass
    return jsonify({'message': 'Quantity reduced', 'order_id': order.id, 'new_quantity': new_qty, 'remaining_items': remaining_items, 'kot_queue_id': kot_queue_id})


@bp.route('/void-item', methods=['POST'])
def void_item():
    from app import push_pos_event
    from routes.bills import invalidate_bill
    from werkzeug.security import check_password_hash
    from models import Staff
    from config import ADMIN_PASSWORD

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    order_item_id = data.get('order_item_id')
    pin = str(data.get('pin') or '').strip()
    password = str(data.get('password') or '').strip()
    staff_id = data.get('staff_id')
    if not token or order_item_id is None:
        return jsonify({'error': 'token and order_item_id required'}), 400

    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Invalid session'}), 400

    # Determine required PIN level based on whether a bill has been printed
    from routes.bills import _active_bill
    bill = _active_bill(sess)
    bill_printed = bill is not None and bill.printed_at is not None

    def _resolve_staff_id():
        if staff_id is None:
            return None
        try:
            staff_id_int = int(staff_id)
        except Exception:
            return jsonify({'error': 'Invalid staff_id'}), 400
        s = db.session.get(Staff, staff_id_int)
        if not s or not s.active:
            return jsonify({'error': 'Staff not found'}), 404
        if sess.session_type == 'table':
            import re as _re
            raw = getattr(s, 'allowed_tables', None)
            allowed = None
            if raw is not None:
                st = str(raw).strip()
                if st:
                    parts = [p for p in _re.split(r'[^0-9]+', st) if p]
                    allowed = set()
                    for p in parts:
                        try:
                            allowed.add(int(p))
                        except Exception:
                            pass
                    if not allowed:
                        allowed = None
            if allowed is not None and int(sess.table_number or 0) not in allowed:
                return jsonify({'error': 'Table not allowed for this staff'}), 403
        return s.id

    voiding_staff_id = None
    if bill_printed:
        if password != ADMIN_PASSWORD:
            return jsonify({'error': 'Invalid admin password', 'needs_admin': True}), 401
        resolved = _resolve_staff_id()
        if isinstance(resolved, tuple):
            return resolved
        voiding_staff_id = resolved
    elif staff_id is not None:
        resolved = _resolve_staff_id()
        if isinstance(resolved, tuple):
            return resolved
        voiding_staff_id = resolved
    elif pin:
        if not pin.isdigit() or len(pin) != 6:
            return jsonify({'error': 'PIN must be exactly 6 digits'}), 400
        all_staff = Staff.query.filter_by(active=True).all()
        pin_valid = any(check_password_hash(s.pin_hash, pin) for s in all_staff)
        if not pin_valid:
            return jsonify({'error': 'Invalid PIN'}), 401
        voiding_staff_id = next((s.id for s in all_staff if check_password_hash(s.pin_hash, pin)), None)

    oi = db.get_or_404(OrderItem, int(order_item_id))
    order = oi.order
    if order.session_id != sess.id:
        return jsonify({'error': 'Item not in session'}), 400
    if oi.voided:
        return jsonify({'error': 'Already voided'}), 400
    needs_kitchen_void = oi.item_status in ('placed', 'preparing')
    oi.voided = True
    oi.voided_at = datetime.now(timezone.utc)
    oi.voided_by_staff_id = voiding_staff_id
    lines_after = order_lines_subtotal(order)
    if float(order.discount_amount or 0) > lines_after:
        order.discount_amount = lines_after

    # Re-derive order status after void
    order.status = _derive_order_status(order)

    db.session.commit()
    db.session.refresh(order)
    invalidate_bill(sess)
    item_name = oi.menu_item.name if oi.menu_item else ''
    remaining_items = _kitchen_items_payload(order)
    loc = ('Takeout #' + (sess.pickup_code or '')) if sess.session_type == 'takeout' else ('Table ' + str(sess.table_number or ''))
    if needs_kitchen_void:
        pass
    # Push to POS stream so head-pos prints the void KOT
    push_pos_event({
        'type': 'void_kot',
        'order_id': order.id,
        'order_item_id': oi.id,
        'location': loc,
        'name': item_name,
        'quantity': oi.quantity,
        'remaining_items': remaining_items,
        'table_number': sess.table_number,
        'session_type': sess.session_type,
        'pickup_code': sess.pickup_code,
        'session_token': sess.token,
    })

    # Enqueue void KOT
    kot_queue_id = None
    try:
        from kot_worker import enqueue_void_kot
        # Include config choices and notes from the original order item
        config_choices = _json.loads(oi.config_choices) if oi.config_choices else None
        kot_queue_id = enqueue_void_kot(order, item_name, oi.quantity, config_choices, oi.notes)
    except Exception:
        pass

    ev = _order_update_event(order)
    pass
    push_pos_event(ev)
    pass
    return jsonify({'message': 'Item voided', 'order_id': order.id, 'remaining_items': remaining_items, 'kot_queue_id': kot_queue_id})


@bp.route('/item-status', methods=['POST'])
def item_status():
    """Kitchen sets per-item status: placed -> preparing -> ready -> served."""
    from app import push_pos_event

    data = request.get_json(force=True, silent=True) or {}
    order_item_id = data.get('order_item_id')
    status = data.get('status')
    if order_item_id is None or status not in ('preparing', 'ready', 'served'):
        return jsonify({'error': 'order_item_id and valid status required'}), 400
    oi = db.get_or_404(OrderItem, int(order_item_id))
    if oi.voided:
        return jsonify({'error': 'Item is voided'}), 400

    oi.item_status = status
    db.session.commit()
    db.session.refresh(oi)

    if status == 'preparing':
        _deduct_stock(oi)
        db.session.commit()

    order = oi.order

    # Auto-derive order-level status from active items only (not held, not voided)
    derived = _derive_order_status(order)
    if order.status != derived:
        order.status = derived
        db.session.commit()
        db.session.refresh(order)

    token = order.session.token
    ev = _order_update_event(order)
    pass
    push_pos_event(ev)
    pass
    return jsonify({'message': 'ok', 'order_item_id': oi.id, 'status': status})
