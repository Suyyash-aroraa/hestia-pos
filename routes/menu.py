import time as _time
from flask import Blueprint, jsonify, request, session

import json as _json
from models import MenuItem, MenuItemConfig, NonVegItem, db

_MENU_CACHE: list | None = None
_MENU_CACHE_TS: float = 0.0
_MENU_CACHE_TTL = 60  # seconds


def _invalidate_menu_cache():
    global _MENU_CACHE, _MENU_CACHE_TS
    _MENU_CACHE = None
    _MENU_CACHE_TS = 0.0


def _normalize_options(raw):
    """Return [{label, extra}] from either plain-string or structured options array."""
    out = []
    for o in (raw or []):
        if isinstance(o, dict):
            out.append({'label': str(o.get('label', '')), 'extra': float(o.get('extra') or 0)})
        else:
            out.append({'label': str(o), 'extra': 0.0})
    return out

bp = Blueprint('menu', __name__, url_prefix='/api/menu')


@bp.route('', methods=['GET'])
def public_menu():
    global _MENU_CACHE, _MENU_CACHE_TS
    now = _time.monotonic()
    if _MENU_CACHE is not None and (now - _MENU_CACHE_TS) < _MENU_CACHE_TTL:
        return jsonify(_MENU_CACHE)
    items = (
        MenuItem.query
        .filter(MenuItem.deleted == False)
        .order_by(MenuItem.category.asc(), MenuItem.name.asc())
        .all()
    )
    non_veg_ids = {r.menu_item_id for r in NonVegItem.query.filter_by(non_veg=True).all()}
    by_cat = {}
    for it in items:
        by_cat.setdefault(it.category, []).append({
            'id': it.id,
            'name': it.name,
            'price': float(it.price),
            'description': it.description,
            'category': it.category,
            'available': it.available,
            'non_veg': it.id in non_veg_ids,
            'configs': [
                {'id': c.id, 'group_name': c.group_name,
                 'options': _normalize_options(_json.loads(c.options) if c.options else []),
                 'required': c.required, 'multi_select': c.multi_select}
                for c in it.configs
            ],
        })
    out = [{'category': c, 'items': by_cat[c]} for c in sorted(by_cat.keys())]
    _MENU_CACHE = out
    _MENU_CACHE_TS = now
    return jsonify(out)
