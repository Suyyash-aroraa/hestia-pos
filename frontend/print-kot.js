(function () {
  function _formatKOTTime(kotPrintedAt) {
    var now = new Date();
    var ts = kotPrintedAt ? new Date(kotPrintedAt) : now;
    if (isNaN(ts.getTime())) ts = now;
    var dateStr = ts.getDate() + '-' + ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][ts.getMonth()] + '-' + ts.getFullYear();
    var hours = ts.getHours();
    var ampm = hours >= 12 ? 'PM' : 'AM';
    hours = hours % 12 || 12;
    var mins = ts.getMinutes();
    var timeStr = hours + ':' + (mins < 10 ? '0' : '') + mins + ' ' + ampm;
    return { dateStr: dateStr, timeStr: timeStr };
  }

  function _printIframe(oid, loc, items, reprint, table, token, orderType, kotComment, kotPrintedAt) {
    var t = _formatKOTTime(kotPrintedAt);
    var dateStr = t.dateStr;
    var timeStr = t.timeStr;

    var itemRows = items.map(function (i) {
      var configLine = '';
      if (i.config) {
        configLine = '<div style="font-size:12px;font-weight:bold;color:#000;padding-left:4px;">[' + i.config + ']</div>';
      }
      if (i.notes) {
        var noteText = i.notes.toUpperCase();
        if (noteText.includes('JAIN')) {
          configLine += '<div style="font-size:12px;font-weight:bold;color:#000;padding-left:4px;">*** JAIN - ' + noteText.replace('JAIN', '').trim() + '</div>';
        } else {
          configLine += '<div style="font-size:12px;font-weight:bold;color:#000;padding-left:4px;">' + noteText + '</div>';
        }
      }
      return '<div style="margin:4px 0 1px 0;">' +
        '<div style="font-size:14px;font-weight:bold;color:#000;">' + i.qty + 'x    ' + i.name + '</div>' +
        configLine +
        '</div>';
    }).join('');

    // Determine identifier for end of KOT
    var identifier = '';
    if (token) {
      identifier = 'Parcel ' + token;
    } else if (table) {
      identifier = 'Table ' + table;
    } else if (loc) {
      identifier = loc;
    } else {
      identifier = '—';
    }

    var html = '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>KOT</title><style>' +
      'body{font-family:"Courier New",Courier,monospace;font-size:14px;font-weight:bold;max-width:280px;margin:0 auto;padding:10px 6px 12px;color:#000;}' +
      '.header-line{text-align:center;font-size:16px;font-weight:bold;letter-spacing:2px;margin:3px 0;}' +
      '.table-line{text-align:center;font-size:14px;font-weight:bold;margin:2px 0;}' +
      'table{width:100%;border-collapse:collapse;margin:2px 0;}' +
      'td{font-size:13px;font-weight:bold;color:#000;padding:1px 0;}' +
      '.separator{border-top:2px solid #000;margin:5px 0;}' +
      '.separator-thin{border-top:1px dashed #000;margin:3px 0;}' +
      '.end-line{text-align:center;font-size:13px;font-weight:bold;letter-spacing:1px;margin:5px 0;color:#000;}' +
      '@media print{body{padding:3px;}}' +
      '</style></head><body>' +
      '<div class="separator"></div>' +
      '<div class="header-line">KOT</div>' +
      '<div class="table-line">★ Table  : ' + (table || '—') + '  ★</div>' +
      '<div class="separator"></div>' +
      '<table><tr><td>KOT #  : ' + (oid || '—') + '</td></tr></table>' +
      '<table><tr><td>Date   : ' + dateStr + '</td></tr></table>' +
      '<table><tr><td>Time   : ' + timeStr + '</td></tr></table>' +
      (kotComment ? '<div class="separator-thin"></div><div style="text-align:center;font-size:14px;font-weight:bold;margin:8px 0;padding:4px;border:1.5px solid #000;">' + kotComment + '</div>' : '') +
      '<div class="separator-thin"></div>' +
      '<div style="font-size:13px;font-weight:bold;margin:3px 0;color:#000;">QTY   ITEM</div>' +
      '<div class="separator-thin"></div>' +
      itemRows +
      '<div class="separator-thin"></div>' +
      '<div class="end-line">END OF KOT - ' + identifier + '</div>' +
      '</body></html>';

    var f = document.createElement('iframe');
    f.style.cssText = 'position:fixed;right:-9999px;top:0;width:300px;height:500px;border:0;visibility:visible;';
    document.body.appendChild(f);
    f.contentWindow.document.open();
    f.contentWindow.document.write(html);
    f.contentWindow.document.close();
    f.contentWindow.focus();
    f.contentWindow.print();
    setTimeout(function () { document.body.removeChild(f); }, 1000);
  }

  window.printKOT = function (oid, loc, items, reprint, table, token, orderType, kotComment, useBillPrinter, kotPrintedAt) {
    var endpoint = useBillPrinter ? '/api/print/kot-at-printer' : '/api/print/kot';

    // Always try backend silent print — printer name is read from config.py by the backend
    fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        order_id: oid,
        location: loc || table || token || 'Order',
        items: items,
        reprint: !!reprint,
        order_type: orderType || '',
        kot_comment: kotComment || '',
        token: token || '',
        kot_printed_at: kotPrintedAt || null
      })
    }).then(function (r) {
      if (!r.ok) {
        console.warn('Silent print failed, falling back to browser dialog');
        _printIframe(oid, loc, items, reprint, table, token, orderType, kotComment, kotPrintedAt);
      }
    }).catch(function (e) {
      console.error('Silent print error:', e);
      _printIframe(oid, loc, items, reprint, table, token, orderType, kotComment, kotPrintedAt);
    });
  };

  window.printKOTBrowserOnly = function (oid, loc, items, reprint, table, token, orderType, kotComment, kotPrintedAt) {
    _printIframe(oid, loc, items, reprint, table, token, orderType, kotComment, kotPrintedAt);
  };

  window.printVoidKOT = function (orderId, loc, itemName, qty, table, remainingItems) {
    var now = new Date();
    var dateStr = now.getDate() + '-' + ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][now.getMonth()] + '-' + now.getFullYear();
    var hours = now.getHours();
    var ampm = hours >= 12 ? 'PM' : 'AM';
    hours = hours % 12 || 12;
    var mins = now.getMinutes();
    var timeStr = hours + ':' + (mins < 10 ? '0' : '') + mins + ' ' + ampm;
    var html = '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>VOID</title><style>' +
      'body{font-family:"Courier New",Courier,monospace;font-size:14px;font-weight:bold;max-width:280px;margin:0 auto;padding:6px;color:#000;text-align:center;}' +
      '.header-line{font-size:16px;font-weight:bold;letter-spacing:2px;margin:4px 0;}' +
      '.item-line{font-size:14px;font-weight:bold;margin:4px 0 1px 0;text-align:left;color:#000;}' +
      '.cancelled{font-size:20px;font-weight:bold;letter-spacing:2px;border:3px solid #000;padding:6px 12px;display:inline-block;margin:6px 0;}' +
      '.separator{border-top:2px solid #000;margin:6px 0;}' +
      'table{width:100%;border-collapse:collapse;margin:2px 0;}' +
      'td{font-size:13px;font-weight:bold;color:#000;padding:1px 0;text-align:left;}' +
      '@media print{body{padding:4px;}}' +
      '</style></head><body>' +
      '<div class="separator"></div>' +
      '<div class="header-line">VOID KOT</div>' +
      '<div class="separator"></div>' +
      '<table><tr><td>Table : ' + (table || '—') + '</td></tr></table>' +
      '<table><tr><td>Order #' + orderId + '</td></tr></table>' +
      '<table><tr><td>' + loc + '</td></tr></table>' +
      '<table><tr><td>' + dateStr + ' ' + timeStr + '</td></tr></table>' +
      '<div class="separator"></div>' +
      '<div class="cancelled">CANCELLED</div>' +
      '<div class="item-line">' + qty + 'x    ' + itemName + '</div>' +
      '<div class="separator"></div>' +
      '</body></html>';

    function _fallback() {
      var f = document.createElement('iframe');
      f.style.cssText = 'position:fixed;right:-9999px;top:0;width:300px;height:400px;border:0;visibility:visible;';
      document.body.appendChild(f);
      f.contentWindow.document.open();
      f.contentWindow.document.write(html);
      f.contentWindow.document.close();
      f.contentWindow.focus();
      f.contentWindow.print();
      setTimeout(function () { document.body.removeChild(f); }, 1000);
    }

    // Always try backend silent print — printer name is read from config.py by the backend
    fetch('/api/print/void', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order_id: orderId, location: loc, item_name: itemName, qty: qty, table: table, config_choices: configChoices || {}, notes: notes || '' })
    }).then(function (r) {
      if (!r.ok) {
        console.warn('Silent print failed, falling back to browser dialog');
        _fallback();
      }
    }).catch(function (e) {
      console.error('Silent print error:', e);
      _fallback();
    });
  };
})();
