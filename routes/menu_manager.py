import time as _time
from datetime import datetime, timezone
import json

from flask import Blueprint, jsonify, request

from models import (
    Customer, Inventory, MenuItem, MenuItemConfig, NonVegItem, Order, OrderItem, RecipeIngredient,
    StockOrder, Supplier, PurchaseInvoice, PurchaseInvoiceItem, db,
)
from routes.menu import _invalidate_menu_cache as _invalidate_public_menu_cache

bp = Blueprint('menu_manager', __name__, url_prefix='/api/menu-manager')

# ── Menu Cache ─────────────────────────────────────────────────────────────

_MENU_CACHE: list | None = None
_MENU_CACHE_TS: float = 0.0
_MENU_CACHE_TTL = 60  # seconds


def _invalidate_menu_cache():
    global _MENU_CACHE, _MENU_CACHE_TS
    _MENU_CACHE = None
    _MENU_CACHE_TS = 0.0


def _broadcast_menu_update(reason: str):
    from app import push_pos_event
    try:
        from routes.menu import _invalidate_menu_cache as _invalidate_public_menu_cache
        _invalidate_public_menu_cache()
    except Exception:
        pass
    push_pos_event({
        'type': 'menu_update',
        'reason': reason,
        'ts': int(datetime.now(timezone.utc).timestamp()),
    })


def _normalize_options(raw):
    """Return [{label, extra}] from either plain-string or structured options array."""
    out = []
    for o in (raw or []):
        if isinstance(o, dict):
            out.append({'label': str(o.get('label', '')), 'extra': float(o.get('extra') or 0)})
        else:
            out.append({'label': str(o), 'extra': 0.0})
    return out


def _normalize_phone(phone):
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if len(digits) > 10 and digits.startswith('91'):
        digits = digits[-10:]
    return digits


def _customer_dict(c):
    from routes.bills import _outstanding_due_payable, _outstanding_due_subtotal
    return {
        'id': c.id,
        'name': c.name,
        'phone': c.phone,
        'gstin': c.gstin,
        'address': c.address,
        'notes': c.notes,
        'last_seen': c.last_seen.isoformat() if c.last_seen else None,
        'created_at': c.created_at.isoformat() if c.created_at else None,
        'outstanding_due_subtotal': _outstanding_due_subtotal(customer=c, phone=c.phone),
        'outstanding_due_payable': _outstanding_due_payable(customer=c, phone=c.phone),
    }


# ── Saved Customers ──────────────────────────────────────────────────────────

@bp.route('/customers', methods=['GET'])
def list_customers():
    q = (request.args.get('q') or '').strip().lower()
    rows = Customer.query.order_by(Customer.last_seen.desc().nullslast(), Customer.created_at.desc()).all()
    out = []
    for c in rows:
        hay = ' '.join([
            c.name or '',
            c.phone or '',
            c.gstin or '',
            c.address or '',
            c.notes or '',
        ]).lower()
        if q and q not in hay:
            continue
        out.append(_customer_dict(c))
    return jsonify(out)


@bp.route('/customers', methods=['POST'])
def create_customer():
    data = request.get_json(force=True, silent=True) or {}
    phone = _normalize_phone(data.get('phone'))
    if not phone:
        return jsonify({'error': 'phone required'}), 400
    if Customer.query.filter_by(phone=phone).first():
        return jsonify({'error': 'Customer with this phone already exists'}), 400
    customer = Customer(
        phone=phone,
        name=(data.get('name') or '').strip() or None,
        gstin=(data.get('gstin') or '').strip() or None,
        address=(data.get('address') or '').strip() or None,
        notes=(data.get('notes') or '').strip() or None,
    )
    db.session.add(customer)
    db.session.commit()
    return jsonify(_customer_dict(customer)), 201


