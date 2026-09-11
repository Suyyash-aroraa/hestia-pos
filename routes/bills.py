import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from flask import Blueprint, jsonify, request, send_file
from sqlalchemy import func, or_

from models import Bill, BillModification, CartItem, Customer, LedgerEntry, Order, OrderItem, Session, db

bp = Blueprint('bills', __name__, url_prefix='/api/bills')


PAYMENT_METHOD_ALIASES = {
    'upi_offline': 'upi',
    'card_offline': 'card',
}


def _normalize_payment_method_alias(method):
    method = (method or '').strip().lower()
    return PAYMENT_METHOD_ALIASES.get(method, method)


def _iso(dt):
    """Return an ISO formatted UTC string, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _session_scoped_payment_method(method, session_type='table'):
    normalized = _normalize_payment_method_alias(method)
    if normalized in ('upi', 'card') and session_type == 'table':
        return f'{normalized}_offline'
    return normalized


def _normalize_split_payments(split_payments, session_type='table', default_method='cash'):
    normalized_split = []
    for s in split_payments or []:
        amt = float(s.get('amount') or 0)
        if amt <= 0:
            continue
        normalized_split.append({
            'method': _session_scoped_payment_method(s.get('method', default_method), session_type),
            'amount': amt,
            'comment': (s.get('comment') or '').strip(),
        })
    return normalized_split


def _build_snapshot(sess):
    """Build items snapshot from all active (non-voided) order items in a session.

    Items with the same name and same effective price are merged into one line
    (quantities summed). Different config choices at the same price are collected
    as sub-variants shown beneath the merged line. If a config changes the unit
    price the items remain separate lines.
    """
    from collections import OrderedDict
    from config import BILL_VISIBLE_CONFIGS

    # Parse visible configs
    visible_configs = set()
    if BILL_VISIBLE_CONFIGS:
        for cfg in BILL_VISIBLE_CONFIGS.split(','):
            cfg = cfg.strip().lower()
            if cfg:
                visible_configs.add(cfg)

    # Collect raw rows
    raw = []
    for order in sess.orders.order_by(Order.created_at.asc()):
        for oi in sorted(order.items, key=lambda x: x.id):
            if oi.voided:
                continue
            # Skip items with no associated menu item or no name (deleted menu items)
            if not oi.menu_item or not getattr(oi.menu_item, 'name', None):
                continue
            price = (float(oi.menu_item.price) + float(oi.config_price_extra or 0)
                     if oi.menu_item else 0.0)
            choices = json.loads(oi.config_choices) if oi.config_choices else {}
            # Filter choices to only include those with price impact or in BILL_VISIBLE_CONFIGS
            price_impact_choices = []
            if choices and oi.menu_item:
                for group_name, selection in choices.items():
                    # selection could be a string, a dict {"label":..., "extra":...}, or a list of these
                    selections = selection if isinstance(selection, list) else [selection]
                    for s in selections:
                        label = s.get('label') if isinstance(s, dict) else s
                        extra = s.get('extra', 0) if isinstance(s, dict) else 0
                        
                        # Check if this choice should be visible
                        is_visible = False
                        if extra != 0:
                            is_visible = True
                        elif label and label.lower() in visible_configs:
                            is_visible = True
                        else:
                            # Fallback lookup in MenuItemConfig if extra is 0 but might be set in DB
                            from models import MenuItemConfig
                            cfg = MenuItemConfig.query.filter_by(menu_item_id=oi.menu_item.id, group_name=group_name).first()
                            if cfg:
                                opts = json.loads(cfg.options)
                                for opt in opts:
                                    if isinstance(opt, dict) and opt.get('label') == label and opt.get('extra', 0) != 0:
                                        is_visible = True
                                        extra = opt.get('extra')
                                        break
                        
                        if is_visible:
                            price_impact_choices.append({'label': label, 'extra': extra})
            raw.append({
                'name':    oi.menu_item.name,
                'price':   price,
                'qty':     oi.quantity,
                'choices': price_impact_choices,
                'order_id': order.id,
                'order_kot_comment': order.kot_comment,
                'order_kot_printed_at': _iso(order.kot_printed_at),
            })

    # Group by (name, price, order_id)
    groups = OrderedDict()
    for r in raw:
        key = (r['name'], r['price'], r['order_id'], r['order_kot_comment'])
        if key not in groups:
            groups[key] = {'name': r['name'], 'price': r['price'], 'order_id': r['order_id'], 'order_kot_comment': r['order_kot_comment'],
                           'order_kot_printed_at': r['order_kot_printed_at'],
                           'total_qty': 0, 'variants': []}
        g = groups[key]
        g['total_qty'] += r['qty']
        
        # Add each price-impacting choice as a variant
        for choice in r['choices']:
            # Merge into an existing variant if label and extra matches
            matched = False
            for v in g['variants']:
                if v['label'] == choice['label'] and v.get('extra') == choice['extra']:
                    v['qty'] += r['qty']
                    matched = True
                    break
            if not matched:
                g['variants'].append({'label': choice['label'], 'qty': r['qty'], 'extra': choice['extra']})

    # Serialise
    items = []
    for g in groups.values():
        # Only show variants when they exist and have price impact
        items.append({
            'name':     g['name'],
            'quantity': g['total_qty'],
            'price':    g['price'],
            'variants': g['variants'],
            'order_id': g['order_id'],
            'order_kot_comment': g['order_kot_comment'],
            'order_kot_printed_at': g['order_kot_printed_at'],
            # kept for legacy callers that read these fields
            'config_choices': {},
            'notes': '',
            'discount': 0.0,
        })
    return items


def _active_bill(sess):
    """Return the current non-cancelled, non-settled bill for a session, or None."""
    return (
        Bill.query
        .filter(Bill.session_id == sess.id, Bill.is_cancelled.is_(False))
        .order_by(Bill.id.desc())
        .first()
    )


def _session_has_unprinted_bill_changes(sess, bill=None, has_pending_cart=None):
    """Return True when a printed bill is stale relative to the current session state.

    This stays true after new items are placed into orders and only resets when the
    bill is printed again, because the print refresh updates bill.items_snapshot.
    """
    bill = bill or _active_bill(sess)
    if not bill or not bill.printed_at:
        return False
    if has_pending_cart is None:
        has_pending_cart = (
            db.session.query(CartItem.id)
            .filter(CartItem.session_id == sess.id)
            .first()
            is not None
        )
    if has_pending_cart:
        return True
    printed_snapshot = json.loads(bill.items_snapshot) if bill.items_snapshot else []
    current_snapshot = _build_snapshot(sess)
    return current_snapshot != printed_snapshot


def _populate_bill_customer_info(bill, sess):
    """Parse session.bill_comment and populate bill's customer fields."""
    info = _parse_bill_comment(sess.bill_comment)
    comment = info['comment']
    bill.bill_comment = comment
    if not comment:
        bill.customer_name = None
        bill.customer_gstin = None
        bill.customer_phone = None
        bill.customer_address = None
        bill.customer_notes = None
        return
    bill.customer_name = info['name'] or None
    bill.customer_gstin = info['gstin'] or None
    bill.customer_phone = info['phone'] or None
    bill.customer_address = info['address'] or None
    bill.customer_notes = info['notes'] or None


def _normalize_phone(phone):
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if len(digits) > 10 and digits.startswith('91'):
        digits = digits[-10:]
    return digits


def _parse_bill_comment(comment):
    info = {
        'comment': (comment or '').strip(),
        'name': '',
        'gstin': '',
        'phone': '',
        'address': '',
        'notes': '',
    }
    if not info['comment']:
        return info
    for line in info['comment'].split('\n'):
        if line.startswith('Name: '):
            info['name'] = line.replace('Name: ', '', 1).strip()
        elif line.startswith('GSTIN: '):
            info['gstin'] = line.replace('GSTIN: ', '', 1).strip()
        elif line.startswith('Ph: '):
            info['phone'] = _normalize_phone(line.replace('Ph: ', '', 1))
        elif line.startswith('Address: '):
            info['address'] = line.replace('Address: ', '', 1).strip()
        elif line.startswith('Notes: '):
            info['notes'] = line.replace('Notes: ', '', 1).strip()
    return info


def _ensure_customer_for_session(sess, persist=True):
    info = _parse_bill_comment(sess.bill_comment)
    phone = info['phone']
    customer = None
    if sess.customer_id:
        customer = db.session.get(Customer, sess.customer_id)
    if not customer and phone:
        customer = Customer.query.filter_by(phone=phone).first()
    if not persist:
        return customer, info
    if customer:
        customer.name = info['name'] or customer.name
        customer.gstin = info['gstin'] or customer.gstin
        customer.address = info['address'] or customer.address
        customer.notes = info['notes'] or customer.notes
        customer.last_seen = datetime.now(timezone.utc)
        sess.customer_id = customer.id
        if phone:
            sess.customer_phone = phone
    elif phone:
        customer = Customer(
            phone=phone,
            name=info['name'] or None,
            gstin=info['gstin'] or None,
            address=info['address'] or None,
            notes=info['notes'] or None,
            last_seen=datetime.now(timezone.utc),
        )
        db.session.add(customer)
        db.session.flush()
        sess.customer_id = customer.id
        sess.customer_phone = phone
    return customer, info


def _ledger_filter(customer=None, phone=None):
    phone = _normalize_phone(phone)
    customer_phone = _normalize_phone(customer.phone) if customer and getattr(customer, 'phone', None) else ''
    filters = []
    if customer and customer.id:
        filters.append(LedgerEntry.customer_id == customer.id)
    if phone:
        filters.append(LedgerEntry.customer_phone == phone)
    elif customer_phone:
        filters.append(LedgerEntry.customer_phone == customer_phone)
    if filters:
        return or_(*filters)
    return None


def _outstanding_due_subtotal(customer=None, phone=None):
    party_filter = _ledger_filter(customer=customer, phone=phone)
    if party_filter is None:
        return 0.0
    total = db.session.query(func.coalesce(func.sum(LedgerEntry.amount_subtotal), 0.0)).filter(party_filter).scalar() or 0.0
    return round(float(total), 2)


def _open_due_bills(customer=None, phone=None):
    phone = _normalize_phone(phone)
    query = Bill.query.filter(
        Bill.payment_method == 'due',
        Bill.due_status == 'open',
    )
    if customer and customer.id:
        query = query.filter(or_(Bill.customer_phone == (customer.phone or ''), Bill.customer_phone == (phone or '')))
    elif phone:
        query = query.filter(Bill.customer_phone == phone)
    else:
        return []
    return query.order_by(Bill.settled_at.asc(), Bill.id.asc()).all()


