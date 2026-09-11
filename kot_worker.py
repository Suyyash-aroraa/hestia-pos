"""Background worker that drains the KOT queue by printing directly via win32print.
Started as a daemon thread from app.py so queued KOTs still print even when
no head-pos browser tab is active."""
import json
import time
from datetime import datetime, timezone

from printing import build_kot_raw
from printing.raw_print import send_raw


def enqueue_kot(order, event_data=None, printer_name=''):
    """Insert a KOT into the queue (dedup by order_id). Returns queue entry id."""
    import config
    if not config.KITCHEN_PRINTER_ENABLED:
        return None
    from models import KOTQueue, db
    from routes.takeout import _pickup_slot_number

    session = order.session
    if not session:
        return None

    # Build location string matching head-pos.js logic
    if session.session_type == 'takeout':
        parcel = _pickup_slot_number(session.pickup_code)
        loc = f"Parcel #{parcel}" if parcel else f"Takeout #{(session.pickup_code or '')[-4:]}"
    else:
        loc = f"Table {session.table_number or ''}"

    items = []
    if event_data:
        items = event_data.get('items', event_data.get('fired_items', []))
    if not items:
        # Fallback: build from order items directly
        for oi in sorted(order.items, key=lambda x: x.id):
            if oi.voided or oi.held:
                continue
            name = oi.menu_item.name if oi.menu_item else ''
            label = ''
            if oi.config_choices:
                try:
                    choices = json.loads(oi.config_choices)
                    if isinstance(choices, dict):
                        label = ' \u00b7 '.join(str(v) for v in choices.values() if v)
                except Exception:
                    pass
            notes = (label + (' \u2014 ' + oi.notes if oi.notes else '')) if label else (oi.notes or '')
            items.append({'name': name, 'quantity': oi.quantity, 'notes': notes})

    # Dedup - if existing pending/printing KOT exists, update it with new items
    existing = KOTQueue.query.filter(
        KOTQueue.order_id == order.id,
        KOTQueue.status.in_(('pending', 'printing')),
    ).first()
    if existing:
        # Update the existing entry with the new items
        existing.items = json.dumps(items) if items else None
        if event_data:
            existing.kot_comment = event_data.get('kot_comment', existing.kot_comment or '')
        db.session.commit()
        _mark_items_queued(order)
        return existing.id

    entry = KOTQueue(
        order_id=order.id,
        session_type=session.session_type,
        table_number=session.table_number,
        pickup_code=session.pickup_code,
        session_token=session.token,
        location=loc,
        items=json.dumps(items) if items else None,
        reprint=bool(event_data.get('reprint')) if event_data else False,
        order_type='Takeout' if session.session_type == 'takeout' else 'Dine-In',
        kot_comment=(order.kot_comment or '') if order else '',
        kot_type='normal',
        printer_name=printer_name,
        retry_count=0,
        status='pending',
    )
    db.session.add(entry)
    db.session.commit()
    _mark_items_queued(order)
    return entry.id


def _mark_items_queued(order):
    """Flag all currently-fired (non-held, non-voided) items of an order as
    having a KOT row, so the reconciliation sweep never re-prints them."""
    from models import db
    changed = False
    for oi in order.items:
        if oi.held or oi.voided:
            continue
        if not oi.kot_queued:
            oi.kot_queued = True
            changed = True
    if changed:
        db.session.commit()


def enqueue_void_kot(order, item_name, quantity, config_choices=None, notes=None, printer_name=''):
    """Insert a VOID KOT into the queue."""
    import config
    if not config.KITCHEN_PRINTER_ENABLED:
        return None
    import json as _json
    from models import KOTQueue, db
    from routes.takeout import _pickup_slot_number

    session = order.session
    if not session:
        return None

    if session.session_type == 'takeout':
        parcel = _pickup_slot_number(session.pickup_code)
        loc = f"Parcel #{parcel}" if parcel else f"Takeout #{(session.pickup_code or '')[-4:]}"
    else:
        loc = f"Table {session.table_number or ''}"

    entry = KOTQueue(
        order_id=order.id,
        session_type=session.session_type,
        table_number=session.table_number,
        pickup_code=session.pickup_code,
        session_token=session.token,
        location=loc,
        order_type='Takeout' if session.session_type == 'takeout' else 'Dine-In',
        kot_type='void',
        void_item_name=item_name,
        void_quantity=quantity,
        void_item_config=_json.dumps(config_choices) if config_choices else None,
        void_item_notes=notes,
        printer_name=printer_name,
        retry_count=0,
        status='pending',
    )
    db.session.add(entry)
    db.session.commit()
    return entry.id


