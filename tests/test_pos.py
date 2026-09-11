import json
import pytest

from models import Bill, CartItem, Order, OrderItem, Payment, Session, db


def post(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 200, (path, response.get_json())
    return response.get_json()


def open_session(client, kind):
    if kind == 'table':
        return post(client, '/api/session/new', {'table_number': 1})['token']
    return post(client, '/api/takeout/offline-session', {'slot_number': 1})['token']


@pytest.mark.parametrize('kind', ['table', 'takeout'])
@pytest.mark.parametrize('method', ['cash', 'upi_offline', 'card_offline'])
def test_order_print_settle_and_close(client, kind, method):
    token = open_session(client, kind)
    post(client, '/api/orders', {'token': token, 'items': [{'menu_item_id': 1, 'quantity': 2, 'notes': 'No sugar'}], 'kot_comment': 'Extra hot'})
    assert OrderItem.query.one().notes is None
    assert Order.query.one().kot_comment is None
    preview = client.get('/api/bills/preview', query_string={'token': token}).get_json()
    assert preview['total'] == 200
    printed = post(client, '/api/bills/print', {'token': token, 'apply_service_charge': True})
    assert Bill.query.one().amount == 200
    assert 'service_charge' not in printed
    post(client, '/api/payments/staff-offline', {'token': token, 'method': method})
    post(client, '/api/payments/confirm-cash', {'token': token, 'method': method})
    assert Payment.query.one().status == 'confirmed'
    assert Payment.query.one().amount == 200
    assert Bill.query.one().settled_at is not None
    totals = client.get('/api/dashboard').get_json()
    assert totals['sales'] == 200
    assert totals['settled_bills'] == 1
    post(client, '/api/session/close', {'token': token})
    assert Session.query.filter_by(token=token).one().status == 'closed'


@pytest.mark.parametrize('kind', ['table', 'takeout'])
def test_cart_variants_and_removed_instructions(client, kind):
    token = open_session(client, kind)
    item = {'menu_item_id': 1, 'name': 'Coffee', 'price': 100, 'qty': 2,
            'notes': 'No sugar', 'config_choices': {'Size': 'Large'}, 'configExtra': 20, 'configLabel': 'Large'}
    cid = post(client, '/api/session/cart/add', {'token': token, 'item': item})['id']
    assert CartItem.query.one().notes is None
    assert CartItem.query.one().config_extra == 20
    response = client.post('/api/bills/print', json={'token': token})
    assert response.status_code == 400
    assert response.get_json()['pending_cart'] is True
    post(client, '/api/session/cart/update-item', {'token': token, 'cart_item_id': cid, 'updates': {'notes': 'Extra hot'}})
    assert CartItem.query.one().notes is None
    post(client, '/api/session/cart/fire', {'token': token, 'kot_comment': 'Extra hot'})
    assert CartItem.query.count() == 0
    assert OrderItem.query.one().notes is None
    assert OrderItem.query.one().config_price_extra == 20
    assert Order.query.one().kot_comment is None
    assert client.get('/api/bills/preview', query_string={'token': token}).get_json()['total'] == 240


@pytest.mark.parametrize('method,path', [
    ('get','/order'), ('get','/takeout'), ('get','/t/1'), ('get','/qr-codes'),
    ('get','/kitchen'), ('get','/kitchen/index.html'), ('get','/takeout/index.html'),
    ('get','/api/kitchen/stream'), ('get','/api/kitchen/pos-stream'),
    ('get','/api/kitchen/customer-stream'), ('get','/api/pickup-tokens'),
    ('post','/api/takeout/session'), ('get','/api/takeout/online-status'),
    ('post','/api/takeout/online-status'), ('post','/api/payments/create-link'),
    ('post','/api/payments/webhook'), ('post','/api/payments/icici/initiate'), ('get','/api/bills/upi-qr'),
    ('post','/api/session/cart/update-comment'),
])
def test_removed_routes_are_unavailable(client, method, path):
    assert getattr(client, method)(path).status_code in (404, 405)


def test_retained_pages_config_and_admin(client):
    for path in ['/', '/pos', '/pos-takeout', '/admin', '/menu-manager', '/history', '/items-report', '/theme.css', '/brand.js']:
        assert client.get(path).status_code == 200, path
    config = client.get('/api/config').get_json()
    assert config['product_name'] == 'Hestia POS'
    assert config['restaurant_name'] == 'Hestia POS'
    import config
    post(client, '/api/admin/login', {'password': config.ADMIN_PASSWORD})
    response = client.get('/api/admin/config')
    assert response.status_code == 200
    data = response.get_json()
    assert not any(k in data for k in ['admin_password','secret_key','cloudflare_tunnel_name','admin_cf_path','enable_online_payments','kitchen_screen_print_kot'])
    for route in ['/api/admin/stats', '/api/admin/bills']:
        assert client.get(route).status_code == 200


def test_configured_taxes_numbering_and_split_settlement(client, monkeypatch):
    import config
    from models import KOTQueue, set_config_value
    monkeypatch.setattr(config, 'CGST_RATE', 0.025)
    monkeypatch.setattr(config, 'SGST_RATE', 0.025)
    set_config_value('bill_start_number', '1001')
    token = open_session(client, 'table')
    post(client, '/api/orders', {'token': token, 'items': [{'menu_item_id': 1, 'quantity': 2}]})
    assert KOTQueue.query.count() == 0
    post(client, '/api/bills/print', {'token': token, 'apply_service_charge': True})
    bill = Bill.query.one()
    assert (bill.id, bill.amount, bill.cgst_amount, bill.sgst_amount, bill.gst_rate) == (1001, 210, 5, 5, 5)
    post(client, '/api/payments/staff-split-confirm', {'token': token, 'split_payments': [
        {'method': 'cash', 'amount': 100}, {'method': 'upi', 'amount': 110}]})
    assert Bill.query.one().amount == 210
    assert Payment.query.one().status == 'confirmed'
    assert client.get('/api/bills/history').status_code == 200
    assert client.get('/api/bills/history.xlsx').status_code == 200


def test_printer_names_in_new_config_format(client, tmp_path, monkeypatch):
    from routes import print_svc
    settings = tmp_path / 'config.py'
    settings.write_text("BILL_PRINTER_NAME = 'Receipt Printer'\nKITCHEN_PRINTER_NAME = \"Kitchen Printer\"\n", encoding='utf-8')
    monkeypatch.setattr(print_svc, '_candidate_config_paths', lambda: [str(settings)])
    assert print_svc._get_config_value('BILL_PRINTER_NAME') == 'Receipt Printer'
    assert print_svc._get_config_value('KITCHEN_PRINTER_NAME') == 'Kitchen Printer'
