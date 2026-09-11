from datetime import datetime, timedelta, timezone
import json
import re

from flask import Blueprint, jsonify, request, session
from sqlalchemy import func

from werkzeug.security import check_password_hash, generate_password_hash

from models import (
    Bill, BillModification, Customer, Expense, Inventory, MenuItem, MenuItemConfig,
    Order, OrderItem, Payment, RecipeIngredient, Session as RestSession, LedgerEntry,
    Staff, StockOrder, AdminSession, db,
)

bp = Blueprint('admin', __name__, url_prefix='/api/admin')


def _generate_token(length=36):
    """Generate a random alphanumeric token."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _get_admin_token_from_request():
    """Get admin token from Authorization header or session cookie."""
    # Check Authorization header first
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    # Fall back to session
    return session.get('admin_token')


def _require_admin():
    """Validate admin session token and check expiry."""
    try:
        token = _get_admin_token_from_request()
        if not token:
            return jsonify({'error': 'Unauthorized', 'reason': 'no_token'}), 401
        
        admin_sess = AdminSession.query.filter_by(token=token).first()
        if not admin_sess:
            return jsonify({'error': 'Unauthorized', 'reason': 'invalid_token'}), 401
        
        if not admin_sess.is_valid():
            # Clean up expired session
            db.session.delete(admin_sess)
            db.session.commit()
            session.pop('admin_token', None)
            return jsonify({'error': 'Unauthorized', 'reason': 'session_expired'}), 401
        
        # Update last activity (use naive datetime for PostgreSQL compatibility)
        from models import utcnow
        admin_sess.last_activity = utcnow()
        db.session.commit()
        
        # Store in flask g for access in route
        from flask import g
        g.admin_session = admin_sess
        return None
    except Exception as e:
        import traceback
        print(f"Admin auth error: {e}")
        print(traceback.format_exc())
        return jsonify({'error': 'Internal error', 'details': str(e)}), 500


@bp.route('/login', methods=['POST'])
def login():
    from config import ADMIN_PASSWORD

    data = request.get_json(force=True, silent=True) or {}
    password = data.get('password')
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Invalid password'}), 401
    
    # Generate 36-char token like order sessions
    token = _generate_token(36)
    
    # Create session with 15-minute expiry (use naive datetime)
    from models import utcnow
    expires_at = utcnow() + timedelta(minutes=15)
    admin_sess = AdminSession(
        token=token,
        expires_at=expires_at,
    )
    db.session.add(admin_sess)
    db.session.commit()
    
    # Store in flask session for browser clients
    session['admin_token'] = token
    session.permanent = True
    
    return jsonify({
        'message': 'Logged in',
        'token': token,
        'expires_at': expires_at.isoformat(),
        'expires_in_seconds': 900,  # 15 minutes
    })


@bp.route('/logout', methods=['POST'])
def logout():
    token = _get_admin_token_from_request()
    if token:
        admin_sess = AdminSession.query.filter_by(token=token).first()
        if admin_sess:
            db.session.delete(admin_sess)
            db.session.commit()
    
    session.pop('admin_token', None)
    session.pop('admin_authenticated', None)  # Legacy cleanup
    return jsonify({'message': 'Logged out'})


@bp.route('/session/refresh', methods=['POST'])
def refresh_session():
    """Extend session by another 15 minutes."""
    err = _require_admin()
    if err:
        return err
    
    from flask import g
    admin_sess = g.admin_session
    
    # Extend by 15 minutes from now (use naive datetime)
    from models import utcnow
    new_expires = utcnow() + timedelta(minutes=15)
    admin_sess.expires_at = new_expires
    db.session.commit()
    
    return jsonify({
        'message': 'Session refreshed',
        'expires_at': new_expires.isoformat(),
        'expires_in_seconds': 900,
    })


@bp.route('/session/check', methods=['GET'])
def check_session():
    """Check if current session is valid and get time remaining."""
    err = _require_admin()
    if err:
        return err
    
    from flask import g
    admin_sess = g.admin_session
    
    # Return basic session info without complex datetime calculations
    return jsonify({
        'valid': True,
        'expires_at': admin_sess.expires_at.isoformat() if admin_sess.expires_at else None,
        'created_at': admin_sess.created_at.isoformat() if admin_sess.created_at else None,
        'last_activity': admin_sess.last_activity.isoformat() if admin_sess.last_activity else None,
    })


@bp.route('/shutdown', methods=['POST'])
def shutdown():
    """Kill the server process. Works in dev (stop.bat) and frozen-exe (os._exit) modes."""
    from config import ADMIN_PASSWORD
    import os, threading

    # Check session auth first (for admin dashboard)
    err = _require_admin()
    if err:
        # Fallback to password for emergency shutdown
        data = request.get_json(force=True, silent=True) or {}
        password = data.get('password')
        if password != ADMIN_PASSWORD:
            return jsonify({'error': 'Unauthorized'}), 401

    threading.Timer(0.5, lambda: os._exit(0)).start()
    return jsonify({'message': 'Shutting down'})


def _day_range(date_str=None):
    """Return (day_start, day_end) UTC datetimes for a given ISO date string or today."""
    if date_str:
        try:
            d = datetime.fromisoformat(date_str).date()
        except (ValueError, TypeError):
            d = datetime.now(timezone.utc).date()
    else:
        d = datetime.now(timezone.utc).date()
    day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    return day_start, day_end


@bp.route('/stats', methods=['GET'])
def stats():
    err = _require_admin()
    if err:
        return err

    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)
    from routes.bills import _bill_due_settlements, _rounded_rupee_amount

    history_payload, history_rows, valid_history_rows = _history_sale_payload(
        from_date=day_start.date().isoformat(),
        to_date=day_start.date().isoformat(),
        settled_by=request.args.get('settled_by'),
    )

    # ── Total Sales by payment method ────────────────────────────────────────
    # We look at settled bills for accurate breakdown of split payments
    settled_by_filter = request.args.get('settled_by')
    
    query = Bill.query.filter(
        Bill.settled_at.isnot(None),
        Bill.settled_at >= day_start,
        Bill.settled_at < day_end,
        Bill.is_cancelled.is_(False),
        Bill.is_complementary.is_(False)
    )
    if settled_by_filter:
        query = query.filter(Bill.settled_by == settled_by_filter)
        
    settled_bills = query.all()

    payload_sales_by_method = history_payload.get('sales_by_method') or {}
    sales_by_method = {
        'cash': round(float(payload_sales_by_method.get('cash') or 0), 2),
        'card': round(float(payload_sales_by_method.get('card') or 0), 2),
        'online': round(float(payload_sales_by_method.get('online') or 0), 2),
        'other': round(float(payload_sales_by_method.get('other') or 0), 2),
        'due': round(float(payload_sales_by_method.get('due') or 0), 2),
    }
    total_sales = round(float(history_payload.get('total_sales') or 0), 2)
    collected_sales = round(sum(_rounded_rupee_amount((row or {}).get('history_amount') or 0) for row in valid_history_rows), 2)

    # Get unique settled_by names for filter
    all_settled_by = [r[0] for r in db.session.query(Bill.settled_by).filter(Bill.settled_at.isnot(None)).distinct().all() if r[0]]
    if 'main_pos' not in all_settled_by:
        all_settled_by.append('main_pos')
    all_settled_by.sort()

    def _method_bucket(method):
        m = (method or '').strip().lower()
        if m == 'due':
            return 'due'
        if 'cash' in m:
            return 'cash'
        if 'card' in m:
            return 'card'
        if 'upi' in m or 'online' in m or 'razorpay' in m:
            return 'online'
        return 'other'

    due_recoveries = (
        LedgerEntry.query
        .filter(
            LedgerEntry.entry_type == 'settlement',
            LedgerEntry.created_at >= day_start,
            LedgerEntry.created_at < day_end,
        )
        .all()
    )
    due_recovered_amount = 0.0
    standalone_due_recovered_amount = 0.0
    for entry in due_recoveries:
        bill = entry.bill
        if not bill or bill.is_cancelled or bill.is_complementary:
            continue
        if float(entry.amount_subtotal or 0) >= 0:
            continue
        due_payable = 0.0
        for s in _bill_due_settlements(bill):
            if s['id'] == entry.id:
                due_payable = float(s.get('amount_payable') or 0)
                break
        if due_payable <= 0:
            due_payable = abs(float(entry.amount_subtotal or 0))
        due_recovered_amount += due_payable
        if entry.session_id:
            continue
        standalone_due_recovered_amount += due_payable
    sales_by_method['cash'] = round(float(sales_by_method.get('cash', 0.0)), 2)
    sales_by_method['card'] = round(float(sales_by_method.get('card', 0.0)), 2)
    sales_by_method['online'] = round(float(sales_by_method.get('online', 0.0)), 2)
    sales_by_method['other'] = round(float(sales_by_method.get('other', 0.0)), 2)
    sales_by_method['due'] = round(float(sales_by_method.get('due', 0.0)), 2)

    # Bills settled today (not_paid = bills printed but unpaid)
    settled_bills = Bill.query.filter(
        Bill.settled_at.isnot(None),
        Bill.settled_at >= day_start,
        Bill.settled_at < day_end,
    ).all()
    successful_count = sum(1 for b in settled_bills if not b.is_complementary and not b.is_cancelled)
    complementary_count = sum(1 for b in settled_bills if b.is_complementary)
    cancelled_count = Bill.query.filter(
        Bill.is_cancelled.is_(True),
        Bill.created_at >= day_start,
        Bill.created_at < day_end,
    ).count()

    # Not paid = payments pending today
    not_paid_amount = (
        db.session.query(func.coalesce(func.sum(Payment.amount), 0.0))
        .join(RestSession, RestSession.id == Payment.session_id)
        .filter(
            Payment.status == 'pending',
            Payment.confirmed_at.is_(None),
            RestSession.created_at >= day_start,
        )
        .scalar() or 0.0
    )

    # ── Sales by order type ──────────────────────────────────────────────────
    sales_by_type = {
        'table': {'amount': 0.0, 'count': 0},
        'takeout': {'amount': 0.0, 'count': 0},
        'delivery': {'amount': 0.0, 'count': 0},
    }
    for row in valid_history_rows:
        key = row.get('session_type') or ''
        if key not in sales_by_type:
            continue
        sales_by_type[key]['amount'] += _rounded_rupee_amount(row.get('history_amount') or 0)
        sales_by_type[key]['count'] += 1

    dine_in_sales = round(float(sales_by_type['table']['amount']), 2)
    dine_in_count = int(sales_by_type['table']['count'])
    pickup_sales = round(float(sales_by_type['takeout']['amount']), 2)
    pickup_count = int(sales_by_type['takeout']['count'])
    delivery_sales = round(float(sales_by_type['delivery']['amount']), 2)
    delivery_count = int(sales_by_type['delivery']['count'])

    # Average turnaround: session created_at → bill settled_at (in minutes)
    def _avg_turnaround(session_type):
        rows = (
            db.session.query(
                RestSession.created_at,
                Bill.settled_at,
            )
            .join(Bill, Bill.session_id == RestSession.id)
            .filter(
                RestSession.session_type == session_type,
                Bill.settled_at.isnot(None),
                Bill.settled_at >= day_start,
                Bill.settled_at < day_end,
            )
            .all()
        )
        if not rows:
            return 0
        diffs = []
        for created, settled in rows:
            if created and settled:
                if created.tzinfo is None:
                    from datetime import timezone as tz
                    created = created.replace(tzinfo=tz.utc)
                if settled.tzinfo is None:
                    from datetime import timezone as tz
                    settled = settled.replace(tzinfo=tz.utc)
                diffs.append((settled - created).total_seconds() / 60)
        return round(sum(diffs) / len(diffs), 1) if diffs else 0

    dine_in_avg = _avg_turnaround('table')
    pickup_avg = _avg_turnaround('takeout')

    # ── KOT leakage ─────────────────────────────────────────────────────────
    cancelled_kots = OrderItem.query.join(Order).filter(
        OrderItem.voided.is_(True),
        OrderItem.voided_at.isnot(None),
        OrderItem.voided_at >= day_start,
        OrderItem.voided_at < day_end,
    ).count()

    # Units cancelled via Edit Qty (partial reductions, not full voids)
    reduced_qty_units = db.session.query(
        func.coalesce(func.sum(OrderItem.quantity_cancelled), 0)
    ).join(Order).filter(
        OrderItem.quantity_cancelled > 0,
        OrderItem.voided.is_(False),
        OrderItem.reduced_at.isnot(None),
        OrderItem.reduced_at >= day_start,
        OrderItem.reduced_at < day_end,
    ).scalar() or 0

    # Orders that had at least one item voided but also have non-voided items = modified KOTs
    from sqlalchemy import exists as _exists, select as _select
    _VoidedOI = db.aliased(OrderItem)
    _ActiveOI = db.aliased(OrderItem)
    voided_exists = _exists().where(_VoidedOI.order_id == Order.id, _VoidedOI.voided.is_(True))
    active_exists = _exists().where(_ActiveOI.order_id == Order.id, _ActiveOI.voided.is_(False))
    modified_kots = (
        db.session.query(func.count(Order.id))
        .filter(
            Order.created_at >= day_start,
            Order.created_at < day_end,
            voided_exists,
            active_exists,
        )
        .scalar() or 0
    )

    # Sessions closed today with orders but no settled bill
    settled_session_ids = _select(Bill.session_id).where(Bill.settled_at.isnot(None))
    has_orders = _select(Order.session_id).where(Order.session_id == RestSession.id).correlate(RestSession)
    not_used_in_bills = (
        RestSession.query
        .filter(
            RestSession.status.in_(['force_closed', 'closed']),
            RestSession.closed_at >= day_start,
            RestSession.closed_at < day_end,
            RestSession.id.in_(has_orders),
            RestSession.id.notin_(settled_session_ids),
        )
        .count()
    )

    # Sessions shifted today
    shifted_sessions = (
        RestSession.query
        .filter(
            RestSession.shifts_count > 0,
            RestSession.created_at >= day_start,
            RestSession.created_at < day_end,
        )
        .count()
    )

    # ── Bill leakage ─────────────────────────────────────────────────────────
    modified_bills = BillModification.query.join(Bill).filter(
        BillModification.created_at >= day_start,
        BillModification.created_at < day_end,
    ).count()

    reprinted_bills = Bill.query.filter(
        Bill.print_count > 1,
        Bill.printed_at.isnot(None),
        Bill.printed_at >= day_start,
        Bill.printed_at < day_end,
    ).count()

    waived_off_total = db.session.query(
        func.coalesce(func.sum(Bill.waived_off_amount), 0.0)
    ).filter(
        Bill.created_at >= day_start,
        Bill.created_at < day_end,
    ).scalar() or 0.0

    # ── Expenses & Withdrawals ───────────────────────────────────────────────
    expense_rows = (
        db.session.query(Expense.category, func.coalesce(func.sum(Expense.amount), 0.0))
        .filter(
            Expense.created_at >= day_start,
            Expense.created_at < day_end,
        )
        .group_by(Expense.category)
        .all()
    )
    expenses_total = 0.0
    expenses_breakdown = []
    for cat, amt in expense_rows:
        amt = float(amt)
        expenses_total += amt
        expenses_breakdown.append({'category': cat or 'other', 'amount': amt})

    # ── Weekly revenue ───────────────────────────────────────────────────────
    weekly_revenue = []
    for i in range(6, -1, -1):
        d0 = day_start - timedelta(days=i)
        d1 = d0 + timedelta(days=1)
        rev = db.session.query(func.coalesce(func.sum(Payment.amount), 0.0)).filter(
            Payment.status == 'confirmed',
            Payment.confirmed_at.isnot(None),
            Payment.confirmed_at >= d0,
            Payment.confirmed_at < d1,
        ).scalar() or 0.0
        weekly_revenue.append({'date': d0.date().isoformat(), 'total': float(rev)})

    # ── Top items ────────────────────────────────────────────────────────────
    top_rows = (
        db.session.query(MenuItem.name, func.sum(OrderItem.quantity).label('tq'))
        .join(OrderItem, OrderItem.menu_item_id == MenuItem.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(
            Order.created_at >= day_start,
            Order.created_at < day_end,
            OrderItem.voided.is_(False),
        )
        .group_by(MenuItem.id)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(5)
        .all()
    )
    top_items = [{'name': r[0], 'total_quantity': int(r[1])} for r in top_rows]

    return jsonify({
        'date': day_start.date().isoformat(),
        'total_sales': round(float(total_sales), 2),
        'collected_sales': round(float(collected_sales), 2),
        'due_recovered': round(float(due_recovered_amount), 2),
        'settled_by_options': all_settled_by,
        'sales_by_method': {
            'cash': round(sales_by_method.get('cash', 0.0), 2),
            'card': round(sales_by_method.get('card', 0.0), 2),
            'online': round(sales_by_method.get('online', 0.0), 2),
            'other': round(sales_by_method.get('other', 0.0), 2),
            'due': round(sales_by_method.get('due', 0.0), 2),
            'not_paid': round(float(not_paid_amount), 2),
        },
        'order_counts': {
            'successful': successful_count,
            'complementary': complementary_count,
            'cancelled': cancelled_count,
        },
        'sales_by_type': {
            'dine_in': {'amount': round(dine_in_sales, 2), 'count': dine_in_count, 'avg_turnaround_min': dine_in_avg},
            'pickup': {'amount': round(pickup_sales, 2), 'count': pickup_count, 'avg_turnaround_min': pickup_avg},
            'delivery': {'amount': round(delivery_sales, 2), 'count': delivery_count, 'avg_turnaround_min': 0},
        },
        'kot_leakage': {
            'cancelled': cancelled_kots,
            'modified': modified_kots,
            'reduced_qty_units': int(reduced_qty_units),
            'not_used_in_bills': not_used_in_bills,
            'shifted': shifted_sessions,
        },
        'bill_leakage': {
            'modified': modified_bills,
            'reprinted': reprinted_bills,
            'waived_off': round(float(waived_off_total), 2),
        },
        'expenses': {
            'total': round(float(expenses_total), 2),
            'breakdown': expenses_breakdown,
        },
        'top_items': top_items,
        'weekly_revenue': weekly_revenue,
    })


def _inv_dict(r):
    linked = [{'id': ri.menu_item_id, 'name': ri.menu_item.name, 'quantity': ri.quantity}
              for ri in r.recipe_uses]
    return {
        'id': r.id,
        'name': r.name,
        'unit': r.unit,
        'stock_level': r.stock_level,
        'low_stock_threshold': r.low_stock_threshold,
        'is_low': r.stock_level <= r.low_stock_threshold,
        'linked_items': linked,
        'expiry_date': r.expiry_date.isoformat() if r.expiry_date else None,
        'average_unit_cost': r.average_unit_cost,
        'last_purchase_price': r.last_purchase_price,
    }


@bp.route('/inventory', methods=['GET'])
def list_inventory():
    err = _require_admin()
    if err:
        return err
    rows = Inventory.query.order_by(Inventory.name.asc()).all()
    return jsonify([_inv_dict(r) for r in rows])


@bp.route('/inventory/items', methods=['POST'])
def create_inventory():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    inv = Inventory(
        name=name,
        unit=data.get('unit', 'units').strip() or 'units',
        stock_level=float(data.get('stock_level', 0)),
        low_stock_threshold=float(data.get('low_stock_threshold', 0)),
    )
    db.session.add(inv)
    db.session.commit()
    return jsonify(_inv_dict(inv)), 201


@bp.route('/inventory/<int:inv_id>', methods=['PUT'])
def update_inventory(inv_id):
    err = _require_admin()
    if err:
        return err
    inv = db.get_or_404(Inventory, inv_id)
    data = request.get_json(force=True, silent=True) or {}
    if 'name' in data:
        inv.name = data['name'].strip() or inv.name
    if 'unit' in data:
        inv.unit = data['unit'].strip() or inv.unit
    if 'stock_level' in data:
        inv.stock_level = float(data['stock_level'])
    if 'low_stock_threshold' in data:
        inv.low_stock_threshold = float(data['low_stock_threshold'])
    db.session.commit()
    return jsonify(_inv_dict(inv))


@bp.route('/inventory/<int:inv_id>', methods=['DELETE'])
def delete_inventory(inv_id):
    err = _require_admin()
    if err:
        return err
    inv = db.get_or_404(Inventory, inv_id)
    db.session.delete(inv)
    db.session.commit()
    return jsonify({'message': 'deleted'})


@bp.route('/inventory/disable-affected', methods=['POST'])
def disable_affected():
    """Disable all menu items whose ingredients are at or below threshold."""
    err = _require_admin()
    if err:
        return err
    low = Inventory.query.filter(Inventory.stock_level <= Inventory.low_stock_threshold).all()
    disabled = []
    seen_ids = set()
    for inv in low:
        for ri in inv.recipe_uses:
            item = ri.menu_item
            if item and item.available and item.id not in seen_ids:
                seen_ids.add(item.id)
                disabled.append({'id': item.id, 'name': item.name})
    # Single batched UPDATE (chunked) instead of one UPDATE per affected item.
    ids = [d['id'] for d in disabled]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        MenuItem.query.filter(MenuItem.id.in_(chunk)).update(
            {MenuItem.available: False}, synchronize_session=False
        )
    db.session.commit()
    return jsonify({'disabled': disabled, 'count': len(disabled)})


@bp.route('/menu/<int:item_id>/ingredients', methods=['GET'])
def get_recipe(item_id):
    err = _require_admin()
    if err:
        return err
    item = db.get_or_404(MenuItem, item_id)
    return jsonify([
        {
            'id': ri.id,
            'inventory_id': ri.inventory_id,
            'name': ri.ingredient.name,
            'unit': ri.ingredient.unit,
            'quantity': ri.quantity,
        }
        for ri in item.recipe_ingredients
    ])


@bp.route('/menu/<int:item_id>/ingredients', methods=['POST'])
def add_recipe_ingredient(item_id):
    err = _require_admin()
    if err:
        return err
    db.get_or_404(MenuItem, item_id)
    data = request.get_json(force=True, silent=True) or {}
    inv_id = data.get('inventory_id')
    qty = data.get('quantity', 1.0)
    if not inv_id:
        return jsonify({'error': 'inventory_id required'}), 400
    inv = db.get_or_404(Inventory, int(inv_id))
    existing = RecipeIngredient.query.filter_by(
        menu_item_id=item_id, inventory_id=inv.id).first()
    if existing:
        existing.quantity = float(qty)
        db.session.commit()
        ri = existing
    else:
        ri = RecipeIngredient(menu_item_id=item_id, inventory_id=inv.id, quantity=float(qty))
        db.session.add(ri)
        db.session.commit()
    return jsonify({
        'id': ri.id,
        'inventory_id': ri.inventory_id,
        'name': inv.name,
        'unit': inv.unit,
        'quantity': ri.quantity,
    }), 201


@bp.route('/menu/<int:item_id>/ingredients/<int:inv_id>', methods=['DELETE'])
def remove_recipe_ingredient(item_id, inv_id):
    err = _require_admin()
    if err:
        return err
    ri = RecipeIngredient.query.filter_by(
        menu_item_id=item_id, inventory_id=inv_id).first_or_404()
    db.session.delete(ri)
    db.session.commit()
    return jsonify({'message': 'removed'})


def _so_dict(so):
    return {
        'id': so.id,
        'inventory_id': so.inventory_id,
        'ingredient_name': so.ingredient.name,
        'unit': so.ingredient.unit,
        'current_stock': so.ingredient.stock_level,
        'quantity_ordered': so.quantity_ordered,
        'quantity_received': so.quantity_received,
        'status': so.status,
        'notes': so.notes,
        'created_at': so.created_at.isoformat() if so.created_at else None,
        'arrived_at': so.arrived_at.isoformat() if so.arrived_at else None,
    }


@bp.route('/stock-orders', methods=['GET'])
def list_stock_orders():
    err = _require_admin()
    if err:
        return err
    status_filter = request.args.get('status')
    q = StockOrder.query.order_by(StockOrder.created_at.desc())
    if status_filter:
        q = q.filter(StockOrder.status == status_filter)
    return jsonify([_so_dict(so) for so in q.all()])


@bp.route('/stock-orders', methods=['POST'])
def create_stock_order():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    inv_id = data.get('inventory_id')
    qty = data.get('quantity_ordered')
    if not inv_id or not qty:
        return jsonify({'error': 'inventory_id and quantity_ordered required'}), 400
    inv = db.get_or_404(Inventory, int(inv_id))
    so = StockOrder(
        inventory_id=inv.id,
        quantity_ordered=float(qty),
        notes=data.get('notes', '').strip() or None,
        status='pending',
    )
    db.session.add(so)
    db.session.commit()
    return jsonify(_so_dict(so)), 201


@bp.route('/stock-orders/<int:so_id>/arrive', methods=['POST'])
def arrive_stock_order(so_id):
    """Mark order as arrived and add received quantity to inventory."""
    err = _require_admin()
    if err:
        return err
    so = db.get_or_404(StockOrder, so_id)
    if so.status == 'arrived':
        return jsonify({'error': 'Already marked as arrived'}), 400
    data = request.get_json(force=True, silent=True) or {}
    qty_received = float(data.get('quantity_received', so.quantity_ordered))
    so.quantity_received = qty_received
    so.status = 'arrived'
    so.arrived_at = datetime.now(timezone.utc)
    so.ingredient.stock_level = so.ingredient.stock_level + qty_received
    db.session.commit()
    return jsonify(_so_dict(so))


@bp.route('/stock-orders/<int:so_id>', methods=['DELETE'])
def cancel_stock_order(so_id):
    err = _require_admin()
    if err:
        return err
    so = db.get_or_404(StockOrder, so_id)
    so.status = 'cancelled'
    db.session.commit()
    return jsonify({'message': 'cancelled'})


# ── Bill list & detail endpoints ─────────────────────────────────────────────

def _bill_status(b):
    if b.is_cancelled:
        return 'cancelled'
    if b.is_complementary:
        return 'complementary'
    if b.settled_at:
        return 'settled'
    if b.printed_at:
        return 'printed'
    return 'open'


def _parsed_split_payments(raw_value):
    if not raw_value or not str(raw_value).strip():
        return None
    try:
        return json.loads(raw_value)
    except Exception:
        return None


def _history_sale_payload(from_date=None, to_date=None, settled_by=None):
    from routes.bills import _build_bill_history_payload

    payload = _build_bill_history_payload(
        from_date=from_date,
        to_date=to_date,
        settled_by=settled_by,
    )
    rows = payload.get('bills') or []
    valid_rows = [
        row for row in rows
        if not row.get('is_cancelled')
        and not row.get('is_complementary')
        and not (
            (row.get('payment_method') or '').strip().lower() == 'due'
            and (row.get('due_status') or '') != 'cleared'
        )
    ]
    return payload, rows, valid_rows


def _admin_bill_financial_map(bills):
    from routes.bills import (
        _bill_due_settlements,
        _bill_history_financials,
        _bill_open_due_payable,
        _history_split_payments,
        _session_due_settlements_map,
    )

    bills = list(bills or [])
    session_due_settlements = _session_due_settlements_map([b.session_id for b in bills])
    out = {}

    for b in bills:
        due_settlements = _bill_due_settlements(b) if b.payment_method == 'due' else []
        linked_session_settlements = session_due_settlements.get(b.session_id, []) if b.payment_method != 'due' else []
        linked_due_recovered = round(sum(float(s.get('amount_payable') or 0) for s in linked_session_settlements), 2)
        history_amount = round(float(b.amount or 0), 2)
        split_payments = _parsed_split_payments(b.split_payments)
        history_split_payments = split_payments

        settlement_methods = []
        for row in due_settlements:
            method = (row.get('payment_method') or '').strip()
            if method and method not in settlement_methods:
                settlement_methods.append(method)

        effective_payment_method = b.payment_method
        if b.payment_method == 'due' and settlement_methods:
            effective_payment_method = settlement_methods[0] if len(settlement_methods) == 1 else 'multiple'
        elif b.due_cleared_method:
            effective_payment_method = b.due_cleared_method

        if b.payment_method == 'due':
            if b.due_status == 'cleared' and due_settlements:
                history_amount = round(sum(float(row.get('amount_payable') or 0) for row in due_settlements), 2)
            else:
                # Use bill.amount directly to preserve exact amount including tax
                history_amount = round(float(b.amount or 0), 2)
        elif linked_due_recovered > 0:
            history_amount = round(max(0.0, float(b.amount or 0) - linked_due_recovered), 2)
            if history_split_payments:
                history_split_payments = _history_split_payments(
                    history_split_payments,
                    linked_due_recovered,
                    linked_session_settlements,
                )

        financials = _bill_history_financials(
            b,
            history_amount=history_amount,
            due_settlements=due_settlements,
            linked_due_recovered=linked_due_recovered,
        )

        out[b.id] = {
            'history_amount': financials['payable_amount'],
            'effective_payment_method': effective_payment_method,
            'split_payments': split_payments,
            'history_split_payments': history_split_payments,
            'due_settlements': due_settlements,
            'linked_due_recovered_payable': round(float(linked_due_recovered or 0), 2),
            'gross_subtotal': financials['gross_subtotal'],
            'subtotal': financials['subtotal'],
            'total_discount': financials['total_discount'],
            'cgst_amount': financials['cgst_amount'],
            'sgst_amount': financials['sgst_amount'],
            'tax_amount': financials['tax_amount'],
            'payable_amount': financials['payable_amount'],
            'previous_due_cleared_payable': financials['previous_due_cleared_payable'],
        }
    return out


@bp.route('/bills', methods=['GET'])
def list_bills_admin():
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)
    bills = (
        Bill.query
        .filter(Bill.created_at >= day_start, Bill.created_at < day_end)
        .order_by(Bill.created_at.asc())
        .all()
    )
    import json as _json
    # Pre-aggregate modification counts in one query to avoid N+1
    bill_ids = [b.id for b in bills]
    mod_counts = {}
    if bill_ids:
        mod_counts = dict(
            db.session.query(BillModification.bill_id, func.count(BillModification.id))
            .filter(BillModification.bill_id.in_(bill_ids))
            .group_by(BillModification.bill_id)
            .all()
        )
    financial_map = _admin_bill_financial_map(bills)
    out = []
    for b in bills:
        items = _json.loads(b.items_snapshot) if b.items_snapshot else []
        financials = financial_map.get(b.id, {})
        out.append({
            'id': b.id,
            'session_id': b.session_id,
            'session_type': b.session_type,
            'table_number': b.table_number,
            'pickup_code': b.pickup_code,
            'status': _bill_status(b),
            'is_complementary': b.is_complementary,
            'is_cancelled': b.is_cancelled,
            'print_count': b.print_count or 0,
            'item_count': len(items),
            'amount': b.amount,
            'history_amount': financials.get('payable_amount', round(float(b.amount or 0), 2)),
            'payable_amount': financials.get('payable_amount', round(float(b.amount or 0), 2)),
            'gross_subtotal': financials.get('gross_subtotal', 0.0),
            'subtotal': financials.get('subtotal', 0.0),
            'total_discount': financials.get('total_discount', 0.0),
            'cgst_amount': financials.get('cgst_amount', round(float(b.cgst_amount or 0), 2)),
            'sgst_amount': financials.get('sgst_amount', round(float(b.sgst_amount or 0), 2)),
            'tax_amount': financials.get('tax_amount', round(float(b.cgst_amount or 0) + float(b.sgst_amount or 0), 2)),
            'linked_due_recovered_payable': financials.get('linked_due_recovered_payable', 0.0),
            'payment_method': b.payment_method,
            'effective_payment_method': financials.get('effective_payment_method', b.payment_method),
            'split_payments': financials.get('split_payments'),
            'history_split_payments': financials.get('history_split_payments'),
            'due_settlements': financials.get('due_settlements', []),
            'waived_off_amount': b.waived_off_amount or 0.0,
            'coupon_code': b.coupon_code,
            'settled_by': b.settled_by,
            'created_at': b.created_at.isoformat() if b.created_at else None,
            'printed_at': b.printed_at.isoformat() if b.printed_at else None,
            'settled_at': b.settled_at.isoformat() if b.settled_at else None,
            'modification_count': mod_counts.get(b.id, 0),
        })
    return jsonify(out)


@bp.route('/bills/<int:bill_id>/detail', methods=['GET'])
def bill_detail(bill_id):
    err = _require_admin()
    if err:
        return err
    import json as _json
    from routes.bills import _bill_due_settlements
    bill = db.get_or_404(Bill, bill_id)
    items = _json.loads(bill.items_snapshot) if bill.items_snapshot else []
    related_financials = _admin_bill_financial_map([bill])
    bill_financials = related_financials.get(bill.id, {})

    # Voided items for this session
    from models import Order as Ord, OrderItem as OI, MenuItem as MI
    voided_rows = (
        db.session.query(MI.name, OI.quantity, OI.notes, OI.voided_at, Ord.created_at)
        .join(OI, OI.menu_item_id == MI.id)
        .join(Ord, Ord.id == OI.order_id)
        .filter(Ord.session_id == bill.session_id, OI.voided.is_(True))
        .order_by(OI.voided_at.asc())
        .all()
    )
    voided_items = [
        {
            'name': r[0],
            'quantity': r[1],
            'notes': r[2] or '',
            'voided_at': r[3].isoformat() if r[3] else None,
            'order_placed_at': r[4].isoformat() if r[4] else None,
        }
        for r in voided_rows
    ]

    # Partially reduced items (qty lowered but not fully voided)
    reduced_rows = (
        db.session.query(MI.name, OI.quantity, OI.quantity_cancelled, OI.notes, OI.reduced_at)
        .join(OI, OI.menu_item_id == MI.id)
        .join(Ord, Ord.id == OI.order_id)
        .filter(
            Ord.session_id == bill.session_id,
            OI.voided.is_(False),
            OI.quantity_cancelled > 0,
        )
        .order_by(OI.reduced_at.asc())
        .all()
    )
    reduced_items = [
        {
            'name': r[0],
            'billed_quantity': r[1],
            'cancelled_quantity': r[2],
            'notes': r[3] or '',
            'reduced_at': r[4].isoformat() if r[4] else None,
        }
        for r in reduced_rows
    ]

    # Audit trail
    mods = [
        {
            'id': m.id,
            'description': m.description,
            'modified_by': m.modified_by,
            'created_at': m.created_at.isoformat() if m.created_at else None,
        }
        for m in bill.modifications.order_by(BillModification.created_at.asc())
    ]

    # Other bills from same session (cancelled → replaced chain)
    session_bills = [
        {'id': o.id, 'status': _bill_status(o), 'amount': o.amount,
         'history_amount': _admin_bill_financial_map([o]).get(o.id, {}).get('history_amount', round(float(o.amount or 0), 2)),
         'print_count': o.print_count or 0,
         'created_at': o.created_at.isoformat() if o.created_at else None,
         'settled_at': o.settled_at.isoformat() if o.settled_at else None}
        for o in Bill.query.filter(Bill.session_id == bill.session_id, Bill.id != bill.id)
                           .order_by(Bill.id.asc()).all()
    ]

    # Other bills from same table on same day (different sessions)
    related = []
    if bill.table_number:
        d = bill.created_at.date() if bill.created_at else datetime.now(timezone.utc).date()
        ds = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        de = ds + timedelta(days=1)
        others = Bill.query.filter(
            Bill.table_number == bill.table_number,
            Bill.session_id != bill.session_id,
            Bill.created_at >= ds,
            Bill.created_at < de,
        ).order_by(Bill.created_at.asc()).all()
        related = [{'id': o.id, 'status': _bill_status(o), 'amount': o.amount,
                    'history_amount': _admin_bill_financial_map([o]).get(o.id, {}).get('history_amount', round(float(o.amount or 0), 2)),
                    'created_at': o.created_at.isoformat() if o.created_at else None} for o in others]

    return jsonify({
        'id': bill.id,
        'session_id': bill.session_id,
        'session_type': bill.session_type,
        'table_number': bill.table_number,
        'pickup_code': bill.pickup_code,
        'status': _bill_status(bill),
        'is_complementary': bill.is_complementary,
        'is_cancelled': bill.is_cancelled,
        'print_count': bill.print_count or 0,
        'amount': bill.amount,
        'history_amount': bill_financials.get('payable_amount', round(float(bill.amount or 0), 2)),
        'payable_amount': bill_financials.get('payable_amount', round(float(bill.amount or 0), 2)),
        'payment_method': bill.payment_method,
        'effective_payment_method': bill_financials.get('effective_payment_method', bill.payment_method),
        'split_payments': bill_financials.get('split_payments'),
        'history_split_payments': bill_financials.get('history_split_payments'),
        'gross_subtotal': bill_financials.get('gross_subtotal', 0.0),
        'subtotal': bill_financials.get('subtotal', 0.0),
        'total_discount': bill_financials.get('total_discount', 0.0),
        'cgst_amount': bill_financials.get('cgst_amount', round(float(bill.cgst_amount or 0), 2)),
        'sgst_amount': bill_financials.get('sgst_amount', round(float(bill.sgst_amount or 0), 2)),
        'tax_amount': bill_financials.get('tax_amount', round(float(bill.cgst_amount or 0) + float(bill.sgst_amount or 0), 2)),
        'previous_due_subtotal': bill.previous_due_subtotal or 0.0,
        'previous_due_payable': bill.previous_due_subtotal or 0.0,
        'linked_due_recovered_payable': bill_financials.get('previous_due_cleared_payable', 0.0),
        'include_previous_due': bool(bill.include_previous_due),
        'due_settlements': bill_financials.get('due_settlements', _bill_due_settlements(bill) if bill.payment_method == 'due' else []),
        'waived_off_amount': bill.waived_off_amount or 0.0,
        'coupon_code': bill.coupon_code,
        'bill_comment': bill.bill_comment,
        'customer_name': bill.customer_name,
        'customer_gstin': bill.customer_gstin,
        'customer_phone': bill.customer_phone,
        'customer_address': bill.customer_address,
        'customer_notes': bill.customer_notes,
        'created_at': bill.created_at.isoformat() if bill.created_at else None,
        'printed_at': bill.printed_at.isoformat() if bill.printed_at else None,
        'settled_at': bill.settled_at.isoformat() if bill.settled_at else None,
        'items': items,
        'voided_items': voided_items,
        'reduced_items': reduced_items,
        'modifications': mods,
        'session_bills': session_bills,
        'related_bills': related,
    })


# ── Expense endpoints ─────────────────────────────────────────────────────────

def _expense_dict(e):
    return {
        'id': e.id,
        'amount': e.amount,
        'description': e.description,
        'category': e.category or 'other',
        'created_by': e.created_by,
        'created_at': e.created_at.isoformat() if e.created_at else None,
    }


@bp.route('/expenses', methods=['GET'])
def list_expenses():
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)
    rows = Expense.query.filter(
        Expense.created_at >= day_start,
        Expense.created_at < day_end,
    ).order_by(Expense.created_at.desc()).all()
    return jsonify([_expense_dict(e) for e in rows])


@bp.route('/expenses', methods=['POST'])
def create_expense():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    amount = data.get('amount')
    description = (data.get('description') or '').strip()
    if not amount or not description:
        return jsonify({'error': 'amount and description required'}), 400
    e = Expense(
        amount=float(amount),
        description=description,
        category=(data.get('category') or 'other').strip(),
        created_by=(data.get('created_by') or '').strip() or None,
    )
    db.session.add(e)
    db.session.commit()
    return jsonify(_expense_dict(e)), 201


@bp.route('/expenses/<int:exp_id>', methods=['DELETE'])
def delete_expense(exp_id):
    err = _require_admin()
    if err:
        return err
    e = db.get_or_404(Expense, exp_id)
    db.session.delete(e)
    db.session.commit()
    return jsonify({'message': 'deleted'})


# ── Bill flag endpoints ────────────────────────────────────────────────────────

@bp.route('/bills/<int:bill_id>/flag', methods=['POST'])
def flag_bill(bill_id):
    """Mark a bill as complementary or cancelled, optionally record a waived amount."""
    err = _require_admin()
    if err:
        return err
    bill = db.get_or_404(Bill, bill_id)
    data = request.get_json(force=True, silent=True) or {}
    if 'is_complementary' in data:
        bill.is_complementary = bool(data['is_complementary'])
    if 'is_cancelled' in data:
        bill.is_cancelled = bool(data['is_cancelled'])
    if 'waived_off_amount' in data:
        bill.waived_off_amount = float(data['waived_off_amount'])
    if 'coupon_code' in data:
        bill.coupon_code = (data['coupon_code'] or '').strip() or None
    db.session.commit()
    return jsonify({'message': 'updated', 'bill_id': bill.id})


# ── System Config endpoints ──────────────────────────────────────────────────

@bp.route('/config', methods=['GET'])
def get_system_config():
    err = _require_admin()
    if err:
        return err
    from models import get_config_value
    import config
    # Get max bill ID to show as reference
    max_bill_id = db.session.query(db.func.max(Bill.id)).scalar() or 0
    return jsonify({
        'bill_start_number': get_config_value('bill_start_number', ''),
        'max_bill_id': max_bill_id,
        'restaurant_name': config.RESTAURANT_NAME,
        'restaurant_address': config.RESTAURANT_ADDRESS,
        'restaurant_phone': config.RESTAURANT_PHONE,
        'fssai_number': config.FSSAI_NUMBER,
        'restaurant_website': config.RESTAURANT_WEBSITE,
        'bill_footer_msg': config.BILL_FOOTER_MSG,
        'bill_visible_configs': config.BILL_VISIBLE_CONFIGS,
        'instagram_handle': config.INSTAGRAM_HANDLE,
        'google_listing': config.GOOGLE_LISTING,
        'bill_jurisdiction': config.BILL_JURISDICTION,
        'gst_number': config.GST_NUMBER,
        'cgst_rate': config.CGST_RATE,
        'sgst_rate': config.SGST_RATE,
        'enable_takeout': config.ENABLE_TAKEOUT,
        'kitchen_printer_enabled': config.KITCHEN_PRINTER_ENABLED,
        'kitchen_printer_name': config.KITCHEN_PRINTER_NAME,
        'theme_primary': config.THEME_PRIMARY,
        'theme_primary_mid': config.THEME_PRIMARY_MID,
        'theme_secondary': config.THEME_SECONDARY,
        'theme_accent': config.THEME_ACCENT,
        'theme_accent_light': config.THEME_ACCENT_LIGHT,
        'theme_bg': config.THEME_BG,
        'theme_bg_dark': config.THEME_BG_DARK,
        'theme_foam': config.THEME_FOAM,
        'theme_muted': config.THEME_MUTED,
        'theme_text': config.THEME_TEXT,
        'theme_border': config.THEME_BORDER,
        'theme_border_strong': config.THEME_BORDER_STRONG,
        'host_ip': config.HOST_IP,
    })


@bp.route('/config', methods=['POST'])
def update_system_config():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    from models import set_config_value
    if 'bill_start_number' in data:
        val = str(data['bill_start_number']).strip()
        if val:
            try:
                # Basic validation
                num = int(val)
                if num <= 0:
                    return jsonify({'error': 'Bill start number must be positive'}), 400
                max_id = db.session.query(db.func.max(Bill.id)).scalar() or 0
                if num <= max_id:
                    return jsonify({'error': f'Start number must be greater than current max bill ID ({max_id})'}), 400
                set_config_value('bill_start_number', val)
            except ValueError:
                return jsonify({'error': 'Invalid number format'}), 400
        else:
            set_config_value('bill_start_number', '')

    config_map = {
        'restaurant_name': 'RESTAURANT_NAME',
        'restaurant_address': 'RESTAURANT_ADDRESS',
        'restaurant_phone': 'RESTAURANT_PHONE',
        'fssai_number': 'FSSAI_NUMBER',
        'restaurant_website': 'RESTAURANT_WEBSITE',
        'bill_footer_msg': 'BILL_FOOTER_MSG',
        'bill_visible_configs': 'BILL_VISIBLE_CONFIGS',
        'instagram_handle': 'INSTAGRAM_HANDLE',
        'google_listing': 'GOOGLE_LISTING',
        'bill_jurisdiction': 'BILL_JURISDICTION',
        'gst_number': 'GST_NUMBER',
        'cgst_rate': 'CGST_RATE',
        'sgst_rate': 'SGST_RATE',
        'enable_takeout': 'ENABLE_TAKEOUT',
        'kitchen_printer_enabled': 'KITCHEN_PRINTER_ENABLED',
        'kitchen_printer_name': 'KITCHEN_PRINTER_NAME',
        'theme_primary': 'THEME_PRIMARY',
        'theme_primary_mid': 'THEME_PRIMARY_MID',
        'theme_secondary': 'THEME_SECONDARY',
        'theme_accent': 'THEME_ACCENT',
        'theme_accent_light': 'THEME_ACCENT_LIGHT',
        'theme_bg': 'THEME_BG',
        'theme_bg_dark': 'THEME_BG_DARK',
        'theme_foam': 'THEME_FOAM',
        'theme_muted': 'THEME_MUTED',
        'theme_text': 'THEME_TEXT',
        'theme_border': 'THEME_BORDER',
        'theme_border_strong': 'THEME_BORDER_STRONG',
        'host_ip': 'HOST_IP',
    }

    import os
    import sys
    if getattr(sys, 'frozen', False):
        config_path = os.path.join(os.path.dirname(sys.executable), 'config.py')
    else:
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.py')

    with open(config_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    import re
    updated_lines = []
    for line in lines:
        updated = False
        for key, var_name in config_map.items():
            if key not in data:
                continue
            if not re.match(rf'^\s*{re.escape(var_name)}\s*=', line):
                continue

            value = data[key]
            formatted_value = repr(value)
            updated_lines.append(f'{var_name} = {formatted_value}\n')
            updated = True
            break
        if not updated:
            updated_lines.append(line)

    with open(config_path, 'w', encoding='utf-8') as f:
        f.writelines(updated_lines)

    # Reload module so admin UI immediately reflects saved settings (app restart may still be required elsewhere)
    import importlib
    import config as _cfg
    importlib.invalidate_caches()
    importlib.reload(_cfg)

    return jsonify({'message': 'Configuration updated.'})


# ── Menu Item Config endpoints ─────────────────────────────────────────────────

def _config_dict(c):
    import json as _json
    return {
        'id': c.id,
        'menu_item_id': c.menu_item_id,
        'group_name': c.group_name,
        'options': [({'label': o, 'extra': 0.0} if isinstance(o, str) else o) for o in (_json.loads(c.options) if c.options else [])],
        'required': c.required,
        'multi_select': c.multi_select,
    }


@bp.route('/menu/<int:item_id>/configs', methods=['GET'])
def list_configs(item_id):
    err = _require_admin()
    if err:
        return err
    db.get_or_404(MenuItem, item_id)
    configs = MenuItemConfig.query.filter_by(menu_item_id=item_id).all()
    return jsonify([_config_dict(c) for c in configs])


@bp.route('/menu/<int:item_id>/configs', methods=['POST'])
def add_config(item_id):
    import json as _json
    err = _require_admin()
    if err:
        return err
    db.get_or_404(MenuItem, item_id)
    data = request.get_json(force=True, silent=True) or {}
    group_name = (data.get('group_name') or '').strip()
    options = data.get('options') or []
    if not group_name or not options:
        return jsonify({'error': 'group_name and options required'}), 400
    c = MenuItemConfig(
        menu_item_id=item_id,
        group_name=group_name,
        options=_json.dumps([{'label': (o['label'] if isinstance(o, dict) else str(o)).strip(), 'extra': float(o.get('extra') or 0) if isinstance(o, dict) else 0.0} for o in options if (o['label'] if isinstance(o, dict) else str(o)).strip()]),
        required=bool(data.get('required', True)),
        multi_select=bool(data.get('multi_select', False)),
    )
    db.session.add(c)
    db.session.commit()
    try:
        from routes.menu import _invalidate_menu_cache as _invalidate_public_menu_cache
        _invalidate_public_menu_cache()
    except Exception:
        pass
    try:
        from app import push_pos_event
        push_pos_event({'type': 'menu_update', 'reason': 'menu_config_created', 'ts': int(datetime.now(timezone.utc).timestamp())})
    except Exception:
        pass
    return jsonify(_config_dict(c)), 201


@bp.route('/menu/<int:item_id>/configs/<int:config_id>', methods=['PUT'])
def update_config(item_id, config_id):
    import json as _json
    err = _require_admin()
    if err:
        return err
    c = db.get_or_404(MenuItemConfig, config_id)
    if c.menu_item_id != item_id:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json(force=True, silent=True) or {}
    if 'group_name' in data:
        c.group_name = data['group_name'].strip()
    if 'options' in data:
        c.options = _json.dumps([{'label': (o['label'] if isinstance(o, dict) else str(o)).strip(), 'extra': float(o.get('extra') or 0) if isinstance(o, dict) else 0.0} for o in data['options'] if (o['label'] if isinstance(o, dict) else str(o)).strip()])
    if 'required' in data:
        c.required = bool(data['required'])
    if 'multi_select' in data:
        c.multi_select = bool(data['multi_select'])
    db.session.commit()
    try:
        from routes.menu import _invalidate_menu_cache as _invalidate_public_menu_cache
        _invalidate_public_menu_cache()
    except Exception:
        pass
    try:
        from app import push_pos_event
        push_pos_event({'type': 'menu_update', 'reason': 'menu_config_updated', 'ts': int(datetime.now(timezone.utc).timestamp())})
    except Exception:
        pass
    return jsonify(_config_dict(c))


@bp.route('/menu/<int:item_id>/configs/<int:config_id>', methods=['DELETE'])
def delete_config(item_id, config_id):
    err = _require_admin()
    if err:
        return err
    c = db.get_or_404(MenuItemConfig, config_id)
    if c.menu_item_id != item_id:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(c)
    db.session.commit()
    try:
        from routes.menu import _invalidate_menu_cache as _invalidate_public_menu_cache
        _invalidate_public_menu_cache()
    except Exception:
        pass
    try:
        from app import push_pos_event
        push_pos_event({'type': 'menu_update', 'reason': 'menu_config_deleted', 'ts': int(datetime.now(timezone.utc).timestamp())})
    except Exception:
        pass
    return jsonify({'message': 'deleted'})


# ── Staff CRUD ────────────────────────────────────────────────────────────────

def _normalize_allowed_tables(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    nums = [int(x) for x in re.findall(r'\d+', s)]
    seen = set()
    out = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return ','.join(str(n) for n in out) if out else None


def _staff_dict(s):
    return {
        'id': s.id,
        'name': s.name,
        'role': s.role,
        'active': s.active,
        'allowed_tables': s.allowed_tables,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    }


@bp.route('/staff', methods=['GET'])
def list_staff():
    err = _require_admin()
    if err:
        return err
    rows = Staff.query.order_by(Staff.name.asc()).all()
    return jsonify([_staff_dict(s) for s in rows])


@bp.route('/staff', methods=['POST'])
def create_staff():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    pin = str(data.get('pin') or '').strip()
    if not name or not pin:
        return jsonify({'error': 'name and pin required'}), 400
    if not pin.isdigit() or len(pin) != 6:
        return jsonify({'error': 'PIN must be exactly 6 digits'}), 400
    s = Staff(
        name=name,
        pin_hash=generate_password_hash(pin),
        role=(data.get('role') or 'captain').strip(),
        active=bool(data.get('active', True)),
        allowed_tables=_normalize_allowed_tables(data.get('allowed_tables')),
    )
    db.session.add(s)
    db.session.commit()
    return jsonify(_staff_dict(s)), 201


@bp.route('/staff/<int:staff_id>', methods=['PUT'])
def update_staff(staff_id):
    err = _require_admin()
    if err:
        return err
    s = db.get_or_404(Staff, staff_id)
    data = request.get_json(force=True, silent=True) or {}
    if 'name' in data:
        s.name = (data['name'] or '').strip() or s.name
    if 'role' in data:
        s.role = (data['role'] or 'captain').strip()
    if 'active' in data:
        s.active = bool(data['active'])
    if 'allowed_tables' in data:
        s.allowed_tables = _normalize_allowed_tables(data.get('allowed_tables'))
    pin = str(data.get('pin') or '').strip()
    if pin:
        if not pin.isdigit() or len(pin) != 6:
            return jsonify({'error': 'PIN must be exactly 6 digits'}), 400
        s.pin_hash = generate_password_hash(pin)
    db.session.commit()
    return jsonify(_staff_dict(s))


@bp.route('/staff/<int:staff_id>', methods=['DELETE'])
def delete_staff(staff_id):
    err = _require_admin()
    if err:
        return err
    s = db.get_or_404(Staff, staff_id)
    s.active = False
    db.session.commit()
    return jsonify({'message': 'deactivated'})


# ── Analytics endpoints ───────────────────────────────────────────────────────

@bp.route('/stats/detailed', methods=['GET'])
def stats_detailed():
    """Gross/net/tax/tips/AOV/discounts for a given date."""
    err = _require_admin()
    if err:
        return err
    from routes.bills import _rounded_rupee_amount
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)
    payload, all_rows, valid_rows = _history_sale_payload(
        from_date=day_start.date().isoformat(),
        to_date=day_start.date().isoformat(),
    )

    gross = round(sum(_rounded_rupee_amount((row or {}).get('gross_subtotal') or 0) for row in valid_rows), 2)
    net = round(sum(_rounded_rupee_amount((row or {}).get('payable_amount') or 0) for row in valid_rows), 2)
    total_tax = round(sum(_rounded_rupee_amount((row or {}).get('tax_amount') or 0) for row in valid_rows), 2)
    total_discounts = round(sum(_rounded_rupee_amount((row or {}).get('total_discount') or 0) for row in valid_rows), 2)
    comp_value = round(sum(_rounded_rupee_amount((row or {}).get('payable_amount') or (row or {}).get('amount') or 0) for row in all_rows if row.get('is_complementary')), 2)
    bill_count = len(valid_rows)
    item_count = sum(
        int(float((item or {}).get('quantity') or 0))
        for row in valid_rows
        for item in ((row or {}).get('items') or [])
    )

    settled_bills = Bill.query.filter(
        Bill.settled_at.isnot(None),
        Bill.settled_at >= day_start,
        Bill.settled_at < day_end,
        Bill.is_cancelled.is_(False),
    ).all()
    total_waived = round(sum(float(b.waived_off_amount or 0) for b in settled_bills), 2)
    aov = round(net / bill_count, 2) if bill_count else 0.0

    # Yesterday comparison
    prev_start = day_start - timedelta(days=1)
    prev_end = day_start
    _, _, prev_valid_rows = _history_sale_payload(
        from_date=prev_start.date().isoformat(),
        to_date=prev_start.date().isoformat(),
    )
    prev_net = round(sum(_rounded_rupee_amount((row or {}).get('payable_amount') or 0) for row in prev_valid_rows), 2)

    # Last 7 days vs previous 7 days
    w0_start = day_start - timedelta(days=6)
    w1_start = day_start - timedelta(days=13)
    _, _, this_week_rows = _history_sale_payload(
        from_date=w0_start.date().isoformat(),
        to_date=day_start.date().isoformat(),
    )
    _, _, last_week_rows = _history_sale_payload(
        from_date=w1_start.date().isoformat(),
        to_date=(w0_start - timedelta(days=1)).date().isoformat(),
    )
    this_week = round(sum(_rounded_rupee_amount((row or {}).get('payable_amount') or 0) for row in this_week_rows), 2)
    last_week = round(sum(_rounded_rupee_amount((row or {}).get('payable_amount') or 0) for row in last_week_rows), 2)

    return jsonify({
        'date': day_start.date().isoformat(),
        'gross': round(gross, 2),
        'net': round(net, 2),
        'total_tax': round(total_tax, 2),
        'total_discounts': round(total_discounts, 2),
        'total_waived': round(total_waived, 2),
        'comp_value': round(comp_value, 2),
        'bill_count': bill_count,
        'item_count': item_count,
        'aov': aov,
        'vs_yesterday': {
            'net': round(float(prev_net), 2),
            'diff': round(net - float(prev_net), 2),
            'pct': round((net - float(prev_net)) / float(prev_net) * 100, 1) if prev_net else None,
        },
        'vs_last_week': {
            'this_week': round(float(this_week), 2),
            'last_week': round(float(last_week), 2),
            'diff': round(float(this_week) - float(last_week), 2),
            'pct': round((float(this_week) - float(last_week)) / float(last_week) * 100, 1) if last_week else None,
        },
    })


@bp.route('/stats/hourly', methods=['GET'])
def stats_hourly():
    """24-slot revenue + order count breakdown by hour."""
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)

    rows = db.session.query(
        func.extract('hour', Bill.settled_at).label('hr'),
        func.coalesce(func.sum(Bill.amount), 0.0),
        func.count(Bill.id),
    ).filter(
        Bill.settled_at.isnot(None),
        Bill.settled_at >= day_start,
        Bill.settled_at < day_end,
        Bill.is_cancelled.is_(False),
    ).group_by('hr').all()

    slots = [{'hour': h, 'label': f'{h:02d}:00', 'revenue': 0.0, 'orders': 0} for h in range(24)]
    for hr, rev, cnt in rows:
        h = int(hr)
        slots[h]['revenue'] = round(float(rev), 2)
        slots[h]['orders'] = int(cnt)

    max_rev = max((s['revenue'] for s in slots), default=1) or 1
    for s in slots:
        s['pct'] = round(s['revenue'] / max_rev * 100, 1)

    busiest = max(slots, key=lambda s: s['revenue'])
    slowest = min((s for s in slots if s['revenue'] > 0), key=lambda s: s['revenue'], default=None)

    return jsonify({
        'slots': slots,
        'busiest_hour': busiest['label'],
        'busiest_revenue': busiest['revenue'],
        'slowest_hour': slowest['label'] if slowest else None,
    })


@bp.route('/stats/menu', methods=['GET'])
def stats_menu():
    """All items: qty, revenue, voids + category totals."""
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)

    sold_rows = (
        db.session.query(
            MenuItem.id, MenuItem.name, MenuItem.category,
            func.coalesce(func.sum(OrderItem.quantity), 0).label('qty'),
            func.coalesce(func.sum(
                (OrderItem.quantity) * (MenuItem.price + OrderItem.config_price_extra)
            ), 0.0).label('revenue'),
        )
        .join(OrderItem, OrderItem.menu_item_id == MenuItem.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(
            Order.created_at >= day_start,
            Order.created_at < day_end,
            OrderItem.voided.is_(False),
        )
        .group_by(MenuItem.id)
        .all()
    )

    void_rows = (
        db.session.query(
            MenuItem.id,
            func.count(OrderItem.id).label('voids'),
        )
        .join(OrderItem, OrderItem.menu_item_id == MenuItem.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(
            Order.created_at >= day_start,
            Order.created_at < day_end,
            OrderItem.voided.is_(True),
        )
        .group_by(MenuItem.id)
        .all()
    )
    void_map = {r[0]: int(r[1]) for r in void_rows}

    items = []
    cat_totals = {}
    for item_id, name, cat, qty, rev in sold_rows:
        qty = int(qty)
        rev = round(float(rev), 2)
        voids = void_map.get(item_id, 0)
        items.append({'id': item_id, 'name': name, 'category': cat, 'qty': qty, 'revenue': rev, 'voids': voids})
        cat_totals[cat] = cat_totals.get(cat, {'qty': 0, 'revenue': 0.0})
        cat_totals[cat]['qty'] += qty
        cat_totals[cat]['revenue'] += rev

    # Zero-sellers today
    sold_ids = {r[0] for r in sold_rows}
    zero = MenuItem.query.filter(MenuItem.available.is_(True), MenuItem.id.notin_(sold_ids)).all()
    zero_items = [{'id': m.id, 'name': m.name, 'category': m.category} for m in zero]

    total_rev = sum(i['revenue'] for i in items) or 1
    categories = [
        {'category': c, 'qty': v['qty'], 'revenue': round(v['revenue'], 2),
         'pct': round(v['revenue'] / total_rev * 100, 1)}
        for c, v in sorted(cat_totals.items(), key=lambda x: -x[1]['revenue'])
    ]

    return jsonify({
        'items': sorted(items, key=lambda i: -i['revenue']),
        'categories': categories,
        'zero_sellers': zero_items,
    })


@bp.route('/stats/tables', methods=['GET'])
def stats_tables():
    """Per-table revenue, session count, avg TAT."""
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)

    rows = (
        db.session.query(
            Bill.table_number,
            func.coalesce(func.sum(Bill.amount), 0.0).label('revenue'),
            func.count(Bill.id).label('sessions'),
            func.min(RestSession.created_at).label('first_open'),
        )
        .join(RestSession, RestSession.id == Bill.session_id)
        .filter(
            Bill.session_type == 'table',
            Bill.settled_at.isnot(None),
            Bill.settled_at >= day_start,
            Bill.settled_at < day_end,
            Bill.is_cancelled.is_(False),
            Bill.table_number.isnot(None),
        )
        .group_by(Bill.table_number)
        .all()
    )

    # Avg TAT per table
    tat_rows = (
        db.session.query(
            Bill.table_number,
            RestSession.created_at,
            Bill.settled_at,
        )
        .join(RestSession, RestSession.id == Bill.session_id)
        .filter(
            Bill.session_type == 'table',
            Bill.settled_at.isnot(None),
            Bill.settled_at >= day_start,
            Bill.settled_at < day_end,
            Bill.is_cancelled.is_(False),
        )
        .all()
    )
    tat_map = {}
    for tnum, created, settled in tat_rows:
        if created and settled:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if settled.tzinfo is None:
                settled = settled.replace(tzinfo=timezone.utc)
            mins = (settled - created).total_seconds() / 60
            if tnum not in tat_map:
                tat_map[tnum] = []
            tat_map[tnum].append(mins)

    tables = []
    for tnum, revenue, sessions, _ in rows:
        tats = tat_map.get(tnum, [])
        avg_tat = round(sum(tats) / len(tats), 1) if tats else 0
        tables.append({
            'table': tnum,
            'revenue': round(float(revenue), 2),
            'sessions': int(sessions),
            'avg_tat_min': avg_tat,
        })

    return jsonify(sorted(tables, key=lambda t: -t['revenue']))


@bp.route('/stats/customers', methods=['GET'])
def stats_customers():
    """New vs returning, top spenders today, churn list."""
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)

    # Sessions with a customer today
    today_sessions = (
        RestSession.query
        .filter(
            RestSession.created_at >= day_start,
            RestSession.created_at < day_end,
            RestSession.customer_id.isnot(None),
        ).all()
    )

    new_count = 0
    returning_count = 0
    for sess in today_sessions:
        cust = sess.customer
        if cust and cust.total_visits <= 1:
            new_count += 1
        else:
            returning_count += 1

    # Top spenders today
    top_rows = (
        db.session.query(
            Customer.id, Customer.name, Customer.phone,
            func.coalesce(func.sum(Bill.amount), 0.0).label('spend'),
        )
        .join(RestSession, RestSession.customer_id == Customer.id)
        .join(Bill, Bill.session_id == RestSession.id)
        .filter(
            Bill.settled_at.isnot(None),
            Bill.settled_at >= day_start,
            Bill.settled_at < day_end,
            Bill.is_cancelled.is_(False),
        )
        .group_by(Customer.id)
        .order_by(func.sum(Bill.amount).desc())
        .limit(10)
        .all()
    )
    top_spenders = [
        {'id': r[0], 'name': r[1] or 'Guest', 'phone': r[2], 'spend': round(float(r[3]), 2)}
        for r in top_rows
    ]

    # Churn: customers not seen in 30 or 60 days
    now = datetime.now(timezone.utc)
    churn_30 = Customer.query.filter(
        Customer.last_seen.isnot(None),
        Customer.last_seen < now - timedelta(days=30),
        Customer.last_seen >= now - timedelta(days=60),
    ).count()
    churn_60 = Customer.query.filter(
        Customer.last_seen.isnot(None),
        Customer.last_seen < now - timedelta(days=60),
        Customer.last_seen >= now - timedelta(days=90),
    ).count()

    total_customers = Customer.query.count()

    return jsonify({
        'date': day_start.date().isoformat(),
        'new': new_count,
        'returning': returning_count,
        'total_customers': total_customers,
        'top_spenders': top_spenders,
        'churn': {'days_30': churn_30, 'days_60': churn_60},
    })


@bp.route('/stats/trends', methods=['GET'])
def stats_trends():
    """30-day revenue array + period comparisons."""
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)

    days = []
    for i in range(29, -1, -1):
        d0 = day_start - timedelta(days=i)
        d1 = d0 + timedelta(days=1)
        rev = db.session.query(func.coalesce(func.sum(Bill.amount), 0.0)).filter(
            Bill.settled_at.isnot(None),
            Bill.settled_at >= d0,
            Bill.settled_at < d1,
            Bill.is_cancelled.is_(False),
            Bill.is_complementary.is_(False),
        ).scalar() or 0.0
        cnt = db.session.query(func.count(Bill.id)).filter(
            Bill.settled_at.isnot(None),
            Bill.settled_at >= d0,
            Bill.settled_at < d1,
            Bill.is_cancelled.is_(False),
        ).scalar() or 0
        days.append({'date': d0.date().isoformat(), 'revenue': round(float(rev), 2), 'orders': int(cnt)})

    best_day = max(days, key=lambda d: d['revenue']) if days else None

    return jsonify({'days': days, 'best_day': best_day})


@bp.route('/stats/staff', methods=['GET'])
def stats_staff():
    """Per-captain KOTs placed, items, tables opened, voids."""
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)

    # Orders (KOTs) placed per staff
    kot_rows = (
        db.session.query(
            Staff.id, Staff.name,
            func.count(Order.id).label('kots'),
            func.coalesce(func.sum(OrderItem.quantity), 0).label('items'),
        )
        .join(Order, Order.placed_by_staff_id == Staff.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .filter(
            Order.created_at >= day_start,
            Order.created_at < day_end,
            OrderItem.voided.is_(False),
        )
        .group_by(Staff.id)
        .all()
    )

    # Sessions opened per staff
    sess_rows = (
        db.session.query(
            Staff.id,
            func.count(RestSession.id).label('tables_opened'),
        )
        .join(RestSession, RestSession.opened_by_staff_id == Staff.id)
        .filter(
            RestSession.created_at >= day_start,
            RestSession.created_at < day_end,
        )
        .group_by(Staff.id)
        .all()
    )
    sess_map = {r[0]: int(r[1]) for r in sess_rows}

    # Voids per staff
    void_rows = (
        db.session.query(
            Staff.id,
            func.count(OrderItem.id).label('voids'),
        )
        .join(Order, Order.placed_by_staff_id == Staff.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .filter(
            Order.created_at >= day_start,
            Order.created_at < day_end,
            OrderItem.voided.is_(True),
        )
        .group_by(Staff.id)
        .all()
    )
    void_map = {r[0]: int(r[1]) for r in void_rows}

    staff_map = {r[0]: {'id': r[0], 'name': r[1], 'kots': int(r[2]), 'items': int(r[3]),
                        'tables_opened': 0, 'voids': 0} for r in kot_rows}
    for sid, tables in sess_map.items():
        if sid in staff_map:
            staff_map[sid]['tables_opened'] = tables
        else:
            s = Staff.query.get(sid)
            if s:
                staff_map[sid] = {'id': sid, 'name': s.name, 'kots': 0, 'items': 0,
                                  'tables_opened': tables, 'voids': 0}
    for sid, v in void_map.items():
        if sid in staff_map:
            staff_map[sid]['voids'] = v

    # Include all active staff even if no activity
    for s in Staff.query.filter_by(active=True).all():
        if s.id not in staff_map:
            staff_map[s.id] = {'id': s.id, 'name': s.name, 'kots': 0, 'items': 0,
                               'tables_opened': 0, 'voids': 0}

    return jsonify(sorted(staff_map.values(), key=lambda x: -x['kots']))


@bp.route('/stats/leakage-detail', methods=['GET'])
def stats_leakage_detail():
    """Full row-level detail behind the Leakage panel."""
    err = _require_admin()
    if err:
        return err
    day_start, day_end = _day_range(request.args.get('date'))

    # ── 1. Fully-cancelled KOTs (all items voided) ───────────────────────────
    # An order where EVERY item is voided (not just quantity reduced)
    from sqlalchemy import and_, not_, exists
    live_item_exists = (
        db.session.query(OrderItem.id)
        .filter(
            OrderItem.order_id == Order.id,
            OrderItem.voided.is_(False),
            OrderItem.quantity > 0
        )
        .correlate(Order)
        .exists()
    )
    cancelled_orders = (
        Order.query
        .join(RestSession, RestSession.id == Order.session_id)
        .filter(
            Order.created_at >= day_start,
            Order.created_at < day_end,
            ~live_item_exists,
        )
        .all()
    )
    cancelled_kots_detail = []
    for o in cancelled_orders:
        sess = RestSession.query.get(o.session_id)
        staff = Staff.query.get(o.placed_by_staff_id) if o.placed_by_staff_id else None
        items_list = []
        for oi in o.items:
            mi = MenuItem.query.get(oi.menu_item_id)
            voided_staff = Staff.query.get(oi.voided_by_staff_id) if oi.voided_by_staff_id else None
            items_list.append({
                'name': mi.name if mi else '?',
                'qty': oi.quantity,
                'voided_at': oi.voided_at.strftime('%H:%M') if oi.voided_at else None,
                'voided_by': voided_staff.name if voided_staff else None,
            })
        cancelled_kots_detail.append({
            'order_id': o.id,
            'table': sess.table_number if sess else None,
            'pickup_code': sess.pickup_code if sess else None,
            'time': o.created_at.strftime('%H:%M'),
            'placed_by': staff.name if staff else None,
            'items': items_list,
        })

    # ── 2. Modified KOTs (mix of voided + live items) ────────────────────────
    voided_order_ids_q = (
        db.session.query(OrderItem.order_id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(OrderItem.voided.is_(True), Order.created_at >= day_start, Order.created_at < day_end)
    )
    active_order_ids_q = (
        db.session.query(OrderItem.order_id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(OrderItem.voided.is_(False), Order.created_at >= day_start, Order.created_at < day_end)
    )
    modified_orders = (
        Order.query
        .filter(
            Order.id.in_(voided_order_ids_q),
            Order.id.in_(active_order_ids_q),
            Order.created_at >= day_start,
            Order.created_at < day_end,
        )
        .all()
    )
    modified_kots_detail = []
    for o in modified_orders:
        sess = RestSession.query.get(o.session_id)
        staff = Staff.query.get(o.placed_by_staff_id) if o.placed_by_staff_id else None
        voided = []
        live = []
        for oi in o.items:
            mi = MenuItem.query.get(oi.menu_item_id)
            name = mi.name if mi else '?'
            if oi.voided:
                voided_staff = Staff.query.get(oi.voided_by_staff_id) if oi.voided_by_staff_id else None
                voided.append({'name': name, 'qty': oi.quantity, 'voided_at': oi.voided_at.strftime('%H:%M') if oi.voided_at else None, 'voided_by': voided_staff.name if voided_staff else None})
            else:
                live.append({'name': name, 'qty': oi.quantity, 'cancelled_qty': oi.quantity_cancelled or 0})
        modified_kots_detail.append({
            'order_id': o.id,
            'table': sess.table_number if sess else None,
            'pickup_code': sess.pickup_code if sess else None,
            'time': o.created_at.strftime('%H:%M'),
            'placed_by': staff.name if staff else None,
            'cancelled_items': voided,
            'remaining_items': live,
        })

    # ── 3. Reduced-qty items (partial cancellations via Edit Qty) ────────────
    reduced_items = (
        OrderItem.query
        .join(Order, Order.id == OrderItem.order_id)
        .filter(
            OrderItem.quantity_cancelled > 0,
            OrderItem.voided.is_(False),
            OrderItem.reduced_at.isnot(None),
            OrderItem.reduced_at >= day_start,
            OrderItem.reduced_at < day_end,
        )
        .all()
    )
    reduced_detail = []
    for oi in reduced_items:
        o = Order.query.get(oi.order_id)
        sess = RestSession.query.get(o.session_id) if o else None
        staff = Staff.query.get(o.placed_by_staff_id) if o and o.placed_by_staff_id else None
        reduced_staff = Staff.query.get(oi.reduced_by_staff_id) if oi.reduced_by_staff_id else None
        mi = MenuItem.query.get(oi.menu_item_id)
        reduced_detail.append({
            'order_id': oi.order_id,
            'table': sess.table_number if sess else None,
            'pickup_code': sess.pickup_code if sess else None,
            'item': mi.name if mi else '?',
            'original_qty': oi.quantity + (oi.quantity_cancelled or 0),
            'cancelled_qty': oi.quantity_cancelled,
            'remaining_qty': oi.quantity,
            'time': oi.reduced_at.strftime('%H:%M'),
            'placed_by': staff.name if staff else None,
            'reduced_by': reduced_staff.name if reduced_staff else None,
        })

    # ── 4. Unsettled sessions (closed with orders but no bill) ───────────────
    settled_ids = db.session.query(Bill.session_id).filter(Bill.settled_at.isnot(None))
    has_orders_sub = (
        db.session.query(Order.session_id)
        .filter(Order.session_id == RestSession.id)
        .correlate(RestSession)
        .exists()
    )
    unsettled_sessions = (
        RestSession.query
        .filter(
            RestSession.status.in_(['force_closed', 'closed']),
            RestSession.closed_at >= day_start,
            RestSession.closed_at < day_end,
            has_orders_sub,
            RestSession.id.notin_(settled_ids),
        )
        .all()
    )
    unsettled_detail = []
    for sess in unsettled_sessions:
        orders_count = Order.query.filter_by(session_id=sess.id).count()
        unsettled_detail.append({
            'session_id': sess.id,
            'table': sess.table_number,
            'pickup_code': sess.pickup_code,
            'closed_at': sess.closed_at.strftime('%H:%M') if sess.closed_at else None,
            'orders_count': orders_count,
        })

    # ── 5. Waived-off bills ───────────────────────────────────────────────────
    waived_bills = (
        Bill.query
        .filter(
            Bill.created_at >= day_start,
            Bill.created_at < day_end,
            Bill.waived_off_amount > 0,
        )
        .all()
    )
    waived_financials = _admin_bill_financial_map(waived_bills)
    waived_detail = []
    for b in waived_bills:
        financials = waived_financials.get(b.id, {})
        waived_detail.append({
            'bill_id': b.id,
            'table': b.table_number,
            'pickup_code': b.pickup_code,
            'amount': round(float(financials.get('history_amount', b.amount or 0)), 2),
            'waived': round(float(b.waived_off_amount), 2),
            'time': b.created_at.strftime('%H:%M'),
            'coupon': b.coupon_code,
        })

    # ── 6. Modified bills (audit trail) ──────────────────────────────────────
    mod_rows = (
        BillModification.query
        .join(Bill, Bill.id == BillModification.bill_id)
        .filter(
            BillModification.created_at >= day_start,
            BillModification.created_at < day_end,
        )
        .order_by(BillModification.created_at)
        .all()
    )
    modified_bills_detail = []
    for m in mod_rows:
        b = Bill.query.get(m.bill_id)
        modified_bills_detail.append({
            'bill_id': m.bill_id,
            'table': b.table_number if b else None,
            'pickup_code': b.pickup_code if b else None,
            'description': m.description,
            'modified_by': m.modified_by,
            'time': m.created_at.strftime('%H:%M'),
        })

    return jsonify({
        'cancelled_kots': cancelled_kots_detail,
        'modified_kots': modified_kots_detail,
        'reduced_qty': reduced_detail,
        'unsettled': unsettled_detail,
        'waived_bills': waived_detail,
        'modified_bills': modified_bills_detail,
    })





# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLIER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

from models import Supplier, PurchaseInvoice, PurchaseInvoiceItem, InventoryWaste, Inventory


def _supplier_dict(s):
    return {
        'id': s.id,
        'name': s.name,
        'contact_person': s.contact_person,
        'phone': s.phone,
        'email': s.email,
        'address': s.address,
        'gstin': s.gstin,
        'payment_terms_days': s.payment_terms_days,
        'is_active': s.is_active,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    }


@bp.route('/suppliers', methods=['GET'])
def list_suppliers():
    err = _require_admin()
    if err:
        return err
    active_only = request.args.get('active_only', 'false').lower() == 'true'
    q = Supplier.query.order_by(Supplier.name.asc())
    if active_only:
        q = q.filter(Supplier.is_active.is_(True))
    return jsonify([_supplier_dict(s) for s in q.all()])


@bp.route('/suppliers', methods=['POST'])
def create_supplier():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    s = Supplier(
        name=name,
        contact_person=(data.get('contact_person') or '').strip() or None,
        phone=(data.get('phone') or '').strip() or None,
        email=(data.get('email') or '').strip() or None,
        address=(data.get('address') or '').strip() or None,
        gstin=(data.get('gstin') or '').strip() or None,
        payment_terms_days=int(data.get('payment_terms_days', 0)),
        is_active=bool(data.get('is_active', True)),
    )
    db.session.add(s)
    db.session.commit()
    return jsonify(_supplier_dict(s)), 201


@bp.route('/suppliers/<int:supplier_id>', methods=['PUT'])
def update_supplier(supplier_id):
    err = _require_admin()
    if err:
        return err
    s = db.get_or_404(Supplier, supplier_id)
    data = request.get_json(force=True, silent=True) or {}
    if 'name' in data:
        s.name = data['name'].strip() or s.name
    if 'contact_person' in data:
        s.contact_person = data['contact_person'].strip() or None
    if 'phone' in data:
        s.phone = data['phone'].strip() or None
    if 'email' in data:
        s.email = data['email'].strip() or None
    if 'address' in data:
        s.address = data['address'].strip() or None
    if 'gstin' in data:
        s.gstin = data['gstin'].strip() or None
    if 'payment_terms_days' in data:
        s.payment_terms_days = int(data['payment_terms_days'])
    if 'is_active' in data:
        s.is_active = bool(data['is_active'])
    db.session.commit()
    return jsonify(_supplier_dict(s))


@bp.route('/suppliers/<int:supplier_id>', methods=['DELETE'])
def delete_supplier(supplier_id):
    err = _require_admin()
    if err:
        return err
    s = db.get_or_404(Supplier, supplier_id)
    # Check for existing invoices
    if s.invoices.count() > 0:
        return jsonify({'error': 'Cannot delete supplier with purchase history'}), 400
    db.session.delete(s)
    db.session.commit()
    return jsonify({'message': 'deleted'})


# ═══════════════════════════════════════════════════════════════════════════════
# PURCHASE INVOICE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _purchase_invoice_item_dict(pi):
    return {
        'id': pi.id,
        'inventory_id': pi.inventory_id,
        'inventory_name': pi.inventory_item.name if pi.inventory_item else None,
        'quantity': pi.quantity,
        'unit_price': pi.unit_price,
        'gst_rate': pi.gst_rate,
        'total': pi.total,
    }


def _purchase_invoice_dict(inv):
    return {
        'id': inv.id,
        'supplier_id': inv.supplier_id,
        'supplier_name': inv.supplier.name if inv.supplier else None,
        'invoice_number': inv.invoice_number,
        'invoice_date': inv.invoice_date.isoformat() if inv.invoice_date else None,
        'total_amount': inv.total_amount,
        'gst_amount': inv.gst_amount,
        'status': inv.status,
        'paid_at': inv.paid_at.isoformat() if inv.paid_at else None,
        'notes': inv.notes,
        'created_at': inv.created_at.isoformat() if inv.created_at else None,
        'items': [_purchase_invoice_item_dict(i) for i in inv.items],
    }


@bp.route('/purchase-invoices', methods=['GET'])
def list_purchase_invoices():
    err = _require_admin()
    if err:
        return err
    status_filter = request.args.get('status')
    supplier_id = request.args.get('supplier_id', type=int)
    q = PurchaseInvoice.query.order_by(PurchaseInvoice.created_at.desc())
    if status_filter:
        q = q.filter(PurchaseInvoice.status == status_filter)
    if supplier_id:
        q = q.filter(PurchaseInvoice.supplier_id == supplier_id)
    return jsonify([_purchase_invoice_dict(i) for i in q.all()])


@bp.route('/purchase-invoices', methods=['POST'])
def create_purchase_invoice():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    supplier_id = data.get('supplier_id')
    invoice_number = (data.get('invoice_number') or '').strip()
    if not supplier_id or not invoice_number:
        return jsonify({'error': 'supplier_id and invoice_number required'}), 400
    
    from datetime import date
    invoice_date = data.get('invoice_date')
    if invoice_date:
        try:
            invoice_date = date.fromisoformat(invoice_date)
        except ValueError:
            invoice_date = date.today()
    else:
        invoice_date = date.today()
    
    inv = PurchaseInvoice(
        supplier_id=int(supplier_id),
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        status='draft',
        notes=(data.get('notes') or '').strip() or None,
    )
    db.session.add(inv)
    db.session.commit()
    return jsonify(_purchase_invoice_dict(inv)), 201


@bp.route('/purchase-invoices/<int:invoice_id>/items', methods=['POST'])
def add_invoice_item(invoice_id):
    err = _require_admin()
    if err:
        return err
    inv = db.get_or_404(PurchaseInvoice, invoice_id)
    if inv.status != 'draft':
        return jsonify({'error': 'Cannot modify confirmed invoice'}), 400
    
    data = request.get_json(force=True, silent=True) or {}
    inventory_id = data.get('inventory_id')
    quantity = float(data.get('quantity', 0))
    unit_price = float(data.get('unit_price', 0))
    gst_rate = float(data.get('gst_rate', 0))
    
    if not inventory_id or quantity <= 0 or unit_price < 0:
        return jsonify({'error': 'inventory_id, positive quantity, and unit_price required'}), 400
    
    total = round(quantity * unit_price * (1 + gst_rate/100), 2)
    item = PurchaseInvoiceItem(
        purchase_invoice_id=invoice_id,
        inventory_id=int(inventory_id),
        quantity=quantity,
        quantity_ordered=quantity,
        unit_price=unit_price,
        gst_rate=gst_rate,
        total=total,
    )
    db.session.add(item)
    
    # Update invoice totals
    inv.total_amount = sum(i.total for i in inv.items) + total
    inv.gst_amount = sum(i.total * i.gst_rate / (100 + i.gst_rate) for i in inv.items)
    
    db.session.commit()
    return jsonify(_purchase_invoice_item_dict(item)), 201


@bp.route('/purchase-invoices/<int:invoice_id>/confirm', methods=['POST'])
def confirm_purchase_invoice(invoice_id):
    """Confirm invoice and add stock to inventory."""
    err = _require_admin()
    if err:
        return err
    inv = db.get_or_404(PurchaseInvoice, invoice_id)
    if inv.status != 'draft':
        return jsonify({'error': 'Invoice already confirmed'}), 400
    
    # Add stock and update costs
    for item in inv.items:
        inv_item = item.inventory_item
        if inv_item:
            # Set received quantity (defaults to ordered in admin confirm)
            item.quantity_received = item.quantity_ordered
            # Update stock level
            inv_item.stock_level += item.quantity_received
            # Update last purchase price
            inv_item.last_purchase_price = item.unit_price
            # Recalculate weighted average cost
            total_value = (inv_item.stock_level - item.quantity_received) * inv_item.average_unit_cost
            total_value += item.quantity_received * item.unit_price
            if inv_item.stock_level > 0:
                inv_item.average_unit_cost = round(total_value / inv_item.stock_level, 2)
    
    inv.status = 'received'
    db.session.commit()
    
    # Recalculate all menu item costs
    from models import update_menu_item_costs
    update_menu_item_costs(db.session)
    
    return jsonify(_purchase_invoice_dict(inv))


@bp.route('/purchase-invoices/<int:invoice_id>/pay', methods=['POST'])
def mark_invoice_paid(invoice_id):
    err = _require_admin()
    if err:
        return err
    inv = db.get_or_404(PurchaseInvoice, invoice_id)
    if inv.status == 'draft':
        return jsonify({'error': 'Confirm invoice before marking paid'}), 400
    if inv.status == 'paid':
        return jsonify({'error': 'Already paid'}), 400
    
    inv.status = 'paid'
    inv.paid_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(_purchase_invoice_dict(inv))


@bp.route('/purchase-invoices/<int:invoice_id>', methods=['DELETE'])
def delete_purchase_invoice(invoice_id):
    err = _require_admin()
    if err:
        return err
    inv = db.get_or_404(PurchaseInvoice, invoice_id)
    if inv.status != 'draft':
        return jsonify({'error': 'Only draft invoices can be deleted'}), 400
    db.session.delete(inv)
    db.session.commit()
    return jsonify({'message': 'deleted'})


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLIER PAYABLES REPORT
# ═══════════════════════════════════════════════════════════════════════════════

@bp.route('/suppliers/payables', methods=['GET'])
def supplier_payables():
    """Get total payable amount per supplier (confirmed but unpaid invoices)."""
    err = _require_admin()
    if err:
        return err
    
    from sqlalchemy import func
    results = (
        db.session.query(
            Supplier.id,
            Supplier.name,
            func.coalesce(func.sum(PurchaseInvoice.total_amount), 0.0).label('total_payable')
        )
        .outerjoin(PurchaseInvoice, 
                   (PurchaseInvoice.supplier_id == Supplier.id) & 
                   (PurchaseInvoice.status == 'confirmed'))
        .filter(Supplier.is_active.is_(True))
        .group_by(Supplier.id, Supplier.name)
        .all()
    )
    
    return jsonify([
        {'supplier_id': r[0], 'supplier_name': r[1], 'total_payable': float(r[2])}
        for r in results
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# INVENTORY WASTE TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

def _waste_dict(w):
    return {
        'id': w.id,
        'inventory_id': w.inventory_id,
        'inventory_name': w.inventory_item.name if w.inventory_item else None,
        'quantity_wasted': w.quantity_wasted,
        'reason': w.reason,
        'noted_by': w.noted_by,
        'notes': w.notes,
        'created_at': w.created_at.isoformat() if w.created_at else None,
    }


@bp.route('/inventory-waste', methods=['GET'])
def list_inventory_waste():
    err = _require_admin()
    if err:
        return err
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)
    rows = InventoryWaste.query.filter(
        InventoryWaste.created_at >= day_start,
        InventoryWaste.created_at < day_end,
    ).order_by(InventoryWaste.created_at.desc()).all()
    return jsonify([_waste_dict(r) for r in rows])


@bp.route('/inventory-waste', methods=['POST'])
def record_inventory_waste():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    inventory_id = data.get('inventory_id')
    quantity = float(data.get('quantity_wasted', 0))
    
    if not inventory_id or quantity <= 0:
        return jsonify({'error': 'inventory_id and positive quantity_wasted required'}), 400
    
    inv = db.get_or_404(Inventory, int(inventory_id))
    if inv.stock_level < quantity:
        return jsonify({'error': 'Cannot waste more than current stock'}), 400
    
    # Deduct from stock
    inv.stock_level -= quantity
    
    waste = InventoryWaste(
        inventory_id=int(inventory_id),
        quantity_wasted=quantity,
        reason=(data.get('reason') or 'other').strip() or 'other',
        noted_by=(data.get('noted_by') or '').strip() or None,
        notes=(data.get('notes') or '').strip() or None,
    )
    db.session.add(waste)
    db.session.commit()
    return jsonify(_waste_dict(waste)), 201


# ═══════════════════════════════════════════════════════════════════════════════
# EXPIRY ALERTS
# ═══════════════════════════════════════════════════════════════════════════════

@bp.route('/inventory/expiry-alerts', methods=['GET'])
def inventory_expiry_alerts():
    """Get inventory items expiring in next 7 or 30 days."""
    err = _require_admin()
    if err:
        return err
    
    from datetime import date, timedelta
    today = date.today()
    days_7 = today + timedelta(days=7)
    days_30 = today + timedelta(days=30)
    
    expiring_7 = Inventory.query.filter(
        Inventory.expiry_date <= days_7,
        Inventory.expiry_date >= today,
        Inventory.stock_level > 0,
    ).order_by(Inventory.expiry_date.asc()).all()
    
    expiring_30 = Inventory.query.filter(
        Inventory.expiry_date <= days_30,
        Inventory.expiry_date > days_7,
        Inventory.stock_level > 0,
    ).order_by(Inventory.expiry_date.asc()).all()
    
    def _inv_alert_dict(inv):
        return {
            'id': inv.id,
            'name': inv.name,
            'stock_level': inv.stock_level,
            'unit': inv.unit,
            'expiry_date': inv.expiry_date.isoformat() if inv.expiry_date else None,
            'days_until_expiry': (inv.expiry_date - today).days if inv.expiry_date else None,
        }
    
    return jsonify({
        'expiring_7_days': [_inv_alert_dict(i) for i in expiring_7],
        'expiring_30_days': [_inv_alert_dict(i) for i in expiring_30],
    })


# ═══════════════════════════════════════════════════════════════════════════════
# MENU ENGINEERING REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

@bp.route('/menu-engineering', methods=['GET'])
def menu_engineering_report():
    """
    Star/Dog/Plow-horse/Puzzle analysis based on margin % and popularity.
    Also includes category performance.
    """
    err = _require_admin()
    if err:
        return err
    
    from sqlalchemy import func, case
    from datetime import datetime, timedelta, timezone
    
    # Date range - accept start/end or fall back to days
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    
    if start_str and end_str:
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            end_date = datetime.strptime(end_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            days = (end_date - start_date).days + 1
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    else:
        days = int(request.args.get('days', 30))
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)
    
    # Get sales data per menu item
    sales_data = (
        db.session.query(
            MenuItem.id,
            MenuItem.name,
            MenuItem.price,
            MenuItem.cost_price,
            MenuItem.category,
            func.coalesce(func.sum(OrderItem.quantity), 0).label('total_sold'),
            func.coalesce(func.sum(OrderItem.quantity * MenuItem.price), 0).label('total_revenue'),
        )
        .outerjoin(OrderItem, (OrderItem.menu_item_id == MenuItem.id) & 
                            (OrderItem.voided.is_(False)) &
                            (OrderItem.order.has(Order.created_at >= start_date)) &
                            (OrderItem.order.has(Order.created_at <= end_date)))
        .group_by(MenuItem.id, MenuItem.name, MenuItem.price, MenuItem.cost_price, MenuItem.category)
        .all()
    )
    
    # Calculate metrics
    items = []
    total_revenue_all = sum(r.total_revenue for r in sales_data)
    
    for r in sales_data:
        cost_price = r.cost_price or 0
        margin = r.price - cost_price if r.price else 0
        margin_pct = (margin / r.price * 100) if r.price else 0
        popularity = (r.total_revenue / total_revenue_all * 100) if total_revenue_all else 0
        
        # Classification
        if margin_pct >= 60 and popularity >= 10:
            classification = 'star'
        elif margin_pct < 60 and popularity >= 10:
            classification = 'plow_horse'
        elif margin_pct >= 60 and popularity < 10:
            classification = 'puzzle'
        else:
            classification = 'dog'
        
        items.append({
            'id': r.id,
            'name': r.name,
            'category': r.category,
            'price': r.price,
            'cost_price': cost_price,
            'margin': round(margin, 2),
            'margin_pct': round(margin_pct, 1),
            'total_sold': int(r.total_sold),
            'total_revenue': round(r.total_revenue, 2),
            'popularity_pct': round(popularity, 1),
            'classification': classification,
        })
    
    # Category summary
    categories = {}
    for item in items:
        cat = item['category']
        if cat not in categories:
            categories[cat] = {'revenue': 0, 'items': 0, 'sold': 0}
        categories[cat]['revenue'] += item['total_revenue']
        categories[cat]['items'] += 1
        categories[cat]['sold'] += item['total_sold']
    
    category_list = [
        {
            'name': cat,
            'total_revenue': round(data['revenue'], 2),
            'item_count': data['items'],
            'units_sold': data['sold'],
            'revenue_pct': round(data['revenue'] / total_revenue_all * 100, 1) if total_revenue_all else 0,
        }
        for cat, data in categories.items()
    ]
    category_list.sort(key=lambda x: x['total_revenue'], reverse=True)
    
    return jsonify({
        'period_days': days,
        'total_revenue': round(total_revenue_all, 2),
        'items': items,
        'categories': category_list,
        'summary': {
            'stars': len([i for i in items if i['classification'] == 'star']),
            'plow_horses': len([i for i in items if i['classification'] == 'plow_horse']),
            'puzzles': len([i for i in items if i['classification'] == 'puzzle']),
            'dogs': len([i for i in items if i['classification'] == 'dog']),
        }
    })


# ═══════════════════════════════════════════════════════════════════════════════
# GST REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

@bp.route('/gst/summary', methods=['GET'])
def gst_summary():
    """Monthly GST summary for filing GSTR-1/GSTR-3B."""
    err = _require_admin()
    if err:
        return err
    
    month_param = request.args.get('month')
    if month_param and '-' in month_param:
        try:
            y_str, m_str = month_param.split('-')
            year = int(y_str)
            month = int(m_str)
        except ValueError:
            year = datetime.now(timezone.utc).year
            month = datetime.now(timezone.utc).month
    else:
        year = int(request.args.get('year', datetime.now(timezone.utc).year))
        month = int(request.args.get('month', datetime.now(timezone.utc).month))
    
    from datetime import date
    from calendar import monthrange
    
    _, last_day = monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)
    
    # Convert to datetime for query
    day_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    day_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)
    
    # Get all settled bills in period
    bills = Bill.query.filter(
        Bill.settled_at.isnot(None),
        Bill.settled_at >= day_start,
        Bill.settled_at < day_end,
        Bill.is_cancelled.is_(False),
    ).all()
    
    # Aggregate by HSN
    hsn_summary = {}
    total_taxable = 0
    total_cgst = 0
    total_sgst = 0
    total_igst = 0
    
    for bill in bills:
        # Parse items to get HSN codes
        import json as _json
        items = _json.loads(bill.items_snapshot) if bill.items_snapshot else []
        
        # Use bill-level GST if item-level not available
        bill_hsn = bill.hsn_code or '996331'
        bill_gst_rate = bill.gst_rate or 5.0
        
        taxable_amount = bill.amount or 0
        cgst = bill.cgst_amount or (taxable_amount * bill_gst_rate / 200)
        sgst = bill.sgst_amount or (taxable_amount * bill_gst_rate / 200)
        igst = bill.igst_amount or 0
        
        if bill_hsn not in hsn_summary:
            hsn_summary[bill_hsn] = {
                'hsn_code': bill_hsn,
                'gst_rate': bill_gst_rate,
                'taxable_value': 0,
                'cgst': 0,
                'sgst': 0,
                'igst': 0,
                'invoice_count': 0,
            }
        
        hsn_summary[bill_hsn]['taxable_value'] += taxable_amount
        hsn_summary[bill_hsn]['cgst'] += cgst
        hsn_summary[bill_hsn]['sgst'] += sgst
        hsn_summary[bill_hsn]['igst'] += igst
        hsn_summary[bill_hsn]['invoice_count'] += 1
        
        total_taxable += taxable_amount
        total_cgst += cgst
        total_sgst += sgst
        total_igst += igst
    
    # Format for GSTR-1
    b2c_summary = [
        {
            'hsn_code': data['hsn_code'],
            'description': 'Restaurant Services' if data['hsn_code'] == '996331' else 'Food & Beverages',
            'uqc': 'NA',
            'total_qty': data['invoice_count'],
            'taxable_value': round(data['taxable_value'], 2),
            'integrated_tax': round(data['igst'], 2),
            'central_tax': round(data['cgst'], 2),
            'state_tax': round(data['sgst'], 2),
            'total_tax': round(data['cgst'] + data['sgst'] + data['igst'], 2),
        }
        for data in hsn_summary.values()
    ]
    
    return jsonify({
        'period': f"{year}-{month:02d}",
        'filing_type': 'GSTR-1 (B2C Small)',
        'summary': {
            'total_invoices': len(bills),
            'total_taxable_value': round(total_taxable, 2),
            'total_cgst': round(total_cgst, 2),
            'total_sgst': round(total_sgst, 2),
            'total_igst': round(total_igst, 2),
            'total_tax': round(total_cgst + total_sgst + total_igst, 2),
        },
        'hsn_wise': sorted(b2c_summary, key=lambda x: x['taxable_value'], reverse=True),
    })


@bp.route('/gst/daily', methods=['GET'])
def gst_daily_report():
    """Daily GST collection report for reconciliation."""
    err = _require_admin()
    if err:
        return err
    
    date_str = request.args.get('date')
    day_start, day_end = _day_range(date_str)
    
    bills = Bill.query.filter(
        Bill.settled_at.isnot(None),
        Bill.settled_at >= day_start,
        Bill.settled_at < day_end,
        Bill.is_cancelled.is_(False),
    ).all()
    
    import json as _json
    result = []
    total_taxable = 0
    total_cgst = 0
    total_sgst = 0
    
    for bill in bills:
        taxable = bill.amount or 0
        gst_rate = bill.gst_rate or 5.0
        cgst = bill.cgst_amount or (taxable * gst_rate / 200)
        sgst = bill.sgst_amount or (taxable * gst_rate / 200)
        
        result.append({
            'bill_id': bill.id,
            'invoice_number': f"BILL-{bill.id:06d}",
            'table_number': bill.table_number,
            'taxable_value': round(taxable, 2),
            'gst_rate': gst_rate,
            'cgst': round(cgst, 2),
            'sgst': round(sgst, 2),
            'total': round(taxable + cgst + sgst, 2),
            'settled_at': bill.settled_at.isoformat() if bill.settled_at else None,
        })
        
        total_taxable += taxable
        total_cgst += cgst
        total_sgst += sgst
    
    return jsonify({
        'date': day_start.date().isoformat(),
        'bills': result,
        'total': {
            'taxable_value': round(total_taxable, 2),
            'cgst': round(total_cgst, 2),
            'sgst': round(total_sgst, 2),
            'total_with_tax': round(total_taxable + total_cgst + total_sgst, 2),
        }
    })