RECONCILE_GRACE_SECONDS = 120
FAILED_RETENTION_HOURS = 24

# Order statuses that mean the KOT should NOT (re)print.
_DEAD_ORDER_STATUSES = ('cancelled', 'collected')
# Session statuses where firing is still valid.
_LIVE_SESSION_STATUSES = ('active',)


def _reconcile_unqueued(db):
    """Find fired items that never got a KOT row (silent enqueue failure) and
    re-enqueue them. Items belonging to cancelled/closed orders are instead
    marked queued so they stop matching the sweep without printing."""
    from datetime import datetime, timedelta
    from models import Order, OrderItem, Session

    cutoff = datetime.now() - timedelta(seconds=RECONCILE_GRACE_SECONDS)
    rows = (
        OrderItem.query
        .join(Order, OrderItem.order_id == Order.id)
        .join(Session, Order.session_id == Session.id)
        .filter(
            OrderItem.kot_queued.is_(False),
            OrderItem.held.is_(False),
            OrderItem.voided.is_(False),
            Order.created_at < cutoff,
        )
        .all()
    )
    if not rows:
        return

    reenqueue_order_ids = set()
    dead_changed = False
    for oi in rows:
        order = oi.order
        sess = order.session if order else None
        dead = (
            order is None
            or order.status in _DEAD_ORDER_STATUSES
            or sess is None
            or sess.status not in _LIVE_SESSION_STATUSES
            # online takeout that hasn't been paid yet shouldn't print
            or (sess.session_type == 'takeout'
                and (sess.source or 'offline') == 'online'
                and order.status == 'processing')
        )
        if dead:
            oi.kot_queued = True  # never meant to print; stop matching
            dead_changed = True
        else:
            reenqueue_order_ids.add(order.id)

    if dead_changed:
        db.session.commit()

    for oid in reenqueue_order_ids:
        order = db.session.get(Order, oid)
        if not order:
            continue
        try:
            enqueue_kot(order)
            print(f'[kot-worker] reconciled unqueued KOT for order {oid}')
        except Exception as e:
            print(f'[kot-worker] reconcile failed for order {oid}: {e}')


def _kot_janitor(db):
    """Purge stale KOTQueue rows: failed rows past retention, and
    pending/printing rows whose order no longer exists or was cancelled."""
    from datetime import datetime, timedelta
    from models import KOTQueue, Order

    # 1. Old failed rows
    failed_cutoff = datetime.now() - timedelta(hours=FAILED_RETENTION_HOURS)
    purged = (
        KOTQueue.query
        .filter(KOTQueue.status == 'failed', KOTQueue.created_at < failed_cutoff)
        .delete(synchronize_session=False)
    )

    # 2. Orphan / cancelled rows among pending/printing
    live = KOTQueue.query.filter(KOTQueue.status.in_(('pending', 'printing'))).all()
    for row in live:
        order = db.session.get(Order, row.order_id)
        if order is None or order.status in _DEAD_ORDER_STATUSES:
            db.session.delete(row)
            purged += 1

    if purged:
        db.session.commit()
        print(f'[kot-worker] janitor purged {purged} stale KOT rows')


