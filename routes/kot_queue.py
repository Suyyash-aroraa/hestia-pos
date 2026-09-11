import json
from datetime import datetime

from flask import Blueprint, jsonify, request

from models import KOTQueue, db

bp = Blueprint('kot_queue', __name__, url_prefix='/api/kot-queue')


@bp.route('/add', methods=['POST'])
def add_kot():
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get('order_id')
    if not order_id:
        return jsonify({'error': 'order_id required'}), 400

    # Dedup: if a pending/printing entry exists for this order_id, return its id
    existing = KOTQueue.query.filter(
        KOTQueue.order_id == int(order_id),
        KOTQueue.status.in_(('pending', 'printing')),
    ).first()
    if existing:
        return jsonify({'queued': True, 'id': existing.id})

    items = data.get('items', [])
    entry = KOTQueue(
        order_id=int(order_id),
        session_type=data.get('session_type') or '',
        table_number=data.get('table_number'),
        pickup_code=data.get('pickup_code'),
        session_token=data.get('session_token'),
        location=data.get('location') or '',
        items=json.dumps(items) if items else None,
        reprint=bool(data.get('reprint')),
        order_type=data.get('order_type') or '',
        kot_comment=data.get('kot_comment') or '',
        printer_name=data.get('printer_name') or '',
        retry_count=0,
        status='pending',
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'queued': True, 'id': entry.id})


@bp.route('/poll', methods=['GET'])
def poll_kot():
    """Return the oldest pending KOT entry, or 204 if none.
    Atomically marks it as 'printing' so concurrent pollers/workers
    cannot grab the same row."""
    entry = (
        KOTQueue.query
        .filter_by(status='pending')
        .order_by(KOTQueue.created_at.asc())
        .with_for_update()
        .first()
    )
    if not entry:
        return '', 204

    entry.status = 'printing'
    entry.printed_at = datetime.now()
    db.session.commit()

    return jsonify({
        'id': entry.id,
        'order_id': entry.order_id,
        'location': entry.location,
        'items': json.loads(entry.items) if entry.items else [],
        'reprint': entry.reprint,
        'order_type': entry.order_type,
        'kot_comment': entry.kot_comment,
        'session_type': entry.session_type,
        'pickup_code': entry.pickup_code,
        'session_token': entry.session_token,
        'kot_type': entry.kot_type or 'normal',
        'void_item_name': entry.void_item_name,
        'void_quantity': entry.void_quantity,
    })


@bp.route('/confirm', methods=['POST'])
def confirm_kot():
    data = request.get_json(force=True, silent=True) or {}
    entry_id = data.get('id')
    if not entry_id:
        return jsonify({'error': 'id required'}), 400

    entry = db.session.get(KOTQueue, int(entry_id))
    if not entry:
        return jsonify({'ok': True, 'note': 'already deleted'})

    db.session.delete(entry)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/status/<int:entry_id>', methods=['GET'])
def kot_status(entry_id):
    entry = db.session.get(KOTQueue, entry_id)
    if not entry:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'found': True,
        'id': entry.id,
        'status': entry.status,
        'order_id': entry.order_id,
        'retry_count': entry.retry_count,
    })


@bp.route('/status-by-order/<int:order_id>', methods=['GET'])
def kot_status_by_order(order_id):
    """Resolve KOT state when the client only knows the order_id.
    active=False means no pending/printing row exists (already printed/cleared)."""
    entry = (
        KOTQueue.query
        .filter(
            KOTQueue.order_id == order_id,
            KOTQueue.status.in_(('pending', 'printing', 'failed')),
        )
        .order_by(KOTQueue.created_at.desc())
        .first()
    )
    if not entry:
        return jsonify({'active': False, 'status': None, 'id': None, 'order_id': order_id})
    return jsonify({
        'active': entry.status in ('pending', 'printing'),
        'status': entry.status,
        'id': entry.id,
        'order_id': order_id,
        'retry_count': entry.retry_count,
    })


@bp.route('/retry', methods=['POST'])
def retry_kot():
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get('order_id')
    if not order_id:
        return jsonify({'error': 'order_id required'}), 400

    entry = KOTQueue.query.filter_by(order_id=int(order_id)).first()
    if not entry:
        return jsonify({'error': 'not found'}), 404

    entry.status = 'pending'
    entry.printed_at = None
    entry.retry_count = 0
    entry.created_at = __import__('models').utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'id': entry.id})