def _bill_open_due_subtotal(bill):
    return round(max(0.0, float(bill.due_added_subtotal or 0) - float(bill.due_cleared_subtotal or 0)), 4)


def _bill_open_due_payable(bill):
    open_subtotal = _bill_open_due_subtotal(bill)
    if open_subtotal <= 0:
        return 0.0
    return _bill_payable_for_subtotal(bill, open_subtotal)


def _bill_payable_for_subtotal(bill, subtotal_amount):
    subtotal_amount = round(float(subtotal_amount or 0), 4)
    if subtotal_amount <= 0:
        return 0.0
    base_subtotal = float(bill.due_added_subtotal or 0)
    if base_subtotal <= 0:
        return round(subtotal_amount, 2)
    tax_total = float(bill.cgst_amount or 0) + float(bill.sgst_amount or 0)
    multiplier = 1.0 + (tax_total / base_subtotal)
    return round(subtotal_amount * multiplier, 2)


def _outstanding_due_payable(customer=None, phone=None):
    return round(sum(_bill_open_due_payable(b) for b in _open_due_bills(customer=customer, phone=phone)), 2)


def _ledger_entry_payable(entry):
    if not entry:
        return 0.0
    subtotal_amount = abs(float(entry.amount_subtotal or 0))
    if entry.bill_id and entry.bill:
        return _bill_payable_for_subtotal(entry.bill, subtotal_amount)
    return round(subtotal_amount, 2)


def _bill_due_settlements(bill):
    entries = (
        LedgerEntry.query
        .filter_by(bill_id=bill.id, entry_type='settlement')
        .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
        .all()
    )
    return [{
        'id': e.id,
        'created_at': e.created_at.replace(tzinfo=timezone.utc).isoformat() if e.created_at else None,
        'session_id': e.session_id,
        'payment_method': e.payment_method,
        'amount_subtotal': round(abs(float(e.amount_subtotal or 0)), 2),
        'amount_payable': _ledger_entry_payable(e),
        'comment': e.comment,
    } for e in entries]


def _session_due_settlements_map(session_ids):
    session_ids = [int(sid) for sid in set(session_ids or []) if sid]
    if not session_ids:
        return {}
    entries = (
        LedgerEntry.query
        .filter(
            LedgerEntry.entry_type == 'settlement',
            LedgerEntry.session_id.in_(session_ids),
        )
        .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
        .all()
    )
    out = {}
    for e in entries:
        out.setdefault(e.session_id, []).append({
            'id': e.id,
            'bill_id': e.bill_id,
            'created_at': e.created_at.replace(tzinfo=timezone.utc).isoformat() if e.created_at else None,
            'payment_method': e.payment_method,
            'amount_subtotal': round(abs(float(e.amount_subtotal or 0)), 2),
            'amount_payable': _ledger_entry_payable(e),
            'comment': e.comment,
        })
    return out


def _allocate_split_rows(split_payments, target_amount):
    rows = []
    remaining_target = round(float(target_amount or 0), 2)
    for s in split_payments or []:
        row_amount = round(float(s.get('amount') or 0), 2)
        if row_amount <= 0 or remaining_target <= 0:
            continue
        applied = min(row_amount, remaining_target)
        if applied <= 0:
            continue
        rows.append({
            'method': s.get('method', ''),
            'amount': round(applied, 2),
            'comment': (s.get('comment') or '').strip(),
        })
        remaining_target = round(remaining_target - applied, 2)
    return rows


def _history_split_payments(split_payments, recovered_payable=0.0, recovery_settlements=None):
    current_rows = [{
        'method': (row or {}).get('method', ''),
        'amount': round(float((row or {}).get('amount') or 0), 2),
        'comment': ((row or {}).get('comment') or '').strip(),
    } for row in (split_payments or []) if round(float((row or {}).get('amount') or 0), 2) > 0]

    recovery_rows = [{
        'method': (row or {}).get('payment_method') or (row or {}).get('method') or '',
        'amount': round(float((row or {}).get('amount_payable') or (row or {}).get('amount') or 0), 2),
        'comment': ((row or {}).get('comment') or '').strip(),
    } for row in (recovery_settlements or []) if round(float((row or {}).get('amount_payable') or (row or {}).get('amount') or 0), 2) > 0]

    if recovery_rows:
        for recovery in recovery_rows:
            remaining = round(float(recovery.get('amount') or 0), 2)
            if remaining <= 0:
                continue
            # Subtract recovery from matching methods first so the current row
            # reflects the actual due-settlement ledger split instead of collapsing
            # to the earliest payment method.
            same_method_rows = [row for row in current_rows if row['method'] == recovery.get('method')]
            fallback_rows = [row for row in current_rows if row['method'] != recovery.get('method')]
            for row in same_method_rows + fallback_rows:
                if remaining <= 0:
                    break
                available = round(float(row.get('amount') or 0), 2)
                if available <= 0:
                    continue
                applied = min(available, remaining)
                row['amount'] = round(available - applied, 2)
                remaining = round(remaining - applied, 2)
        return [row for row in current_rows if round(float(row.get('amount') or 0), 2) > 0]

    total_split = round(sum(float((s or {}).get('amount') or 0) for s in (split_payments or [])), 2)
    current_target = round(max(0.0, total_split - float(recovered_payable or 0)), 2)
    return _allocate_split_rows(split_payments, current_target)


def _recovery_split_payments(split_payments, recovered_payable=0.0):
    total_split = round(sum(float((s or {}).get('amount') or 0) for s in (split_payments or [])), 2)
    current_target = round(max(0.0, total_split - float(recovered_payable or 0)), 2)
    recovery_target = round(min(float(recovered_payable or 0), total_split), 2)
    consumed = round(current_target, 2)
    rows = []
    for s in split_payments or []:
        row_amount = round(float(s.get('amount') or 0), 2)
        if row_amount <= 0:
            continue
        consume_current = min(row_amount, consumed)
        row_amount = round(row_amount - consume_current, 2)
        consumed = round(consumed - consume_current, 2)
        if row_amount <= 0 or recovery_target <= 0:
            continue
        applied = min(row_amount, recovery_target)
        rows.append({
            'method': s.get('method', ''),
            'amount': round(applied, 2),
            'comment': (s.get('comment') or '').strip(),
        })
        recovery_target = round(recovery_target - applied, 2)
    return rows


def _split_due_recovery_allocations(due_allocations, recovery_split_rows):
    entries = []
    payment_rows = [{
        'method': row.get('method', ''),
        'comment': (row.get('comment') or '').strip(),
        'remaining_payable': round(float(row.get('amount') or 0), 2),
    } for row in (recovery_split_rows or []) if float(row.get('amount') or 0) > 0]

    for alloc in due_allocations or []:
        bill_payable_total = round(float(alloc.get('amount_payable') or 0), 2)
        bill_subtotal_total = round(float(alloc.get('amount_subtotal') or 0), 4)
        remaining_bill_payable = bill_payable_total
        remaining_bill_subtotal = bill_subtotal_total
        for row in payment_rows:
            if remaining_bill_payable <= 0:
                break
            if row['remaining_payable'] <= 0:
                continue
            applied_payable = min(remaining_bill_payable, row['remaining_payable'])
            if applied_payable <= 0:
                continue
            if applied_payable >= remaining_bill_payable - 0.009:
                applied_subtotal = remaining_bill_subtotal
            else:
                applied_subtotal = round(
                    bill_subtotal_total * (applied_payable / bill_payable_total),
                    4,
                ) if bill_payable_total > 0 else 0.0
            entries.append({
                'bill_id': alloc.get('bill_id'),
                'amount_payable': round(applied_payable, 2),
                'amount_subtotal': round(applied_subtotal, 4),
                'payment_method': row['method'],
                'comment': row['comment'],
            })
            remaining_bill_payable = round(remaining_bill_payable - applied_payable, 2)
            remaining_bill_subtotal = round(remaining_bill_subtotal - applied_subtotal, 4)
            row['remaining_payable'] = round(row['remaining_payable'] - applied_payable, 2)
    return entries


def _customer_due_context(sess, persist_customer=True):
    customer, info = _ensure_customer_for_session(sess, persist=persist_customer)
    outstanding_subtotal = _outstanding_due_subtotal(customer=customer, phone=info['phone'])
    outstanding_payable = _outstanding_due_payable(customer=customer, phone=info['phone'])
    return customer, info, outstanding_subtotal, outstanding_payable


def _bill_amounts(sess, include_previous_due=False, persist_customer=True):
    from config import CGST_RATE, SGST_RATE
    from models import order_lines_subtotal, session_billable_subtotal

    customer, info, outstanding_subtotal, outstanding_payable = _customer_due_context(sess, persist_customer=persist_customer)
    gross_sub = round(sum(order_lines_subtotal(o) for o in sess.orders), 2)
    net_sub = round(session_billable_subtotal(sess), 2)
    total_disc = round(gross_sub - net_sub, 2)
    cgst = round(net_sub * CGST_RATE, 2)
    sgst = round(net_sub * SGST_RATE, 2)
    included_due_payable = outstanding_payable if include_previous_due else 0.0
    total = round(net_sub + cgst + sgst + included_due_payable, 2)
    return {
        'customer': customer,
        'customer_info': info,
        'gross_subtotal': gross_sub,
        'subtotal': net_sub,
        'total_discount': total_disc,
        'cgst': cgst,
        'sgst': sgst,
        'cgst_rate': CGST_RATE,
        'sgst_rate': SGST_RATE,
        'previous_due_subtotal': round(outstanding_subtotal, 2),
        'previous_due_payable': round(outstanding_payable, 2),
        'included_due_subtotal': round(outstanding_subtotal if include_previous_due else 0.0, 2),
        'included_due_payable': round(included_due_payable, 2),
        'total': total,
    }