def _kot_worker_loop(app, db):
    """Poll the kot_queue table and print pending KOTs via ESC/POS RAW."""
    from models import KOTQueue
    from routes.print_svc import _get_config_value

    cycle = 0
    while True:
        time.sleep(2)
        cycle += 1
        try:
            with app.app_context():
                # 1. Recover KOTs stuck in 'printing' for > 60s (head-pos crashed, etc.)
                from datetime import datetime, timedelta
                stuck_cutoff = datetime.now() - timedelta(seconds=60)
                created_fallback_cutoff = datetime.now() - timedelta(seconds=180)
                stuck = (
                    KOTQueue.query
                    .filter(
                        KOTQueue.status == 'printing',
                        db.or_(
                            db.and_(KOTQueue.printed_at.isnot(None), KOTQueue.printed_at < stuck_cutoff),
                            db.and_(KOTQueue.printed_at.is_(None), KOTQueue.created_at < created_fallback_cutoff),
                        ),
                    )
                    .all()
                )
                for s in stuck:
                    s.status = 'pending'
                    s.printed_at = None
                    s.retry_count += 1
                    print(f'[kot-worker] recovered stuck KOT #{s.id} (order {s.order_id})')
                if stuck:
                    db.session.commit()

                # 1b. Reconciliation sweep (~every 8s): re-enqueue fired-but-
                #     never-queued items so a silently-failed enqueue can't lose a KOT.
                if cycle % 4 == 0:
                    try:
                        _reconcile_unqueued(db)
                    except Exception as se:
                        print(f'[kot-worker] reconcile error: {se}')

                # 1c. Janitor (~every 60s): purge failed / orphaned / cancelled rows.
                if cycle % 30 == 0:
                    try:
                        _kot_janitor(db)
                    except Exception as je:
                        print(f'[kot-worker] janitor error: {je}')

                # 2. Pick oldest pending row with row-lock
                now_dt = datetime.now()
                entry = (
                    KOTQueue.query
                    .filter_by(status='pending')
                    .order_by(KOTQueue.created_at.asc())
                    .with_for_update()
                    .first()
                )
                if not entry:
                    continue

                entry.status = 'printing'
                entry.printed_at = now_dt
                db.session.commit()

                printer_name = entry.printer_name or _get_config_value('KITCHEN_PRINTER_NAME')
                if not printer_name:
                    raise RuntimeError('KITCHEN_PRINTER_NAME not configured')

                import win32print
                if entry.kot_type == 'void':
                    # Build void KOT raw bytes
                    from printing.kot_builder import _kot_beep_raw
                    import json as _json
                    esc = b'\x1b'
                    gs = b'\x1d'
                    from datetime import datetime
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
                    out.append((f"* {entry.location} *\n").encode('cp437', errors='replace'))
                    out.append(gs + b'!\x00')
                    out.append(esc + b'a\x00')
                    out.append(solid)
                    out.append((f"KOT #{entry.order_id} | {date_str} | {time_str}\n").encode('cp437', errors='replace'))
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
                    out.append((f"{entry.void_quantity or 1}x    {entry.void_item_name or 'Item'}\n").encode('cp437', errors='replace'))
                    out.append(gs + b'!\x00')
                    out.append(esc + b'M\x00')

                    # Print config label and notes in same format as normal KOT
                    config_label = ''
                    if entry.void_item_config:
                        config_choices = _json.loads(entry.void_item_config)
                        if isinstance(config_choices, dict):
                            config_label = ' · '.join(str(v) for v in config_choices.values() if v)
                    
                    item_line = ''
                    if config_label:
                        item_line = f"  {config_label}\n"
                        out.append(item_line.encode('cp437', errors='replace'))
                    if entry.void_item_notes:
                        notes_line = f"  {entry.void_item_notes}\n"
                        out.append(notes_line.encode('cp437', errors='replace'))

                    out.append(dash)
                    out.append(esc + b'a\x01')
                    out.append(b'END OF KOT\n')
                    out.append(esc + b'a\x00')
                    out.append(b'\n\n\n')
                    out.append(gs + b'V\x01')
                    raw = b''.join(out)
                else:
                    from models import Order
                    items = json.loads(entry.items) if entry.items else []
                    order = db.session.get(Order, entry.order_id)
                    if order is not None:
                        if entry.reprint:
                            kot_printed_at = order.kot_printed_at
                        else:
                            if order.kot_printed_at is None:
                                order.kot_printed_at = datetime.now(timezone.utc)
                            kot_printed_at = order.kot_printed_at
                    else:
                        kot_printed_at = None
                    raw = build_kot_raw(
                        str(entry.order_id),
                        entry.location or '',
                        items,
                        reprint=entry.reprint,
                        kot_comment=entry.kot_comment or None,
                        token=entry.pickup_code,
                        kot_printed_at=kot_printed_at,
                    )

                doc_name = 'VOID KOT' if entry.kot_type == 'void' else 'KOT'
                # send_raw restarts the print spooler and replays the job if the
                # spooler has died — otherwise every queued KOT burns its three
                # retries against a service that is not coming back on its own.
                send_raw(printer_name, raw, doc_name)

                # Success — delete the row
                db.session.delete(entry)
                db.session.commit()

        except Exception as e:
            try:
                with app.app_context():
                    if entry:
                        entry.retry_count += 1
                        entry.printed_at = None
                        if entry.retry_count >= 3:
                            entry.status = 'failed'
                        else:
                            entry.status = 'pending'
                        db.session.commit()
            except Exception:
                pass
            print(f'[kot-worker] error: {e}', flush=True)