@bp.route('/customers/<int:customer_id>', methods=['PUT'])
def update_customer(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    data = request.get_json(force=True, silent=True) or {}
    if 'phone' in data:
        phone = _normalize_phone(data.get('phone'))
        if not phone:
            return jsonify({'error': 'phone required'}), 400
        existing = Customer.query.filter(Customer.phone == phone, Customer.id != customer.id).first()
        if existing:
            return jsonify({'error': 'Customer with this phone already exists'}), 400
        customer.phone = phone
    if 'name' in data:
        customer.name = (data.get('name') or '').strip() or None
    if 'gstin' in data:
        customer.gstin = (data.get('gstin') or '').strip() or None
    if 'address' in data:
        customer.address = (data.get('address') or '').strip() or None
    if 'notes' in data:
        customer.notes = (data.get('notes') or '').strip() or None
    db.session.commit()
    return jsonify(_customer_dict(customer))


# ── Menu Item Endpoints ─────────────────────────────────────────────────────

@bp.route('/menu/all', methods=['GET'])
def all_menu():
    items = MenuItem.query.filter(MenuItem.deleted == False).order_by(MenuItem.category.asc(), MenuItem.name.asc()).all()
    return jsonify([
        {
            'id': it.id,
            'name': it.name,
            'price': float(it.price),
            'cost_price': float(getattr(it, 'cost_price', 0)),
            'description': it.description,
            'category': it.category,
            'available': it.available,
        }
        for it in items
    ])


# ── Non-Veg Endpoints ──────────────────────────────────────────────────────

@bp.route('/non-veg', methods=['GET'])
def list_non_veg():
    rows = NonVegItem.query.filter_by(non_veg=True).all()
    return jsonify([r.menu_item_id for r in rows])


@bp.route('/non-veg/toggle/<int:item_id>', methods=['POST'])
def toggle_non_veg(item_id):
    db.get_or_404(MenuItem, item_id)
    row = NonVegItem.query.filter_by(menu_item_id=item_id).first()
    if row:
        row.non_veg = not row.non_veg
    else:
        row = NonVegItem(menu_item_id=item_id, non_veg=True)
        db.session.add(row)
    db.session.commit()
    _invalidate_public_menu_cache()
    _broadcast_menu_update('non_veg_toggle')
    return jsonify({'menu_item_id': item_id, 'non_veg': row.non_veg})


@bp.route('/menu', methods=['POST'])
def create_item():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get('name')
    price = data.get('price')
    category = data.get('category')
    if name is None or price is None or category is None:
        return jsonify({'error': 'name, price, category required'}), 400
    it = MenuItem(
        name=str(name),
        price=float(price),
        category=str(category),
        description=data.get('description'),
        available=True,
    )
    db.session.add(it)
    db.session.commit()
    _invalidate_menu_cache()
    _invalidate_public_menu_cache()
    _broadcast_menu_update('menu_item_created')
    return jsonify({
        'id': it.id,
        'name': it.name,
        'price': float(it.price),
        'description': it.description,
        'category': it.category,
        'available': it.available,
    })


@bp.route('/menu/<int:item_id>', methods=['PUT'])
def update_item(item_id):
    it = MenuItem.query.get_or_404(item_id)
    data = request.get_json(force=True, silent=True) or {}
    if 'name' in data:
        it.name = data['name']
    if 'price' in data:
        it.price = float(data['price'])
    if 'category' in data:
        it.category = data['category']
    if 'description' in data:
        it.description = data['description']
    if 'available' in data:
        it.available = bool(data['available'])
    db.session.commit()
    _invalidate_menu_cache()
    _invalidate_public_menu_cache()
    _broadcast_menu_update('menu_item_updated')
    return jsonify({
        'id': it.id,
        'name': it.name,
        'price': float(it.price),
        'description': it.description,
        'category': it.category,
        'available': it.available,
    })


@bp.route('/menu/<int:item_id>', methods=['DELETE'])
def delete_item(item_id):
    it = MenuItem.query.get_or_404(item_id)
    it.deleted = True
    db.session.commit()
    _invalidate_menu_cache()
    _invalidate_public_menu_cache()
    _broadcast_menu_update('menu_item_deleted')
    return jsonify({'message': 'deleted'})


# ── Config Endpoints ────────────────────────────────────────────────────────

def _config_dict(c):
    return {
        'id': c.id,
        'menu_item_id': c.menu_item_id,
        'group_name': c.group_name,
        'options': [({'label': o, 'extra': 0.0} if isinstance(o, str) else o) for o in (json.loads(c.options) if c.options else [])],
        'required': c.required,
        'multi_select': c.multi_select,
    }


def _ensure_visible_configs(labels):
    """Automatically adds labels to BILL_VISIBLE_CONFIGS in config.py if not already present."""
    if not labels:
        return
    import config
    import os, sys
    
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.py')
    if not os.path.exists(config_path):
        return
    
    with open(config_path, 'r') as f:
        content = f.read()
    
    existing = getattr(config, 'BILL_VISIBLE_CONFIGS', [])
    labels_to_add = [l for l in labels if l not in existing]
    if not labels_to_add:
        return
    
    # Find the BILL_VISIBLE_CONFIGS line and add new labels
    lines = content.split('\n')
    new_lines = []
    for line in lines:
        new_lines.append(line)
        if 'BILL_VISIBLE_CONFIGS' in line and '=' in line:
            # Add new labels after this line
            pass
    
    # Simpler approach: just append to the list in the file
    for i, line in enumerate(lines):
        if 'BILL_VISIBLE_CONFIGS' in line and '=' in line:
            # Extract current list
            import re
            match = re.search(r'BILL_VISIBLE_CONFIGS\s*=\s*\[(.*?)\]', content, re.DOTALL)
            if match:
                current_list = match.group(1)
                for label in labels_to_add:
                    if f'"{label}"' not in current_list and f"'{label}'" not in current_list:
                        current_list = current_list.rstrip() + f', "{label}"'
                lines[i] = line.replace(match.group(1), current_list)
            break
    
    with open(config_path, 'w') as f:
        f.write('\n'.join(lines))


@bp.route('/menu/<int:item_id>/configs', methods=['GET'])
def list_configs(item_id):
    db.get_or_404(MenuItem, item_id)
    configs = MenuItemConfig.query.filter_by(menu_item_id=item_id).all()
    return jsonify([_config_dict(c) for c in configs])


@bp.route('/menu/<int:item_id>/configs', methods=['POST'])
def add_config(item_id):
    db.get_or_404(MenuItem, item_id)
    data = request.get_json(force=True, silent=True) or {}
    group_name = (data.get('group_name') or '').strip()
    options = data.get('options') or []
    if not group_name or not options:
        return jsonify({'error': 'group_name and options required'}), 400
    c = MenuItemConfig(
        menu_item_id=item_id,
        group_name=group_name,
        options=json.dumps([{'label': (o['label'] if isinstance(o, dict) else str(o)).strip(), 'extra': float(o.get('extra') or 0) if isinstance(o, dict) else 0.0} for o in options if (o['label'] if isinstance(o, dict) else str(o)).strip()]),
        required=bool(data.get('required', True)),
        multi_select=bool(data.get('multi_select', False)),
    )
    db.session.add(c)
    db.session.commit()
    _invalidate_menu_cache()
    
    # Auto-add labels with extra price to BILL_VISIBLE_CONFIGS
    labels_to_add = []
    for o in options:
        label = (o['label'] if isinstance(o, dict) else str(o)).strip()
        extra = float(o.get('extra') or 0) if isinstance(o, dict) else 0.0
        if extra != 0 and label:
            labels_to_add.append(label)
    if labels_to_add:
        _ensure_visible_configs(labels_to_add)

    _broadcast_menu_update('menu_config_created')
    return jsonify(_config_dict(c)), 201


@bp.route('/menu/<int:item_id>/configs/<int:config_id>', methods=['PUT'])
def update_config(item_id, config_id):
    c = db.get_or_404(MenuItemConfig, config_id)
    if c.menu_item_id != item_id:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json(force=True, silent=True) or {}
    if 'group_name' in data:
        c.group_name = data['group_name'].strip()
    if 'options' in data:
        c.options = json.dumps([{'label': (o['label'] if isinstance(o, dict) else str(o)).strip(), 'extra': float(o.get('extra') or 0) if isinstance(o, dict) else 0.0} for o in data['options'] if (o['label'] if isinstance(o, dict) else str(o)).strip()])
    if 'required' in data:
        c.required = bool(data['required'])
    if 'multi_select' in data:
        c.multi_select = bool(data['multi_select'])
    db.session.commit()
    _invalidate_menu_cache()

    if 'options' in data:
        labels_to_add = []
        for o in data['options']:
            label = (o['label'] if isinstance(o, dict) else str(o)).strip()
            extra = float(o.get('extra') or 0) if isinstance(o, dict) else 0.0
            if extra != 0 and label:
                labels_to_add.append(label)
        if labels_to_add:
            _ensure_visible_configs(labels_to_add)

    _broadcast_menu_update('menu_config_updated')
    return jsonify(_config_dict(c))


@bp.route('/menu/<int:item_id>/configs/<int:config_id>', methods=['DELETE'])
def delete_config(item_id, config_id):
    c = db.get_or_404(MenuItemConfig, config_id)
    if c.menu_item_id != item_id:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(c)
    db.session.commit()
    _invalidate_menu_cache()
    _broadcast_menu_update('menu_config_deleted')
    return jsonify({'message': 'deleted'})


# ── Ingredient/Recipe Endpoints ───────────────────────────────────────────────

@bp.route('/menu/<int:item_id>/ingredients', methods=['GET'])
def get_recipe(item_id):
    item = db.get_or_404(MenuItem, item_id)
    return jsonify([
        {
            'id': ri.id,
            'inventory_id': ri.inventory_id,
            'name': ri.ingredient.name,
            'unit': ri.ingredient.unit,
            'quantity': ri.quantity,
            'config_group': ri.config_group,
            'config_option': ri.config_option,
        }
        for ri in item.recipe_ingredients
    ])


@bp.route('/menu/<int:item_id>/ingredients', methods=['POST'])
def add_recipe_ingredient(item_id):
    mi = db.get_or_404(MenuItem, item_id)
    data = request.get_json(force=True, silent=True) or {}
    inv_id = data.get('inventory_id')
    qty = data.get('quantity', 1.0)
    config_group = data.get('config_group')
    config_option = data.get('config_option')
    
    if not inv_id:
        return jsonify({'error': 'inventory_id required'}), 400
    inv = db.get_or_404(Inventory, int(inv_id))
    
    # Check if ingredient already exists for this exact combination
    existing = RecipeIngredient.query.filter_by(
        menu_item_id=item_id, 
        inventory_id=inv.id,
        config_group=config_group,
        config_option=config_option
    ).first()
    
    if existing:
        existing.quantity = float(qty)
        db.session.commit()
        ri = existing
    else:
        ri = RecipeIngredient(
            menu_item_id=item_id, 
            inventory_id=inv.id, 
            quantity=float(qty),
            config_group=config_group,
            config_option=config_option
        )
        db.session.add(ri)
        db.session.commit()
    
    # Refresh the menu item to ensure recipe_ingredients are loaded
    db.session.refresh(mi)
    
    # Recalculate menu item cost
    from models import calculate_menu_item_cost
    mi.cost_price = calculate_menu_item_cost(mi)
    db.session.commit()
    
    return jsonify({
        'id': ri.id,
        'inventory_id': ri.inventory_id,
        'name': inv.name,
        'unit': inv.unit,
        'quantity': ri.quantity,
    }), 201


@bp.route('/menu/<int:item_id>/ingredients/<int:ri_id>', methods=['DELETE'])
def remove_recipe_ingredient(item_id, ri_id):
    ri = RecipeIngredient.query.filter_by(
        menu_item_id=item_id, id=ri_id).first_or_404()
    db.session.delete(ri)
    db.session.commit()
    
    # Recalculate menu item cost
    mi = db.session.get(MenuItem, item_id)
    if mi:
        # Refresh to ensure recipe_ingredients are loaded
        db.session.refresh(mi)
        from models import calculate_menu_item_cost
        mi.cost_price = calculate_menu_item_cost(mi)
        db.session.commit()
    
    return jsonify({'message': 'removed'})


# ── Inventory Endpoints ─────────────────────────────────────────────────────

def _inv_dict(r):
    linked = [{'id': ri.menu_item_id, 'name': ri.menu_item.name, 'quantity': ri.quantity}
              for ri in r.recipe_uses]
    return {
        'id': r.id,
        'name': r.name,
        'unit': r.unit,
        'stock_level': r.stock_level,
        'average_unit_cost': float(getattr(r, 'average_unit_cost', 0)),
        'last_purchase_price': float(getattr(r, 'last_purchase_price', 0)),
        'low_stock_threshold': r.low_stock_threshold,
        'is_low': r.stock_level <= r.low_stock_threshold,
        'linked_items': linked,
    }


@bp.route('/inventory', methods=['GET'])
def list_inventory():
    rows = Inventory.query.order_by(Inventory.name.asc()).all()
    return jsonify([_inv_dict(r) for r in rows])


@bp.route('/inventory/items', methods=['POST'])
def create_inventory():
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
    inv = db.get_or_404(Inventory, inv_id)
    db.session.delete(inv)
    db.session.commit()
    return jsonify({'message': 'deleted'})


@bp.route('/inventory/disable-affected', methods=['POST'])
def disable_affected():
    """Disable all menu items whose ingredients are at or below threshold."""
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


# ── Stock Order Endpoints ───────────────────────────────────────────────────

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
    status_filter = request.args.get('status')
    q = StockOrder.query.order_by(StockOrder.created_at.desc())
    if status_filter:
        q = q.filter(StockOrder.status == status_filter)
    return jsonify([_so_dict(so) for so in q.all()])


@bp.route('/stock-orders', methods=['POST'])
def create_stock_order():
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
    """Mark order as arrived and add received quantity to inventory.
    Optionally creates a purchase invoice if supplier_id is provided."""
    so = db.get_or_404(StockOrder, so_id)
    if so.status == 'arrived':
        return jsonify({'error': 'Already marked as arrived'}), 400
    data = request.get_json(force=True, silent=True) or {}
    qty_received = float(data.get('quantity_received', so.quantity_ordered))
    so.quantity_received = qty_received
    so.status = 'arrived'
    so.arrived_at = datetime.now(timezone.utc)
    so.ingredient.stock_level = so.ingredient.stock_level + qty_received
    
    purchase_invoice_id = None
    
    # Auto-create purchase invoice if supplier_id provided
    supplier_id = data.get('supplier_id')
    unit_price = float(data.get('unit_price') or so.ingredient.last_purchase_price or 0)
    
    if supplier_id and unit_price > 0:
        gst_rate = float(data.get('gst_rate', 5))
        item_total = qty_received * unit_price
        item_gst = item_total * (gst_rate / 100)
        
        pi = PurchaseInvoice(
            supplier_id=supplier_id,
            invoice_number=data.get('invoice_number', f'SO-{so.id}'),
            invoice_date=datetime.now(timezone.utc).date(),
            total_amount=item_total + item_gst,
            gst_amount=item_gst,
            status='confirmed'
        )
        db.session.add(pi)
        db.session.flush()  # Get pi.id
        
        pi_item = PurchaseInvoiceItem(
            invoice=pi,
            inventory_id=so.inventory_id,
            quantity=qty_received,
            unit_price=unit_price,
            gst_rate=gst_rate,
            total=item_total + item_gst
        )
        db.session.add(pi_item)
        
        # Update inventory average cost
        inv = so.ingredient
        current_val = inv.stock_level * (inv.average_unit_cost or unit_price)
        new_val = qty_received * unit_price
        new_qty = inv.stock_level
        inv.average_unit_cost = (current_val + new_val) / new_qty if new_qty > 0 else unit_price
        inv.last_purchase_price = unit_price
        
        purchase_invoice_id = pi.id
    
    db.session.commit()
    result = _so_dict(so)
    if purchase_invoice_id:
        result['purchase_invoice_id'] = purchase_invoice_id
    return jsonify(result)


@bp.route('/stock-orders/<int:so_id>', methods=['DELETE'])
def cancel_stock_order(so_id):
    so = db.get_or_404(StockOrder, so_id)
    so.status = 'cancelled'
    db.session.commit()
    return jsonify({'message': 'cancelled'})


# ── Supplier Endpoints ───────────────────────────────────────────────────────

@bp.route('/suppliers', methods=['GET'])
def list_suppliers():
    suppliers = Supplier.query.order_by(Supplier.name.asc()).all()
    return jsonify([{
        'id': s.id,
        'name': s.name,
        'contact_person': s.contact_person,
        'phone': s.phone,
        'email': s.email,
        'address': s.address,
        'gstin': s.gstin,
        'is_active': s.is_active
    } for s in suppliers])


@bp.route('/suppliers', methods=['POST'])
def create_supplier():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get('name')
    if not name:
        return jsonify({'error': 'name required'}), 400
    s = Supplier(
        name=name,
        contact_person=data.get('contact_person'),
        phone=data.get('phone'),
        email=data.get('email'),
        address=data.get('address'),
        gstin=data.get('gstin'),
        is_active=True
    )
    db.session.add(s)
    db.session.commit()
    return jsonify({'id': s.id, 'name': s.name}), 201


@bp.route('/suppliers/<int:s_id>', methods=['PUT'])
def update_supplier(s_id):
    s = db.get_or_404(Supplier, s_id)
    data = request.get_json(force=True, silent=True) or {}
    if 'name' in data: s.name = data['name']
    if 'contact_person' in data: s.contact_person = data['contact_person']
    if 'phone' in data: s.phone = data['phone']
    if 'email' in data: s.email = data['email']
    if 'address' in data: s.address = data['address']
    if 'gstin' in data: s.gstin = data['gstin']
    if 'is_active' in data: s.is_active = bool(data['is_active'])
    db.session.commit()
    return jsonify({'id': s.id, 'name': s.name})


# ── Recipe Costing ───────────────────────────────────────────────────────────

@bp.route('/menu-items/<int:mi_id>/cost', methods=['GET'])
def get_menu_item_cost(mi_id):
    mi = db.get_or_404(MenuItem, mi_id)
    # The cost_price is already updated by update_menu_item_costs hook
    ingredients = []
    try:
        for li in mi.recipe_ingredients:
            # Load the ingredient relationship
            inv = db.session.get(Inventory, li.inventory_id)
            if inv:
                avg_cost = inv.average_unit_cost or 0
                ingredients.append({
                    'id': li.id,
                    'inventory_id': li.inventory_id,
                    'name': inv.name,
                    'quantity': li.quantity,
                    'unit': inv.unit,
                    'avg_cost': avg_cost,
                    'line_cost': li.quantity * avg_cost
                })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
    return jsonify({
        'id': mi.id,
        'cost_price': mi.cost_price or 0,
        'ingredients': ingredients
    })


@bp.route('/purchase-invoices', methods=['GET'])
def list_purchase_invoices():
    invoices = PurchaseInvoice.query.order_by(PurchaseInvoice.invoice_date.desc()).all()
    return jsonify([{
        'id': pi.id,
        'supplier_name': pi.supplier.name,
        'invoice_number': pi.invoice_number,
        'invoice_date': pi.invoice_date.isoformat(),
        'total_amount': pi.total_amount,
        'gst_amount': pi.gst_amount,
        'status': pi.status,
        'items': [{
            'id': item.id,
            'inventory_name': item.inventory_item.name if item.inventory_item else 'Unknown',
            'quantity': item.quantity,
            'quantity_ordered': item.quantity_ordered or item.quantity,
            'quantity_received': item.quantity_received,
            'unit_price': item.unit_price,
            'gst_rate': item.gst_rate,
            'total': item.total
        } for item in pi.items]
    } for pi in invoices])


@bp.route('/purchase-invoices', methods=['POST'])
def create_purchase_invoice():
    data = request.get_json(force=True, silent=True) or {}
    supplier_id = data.get('supplier_id')
    inv_num = data.get('invoice_number')
    inv_date_str = data.get('invoice_date')
    items = data.get('items') or []
    
    if not supplier_id or not inv_num or not inv_date_str or not items:
        return jsonify({'error': 'supplier_id, invoice_number, invoice_date, items required'}), 400
        
    try:
        inv_date = datetime.strptime(inv_date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'invalid date format'}), 400

    # Create as 'ordered' status, don't update stock yet
    pi = PurchaseInvoice(
        supplier_id=supplier_id,
        invoice_number=inv_num,
        invoice_date=inv_date,
        total_amount=0,
        gst_amount=0,
        status='ordered'
    )
    db.session.add(pi)
    
    total_net = 0
    total_gst = 0
    
    for item in items:
        inv_item_id = item.get('inventory_id')
        qty = float(item.get('quantity') or 0)
        price = float(item.get('unit_price') or 0)
        gst_rate = float(item.get('gst_rate') or 0)
        
        if not inv_item_id or qty <= 0: continue
        
        item_total = qty * price
        item_gst = item_total * (gst_rate / 100)
        
        pi_item = PurchaseInvoiceItem(
            invoice=pi,
            inventory_id=inv_item_id,
            quantity=qty,
            quantity_ordered=qty,
            unit_price=price,
            gst_rate=gst_rate,
            total=item_total + item_gst
        )
        db.session.add(pi_item)
        
        total_net += item_total
        total_gst += item_gst

    pi.total_amount = total_net + total_gst
    pi.gst_amount = total_gst
    
    db.session.commit()
    
    return jsonify({'id': pi.id, 'total_amount': pi.total_amount, 'status': pi.status}), 201