def _sync_due_bill_status(customer, info, clear_amount=None, method=None, settled_at=None, clear_payable_amount=None):
    remaining_subtotal = round(float(clear_amount or 0), 4)
    remaining_payable = round(float(clear_payable_amount or 0), 2)
    if remaining_subtotal <= 0 and remaining_payable <= 0:
        return {'cleared_subtotal': 0.0, 'cleared_payable': 0.0, 'allocations': []}

    cleared_subtotal = 0.0
    cleared_payable = 0.0
    allocations = []
    for bill in _open_due_bills(customer=customer, phone=info.get('phone')):
        open_subtotal = _bill_open_due_subtotal(bill)
        if open_subtotal <= 0:
            bill.due_status = 'cleared'
            if not bill.due_cleared_at:
                bill.due_cleared_at = settled_at
            if method and not bill.due_cleared_method:
                bill.due_cleared_method = method
            continue

        open_payable = _bill_open_due_payable(bill)
        if clear_payable_amount is not None:
            if remaining_payable <= 0:
                break
            applied_payable = min(open_payable, remaining_payable)
            if applied_payable >= open_payable - 0.009:
                applied_subtotal = open_subtotal
            else:
                applied_subtotal = round(open_subtotal * (applied_payable / open_payable), 4) if open_payable > 0 else 0.0
        else:
            if remaining_subtotal <= 0:
                break
            applied_subtotal = min(open_subtotal, remaining_subtotal)
            applied_payable = round(open_payable * (applied_subtotal / open_subtotal), 2) if open_subtotal > 0 else 0.0

        bill.due_cleared_subtotal = round(float(bill.due_cleared_subtotal or 0) + applied_subtotal, 4)
        outstanding_after = _bill_open_due_subtotal(bill)
        if outstanding_after <= 0.009:
            bill.due_cleared_subtotal = float(bill.due_added_subtotal or 0)
            bill.due_status = 'cleared'
            bill.due_cleared_at = settled_at
            bill.due_cleared_method = method
            applied_subtotal = open_subtotal
            applied_payable = open_payable
        else:
            bill.due_status = 'open'

        cleared_subtotal += applied_subtotal
        cleared_payable += applied_payable
        allocations.append({
            'bill': bill,
            'bill_id': bill.id,
            'amount_subtotal': round(applied_subtotal, 4),
            'amount_payable': round(applied_payable, 2),
        })
        remaining_subtotal = round(max(0.0, remaining_subtotal - applied_subtotal), 4)
        remaining_payable = round(max(0.0, remaining_payable - applied_payable), 2)

    return {
        'cleared_subtotal': round(cleared_subtotal, 2),
        'cleared_payable': round(cleared_payable, 2),
        'allocations': allocations,
    }


def _diff_snapshots(old_items, new_items):
    """Return a human-readable description of what changed between two item snapshots."""
    from collections import Counter
    old_c = Counter()
    new_c = Counter()
    for item in old_items:
        old_c[item['name']] += item['quantity']
    for item in new_items:
        new_c[item['name']] += item['quantity']
    added, removed = [], []
    for k in set(old_c) | set(new_c):
        diff = new_c[k] - old_c[k]
        if diff > 0:
            added.append(f"+{diff}\u00d7 {k}")
        elif diff < 0:
            removed.append(f"\u2212{abs(diff)}\u00d7 {k}")
    parts = []
    if added:
        parts.append("Added: " + ", ".join(added))
    if removed:
        parts.append("Removed: " + ", ".join(removed))
    return "; ".join(parts) if parts else "Items changed"


def _calc_tax_amounts(sess):
    """Return (cgst, sgst) amounts for a session using the configured GST rates."""
    from config import CGST_RATE, SGST_RATE
    from models import session_billable_subtotal
    subtotal = session_billable_subtotal(sess)
    cgst = round(subtotal * CGST_RATE, 2)
    sgst = round(subtotal * SGST_RATE, 2)
    return cgst, sgst


def _requested_bill_id():
    """Apply the configured first bill number for either supported database."""
    from models import get_config_value, set_config_value
    raw = get_config_value('bill_start_number')
    if not raw:
        return None
    try:
        start = int(raw)
    except (ValueError, TypeError):
        return None
    highest = db.session.query(db.func.max(Bill.id)).scalar() or 0
    if start <= highest:
        return None
    set_config_value('bill_start_number', '')
    if db.engine.dialect.name == 'postgresql':
        db.session.execute(db.text("SELECT setval('bills_id_seq', :value, true)"), {'value': start - 1})
        return None
    return start


def create_or_refresh_bill(sess, include_previous_due=False):
    """Create or re-snapshot a bill on Print Bill. Returns the bill."""
    now = datetime.now(timezone.utc)
    snapshot = _build_snapshot(sess)
    from models import session_billable_subtotal, get_config_value, set_config_value
    amounts = _bill_amounts(sess, include_previous_due=include_previous_due)
    subtotal = session_billable_subtotal(sess)
    total_amount = amounts['total']
    bill = _active_bill(sess)
    if bill is None:
        # Check if we have a custom starting bill number
        bill = Bill(
            id=_requested_bill_id(),
            session_id=sess.id,
            session_type=sess.session_type,
            table_number=sess.table_number,
            pickup_code=sess.pickup_code,
            printed_at=now,
            items_snapshot=json.dumps(snapshot),
            created_at=now,
            print_count=1,
            gst_rate=round((amounts['cgst_rate'] + amounts['sgst_rate']) * 100, 3),
            cgst_amount=amounts['cgst'],
            sgst_amount=amounts['sgst'],
            amount=total_amount,
        )
        _populate_bill_customer_info(bill, sess)
        bill.previous_due_subtotal = amounts['previous_due_subtotal']
        bill.include_previous_due = bool(include_previous_due and amounts['previous_due_subtotal'] > 0)
        bill.due_outstanding_after = amounts['previous_due_subtotal']
        db.session.add(bill)
    else:
        bill.amount = total_amount
        prev_snapshot = json.loads(bill.items_snapshot) if bill.items_snapshot else []
        bill.printed_at = now
        bill.items_snapshot = json.dumps(snapshot)
        bill.print_count = (bill.print_count or 0) + 1
        bill.gst_rate = round((amounts['cgst_rate'] + amounts['sgst_rate']) * 100, 3)
        bill.cgst_amount = amounts['cgst']
        bill.sgst_amount = amounts['sgst']
        bill.previous_due_subtotal = amounts['previous_due_subtotal']
        bill.include_previous_due = bool(include_previous_due and amounts['previous_due_subtotal'] > 0)
        bill.due_outstanding_after = amounts['previous_due_subtotal']
        _populate_bill_customer_info(bill, sess)
        # If snapshot changed after first print, record a modification
        if prev_snapshot != snapshot and bill.print_count > 1:
            mod = BillModification(
                bill_id=bill.id,
                description=f"Bill re-printed with updated items (print #{bill.print_count})",
                modified_by='staff',
            )
            db.session.add(mod)
    db.session.commit()
    return bill


def settle_bill(sess, payment_method, amount, split_payments=None, payment_comment='', settled_by=None, include_previous_due=False):
    """Mark bill as settled (called on payment confirmed). Creates one if not yet printed."""
    from models import Payment
    now = datetime.now(timezone.utc)
    bill = _active_bill(sess)
    snapshot = _build_snapshot(sess)
    amounts = _bill_amounts(sess, include_previous_due=include_previous_due, persist_customer=False)
    payable_total = round(float(amounts.get('total') or 0), 2)
    if payable_total <= 0:
        raise ValueError('Bill total must be greater than 0 before settlement')
    customer = amounts['customer']
    customer_info = amounts['customer_info']
    previous_due_subtotal = amounts['previous_due_subtotal']
    previous_due_payable = amounts['previous_due_payable']
    current_subtotal = amounts['subtotal']
    # Fetch tip from the latest confirmed payment
    pay = (
        Payment.query
        .filter(Payment.session_id == sess.id, Payment.status == 'confirmed')
        .order_by(Payment.id.desc())
        .first()
    )
    tip = float(pay.tip) if pay else 0.0
    final_amount = round(float(amounts.get('total') or 0) + tip, 2)
    if final_amount <= 0:
        raise ValueError('Bill total must be greater than 0 before settlement')
    if bill is None:
        bill = Bill(
            id=_requested_bill_id(),
            session_id=sess.id,
            session_type=sess.session_type,
            table_number=sess.table_number,
            pickup_code=sess.pickup_code,
            printed_at=None,
            items_snapshot=json.dumps(snapshot),
            created_at=now,
            print_count=0,
            gst_rate=round((amounts['cgst_rate'] + amounts['sgst_rate']) * 100, 3),
            cgst_amount=amounts['cgst'],
            sgst_amount=amounts['sgst'],
            tip=tip,
            settled_by=settled_by,
        )
        _populate_bill_customer_info(bill, sess)
        db.session.add(bill)
    else:
        bill.items_snapshot = json.dumps(snapshot)
        bill.gst_rate = round((amounts['cgst_rate'] + amounts['sgst_rate']) * 100, 3)
        bill.cgst_amount = amounts['cgst']
        bill.sgst_amount = amounts['sgst']
        bill.tip = tip
        _populate_bill_customer_info(bill, sess)
        if settled_by:
            bill.settled_by = settled_by
    bill.amount = final_amount
    bill.settled_at = now
    # If payment method is due, don't set previous_due fields since this bill itself is a due bill
    if payment_method == 'due':
        bill.previous_due_subtotal = 0.0
        bill.include_previous_due = False
    else:
        bill.previous_due_subtotal = previous_due_subtotal
        bill.include_previous_due = bool(include_previous_due and previous_due_payable > 0)
    bill.due_added_subtotal = 0.0
    bill.due_cleared_subtotal = 0.0
    bill.due_status = 'none'
    normalized_split = None
    if split_payments:
        normalized_split = _normalize_split_payments(split_payments, sess.session_type, payment_method)
        bill.split_payments = json.dumps(normalized_split)
        # Generate descriptive payment method string: "cash + card"
        methods = []
        for s in normalized_split:
            m = s.get('method', '').replace('_offline', '')
            if m not in methods:
                methods.append(m)
        bill.payment_method = ' + '.join(methods)
    else:
        bill.payment_method = payment_method
        clean_comment = (payment_comment or '').strip()
        base_method = (payment_method or '').replace('_offline', '')
        if clean_comment and base_method != 'cash':
            bill.split_payments = json.dumps([{
                'method': payment_method,
                'amount': float(amount or 0),
                'comment': clean_comment,
            }])

    if payment_method == 'due':
        if not customer_info.get('phone'):
            raise ValueError('Phone number is required in bill comments for due payments')
        entry = LedgerEntry(
            customer_id=customer.id if customer else None,
            session_id=sess.id,
            bill=bill,
            entry_type='due',
            amount_subtotal=current_subtotal,
            payment_method='due',
            customer_name=customer_info.get('name') or None,
            customer_phone=customer_info.get('phone') or None,
            customer_gstin=customer_info.get('gstin') or None,
            customer_address=customer_info.get('address') or None,
            customer_notes=customer_info.get('notes') or None,
            comment=(payment_comment or '').strip() or bill.bill_comment,
            created_at=now,
        )
        db.session.add(entry)
        bill.due_added_subtotal = current_subtotal
        bill.due_status = 'open'
    elif include_previous_due and previous_due_payable > 0:
        cleared = _sync_due_bill_status(
            customer,
            customer_info,
            method=payment_method,
            settled_at=now,
            clear_payable_amount=previous_due_payable,
        )
        if cleared['cleared_subtotal'] > 0:
            settlement_rows = None
            if normalized_split and bill.amount is not None:
                settlement_rows = _split_due_recovery_allocations(
                    cleared.get('allocations', []),
                    _recovery_split_payments(normalized_split, float(cleared.get('cleared_payable') or 0)),
                )
            if settlement_rows:
                for row in settlement_rows:
                    db.session.add(LedgerEntry(
                        customer_id=customer.id if customer else None,
                        session_id=sess.id,
                        bill_id=row['bill_id'],
                        entry_type='settlement',
                        amount_subtotal=-float(row['amount_subtotal'] or 0),
                        payment_method=row['payment_method'] or payment_method,
                        customer_name=customer_info.get('name') or None,
                        customer_phone=customer_info.get('phone') or None,
                        customer_gstin=customer_info.get('gstin') or None,
                        customer_address=customer_info.get('address') or None,
                        customer_notes=customer_info.get('notes') or None,
                        comment=row['comment'] or (payment_comment or '').strip() or bill.bill_comment,
                        created_at=now,
                    ))
            else:
                for alloc in cleared.get('allocations', []):
                    db.session.add(LedgerEntry(
                        customer_id=customer.id if customer else None,
                        session_id=sess.id,
                        bill_id=alloc['bill_id'],
                        entry_type='settlement',
                        amount_subtotal=-float(alloc['amount_subtotal'] or 0),
                        payment_method=payment_method,
                        customer_name=customer_info.get('name') or None,
                        customer_phone=customer_info.get('phone') or None,
                        customer_gstin=customer_info.get('gstin') or None,
                        customer_address=customer_info.get('address') or None,
                        customer_notes=customer_info.get('notes') or None,
                        comment=(payment_comment or '').strip() or bill.bill_comment,
                        created_at=now,
                    ))
            bill.due_cleared_subtotal = cleared['cleared_subtotal']

    bill.due_outstanding_after = _outstanding_due_subtotal(customer=customer, phone=customer_info.get('phone'))
    db.session.commit()
    return bill


