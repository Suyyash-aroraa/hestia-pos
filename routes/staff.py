from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from models import Staff, db

bp = Blueprint('staff', __name__, url_prefix='/api/staff')


def _staff_dict(s):
    return {
        'id': s.id,
        'name': s.name,
        'role': s.role,
        'active': s.active,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    }


@bp.route('/auth', methods=['POST'])
def auth():
    """Verify a captain PIN and return staff info. Public endpoint."""
    data = request.get_json(force=True, silent=True) or {}
    pin = str(data.get('pin', '')).strip()
    if not pin:
        return jsonify({'error': 'pin required'}), 400
    if not pin.isdigit() or len(pin) != 6:
        return jsonify({'error': 'PIN must be exactly 6 digits'}), 400

    staff_members = Staff.query.filter_by(active=True).all()
    for s in staff_members:
        if check_password_hash(s.pin_hash, pin):
            return jsonify({'id': s.id, 'name': s.name, 'role': s.role})

    return jsonify({'error': 'Invalid PIN'}), 401


@bp.route('/validate-pin', methods=['POST'])
def validate_pin():
    """Verify a captain PIN and return staff info. Alias for auth endpoint."""
    return auth()