@bp.route('/purchase-invoices/<int:pi_id>/receive', methods=['POST'])
def receive_purchase_invoice(pi_id):
    """Mark purchase as received and update inventory stock levels."""
    pi = db.get_or_404(PurchaseInvoice, pi_id)
    if pi.status == 'received':
        return jsonify({'error': 'Already marked as received'}), 400
        
    data = request.get_json(force=True, silent=True) or {}
    received_items = data.get('items', []) # List of {id: item_id, quantity_received: x}
    
    # Map received items by ID for easy lookup
    received_map = {int(ri['id']): float(ri['quantity_received']) for ri in received_items if 'id' in ri}
    
    total_net = 0
    total_gst = 0
    
    for item in pi.items:
        qty_received = received_map.get(item.id, item.quantity_ordered)
        item.quantity_received = qty_received
        
        # Update Inventory Stock and Avg Cost
        inv = item.inventory_item
        if inv:
            # Update average cost: Weighted Average
            current_val = inv.stock_level * (inv.average_unit_cost or item.unit_price)
            new_val = qty_received * item.unit_price
            new_qty = inv.stock_level + qty_received
            
            inv.average_unit_cost = (current_val + new_val) / new_qty if new_qty > 0 else item.unit_price
            inv.stock_level = new_qty
            inv.last_purchase_price = item.unit_price
            
        # Update item total based on received quantity
        item_total = qty_received * item.unit_price
        item_gst = item_total * (item.gst_rate / 100)
        item.total = item_total + item_gst
        
        total_net += item_total
        total_gst += item_gst

    pi.total_amount = total_net + total_gst
    pi.gst_amount = total_gst
    pi.status = 'received'
    pi.paid_at = datetime.now(timezone.utc)
    
    db.session.commit()
    
    # Recalculate all menu item costs after inventory cost changes
    from models import update_menu_item_costs
    update_menu_item_costs(db.session)
    
    return jsonify({'id': pi.id, 'status': pi.status, 'total_amount': pi.total_amount})


@bp.route('/purchase-invoices/<int:pi_id>', methods=['GET'])
def get_purchase_invoice(pi_id):
    """Get a single purchase invoice with all its items."""
    pi = db.get_or_404(PurchaseInvoice, pi_id)
    return jsonify({
        'id': pi.id,
        'supplier_name': pi.supplier.name,
        'invoice_number': pi.invoice_number,
        'invoice_date': pi.invoice_date.isoformat(),
        'total_amount': pi.total_amount,
        'gst_amount': pi.gst_amount,
        'status': pi.status,
        'items': [{
            'id': item.id,
            'inventory_name': item.inventory_item.name if item.inventory_item else 'Unknown',
            'inventory_id': item.inventory_id,
            'quantity': item.quantity,
            'quantity_ordered': item.quantity_ordered or item.quantity,
            'quantity_received': item.quantity_received,
            'unit_price': item.unit_price,
            'gst_rate': item.gst_rate,
            'total': item.total
        } for item in pi.items]
    })