def invalidate_bill(sess):
    """Update bill snapshot in-place when items change. Keeps the same bill ID. Settled bills are untouched."""
    bill = _active_bill(sess)
    if bill is None or bill.settled_at is not None:
        return
    old_items = json.loads(bill.items_snapshot) if bill.items_snapshot else []
    new_items = _build_snapshot(sess)
    if old_items == new_items:
        return  # nothing changed, no-op
    change_desc = _diff_snapshots(old_items, new_items)
    cgst, sgst = _calc_tax_amounts(sess)
    bill.items_snapshot = json.dumps(new_items)
    bill.cgst_amount = cgst
    bill.sgst_amount = sgst
    mod = BillModification(
        bill_id=bill.id,
        description=f"Items modified \u2014 {change_desc}",
        modified_by='staff',
    )
    db.session.add(mod)
    db.session.commit()


@bp.route('/preview', methods=['GET', 'POST'])
def preview_bill():
    """Return bill totals without creating or modifying any bill record."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token') or request.args.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 404

    include_previous_due = bool(data.get('include_previous_due') or request.args.get('include_previous_due') == '1')
    amounts = _bill_amounts(sess, include_previous_due=include_previous_due)
    return jsonify({
        'gross_subtotal': amounts['gross_subtotal'],
        'total_discount': amounts['total_discount'],
        'subtotal': amounts['subtotal'],
        'cgst': amounts['cgst'],
        'sgst': amounts['sgst'],
        'cgst_rate': amounts['cgst_rate'],
        'sgst_rate': amounts['sgst_rate'],
        'previous_due_subtotal': amounts['previous_due_subtotal'],
        'previous_due_payable': amounts['previous_due_payable'],
        'included_due_subtotal': amounts['included_due_subtotal'],
        'included_due_payable': amounts['included_due_payable'],
        'total': amounts['total'],
        'customer_name': amounts['customer_info'].get('name') or None,
        'customer_phone': amounts['customer_info'].get('phone') or None,
    })


@bp.route('/print', methods=['POST'])
def print_bill():
    from app import push_pos_event
    from routes.orders import _order_update_event, _derive_order_status
    from config import (
        RESTAURANT_NAME, RESTAURANT_ADDRESS, RESTAURANT_PHONE,
        GST_NUMBER, FSSAI_NUMBER, RESTAURANT_WEBSITE,
        BILL_FOOTER_MSG, INSTAGRAM_HANDLE, GOOGLE_LISTING,
        BILL_JURISDICTION, BILL_VISIBLE_CONFIGS, SITE_URL,
    )

    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    include_previous_due = bool(data.get('include_previous_due'))
    silent_print = bool(data.get('silent_print'))
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 404

    if sess.is_vip:
        return jsonify({'error': 'Bill printing is disabled for VIP tables.'}), 400

    pending_names = [
        row[0] for row in db.session.query(CartItem.name)
        .filter(CartItem.session_id == sess.id)
        .order_by(CartItem.id)
        .limit(4)
        .all()
    ]
    if pending_names:
        listed = ', '.join(n for n in pending_names[:3] if n)
        more = ' and more' if len(pending_names) > 3 else ''
        return jsonify({
            'error': (
                'Cart still has un-sent items'
                + (f' ({listed}{more})' if listed else '')
                + '. Send them to the kitchen or remove them before printing.'
            ),
            'pending_cart': True,
        }), 400

    amounts = _bill_amounts(sess, include_previous_due=include_previous_due)
    if round(float(amounts.get('total') or 0), 2) <= 0:
        return jsonify({'error': 'Bill total must be greater than 0 before printing'}), 400

    # Auto-serve all active (non-voided, non-held) items that aren't already served
    now = datetime.now(timezone.utc)
    changed_orders = set()
    for order in sess.orders:
        for oi in order.items:
            if not oi.voided and not oi.held and oi.item_status != 'served':
                oi.item_status = 'served'
                changed_orders.add(order)
    for order in changed_orders:
        order.status = _derive_order_status(order)
    if changed_orders:
        db.session.commit()
        for order in changed_orders:
            db.session.refresh(order)
            ev = _order_update_event(order)
            pass
            push_pos_event(ev)

    bill = create_or_refresh_bill(sess, include_previous_due=include_previous_due)

    # Set table status to 'paying' if it's a table session
    if sess.session_type == 'table':
        from models import Table
        t = Table.query.filter_by(number=sess.table_number).first()
        if t:
            t.status = 'paying'
            db.session.commit()
            from routes.tables import _invalidate_tables_cache
            _invalidate_tables_cache()
            push_pos_event({'type': 'table_update'})

    captain = sess.opened_by.name if sess.opened_by else None

    split_payments = json.loads(bill.split_payments) if bill.split_payments else None

    # Push bill_print event so head-pos can handle silent printing.
    # Only when silent_print is requested (pos-phone) — /pos uses browser dialog only.
    bill_data = {
        'type': 'bill_print',
        'bill_id': bill.id,
        'printed_at': bill.printed_at.isoformat() if bill.printed_at else None,
        'print_count': bill.print_count,
        'items': json.loads(bill.items_snapshot),
        'gross_subtotal': amounts['gross_subtotal'],
        'total_discount': amounts['total_discount'],
        'subtotal': amounts['subtotal'],
        'previous_due_subtotal': amounts['previous_due_subtotal'],
        'previous_due_payable': amounts['previous_due_payable'],
        'included_due_subtotal': amounts['included_due_subtotal'],
        'included_due_payable': amounts['included_due_payable'],
        'cgst': amounts['cgst'],
        'sgst': amounts['sgst'],
        'cgst_rate': amounts['cgst_rate'],
        'sgst_rate': amounts['sgst_rate'],
        'total': amounts['total'],
        'captain': captain,
        'customer_name': bill.customer_name or (sess.customer.name if sess.customer else None),
        'customer_phone': bill.customer_phone or (sess.customer.phone if sess.customer else sess.customer_phone),
        'customer_gstin': bill.customer_gstin,
        'customer_address': bill.customer_address,
        'customer_notes': bill.customer_notes,
        'bill_comment': bill.bill_comment,
        'payment_method': bill.payment_method,
        'split_payments': split_payments,
        'tip': float(bill.tip or 0),
        'session_type': sess.session_type,
        'table_number': sess.table_number,
        'pickup_code': sess.pickup_code,
        'include_previous_due': bill.include_previous_due,
        'due_outstanding_after': bill.due_outstanding_after,
        # Brand details so head-pos can build a complete bill without relying on window.__brand
        'restaurant_name': RESTAURANT_NAME,
        'address': RESTAURANT_ADDRESS,
        'phone': RESTAURANT_PHONE,
        'gst_number': GST_NUMBER,
        'fssai_number': FSSAI_NUMBER,
        'restaurant_website': RESTAURANT_WEBSITE,
        'footer_msg': BILL_FOOTER_MSG,
        'instagram': INSTAGRAM_HANDLE,
        'google_listing': GOOGLE_LISTING,
        'jurisdiction': BILL_JURISDICTION,
        'site_url': SITE_URL,
        'bill_visible_configs': BILL_VISIBLE_CONFIGS,
    }
    if silent_print:
        push_pos_event(bill_data)

    split_payments = json.loads(bill.split_payments) if bill.split_payments else None

    return jsonify({
        'bill_id':        bill.id,
        'printed_at':     bill.printed_at.isoformat() if bill.printed_at else None,
        'print_count':    bill.print_count,
        'items':          json.loads(bill.items_snapshot),
        'gross_subtotal': amounts['gross_subtotal'],
        'total_discount': amounts['total_discount'],
        'subtotal':       amounts['subtotal'],
        'previous_due_subtotal': amounts['previous_due_subtotal'],
        'previous_due_payable': amounts['previous_due_payable'],
        'included_due_subtotal': amounts['included_due_subtotal'],
        'included_due_payable': amounts['included_due_payable'],
        'cgst':           amounts['cgst'],
        'sgst':           amounts['sgst'],
        'cgst_rate':      amounts['cgst_rate'],
        'sgst_rate':      amounts['sgst_rate'],
        'total':          amounts['total'],
        'captain':        captain,
        'customer_name':  bill.customer_name or (sess.customer.name if sess.customer else None),
        'customer_phone': bill.customer_phone or (sess.customer.phone if sess.customer else sess.customer_phone),
        'customer_gstin': bill.customer_gstin,
        'customer_address': bill.customer_address,
        'customer_notes': bill.customer_notes,
        'bill_comment':   bill.bill_comment,
        'payment_method': bill.payment_method,
        'split_payments': split_payments,
        'tip':            float(bill.tip or 0),
        'session_type':   sess.session_type,
        'table_number':   sess.table_number,
        'pickup_code':    sess.pickup_code,
        'include_previous_due': bill.include_previous_due,
        'due_outstanding_after': bill.due_outstanding_after,
    })


@bp.route('/<int:bill_id>/edit', methods=['POST'])
def edit_bill(bill_id):
    data = request.get_json(force=True, silent=True) or {}
    bill = db.session.get(Bill, bill_id)
    if not bill:
        return jsonify({'error': 'Bill not found'}), 404
    sess = db.session.get(Session, bill.session_id) if bill.session_id else None
    session_type = sess.session_type if sess else 'table'
    allowed = ('cash', 'upi_offline', 'card_offline', 'upi', 'card', 'razorpay', 'split', 'due')
    method = data.get('payment_method')
    if method and method in allowed:
        if method == 'split':
            split = data.get('split_payments') or []
            normalized_split = _normalize_split_payments(split, session_type)
            if not normalized_split:
                return jsonify({'error': 'split_payments required for split method'}), 400
            bill.split_payments = json.dumps(normalized_split)
            methods = []
            for s in normalized_split:
                m = (s.get('method') or '').replace('_offline', '')
                if m and m not in methods:
                    methods.append(m)
            bill.payment_method = ' + '.join(methods) if methods else 'split'
        else:
            stored_method = _session_scoped_payment_method(method, session_type)
            bill.payment_method = stored_method
            split = data.get('split_payments')
            if isinstance(split, list) and split:
                normalized_split = _normalize_split_payments(
                    split,
                    session_type,
                    stored_method,
                )
                bill.split_payments = json.dumps(normalized_split)
            else:
                bill.split_payments = None
    db.session.commit()
    return jsonify({
        'message': 'Bill updated',
        'payment_method': bill.payment_method,
        'split_payments': json.loads(bill.split_payments) if bill.split_payments else None,
    })


@bp.route('/history', methods=['GET'])
def bill_history():
    from_date = request.args.get('from')
    to_date = request.args.get('to')
    settled_by = request.args.get('settled_by')
    payment_method = (request.args.get('payment_method') or '').strip().lower()

    payload = _build_bill_history_payload(
        from_date=from_date,
        to_date=to_date,
        settled_by=settled_by,
        payment_method=payment_method,
    )
    return jsonify(payload)


def _bill_snapshot_subtotal(bill):
    items = json.loads(bill.items_snapshot) if bill.items_snapshot else []
    return round(sum(
        float((item or {}).get('price') or 0) * float((item or {}).get('quantity') or 0)
        for item in items
    ), 2)


def _bill_history_financials(bill, history_amount=None, due_settlements=None, linked_due_recovered=0.0):
    due_settlements = due_settlements or []
    # Exclude tips from all sales calculations - tips are not revenue
    tip = round(float(bill.tip or 0), 2)
    amount_with_tip = round(float(history_amount if history_amount is not None else (bill.amount or 0)), 2)
    payable = round(max(0.0, amount_with_tip - tip), 2)
    cgst = round(float(bill.cgst_amount or 0), 2)
    sgst = round(float(bill.sgst_amount or 0), 2)
    tax_total = round(cgst + sgst, 2)
    snapshot_subtotal = _bill_snapshot_subtotal(bill)
    previous_due_cleared_payable = 0.0

    if bill.payment_method == 'due':
        if due_settlements:
            subtotal = round(sum(float((row or {}).get('amount_subtotal') or 0) for row in due_settlements), 2)
        else:
            subtotal = round(_bill_open_due_subtotal(bill), 2)
        gross_subtotal = subtotal
        total_discount = 0.0
        # Due bills don't clear previous due - they ARE the due
        previous_due_cleared_payable = 0.0
    else:
        subtotal = round(max(0.0, payable - tax_total), 2)
        gross_subtotal = round(snapshot_subtotal, 2)
        total_discount = round(max(0.0, gross_subtotal - subtotal), 2)
        previous_due_cleared_payable = round(float(linked_due_recovered or 0), 2)

    return {
        'gross_subtotal': gross_subtotal,
        'subtotal': subtotal,
        'total_discount': total_discount,
        'cgst_amount': cgst,
        'sgst_amount': sgst,
        'tax_amount': tax_total,
        'payable_amount': payable,
        'previous_due_cleared_payable': previous_due_cleared_payable,
    }


def _round_half_up(value, digits=0):
    quant = '1' if digits <= 0 else ('1.' + ('0' * digits))
    return float(Decimal(str(value or 0)).quantize(Decimal(quant), rounding=ROUND_HALF_UP))


def _rounded_rupee_amount(value):
    return _round_half_up(value, 0)


def _history_bill_counts_in_total_sales(bill_row):
    if not bill_row:
        return False
    if bill_row.get('is_cancelled') or bill_row.get('is_complementary'):
        return False
    return True


def _build_bill_history_payload(from_date=None, to_date=None, settled_by=None, payment_method=''):
    payment_method = (payment_method or '').strip().lower()

    query = Bill.query.filter(Bill.settled_at.isnot(None))
    
    if from_date:
        try:
            dt_from = datetime.strptime(from_date, '%Y-%m-%d').replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(
                or_(
                    Bill.settled_at >= dt_from,
                    Bill.due_cleared_at >= dt_from,
                )
            )
        except ValueError: pass
    if to_date:
        try:
            dt_to = datetime.strptime(to_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(
                or_(
                    Bill.settled_at <= dt_to,
                    Bill.due_cleared_at <= dt_to,
                )
            )
        except ValueError: pass
    
    if settled_by:
        query = query.filter(Bill.settled_by == settled_by)
        
    bills = query.order_by(Bill.due_cleared_at.desc(), Bill.settled_at.desc(), Bill.id.desc()).limit(1000).all()
    session_due_settlements = _session_due_settlements_map([b.session_id for b in bills])
    
    # Get unique settled_by names for filter
    all_settled_by = [r[0] for r in db.session.query(Bill.settled_by).filter(Bill.settled_at.isnot(None)).distinct().all() if r[0]]
    if 'main_pos' not in all_settled_by:
        all_settled_by.append('main_pos')
    all_settled_by.sort()
    
    total_sales = 0.0
    sales_by_method = {'cash': 0.0, 'card': 0.0, 'online': 0.0, 'other': 0.0, 'due': 0.0}
    
    def _method_bucket(method):
        m = (method or '').strip().lower()
        if m == 'due': return 'due'
        if 'cash' in m: return 'cash'
        if 'card' in m: return 'card'
        if 'upi' in m or 'online' in m or 'razorpay' in m: return 'online'
        return 'other'

    out = []
    for b in bills:
        due_settlements = _bill_due_settlements(b) if b.payment_method == 'due' else []
        linked_session_settlements = session_due_settlements.get(b.session_id, []) if b.payment_method != 'due' else []
        linked_due_recovered = round(sum(float(s.get('amount_payable') or 0) for s in linked_session_settlements), 2)
        settlement_methods = []
        for s in due_settlements:
            m = (s.get('payment_method') or '').strip()
            if m and m not in settlement_methods:
                settlement_methods.append(m)
        effective_method = b.payment_method
        if b.payment_method == 'due' and settlement_methods:
            effective_method = settlement_methods[0] if len(settlement_methods) == 1 else 'multiple'
        elif b.due_cleared_method:
            effective_method = b.due_cleared_method

        if payment_method:
            if payment_method == 'due':
                if b.payment_method != 'due':
                    continue
            elif b.payment_method == 'due':
                if not any(_method_bucket(s.get('payment_method')) == payment_method for s in due_settlements):
                    continue
            elif _method_bucket(b.payment_method) != payment_method:
                continue

        is_valid_sale = not b.is_cancelled and not b.is_complementary
        amt = float(b.amount or 0)
        history_amount = round(amt, 2)
        history_split_payments = json.loads(b.split_payments) if b.split_payments and b.split_payments.strip() else None

        if b.payment_method == 'due':
            if b.due_status == 'cleared' and due_settlements:
                history_amount = round(sum(float(s.get('amount_payable') or 0) for s in due_settlements), 2)
            else:
                # Use bill.amount directly to preserve exact amount including tax
                history_amount = round(float(b.amount or 0), 2)
        elif linked_due_recovered > 0:
            history_amount = round(max(0.0, amt - linked_due_recovered), 2)
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

        # Use payable_amount (excludes tips) for all sales calculations
        payable_amount = financials['payable_amount']

        if is_valid_sale:
            if b.payment_method == 'due':
                if b.due_status != 'cleared':
                    sales_by_method['due'] += _rounded_rupee_amount(_bill_open_due_payable(b))
                else:
                    if due_settlements:
                        total_sales += _rounded_rupee_amount(payable_amount)
                        for s in due_settlements:
                            sales_by_method[_method_bucket(s.get('payment_method'))] += _rounded_rupee_amount(float(s.get('amount_payable') or 0))
                    else:
                        rounded_payable_amount = _rounded_rupee_amount(payable_amount)
                        total_sales += rounded_payable_amount
                        sales_by_method[_method_bucket(b.due_cleared_method or b.payment_method)] += rounded_payable_amount
            else:
                rounded_payable_amount = _rounded_rupee_amount(payable_amount)
                total_sales += rounded_payable_amount
                if history_split_payments:
                    try:
                        for s in history_split_payments:
                            m = _method_bucket(s.get('method'))
                            sales_by_method[m] += _rounded_rupee_amount(float(s.get('amount') or 0))
                    except:
                        sales_by_method[_method_bucket(b.payment_method)] += rounded_payable_amount
                else:
                    sales_by_method[_method_bucket(b.payment_method)] += rounded_payable_amount

        out.append({
            'id': b.id,
            'session_type': b.session_type,
            'table_number': b.table_number,
            'pickup_code': b.pickup_code,
            'settled_at': b.settled_at.replace(tzinfo=timezone.utc).isoformat() if b.settled_at else None,
            'effective_settled_at': (
                b.due_cleared_at.replace(tzinfo=timezone.utc).isoformat()
                if b.payment_method == 'due' and b.due_status == 'cleared' and b.due_cleared_at
                else (
                    b.settled_at.replace(tzinfo=timezone.utc).isoformat()
                    if b.payment_method != 'due' and b.settled_at
                    else None
                )
            ),
            'printed_at': b.printed_at.replace(tzinfo=timezone.utc).isoformat() if b.printed_at else None,
            'payment_method': b.payment_method,
            'effective_payment_method': effective_method,
            'settled_by': b.settled_by,
            'amount': b.amount,
            'history_amount': financials['payable_amount'],
            'gross_subtotal': financials['gross_subtotal'],
            'subtotal': financials['subtotal'],
            'total_discount': financials['total_discount'],
            'cgst_amount': financials['cgst_amount'],
            'sgst_amount': financials['sgst_amount'],
            'tax_amount': financials['tax_amount'],
            'split_payments': json.loads(b.split_payments) if b.split_payments and b.split_payments.strip() else None,
            'history_split_payments': history_split_payments,
            'print_count': b.print_count or 0,
            'is_cancelled': b.is_cancelled,
            'is_complementary': b.is_complementary,
            'bill_comment': b.bill_comment,
            'customer_name': b.customer_name,
            'customer_gstin': b.customer_gstin,
            'customer_phone': b.customer_phone,
            'customer_address': b.customer_address,
            'customer_notes': b.customer_notes,
            'previous_due_subtotal': float(b.previous_due_subtotal or 0),
            'previous_due_payable': round(float(b.previous_due_subtotal or 0), 2),
            'include_previous_due': bool(b.include_previous_due),
            'due_added_subtotal': float(b.due_added_subtotal or 0),
            'due_cleared_subtotal': float(b.due_cleared_subtotal or 0),
            'linked_due_recovered_payable': financials['previous_due_cleared_payable'],
            'due_outstanding_after': float(b.due_outstanding_after or 0),
            'due_status': b.due_status,
            'due_cleared_at': b.due_cleared_at.replace(tzinfo=timezone.utc).isoformat() if b.due_cleared_at else None,
            'due_cleared_method': b.due_cleared_method,
            'due_settlements': due_settlements,
            'items': json.loads(b.items_snapshot) if b.items_snapshot else [],
        })
        
    return {
        'bills': out,
        'total_sales': round(total_sales, 2),
        'sales_by_method': sales_by_method,
        'settled_by_options': all_settled_by
    }


def _excel_payment_label(method):
    method = (method or '').strip().lower()
    labels = {
        'cash': 'Cash',
        'upi': 'UPI',
        'upi_offline': 'UPI',
        'card': 'Card',
        'card_offline': 'Card',
        'due': 'Due',
        'multiple': 'Multiple',
        'razorpay': 'Online (Razorpay)',
    }
    return labels.get(method, method or 'Unpaid')


def _export_payment_rows(bill):
    history_splits = bill.get('history_split_payments') or []
    if history_splits:
        return [{
            'method': (row or {}).get('method', ''),
            'amount': round(float((row or {}).get('amount') or 0), 2),
            'comment': ((row or {}).get('comment') or '').strip(),
        } for row in history_splits if round(float((row or {}).get('amount') or 0), 2) > 0]

    due_settlements = bill.get('due_settlements') or []
    if due_settlements:
        return [{
            'method': (row or {}).get('payment_method', ''),
            'amount': round(float((row or {}).get('amount_payable') or 0), 2),
            'comment': ((row or {}).get('comment') or '').strip(),
        } for row in due_settlements if round(float((row or {}).get('amount_payable') or 0), 2) > 0]

    method = bill.get('effective_payment_method') or bill.get('payment_method') or ''
    # Use payable_amount (excludes tips) instead of history_amount (includes tips)
    amount = round(float(bill.get('payable_amount') or bill.get('history_amount') or bill.get('amount') or 0), 2)
    if amount > 0 and method and method != 'due':
        return [{
            'method': method,
            'amount': amount,
            'comment': '',
        }]
    return []


def _export_payment_bucket(method):
    method = (method or '').strip().lower()
    if 'cash' in method:
        return 'cash'
    if 'card' in method:
        return 'card'
    if method in ('upi', 'upi_offline'):
        return 'upi'
    return ''


def _export_payment_bucket_amount(payment_rows, bucket):
    return round(sum(
        float((row or {}).get('amount') or 0)
        for row in (payment_rows or [])
        if _export_payment_bucket((row or {}).get('method')) == bucket
    ), 2)


def _excel_round_half_up(value, digits=0):
    return _round_half_up(value, digits)




@bp.route('/history.xlsx', methods=['GET'])
def bill_history_xlsx():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({'error': 'Excel export requires openpyxl to be installed'}), 500

    from_date = request.args.get('from')
    to_date = request.args.get('to')
    settled_by = request.args.get('settled_by')
    payment_method = (request.args.get('payment_method') or '').strip().lower()

    payload = _build_bill_history_payload(
        from_date=from_date,
        to_date=to_date,
        settled_by=settled_by,
        payment_method=payment_method,
    )
    bills = payload.get('bills') or []

    wb = Workbook()
    ws = wb.active
    ws.title = 'Sales Register'

    title_fill = PatternFill('solid', fgColor='2C1810')
    header_fill = PatternFill('solid', fgColor='7B3F1E')
    subheader_fill = PatternFill('solid', fgColor='F3E7D6')
    title_font = Font(color='F8C471', bold=True, size=14)
    header_font = Font(color='FFFFFF', bold=True)
    bold_font = Font(bold=True)
    thin_side = Side(style='thin', color='D8C3AE')
    thin_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    center = Alignment(horizontal='center', vertical='center')
    wrap = Alignment(vertical='top', wrap_text=True)
    currency_format = '"₹"#,##0.00'
    date_format = 'dd-mmm-yyyy'

    ws.merge_cells('A1:Q1')
    ws['A1'] = 'Sales Register Export'
    ws['A1'].fill = title_fill
    ws['A1'].font = title_font
    ws['A1'].alignment = center

    meta = [
        ('From', from_date or 'All'),
        ('To', to_date or 'All'),
        ('Settled By', settled_by or 'All'),
        ('Payment Filter', payment_method or 'All'),
        ('Generated At', datetime.now().strftime('%Y-%m-%d %I:%M %p')),
    ]
    meta_row = 2
    meta_col = 1
    for label, value in meta:
        ws.cell(row=meta_row, column=meta_col, value=label).font = bold_font
        ws.cell(row=meta_row, column=meta_col, value=label).fill = subheader_fill
        ws.cell(row=meta_row, column=meta_col + 1, value=value)
        ws.cell(row=meta_row, column=meta_col).border = thin_border
        ws.cell(row=meta_row, column=meta_col + 1).border = thin_border
        meta_col += 2

    headers = [
        'Bill No.',
        'Basic Subtotal',
        'Discount',
        'Net Subtotal',
        'SGST Amount',
        'CGST Amount',
        'Previous Due Cleared',
        'Round Off',
        'Total Payable',
        'Cash Amount',
        'Card Amount',
        'UPI Amount',
        'Payment Method Breakdown',
        'Payment Notes',
        'Settlement Date',
        'Customer Details',
    ]
    header_row = 4
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center
        cell.border = thin_border

    totals_values = {
        2: 0.0,   # Basic Subtotal
        3: 0.0,   # Discount
        4: 0.0,   # Net Subtotal
        5: 0.0,   # SGST Amount
        6: 0.0,   # CGST Amount
        7: 0.0,   # Previous Due Cleared
        8: 0.0,   # Round Off
        9: 0.0,   # Total Payable
        10: 0.0,  # Cash Amount
        11: 0.0,  # Card Amount
        12: 0.0,  # UPI Amount
    }

    for row_idx, bill in enumerate(bills, start=header_row + 1):
        basic_exact = float(bill.get('gross_subtotal') or 0)
        discount_exact = float(bill.get('total_discount') or 0)
        session_after_discount_exact = float(bill.get('subtotal') or 0)
        sgst = float(bill.get('sgst_amount') or 0)
        cgst = float(bill.get('cgst_amount') or 0)
        payable_amount_exact = float(bill.get('payable_amount') or bill.get('history_amount') or bill.get('amount') or 0)
        due_cleared_exact = float(bill.get('linked_due_recovered_payable') or 0)

        basic = _excel_round_half_up(basic_exact, 0)
        discount = _excel_round_half_up(discount_exact, 0)
        session_after_discount = _excel_round_half_up(session_after_discount_exact, 0)
        payable_amount = _excel_round_half_up(payable_amount_exact, 0)
        due_cleared = _excel_round_half_up(due_cleared_exact, 0)
        displayed_components_total = round(session_after_discount + sgst + cgst + due_cleared, 2)
        round_off = round(payable_amount - displayed_components_total, 2)
        if abs(round_off) < 0.005:
            round_off = 0.0

        payment_rows = _export_payment_rows(bill)
        if payment_rows:
            payment_mode = ' | '.join(
                f"{_excel_payment_label((row or {}).get('method'))}: {_excel_round_half_up(float((row or {}).get('amount') or 0), 0):.2f}"
                for row in payment_rows
            )
            payment_comments = ' | '.join(
                ((row or {}).get('comment') or '').strip()
                for row in payment_rows
                if ((row or {}).get('comment') or '').strip()
            )
        else:
            payment_mode = _excel_payment_label(bill.get('effective_payment_method') or bill.get('payment_method'))
            payment_comments = ''
        cash_amount_exact = _export_payment_bucket_amount(payment_rows, 'cash')
        card_amount_exact = _export_payment_bucket_amount(payment_rows, 'card')
        upi_amount_exact = _export_payment_bucket_amount(payment_rows, 'upi')
        cash_amount = _excel_round_half_up(cash_amount_exact, 0)
        card_amount = _excel_round_half_up(card_amount_exact, 0)
        upi_amount = _excel_round_half_up(upi_amount_exact, 0)

        customer_bits = [
            (bill.get('customer_name') or '').strip(),
            (bill.get('customer_phone') or '').strip(),
        ]
        customer = ' | '.join(part for part in customer_bits if part)

        settled_at = None
        settled_at_raw = bill.get('effective_settled_at') or ''
        if settled_at_raw:
            try:
                settled_at = datetime.fromisoformat(settled_at_raw.replace('Z', '+00:00')).replace(tzinfo=None)
            except ValueError:
                settled_at = None

        row_values = [
            bill.get('id'),
            basic,
            discount,
            session_after_discount,
            sgst,
            cgst,
            due_cleared,
            round_off,
            payable_amount,
            cash_amount,
            card_amount,
            upi_amount,
            payment_mode,
            payment_comments,
            settled_at,
            customer,
        ]

        if _history_bill_counts_in_total_sales(bill):
            for total_col, total_value in {
                2: basic,
                3: discount,
                4: session_after_discount,
                5: sgst,
                6: cgst,
                7: due_cleared,
                8: round_off,
                9: payable_amount,
                10: cash_amount,
                11: card_amount,
                12: upi_amount,
            }.items():
                totals_values[total_col] += float(total_value or 0)

        for col_idx, value in enumerate(row_values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border = thin_border
            if col_idx in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12):
                cell.number_format = currency_format
            elif col_idx == 15 and settled_at:
                cell.number_format = date_format
            elif col_idx in (13, 14, 16):
                cell.alignment = wrap

    totals_row = header_row + len(bills) + 1
    totals_values = [
        'TOTALS',
        totals_values[2],   # Basic Subtotal
        totals_values[3],   # Discount
        totals_values[4],   # Net Subtotal
        totals_values[5],   # SGST Amount
        totals_values[6],   # CGST Amount
        totals_values[7],   # Previous Due Cleared
        totals_values[8],   # Round Off
        totals_values[9],   # Total Payable
        totals_values[10],  # Cash Amount
        totals_values[11],  # Card Amount
        totals_values[12],  # UPI Amount
        '',
        '',
        '',
        '',
    ]
    for col_idx, value in enumerate(totals_values, start=1):
        cell = ws.cell(row=totals_row, column=col_idx, value=value)
        cell.border = thin_border
        cell.fill = subheader_fill
        cell.font = bold_font
        if col_idx in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12):
            cell.number_format = currency_format
        elif col_idx in (13, 14, 16):
            cell.alignment = wrap

    widths = {
        1: 10,   # Bill No.
        2: 18,   # Basic Subtotal
        3: 12,   # Discount
        4: 22,   # Net Subtotal
        5: 12,   # SGST Amount
        6: 12,   # CGST Amount
        7: 20,   # Previous Due Cleared
        8: 12,   # Round Off
        9: 18,   # Total Payable
        10: 14,  # Cash Amount
        11: 14,  # Card Amount
        12: 14,  # UPI Amount
        13: 28,  # Payment Method Breakdown
        14: 28,  # Payment Notes
        15: 18,  # Settlement Date
        16: 24,  # Customer Details
    }
    for col_idx, width in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.freeze_panes = 'A5'
    ws.auto_filter.ref = f'A4:P{max(header_row + len(bills), 4)}'

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"sales_report_{from_date or 'all'}_to_{to_date or 'all'}.xlsx"
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename,
    )


@bp.route('/item-report', methods=['GET'])
def item_report():
    from_date = request.args.get('from')
    to_date = request.args.get('to')
    session_type = request.args.get('type')
    staff_id = request.args.get('staff_id')
    category = request.args.get('category')

    from sqlalchemy import func
    from models import MenuItem, Staff

    query = (
        db.session.query(
            MenuItem.name,
            MenuItem.category,
            # OrderItem.quantity is already the net billed qty (reduce-item lowers it
            # and records the removed units in quantity_cancelled) — do not subtract again.
            func.sum(OrderItem.quantity).label('total_qty'),
            func.sum(OrderItem.quantity * (MenuItem.price + func.coalesce(OrderItem.config_price_extra, 0))).label('total_amount')
        )
        .select_from(OrderItem)
        .join(MenuItem, OrderItem.menu_item_id == MenuItem.id)
        .join(Order, OrderItem.order_id == Order.id)
        .join(Session, Order.session_id == Session.id)
        .filter(OrderItem.voided == False)
    )

    if from_date:
        try:
            dt_from = datetime.strptime(from_date, '%Y-%m-%d').replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(Session.created_at >= dt_from)
        except ValueError: pass
    if to_date:
        try:
            dt_to = datetime.strptime(to_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(Session.created_at <= dt_to)
        except ValueError: pass

    if session_type:
        query = query.filter(Session.session_type == session_type)

    if staff_id and staff_id != 'all':
        if staff_id == 'main_pos':
            query = query.filter(Order.placed_by_staff_id.is_(None))
        else:
            query = query.filter(Order.placed_by_staff_id == int(staff_id))

    if category:
        query = query.filter(MenuItem.category == category)

    report = query.group_by(MenuItem.name, MenuItem.category).order_by(func.sum(OrderItem.quantity).desc()).all()

    # Get staff options for filter
    staff_options = Staff.query.filter_by(active=True).all()
    staff_list = [{'id': s.id, 'name': s.name} for s in staff_options]

    # Get distinct categories from this report
    categories = sorted({row[1] for row in report if row[1]})

    out = []
    for name, cat, qty, amount in report:
        if qty and qty > 0:
            out.append({
                'name': name,
                'category': cat,
                'quantity': float(qty),
                'amount': float(amount or 0)
            })

    return jsonify({
        'report': out,
        'staff_options': staff_list,
        'categories': categories
    })


@bp.route('/bill-notes/search', methods=['GET'])
def bill_notes_search():
    q = (request.args.get('q') or '').strip()
    try:
        limit = int(request.args.get('limit') or 10)
    except Exception:
        limit = 10
    limit = max(1, min(30, limit))

    out = []
    seen = set()
    like = f"%{q}%" if q else None

    customers_query = Customer.query
    if like:
        customers_query = customers_query.filter(
            or_(
                Customer.name.ilike(like),
                Customer.phone.ilike(like),
                Customer.address.ilike(like),
                Customer.gstin.ilike(like),
                Customer.notes.ilike(like),
            )
        )
    for c in customers_query.order_by(Customer.last_seen.desc().nullslast(), Customer.created_at.desc()).limit(200).all():
        key = ((c.phone or '').strip(), (c.name or '').strip(), (c.address or '').strip(), (c.gstin or '').strip())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            'customer_id': c.id,
            'bill_id': None,
            'created_at': c.created_at.replace(tzinfo=timezone.utc).isoformat() if c.created_at else None,
            'name': c.name,
            'phone': c.phone,
            'address': c.address,
            'gstin': c.gstin,
            'notes': c.notes,
            'due_subtotal': _outstanding_due_subtotal(customer=c, phone=c.phone),
            'source': 'customer',
        })
        if len(out) >= limit:
            return jsonify({'results': out})

    bill_query = (
        Bill.query
        .filter(
            Bill.is_cancelled.is_(False),
            or_(
                Bill.bill_comment.isnot(None),
                Bill.customer_name.isnot(None),
                Bill.customer_phone.isnot(None),
                Bill.customer_address.isnot(None),
                Bill.customer_gstin.isnot(None),
            ),
        )
    )
    if like:
        bill_query = bill_query.filter(
            or_(
                Bill.customer_name.ilike(like),
                Bill.customer_phone.ilike(like),
                Bill.customer_address.ilike(like),
                Bill.customer_gstin.ilike(like),
                Bill.bill_comment.ilike(like),
            )
        )
    for b in bill_query.order_by(Bill.created_at.desc()).limit(200).all():
        key = ((b.customer_phone or '').strip(), (b.customer_name or '').strip(), (b.customer_address or '').strip(), (b.customer_gstin or '').strip())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            'customer_id': None,
            'bill_id': b.id,
            'created_at': b.created_at.replace(tzinfo=timezone.utc).isoformat() if b.created_at else None,
            'name': b.customer_name,
            'phone': b.customer_phone,
            'address': b.customer_address,
            'gstin': b.customer_gstin,
            'notes': b.customer_notes,
            'due_subtotal': _outstanding_due_subtotal(phone=b.customer_phone),
            'source': 'bill',
        })
        if len(out) >= limit:
            break

    return jsonify({'results': out})


@bp.route('/due-summary', methods=['GET'])
def due_summary():
    token = request.args.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400
    sess = Session.query.filter_by(token=token, status='active').first()
    if not sess:
        return jsonify({'error': 'Session not found'}), 404
    amounts = _bill_amounts(sess)
    info = amounts['customer_info']
    return jsonify({
        'customer_name': info.get('name') or None,
        'customer_phone': info.get('phone') or None,
        'previous_due_subtotal': amounts['previous_due_subtotal'],
        'previous_due_payable': amounts['previous_due_payable'],
        'has_due_customer': bool(info.get('phone')),
    })


@bp.route('/dues', methods=['GET'])
def dues_list():
    open_bills = (
        Bill.query
        .filter(
            Bill.payment_method == 'due',
            Bill.due_status == 'open',
        )
        .order_by(Bill.settled_at.desc(), Bill.id.desc())
        .all()
    )

    grouped = {}
    phones = set()
    for bill in open_bills:
        phone = _normalize_phone(bill.customer_phone)
        key = phone or f'bill:{bill.id}'
        bucket = grouped.setdefault(key, {
            'phone': phone or (bill.customer_phone or '').strip(),
            'bills': [],
        })
        bucket['bills'].append(bill)
        if phone:
            phones.add(phone)

    customers = {}
    if phones:
        customers = {
            _normalize_phone(c.phone): c
            for c in Customer.query.filter(Customer.phone.in_(list(phones))).all()
            if _normalize_phone(c.phone)
        }

    out = []
    for bucket in grouped.values():
        bills = bucket['bills']
        if not bills:
            continue
        latest = max(bills, key=lambda b: b.due_cleared_at or b.settled_at or b.created_at or datetime.min)
        phone = bucket['phone']
        customer = customers.get(_normalize_phone(phone)) if phone else None
        outstanding_subtotal = round(sum(_bill_open_due_subtotal(b) for b in bills), 2)
        outstanding_payable = round(sum(_bill_open_due_payable(b) for b in bills), 2)
        if outstanding_subtotal <= 0.009 and outstanding_payable <= 0.009:
            continue
        out.append({
            'customer_id': customer.id if customer else None,
            'phone': phone,
            'name': latest.customer_name or (customer.name if customer else None),
            'gstin': latest.customer_gstin or (customer.gstin if customer else None),
            'address': latest.customer_address or (customer.address if customer else None),
            'notes': latest.customer_notes or (customer.notes if customer else None),
            'outstanding_subtotal': outstanding_subtotal,
            'outstanding_payable': outstanding_payable,
            'last_entry_at': (latest.due_cleared_at or latest.settled_at or latest.created_at).replace(tzinfo=timezone.utc).isoformat() if (latest.due_cleared_at or latest.settled_at or latest.created_at) else None,
            'open_due_bills': [
                {
                    'bill_id': b.id,
                    'created_at': b.created_at.replace(tzinfo=timezone.utc).isoformat() if b.created_at else None,
                    'session_type': b.session_type,
                    'table_number': b.table_number,
                    'pickup_code': b.pickup_code,
                    'amount': float(b.amount or 0),
                    'due_added_subtotal': float(b.due_added_subtotal or 0),
                    'due_cleared_subtotal': float(b.due_cleared_subtotal or 0),
                    'open_due_subtotal': round(_bill_open_due_subtotal(b), 2),
                    'open_due_payable': _bill_open_due_payable(b),
                }
                for b in bills
            ],
        })
    out.sort(key=lambda row: row.get('last_entry_at') or '', reverse=True)
    return jsonify({'dues': out})


@bp.route('/dues/settle', methods=['POST'])
def settle_dues():
    data = request.get_json(force=True, silent=True) or {}
    customer_id = data.get('customer_id')
    phone = _normalize_phone(data.get('phone'))
    method = (data.get('method') or '').strip()
    comment = (data.get('comment') or '').strip()
    settled_by = (data.get('settled_by') or '').strip() or None
    if method not in ('cash', 'upi_offline', 'card_offline', 'upi', 'card', 'razorpay'):
        return jsonify({'error': 'valid settlement method required'}), 400

    customer = db.session.get(Customer, int(customer_id)) if customer_id else None
    if not customer and not phone:
        return jsonify({'error': 'customer_id or phone required'}), 400

    outstanding = _outstanding_due_subtotal(customer=customer, phone=phone)
    if outstanding <= 0:
        return jsonify({'error': 'No outstanding due found'}), 400
    outstanding_payable = _outstanding_due_payable(customer=customer, phone=phone)
    amount_payable = round(float(data.get('amount_payable') or outstanding_payable), 2)
    if amount_payable <= 0:
        return jsonify({'error': 'Settlement amount must be greater than 0'}), 400
    if amount_payable - outstanding_payable > 0.01:
        return jsonify({'error': 'Settlement amount cannot exceed outstanding payable'}), 400

    info = {
        'name': customer.name if customer else None,
        'phone': customer.phone if customer else phone,
        'gstin': customer.gstin if customer else None,
        'address': customer.address if customer else None,
        'notes': customer.notes if customer else None,
    }
    now = datetime.now(timezone.utc)
    cleared = _sync_due_bill_status(customer, info, method=method, settled_at=now, clear_payable_amount=amount_payable)
    if cleared['cleared_subtotal'] <= 0:
        return jsonify({'error': 'Nothing was settled'}), 400
    for alloc in cleared.get('allocations', []):
        db.session.add(LedgerEntry(
            customer_id=customer.id if customer else None,
            bill_id=alloc['bill_id'],
            entry_type='settlement',
            amount_subtotal=-float(alloc['amount_subtotal'] or 0),
            payment_method=method,
            customer_name=info.get('name'),
            customer_phone=info.get('phone'),
            customer_gstin=info.get('gstin'),
            customer_address=info.get('address'),
            customer_notes=info.get('notes'),
            comment=comment or f'Due settled via {method}',
            created_at=now,
        ))
    db.session.commit()
    return jsonify({
        'message': 'Due settled',
        'cleared_subtotal': cleared['cleared_subtotal'],
        'cleared_payable': cleared['cleared_payable'],
        'remaining_subtotal': _outstanding_due_subtotal(customer=customer, phone=phone),
        'remaining_payable': _outstanding_due_payable(customer=customer, phone=phone),
        'method': method,
        'settled_by': settled_by,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# BILL SPLITTING
# ═══════════════════════════════════════════════════════════════════════════════

from models import BillSplit


def _split_dict(s):
    import json as _json
    return {
        'id': s.id,
        'bill_id': s.bill_id,
        'person_name': s.person_name,
        'amount': s.amount,
        'items': _json.loads(s.items) if s.items else [],
        'is_paid': s.is_paid,
        'paid_at': s.paid_at.isoformat() if s.paid_at else None,
        'payment_method': s.payment_method,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    }


@bp.route('/<int:bill_id>/splits', methods=['GET'])
def get_bill_splits(bill_id):
    """Get all splits for a bill."""
    bill = Bill.query.get_or_404(bill_id)
    splits = BillSplit.query.filter_by(bill_id=bill_id).all()
    return jsonify({
        'bill_id': bill_id,
        'total_amount': bill.amount,
        'splits': [_split_dict(s) for s in splits],
        'total_split': sum(s.amount for s in splits),
        'remaining': bill.amount - sum(s.amount for s in splits) if splits else bill.amount,
    })


@bp.route('/<int:bill_id>/splits', methods=['POST'])
def create_bill_split(bill_id):
    """Create a new split for a bill.
    
    Request body:
    - person_name: Name of person
    - amount: Amount they will pay
    - items: Optional JSON array of {order_item_id, quantity, amount}
    """
    bill = Bill.query.get_or_404(bill_id)
    
    if bill.settled_at:
        return jsonify({'error': 'Cannot split a settled bill'}), 400
    
    data = request.get_json(force=True, silent=True) or {}
    person_name = (data.get('person_name') or '').strip()
    amount = float(data.get('amount', 0))
    
    if not person_name or amount <= 0:
        return jsonify({'error': 'person_name and positive amount required'}), 400
    
    # Check if total splits would exceed bill amount (with tolerance for floating point)
    existing_splits = BillSplit.query.filter_by(bill_id=bill_id).all()
    total_existing = sum(s.amount for s in existing_splits)
    bill_amount = bill.amount or 0
    # Allow 1 rupee tolerance for floating point rounding issues
    if bill_amount > 0 and total_existing + amount > bill_amount + 1:
        return jsonify({'error': f'Split total {total_existing + amount:.2f} would exceed bill amount of {bill_amount:.2f}'}), 400
    
    items_json = json.dumps(data.get('items', [])) if data.get('items') else None
    
    split = BillSplit(
        bill_id=bill_id,
        person_name=person_name,
        amount=amount,
        items=items_json,
    )
    db.session.add(split)
    db.session.commit()
    
    return jsonify(_split_dict(split)), 201


@bp.route('/<int:bill_id>/splits/<int:split_id>/pay', methods=['POST'])
def pay_bill_split(bill_id, split_id):
    """Mark a split as paid."""
    split = BillSplit.query.filter_by(id=split_id, bill_id=bill_id).first_or_404()
    
    if split.is_paid:
        return jsonify({'error': 'Already paid'}), 400
    
    data = request.get_json(force=True, silent=True) or {}
    payment_method = (data.get('payment_method') or 'cash').strip()
    
    split.is_paid = True
    split.paid_at = datetime.now(timezone.utc)
    split.payment_method = payment_method
    
    # Check if all splits are paid
    all_splits = BillSplit.query.filter_by(bill_id=bill_id).all()
    if all(s.is_paid for s in all_splits):
        # Mark bill as settled
        bill = Bill.query.get(bill_id)
        if bill:
            bill.settled_at = datetime.now(timezone.utc)
            bill.payment_method = 'split'
            # Build split payments JSON
            splits_data = [
                {'method': s.payment_method, 'amount': s.amount, 'person': s.person_name}
                for s in all_splits
            ]
            bill.split_payments = json.dumps(splits_data)
    
    db.session.commit()
    return jsonify(_split_dict(split))


@bp.route('/<int:bill_id>/splits/<int:split_id>', methods=['DELETE'])
def delete_bill_split(bill_id, split_id):
    """Delete a split (only if not paid)."""
    split = BillSplit.query.filter_by(id=split_id, bill_id=bill_id).first_or_404()
    
    if split.is_paid:
        return jsonify({'error': 'Cannot delete a paid split'}), 400
    
    db.session.delete(split)
    db.session.commit()
    return jsonify({'message': 'deleted'})


@bp.route('/<int:bill_id>/splits/<int:split_id>/print', methods=['POST'])
def print_split_bill(bill_id, split_id):
    """Print a single split as its own bill (same bill number)."""
    bill = Bill.query.get_or_404(bill_id)
    split = BillSplit.query.filter_by(id=split_id, bill_id=bill_id).first_or_404()
    
    import json as _json
    bill_items = _json.loads(bill.items_snapshot) if bill.items_snapshot else []
    split_items = _json.loads(split.items) if split.items else []
    
    # Filter bill items to only those in this split (match by name and price)
    filtered_items = []
    for si in split_items:
        si_name = si.get('name')
        si_qty = si.get('quantity', 1)
        si_amt = si.get('amount', 0)
        si_price = round(si_amt / si_qty, 2) if si_qty > 0 else 0
        
        # Find matching item in bill_items to get correct price and variants
        for bi in bill_items:
            # Match by name and effective price
            if bi.get('name') == si_name and abs(bi.get('price', 0) - si_price) < 0.05:
                filtered_items.append({
                    'name': bi.get('name'),
                    'quantity': si_qty,
                    'price': bi.get('price'),
                    'variants': bi.get('variants', []),
                    'notes': bi.get('notes', ''),
                })
                break
    
    # Calculate proportional tax
    bill_total = bill.amount or 1
    proportion = split.amount / bill_total if bill_total > 0 else 0
    split_cgst = round((bill.cgst_amount or 0) * proportion, 2)
    split_sgst = round((bill.sgst_amount or 0) * proportion, 2)
    split_subtotal = round(split.amount - split_cgst - split_sgst, 2)
    
    from config import CGST_RATE, SGST_RATE
    
    # Get session for additional info
    sess = db.session.get(Session, bill.session_id)
    captain = sess.opened_by.name if sess and sess.opened_by else None
    
    return jsonify({
        'bill_id': bill.id,
        'split_id': split.id,
        'person_name': split.person_name,
        'customer_name': split.person_name,  # Map person_name to customer_name for standard format
        'table_number': bill.table_number,
        'pickup_code': sess.pickup_code if sess else None,
        'session_type': sess.session_type if sess else 'table',
        'captain': captain,
        'items': filtered_items,
        'subtotal': split_subtotal,
        'cgst': split_cgst,
        'sgst': split_sgst,
        'cgst_rate': CGST_RATE,
        'sgst_rate': SGST_RATE,
        'total': split.amount,
        'payment_method': split.payment_method,
        'is_paid': split.is_paid,
        'printed_at': datetime.now(timezone.utc).isoformat(),
        'settled_at': bill.settled_at.isoformat() if bill.settled_at else None,
        'created_at': bill.created_at.isoformat() if bill.created_at else None,
    })
