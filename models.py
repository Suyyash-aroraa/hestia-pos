from datetime import datetime, time as _time, timedelta, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class Table(db.Model):
    __tablename__ = 'restaurant_tables'

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, unique=True, nullable=False, index=True)
    capacity = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(32), nullable=False, default='empty', index=True)

    sessions = db.relationship(
        'Session',
        foreign_keys='Session.table_number',
        primaryjoin='Table.number == Session.table_number',
        backref='table_ref',
        viewonly=True,
    )


class Staff(db.Model):
    __tablename__ = 'staff'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    pin_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False, default='captain')  # 'captain' / 'manager'
    active = db.Column(db.Boolean, nullable=False, default=True)
    allowed_tables = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Session(db.Model):
    __tablename__ = 'sessions'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    table_number = db.Column(db.Integer, db.ForeignKey('restaurant_tables.number'), nullable=True, index=True)
    session_type = db.Column(db.String(16), nullable=False, default='table', index=True)
    pickup_code = db.Column(db.String(16), unique=True, nullable=True, index=True)
    customer_phone = db.Column(db.String(32), nullable=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    status = db.Column(db.String(16), nullable=False, default='active', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    last_order_at = db.Column(db.DateTime, nullable=True)
    phone_submitted_at = db.Column(db.DateTime, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    closed_by = db.Column(db.String(64), nullable=True)
    source = db.Column(db.String(16), nullable=True, default='offline')  # staff-created sessions
    shifts_count = db.Column(db.Integer, nullable=False, default=0)  # how many times this session was shifted to a different table
    opened_by_staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    is_vip = db.Column(db.Boolean, nullable=False, default=False)
    bill_comment = db.Column(db.Text, nullable=True)
    cash_flag = db.Column(db.Boolean, nullable=False, default=False, index=True)
    staff_cart = db.Column(db.Text, nullable=True)  # Legacy JSON storage

    customer = db.relationship('Customer', backref=db.backref('sessions', lazy='dynamic'))
    orders = db.relationship('Order', backref='session', lazy='dynamic')
    opened_by = db.relationship('Staff', foreign_keys=[opened_by_staff_id])
    cart_items = db.relationship('CartItem', backref='session', lazy='dynamic', cascade='all, delete-orphan')


class CartItem(db.Model):
    __tablename__ = 'cart_items'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id'), nullable=False, index=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    price = db.Column(db.Float, nullable=False)
    qty = db.Column(db.Integer, nullable=False, default=1)
    hold = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.Text, nullable=True)
    config_choices = db.Column(db.Text, nullable=True)  # JSON string
    config_extra = db.Column(db.Float, nullable=False, default=0)
    config_label = db.Column(db.String(256), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    menu_item = db.relationship('MenuItem')


class Customer(db.Model):
    __tablename__ = 'customers'

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(32), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=True)
    gstin = db.Column(db.String(32), nullable=True)
    address = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    total_visits = db.Column(db.Integer, nullable=False, default=0)
    last_seen = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Inventory(db.Model):
    __tablename__ = 'inventory'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    unit = db.Column(db.String(32), nullable=False, default='units')  # ml, g, units, etc.
    stock_level = db.Column(db.Float, nullable=False, default=0)
    low_stock_threshold = db.Column(db.Float, nullable=False, default=0)
    expiry_date = db.Column(db.Date, nullable=True)
    average_unit_cost = db.Column(db.Float, nullable=False, default=0)  # weighted avg from purchases
    last_purchase_price = db.Column(db.Float, nullable=False, default=0)

    menu_items = db.relationship('MenuItem', backref='inventory_item', lazy='dynamic')
    recipe_uses = db.relationship('RecipeIngredient', backref='ingredient', lazy='dynamic',
                                  cascade='all, delete-orphan')


class RecipeIngredient(db.Model):
    __tablename__ = 'recipe_ingredients'

    id = db.Column(db.Integer, primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False)
    inventory_id = db.Column(db.Integer, db.ForeignKey('inventory.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1.0)  # amount used per serving
    config_group = db.Column(db.String(64), nullable=True)   # Optional: link to a config group
    config_option = db.Column(db.String(64), nullable=True)  # Optional: link to a specific option

    menu_item = db.relationship('MenuItem', backref=db.backref('recipe_ingredients', lazy='select',
                                                                cascade='all, delete-orphan'))


class StockOrder(db.Model):
    __tablename__ = 'stock_orders'

    id = db.Column(db.Integer, primary_key=True)
    inventory_id = db.Column(db.Integer, db.ForeignKey('inventory.id'), nullable=False)
    quantity_ordered = db.Column(db.Float, nullable=False)
    quantity_received = db.Column(db.Float, nullable=True)   # set when arrived
    status = db.Column(db.String(16), nullable=False, default='pending')  # pending / arrived / cancelled
    notes = db.Column(db.String(256), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    arrived_at = db.Column(db.DateTime, nullable=True)

    ingredient = db.relationship('Inventory', backref=db.backref('stock_orders', lazy='dynamic'))


class MenuItem(db.Model):
    __tablename__ = 'menu_items'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    price = db.Column(db.Float, nullable=False)
    cost_price = db.Column(db.Float, nullable=False, default=0)  # auto-calculated from recipe
    category = db.Column(db.String(64), nullable=False)
    description = db.Column(db.String(512), nullable=True)
    available = db.Column(db.Boolean, nullable=False, default=True)
    deleted = db.Column(db.Boolean, nullable=False, default=False)
    inventory_id = db.Column(db.Integer, db.ForeignKey('inventory.id'), nullable=True)
    hsn_category_id = db.Column(db.Integer, db.ForeignKey('hsn_categories.id'), nullable=True)

    configs = db.relationship('MenuItemConfig', backref='menu_item', lazy='select', cascade='all, delete-orphan')
    hsn_category = db.relationship('HSNCategory', backref=db.backref('menu_items', lazy='dynamic'))


class NonVegItem(db.Model):
    __tablename__ = 'non_veg_items'

    id = db.Column(db.Integer, primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False, unique=True)
    non_veg = db.Column(db.Boolean, nullable=False, default=True)


class MenuItemConfig(db.Model):
    __tablename__ = 'menu_item_configs'

    id = db.Column(db.Integer, primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False)
    group_name = db.Column(db.String(64), nullable=False)   # e.g. "Strength"
    options = db.Column(db.Text, nullable=False)             # JSON array e.g. ["Regular","Strong","Mix"]
    required = db.Column(db.Boolean, nullable=False, default=True)
    multi_select = db.Column(db.Boolean, nullable=False, default=False)


class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id'), nullable=False)
    status = db.Column(db.String(32), nullable=False, default='placed')
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    discount_amount = db.Column(db.Float, nullable=False, default=0.0)
    discount_note = db.Column(db.String(256), nullable=True)
    placed_by_staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    kot_comment = db.Column(db.String(512), nullable=True)
    fire_id = db.Column(db.String(64), nullable=True, unique=True, index=True)  # client idempotency key
    kot_printed_at = db.Column(db.DateTime, nullable=True)  # first KOT print time; reprints use this

    items = db.relationship(
        'OrderItem',
        backref='order',
        lazy='select',
        cascade='all, delete-orphan',
    )


class OrderItem(db.Model):
    __tablename__ = 'order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.String(256), nullable=True)
    voided = db.Column(db.Boolean, nullable=False, default=False)
    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by_staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    quantity_cancelled = db.Column(db.Integer, nullable=False, default=0)
    reduced_at = db.Column(db.DateTime, nullable=True)
    reduced_by_staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    config_choices = db.Column(db.Text, nullable=True)  # JSON dict e.g. {"Strength": "Strong"}
    # NEW: held items are saved but not sent to kitchen until fired
    held = db.Column(db.Boolean, nullable=False, default=False)
    fired_at = db.Column(db.DateTime, nullable=True)
    config_price_extra = db.Column(db.Float, nullable=False, default=0.0)
    item_status = db.Column(db.String(32), nullable=False, default='placed')  # placed/preparing/ready/served
    stock_deducted = db.Column(db.Boolean, nullable=False, default=False)
    kot_queued = db.Column(db.Boolean, nullable=False, default=False)  # True once a KOT row was successfully created for this item

    menu_item = db.relationship('MenuItem', backref=db.backref('order_items', lazy='dynamic'))
    voided_by_staff = db.relationship('Staff', foreign_keys=[voided_by_staff_id])
    reduced_by_staff = db.relationship('Staff', foreign_keys=[reduced_by_staff_id])


class Payment(db.Model):
    __tablename__ = 'payments'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id'), nullable=False)
    method = db.Column(db.String(32), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    tip = db.Column(db.Float, nullable=False, default=0.0)
    tax = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(32), nullable=False, default='pending')
    razorpay_payment_id = db.Column(db.String(128), nullable=True)
    confirmed_at = db.Column(db.DateTime, nullable=True)

    session = db.relationship('Session', backref=db.backref('payments', lazy='dynamic'))


class Bill(db.Model):
    __tablename__ = 'bills'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id'), nullable=False)
    session_type = db.Column(db.String(16), nullable=False)
    table_number = db.Column(db.Integer, nullable=True)
    pickup_code = db.Column(db.String(16), nullable=True)
    printed_at = db.Column(db.DateTime, nullable=True)
    settled_at = db.Column(db.DateTime, nullable=True)
    payment_method = db.Column(db.String(32), nullable=True)
    amount = db.Column(db.Float, nullable=True)
    items_snapshot = db.Column(db.Text, nullable=False)  # JSON list
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # --- stats / audit fields ---
    print_count = db.Column(db.Integer, nullable=False, default=0)
    tip = db.Column(db.Float, nullable=False, default=0.0)
    cgst_amount = db.Column(db.Float, nullable=True)
    sgst_amount = db.Column(db.Float, nullable=True)
    igst_amount = db.Column(db.Float, nullable=False, default=0.0)
    gst_rate = db.Column(db.Float, nullable=False, default=5.0)  # restaurant GST default
    hsn_code = db.Column(db.String(8), nullable=True)
    is_complementary = db.Column(db.Boolean, nullable=False, default=False)
    is_cancelled = db.Column(db.Boolean, nullable=False, default=False)
    waived_off_amount = db.Column(db.Float, nullable=False, default=0.0)
    coupon_code = db.Column(db.String(64), nullable=True)
    split_payments = db.Column(db.Text, nullable=True)  # JSON: [{method, amount}] for split payments
    settled_by = db.Column(db.String(64), nullable=True) # "main_pos" or manager name
    is_split_child = db.Column(db.Boolean, nullable=False, default=False)  # true if this is a split payment partial bill
    bill_comment = db.Column(db.Text, nullable=True)
    customer_name = db.Column(db.String(128), nullable=True)
    customer_gstin = db.Column(db.String(32), nullable=True)
    customer_phone = db.Column(db.String(32), nullable=True)
    customer_address = db.Column(db.Text, nullable=True)
    customer_notes = db.Column(db.Text, nullable=True)
    previous_due_subtotal = db.Column(db.Float, nullable=False, default=0.0)
    include_previous_due = db.Column(db.Boolean, nullable=False, default=False)
    due_added_subtotal = db.Column(db.Float, nullable=False, default=0.0)
    due_cleared_subtotal = db.Column(db.Float, nullable=False, default=0.0)
    due_outstanding_after = db.Column(db.Float, nullable=False, default=0.0)
    due_status = db.Column(db.String(16), nullable=False, default='none')  # none / open / cleared
    due_cleared_at = db.Column(db.DateTime, nullable=True)
    due_cleared_method = db.Column(db.String(32), nullable=True)

    session = db.relationship('Session', backref=db.backref('bill', uselist=False))
    modifications = db.relationship('BillModification', backref='bill', lazy='dynamic',
                                    cascade='all, delete-orphan')


class BillModification(db.Model):
    __tablename__ = 'bill_modifications'

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey('bills.id'), nullable=False, index=True)
    description = db.Column(db.String(512), nullable=False)
    modified_by = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Expense(db.Model):
    __tablename__ = 'expenses'

    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(256), nullable=False)
    category = db.Column(db.String(64), nullable=True)  # e.g. 'supplies', 'withdrawal', 'other'
    created_by = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)






class LedgerEntry(db.Model):
    __tablename__ = 'ledger_entries'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id'), nullable=True, index=True)
    bill_id = db.Column(db.Integer, db.ForeignKey('bills.id'), nullable=True, index=True)
    entry_type = db.Column(db.String(16), nullable=False, index=True)  # due / settlement / adjustment
    amount_subtotal = db.Column(db.Float, nullable=False)  # positive raises due, negative clears due
    payment_method = db.Column(db.String(32), nullable=True)
    customer_name = db.Column(db.String(128), nullable=True)
    customer_phone = db.Column(db.String(32), nullable=True, index=True)
    customer_gstin = db.Column(db.String(32), nullable=True)
    customer_address = db.Column(db.Text, nullable=True)
    customer_notes = db.Column(db.Text, nullable=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    customer = db.relationship('Customer', backref=db.backref('ledger_entries', lazy='dynamic'))
    session = db.relationship('Session', backref=db.backref('ledger_entries', lazy='dynamic'))
    bill = db.relationship('Bill', backref=db.backref('ledger_entries', lazy='dynamic'))


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLIER & PURCHASE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

class Supplier(db.Model):
    __tablename__ = 'suppliers'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    contact_person = db.Column(db.String(128), nullable=True)
    phone = db.Column(db.String(32), nullable=True)
    email = db.Column(db.String(128), nullable=True)
    address = db.Column(db.Text, nullable=True)
    gstin = db.Column(db.String(20), nullable=True)
    payment_terms_days = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    invoices = db.relationship('PurchaseInvoice', backref='supplier', lazy='dynamic')


class PurchaseInvoice(db.Model):
    __tablename__ = 'purchase_invoices'

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    invoice_number = db.Column(db.String(64), nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    total_amount = db.Column(db.Float, nullable=False, default=0)
    gst_amount = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(16), nullable=False, default='draft')  # draft / confirmed / paid
    paid_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    items = db.relationship('PurchaseInvoiceItem', backref='invoice', lazy='dynamic',
                            cascade='all, delete-orphan')


class PurchaseInvoiceItem(db.Model):
    __tablename__ = 'purchase_invoice_items'

    id = db.Column(db.Integer, primary_key=True)
    purchase_invoice_id = db.Column(db.Integer, db.ForeignKey('purchase_invoices.id'), nullable=False)
    inventory_id = db.Column(db.Integer, db.ForeignKey('inventory.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False)  # keep for legacy, but will use quantity_ordered
    quantity_ordered = db.Column(db.Float, nullable=True)
    quantity_received = db.Column(db.Float, nullable=True)
    unit_price = db.Column(db.Float, nullable=False)
    gst_rate = db.Column(db.Float, nullable=False, default=0)
    total = db.Column(db.Float, nullable=False)

    inventory_item = db.relationship('Inventory', backref=db.backref('purchase_items', lazy='dynamic'))


# ═══════════════════════════════════════════════════════════════════════════════
# INVENTORY WASTE TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

class InventoryWaste(db.Model):
    __tablename__ = 'inventory_wastes'

    id = db.Column(db.Integer, primary_key=True)
    inventory_id = db.Column(db.Integer, db.ForeignKey('inventory.id'), nullable=False)
    quantity_wasted = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(32), nullable=False, default='other')  # expired / spoiled / damage / other
    noted_by = db.Column(db.String(64), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    inventory_item = db.relationship('Inventory', backref=db.backref('wastes', lazy='dynamic'))


# ═══════════════════════════════════════════════════════════════════════════════
# PICKUP TOKEN MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# BILL SPLITTING
# ═══════════════════════════════════════════════════════════════════════════════

class BillSplit(db.Model):
    __tablename__ = 'bill_splits'

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey('bills.id'), nullable=False)
    person_name = db.Column(db.String(64), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    items = db.Column(db.Text, nullable=True)  # JSON: [{order_item_id, quantity, amount}]
    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    paid_at = db.Column(db.DateTime, nullable=True)
    payment_method = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bill = db.relationship('Bill', backref=db.backref('splits', lazy='dynamic'))


# ═══════════════════════════════════════════════════════════════════════════════
# GST & COMPLIANCE
# ═══════════════════════════════════════════════════════════════════════════════

class HSNCategory(db.Model):
    __tablename__ = 'hsn_categories'

    id = db.Column(db.Integer, primary_key=True)
    category_name = db.Column(db.String(64), nullable=False)
    hsn_code = db.Column(db.String(8), nullable=False)
    gst_rate_default = db.Column(db.Float, nullable=False, default=5.0)


# ═══════════════════════════════════════════════════════════════════════════════
# AGGREGATOR INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

class AdminSession(db.Model):
    __tablename__ = 'admin_sessions'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(36), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_activity = db.Column(db.DateTime, nullable=False, default=utcnow)

    def is_valid(self):
        if not self.expires_at:
            return False
        # SQLite returns naive datetimes; persisted timestamps represent UTC.
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(timezone.utc)


class KOTQueue(db.Model):
    __tablename__ = 'kot_queue'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, nullable=False, index=True)
    session_type = db.Column(db.String(16), nullable=True)
    table_number = db.Column(db.Integer, nullable=True)
    pickup_code = db.Column(db.String(16), nullable=True)
    session_token = db.Column(db.String(64), nullable=True)
    location = db.Column(db.String(64), nullable=True)   # e.g. "Table 5" / "Parcel #7"
    items = db.Column(db.Text, nullable=True)          # JSON [{name,qty,notes},...]
    reprint = db.Column(db.Boolean, nullable=False, default=False)
    order_type = db.Column(db.String(16), nullable=True)
    kot_comment = db.Column(db.String(512), nullable=True)
    printer_name = db.Column(db.String(64), nullable=True)
    kot_type = db.Column(db.String(16), nullable=False, default='normal')  # normal / void
    void_item_name = db.Column(db.String(128), nullable=True)
    void_quantity = db.Column(db.Integer, nullable=True)
    void_item_config = db.Column(db.Text, nullable=True)  # JSON config choices for voided item
    void_item_notes = db.Column(db.String(256), nullable=True)  # Notes for voided item
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(16), nullable=False, default='pending')  # pending / printing / failed
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    printed_at = db.Column(db.DateTime, nullable=True)


class SystemConfig(db.Model):
    __tablename__ = 'system_config'

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)


def get_config_value(key, default=None):
    conf = SystemConfig.query.filter_by(key=key).first()
    return conf.value if conf else default


def set_config_value(key, value):
    conf = SystemConfig.query.filter_by(key=key).first()
    if conf:
        conf.value = str(value)
        conf.updated_at = utcnow()
    else:
        conf = SystemConfig(key=key, value=str(value))
        db.session.add(conf)
    db.session.commit()




def order_lines_subtotal(order):
    total = 0.0
    for oi in order.items:
        if oi.voided:
            continue
        if oi.menu_item:
            total += oi.quantity * (float(oi.menu_item.price) + float(oi.config_price_extra or 0))
    return total


def order_net_amount(order):
    sub = order_lines_subtotal(order)
    disc = float(order.discount_amount or 0)
    if disc > sub:
        disc = sub
    return max(0.0, sub - disc)


def session_billable_subtotal(session):
    total = 0.0
    # Add fired orders
    for order in session.orders:
        total += order_net_amount(order)
    # Add cart items (not yet fired)
    for ci in session.cart_items:
        total += (ci.price + ci.config_extra) * ci.qty
    return total


def calculate_menu_item_cost(menu_item):
    """Calculate cost price from recipe ingredients using average unit cost."""
    total_cost = 0.0
    for ri in menu_item.recipe_ingredients:
        if ri.ingredient and ri.ingredient.average_unit_cost:
            total_cost += ri.quantity * ri.ingredient.average_unit_cost
    return round(total_cost, 2)


def update_menu_item_costs(db_session):
    """Recalculate cost_price for all menu items with recipes."""
    items = MenuItem.query.filter(MenuItem.recipe_ingredients.any()).all()
    # Compute in Python, then push all rows in a single batched statement
    # (executemany) instead of one ORM UPDATE per item.
    mappings = [
        {'id': item.id, 'cost_price': calculate_menu_item_cost(item)}
        for item in items
    ]
    if mappings:
        db_session.bulk_update_mappings(MenuItem, mappings)
    db_session.commit()
