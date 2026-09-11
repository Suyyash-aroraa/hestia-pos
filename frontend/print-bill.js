/**
 * print-bill.js — Beautiful 79mm thermal receipt generator.
 * Call: window.printBillReceipt(data)
 *
 * data fields:
 *   restaurant_name, address, phone, gst_number, fssai_number,
 *   site_url, footer_msg, instagram, google_listing, jurisdiction,
 *   bill_id, print_count, printed_at,
 *   session_type, table_number, pickup_code, captain,
 *   customer_name, customer_phone,
 *   items: [{name, quantity, price, config_choices, notes, discount}],
 *   gross_subtotal, total_discount, subtotal, cgst_rate, sgst_rate, cgst, sgst, total,
 *   payment_method, split_payments, tip
 */
(function () {
  'use strict';

  function cur(n) {
    var num = Math.floor(Number(n || 0));
    return num.toLocaleString('en-IN');
  }

  function tax(n) {
    var num = Number(n || 0);
    return num.toFixed(2);
  }

  function maskPhone(p) {
    if (!p) return '';
    var d = String(p).replace(/\D/g, '');
    if (d.length >= 10) return d.slice(0, 2) + 'XXXXX' + d.slice(-3);
    return p;
  }

  function fmtDateTime(iso) {
    var dt = iso ? new Date(iso) : new Date();
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var h = dt.getHours(), m = dt.getMinutes();
    var ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return {
      date: dt.getDate() + '-' + months[dt.getMonth()] + '-' + dt.getFullYear(),
      time: h + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm
    };
  }

  function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); 
  }

  function _num(n, fallback) {
    var x = Number(n);
    return isFinite(x) ? x : (fallback === undefined ? 0 : fallback);
  }

  function amountInWords(n) {
    var ones = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine',
      'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen',
      'Seventeen', 'Eighteen', 'Nineteen'];
    var tens = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety'];

    function belowHundred(num) {
      if (num < 20) return ones[num];
      return tens[Math.floor(num / 10)] + (num % 10 ? ' ' + ones[num % 10] : '');
    }

    function belowThousand(num) {
      var out = '';
      if (num >= 100) {
        out += ones[Math.floor(num / 100)] + ' Hundred';
        num %= 100;
        if (num) out += ' ';
      }
      if (num) out += belowHundred(num);
      return out.trim();
    }

    var num = Math.round(Math.max(0, Number(n || 0)));
    if (!num) return 'Zero Rupees Only';

    var parts = [];
    var scales = [
      { value: 10000000, label: 'Crore' },
      { value: 100000, label: 'Lakh' },
      { value: 1000, label: 'Thousand' }
    ];

    for (var i = 0; i < scales.length; i++) {
      var scale = scales[i];
      if (num >= scale.value) {
        var chunk = Math.floor(num / scale.value);
        parts.push(belowThousand(chunk) + ' ' + scale.label);
        num = num % scale.value;
      }
    }

    if (num) parts.push(belowThousand(num));
    return parts.join(' ').replace(/\s+/g, ' ').trim() + ' Rupees Only';
  }

  function groupItemsForBill(items) {
    var src = items || [];
    var out = [];
    var indexByKey = Object.create(null);

    for (var i = 0; i < src.length; i++) {
      var it = src[i] || {};
      var name = String(it.name || '');
      var price = _num(it.price, 0);
      var qty = _num(it.quantity, 0);
      if (!name || qty <= 0) continue;

      var key = name + '|' + price.toFixed(2);
      var gi;
      var idx = indexByKey[key];
      if (idx === undefined) {
        idx = out.length;
        indexByKey[key] = idx;
        gi = { name: name, price: price, quantity: 0, variants: [] };
        out.push(gi);
      } else {
        gi = out[idx];
      }

      gi.quantity += qty;

      var vars = it.variants || [];
      for (var v = 0; v < vars.length; v++) {
        var vv = vars[v] || {};
        var label = String(vv.label || '').trim();
        if (!label) continue;
        var extra = _num(vv.extra, 0);
        var vQty = _num(vv.qty, qty);

        var found = false;
        for (var j = 0; j < gi.variants.length; j++) {
          var existing = gi.variants[j];
          if (existing.label === label && _num(existing.extra, 0) === extra) {
            existing.qty = _num(existing.qty, 0) + vQty;
            found = true;
            break;
          }
        }
        if (!found) {
          gi.variants.push({ label: label, qty: vQty, extra: extra });
        }
      }
    }

    return out;
  }

  /* QR display size is 128px in the bill stylesheet; generate at 2x so each
     module still lands on whole printer dots at 576-across print resolution. */
  var QR_DISPLAY_PX = 128;
  var QR_RENDER_PX = QR_DISPLAY_PX * 2;

  var qrLibLoading = false;
  var qrLibWaiters = [];

  function finishQrLibLoad(ok) {
    var waiters = qrLibWaiters.slice();
    qrLibWaiters = [];
    qrLibLoading = false;
    for (var i = 0; i < waiters.length; i++) {
      try { waiters[i](ok); } catch (e) {}
    }
  }

  function ensureQrLib(done) {
    if (window.QRCode) {
      done(true);
      return;
    }
    qrLibWaiters.push(done);
    if (qrLibLoading) return;
    qrLibLoading = true;
    var s = document.createElement('script');
    s.src = '/qrcode.min.js';
    s.onload = function () { finishQrLibLoad(!!window.QRCode); };
    s.onerror = function () { finishQrLibLoad(false); };
    document.head.appendChild(s);
  }

  function markQrUnavailable(doc) {
    var slots = doc.querySelectorAll('.qr-svg[data-upi-url]');
    for (var i = 0; i < slots.length; i++) {
      slots[i].textContent = 'QR unavailable';
    }
  }

  /* Paint the QR from the library's module matrix rather than using the canvas
     or <img> it renders for itself. qrcode.js draws a canvas and then swaps in
     an <img> asynchronously; at print time we were racing that swap. When it
     lost, the bill carried an empty <img> plus a <canvas> — and a canvas
     serializes to an empty element when the bill is rasterized, so the QR
     silently disappeared. Reading the matrix is synchronous and exact. */
  function paintQrFromModel(doc, slot, model, sizePx) {
    var n = model.getModuleCount();
    var quiet = 4;                       // scanners need a white margin
    var total = n + quiet * 2;
    var unit = sizePx / total;

    var canvas = doc.createElement('canvas');
    canvas.width = sizePx;
    canvas.height = sizePx;
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, sizePx, sizePx);
    ctx.fillStyle = '#000';

    for (var r = 0; r < n; r++) {
      for (var c = 0; c < n; c++) {
        if (!model.isDark(r, c)) continue;
        // Round each edge independently so modules tile with no seams or gaps
        // even though the module size is fractional (204px / 45 modules).
        var x0 = Math.round((c + quiet) * unit), x1 = Math.round((c + quiet + 1) * unit);
        var y0 = Math.round((r + quiet) * unit), y1 = Math.round((r + quiet + 1) * unit);
        ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
      }
    }

    var img = doc.createElement('img');
    img.className = 'qr-img';
    img.alt = 'UPI QR';
    img.src = canvas.toDataURL('image/png');
    slot.innerHTML = '';
    slot.appendChild(img);
  }

  function renderReceiptQr(win, done) {
    try {
      var slots = win.document.querySelectorAll('.qr-svg[data-upi-url]');
      if (!slots.length) {
        done();
        return;
      }
      if (!window.QRCode) {
        markQrUnavailable(win.document);
        done();
        return;
      }
      var pending = slots.length;
      function finishOne() {
        pending -= 1;
        if (pending <= 0) done();
      }
      slots.forEach(function (slot) {
        try {
          var upiUrl = slot.getAttribute('data-upi-url') || '';
          slot.innerHTML = '';
          // Built into a throwaway holder: we only want the matrix, not the
          // canvas/img pair the library leaves behind. Rendered at 2x the
          // 128px display size so the QR stays 1:1 at print resolution.
          var holder = win.document.createElement('div');
          slot.appendChild(holder);
          var qr = new window.QRCode(holder, {
            text: upiUrl,
            width: QR_RENDER_PX,
            height: QR_RENDER_PX,
            correctLevel: window.QRCode.CorrectLevel.M
          });

          var model = qr && qr._oQRCode;
          if (model && typeof model.getModuleCount === 'function' && typeof model.isDark === 'function') {
            paintQrFromModel(win.document, slot, model, QR_RENDER_PX);
            finishOne();
            return;
          }

          // Older builds without an exposed matrix: fall back to waiting for
          // the library's own canvas-to-image swap.
          setTimeout(function () {
            try {
              var canvas = slot.querySelector('canvas');
              var img = slot.querySelector('img');
              if (canvas && !img) {
                var replacement = win.document.createElement('img');
                replacement.className = 'qr-img';
                replacement.alt = 'UPI QR';
                replacement.src = canvas.toDataURL('image/png');
                slot.innerHTML = '';
                slot.appendChild(replacement);
              } else if (img) {
                img.className = 'qr-img';
                img.alt = 'UPI QR';
              }
            } catch (e) {
              slot.textContent = 'QR unavailable';
            }
            finishOne();
          }, 120);
        } catch (e) {
          slot.textContent = 'QR unavailable';
          finishOne();
        }
      });
    } catch (e) {
      done();
    }
  }

  function buildHTML(d) {
    var dt       = fmtDateTime(d.printed_at);
    var isReprint = d.is_reprint === true;
    var payLabels = { cash: 'Cash', upi_offline: 'UPI', card_offline: 'Card', online: 'Online' };
    
    // Parse visible configs for case-insensitive check
    var visibleConfigs = (d.bill_visible_configs || '').split(',').map(function(s) { return s.trim().toLowerCase(); }).filter(Boolean);

    var css = [
      /* ── Reset ── */
      '* { margin:0; padding:0; box-sizing:border-box; }',
      'body { font-family:Helvetica, Arial, sans-serif; font-size:11px; font-weight:500;',
      '       max-width:290px; margin:0 auto; padding:0 8px 4px; color:#000; line-height:1.4;',
      '       -webkit-font-smoothing:antialiased; }',
      '.c  { text-align:center; }',
      '.r  { text-align:right; }',
      '.b  { font-weight:bold; }',

      /* ── Type system ──
         display (serif)  : masthead accents, item names, grand total, amount-in-words
         data (monospace)  : anything tabular — qty/rate/amt, kv values, totals figures
         label (sans)      : uppercase, tracked-out labels and micro-copy               */
      '.serif { font-family:"Times New Roman", "Times New Roman", serif; }',
      '.mono  { font-family:"Courier New", Courier, monospace; }',

      /* ── Section dividers ── */
      '.perf { border-top:1px solid #000; height:0; line-height:0; font-size:0; margin:10px 0; }',
      '.perf.tight { margin:7px 0; }',
      '.ss { border-top:1px solid #000; border-bottom:1px solid #000; height:3px; margin:8px 0; }',

      /* ── Masthead ── */
      '.logo-wrap { text-align:center; margin:5px 0 3px; }',
      '.logo-img { width:168px; height:auto; }',
      '.ra { font-family:"Times New Roman", serif; font-style:italic; font-weight:400; font-size:10.5px;',
      '      text-align:center; color:#000; white-space:pre-line; max-width:250px; margin:3px auto; line-height:1.45; }',
      '.rs { font-family:"Courier New", monospace; font-size:9px; font-weight:600; text-align:center;',
      '      color:#000; margin:2px 0; letter-spacing:0.3px; }',

      /* ── Order details line ── */
      '.tl { font-family:"Times New Roman", serif; font-size:14px; font-style:italic; font-weight:600; letter-spacing:0.5px;',
      '      text-align:center; margin:9px 0 7px; }',

      /* ── Key-value rows ── */
      '.kv { display:flex; font-size:9.5px; margin:3px 0; color:#000; }',
      '.kv .k { width:46%; color:#000; text-transform:uppercase; letter-spacing:1.2px; font-weight:600; }',
      '.kv .v { font-family:"Courier New", monospace; font-weight:700; flex:1; color:#000; text-align:right; font-size:10.5px; }',

      /* ── Bill comment coupon ── */
      '.note { text-align:left; font-family:"Times New Roman", serif; font-style:italic; font-size:11px; font-weight:400;',
      '        padding:7px 9px; border:1px solid #000; margin:8px 0; line-height:1.5; white-space:pre-wrap; }',

      /* ── Items table ── */
      '.ih { display:flex; align-items:center; font-size:9px; font-weight:700; letter-spacing:1.4px; text-transform:uppercase;',
      '      border-bottom:1px solid #000; padding-bottom:4px; margin:6px 0 5px; color:#000; }',
      '.ih .item-col { flex:1; }',
      '.ih .qty-col { width:26px; text-align:center; }',
      '.ih .rate-col { width:42px; text-align:right; }',
      '.ih .amt-col { width:44px; text-align:right; }',
      '.ir { display:flex; align-items:flex-start; margin-top:3px; }',
      '.ir .item-col { flex:1; font-family:"Times New Roman", serif; font-size:13px; font-weight:400; word-break:break-word; padding-right:4px; }',
      '.ir .qty-col { width:26px; text-align:center; font-family:"Courier New", monospace; font-size:11px; font-weight:600; }',
      '.ir .rate-col { width:42px; text-align:right; font-family:"Courier New", monospace; font-size:11px; font-weight:600; }',
      '.ir .amt-col { width:44px; text-align:right; font-family:"Courier New", monospace; font-size:11px; font-weight:700; }',
      '.im { font-family:"Times New Roman", serif; font-style:italic; font-size:10px; font-weight:400; color:#000;',
      '      padding-left:10px; margin:2px 0 0; }',

      /* ── Totals ── */
      '.tr { display:flex; justify-content:space-between; margin:1px 0; color:#000; }',
      '.tr span:first-child { font-size:9.5px; text-transform:uppercase; letter-spacing:0.8px; font-weight:600; }',
      '.tr span:last-child { font-family:"Courier New", monospace; font-size:10.5px; font-weight:700; }',
      '.tg { display:flex; justify-content:space-between; align-items:baseline;',
      '      border-top:1px solid #000; border-bottom:3px solid #000; padding:9px 1px; margin:10px 0 8px; color:#000; }',
      '.tg span:first-child { font-family:"Times New Roman", serif; font-style:italic; font-size:11px; letter-spacing:1.5px; text-transform:uppercase; }',
      '.tg span:last-child { font-family:"Times New Roman", serif; font-size:21px; font-weight:700; }',
      '.aw { font-family:"Times New Roman", serif; font-style:italic; font-size:10px; font-weight:400; text-align:center;',
      '      margin:-2px 0 8px; line-height:1.4; }',
      '.eo { display:flex; justify-content:space-between; font-size:8.5px; font-weight:700; letter-spacing:1px;',
      '      text-transform:uppercase; margin:8px 0 2px; color:#000; }',

      /* ── QR stub, framed like a ticket barcode panel ── */
      '.qr-wrap { display:flex; flex-direction:column; align-items:center; margin:3px 0 2px; }',
      '.qr-title { font-family:"Times New Roman", serif; font-style:italic; font-size:10.5px; letter-spacing:1px; margin-bottom:8px; }',
      '.qr-frame { position:relative; padding:8px; }',
      '.corner { position:absolute; width:11px; height:11px; }',
      '.corner-tl { top:0; left:0; border-top:2px solid #000; border-left:2px solid #000; }',
      '.corner-tr { top:0; right:0; border-top:2px solid #000; border-right:2px solid #000; }',
      '.corner-bl { bottom:0; left:0; border-bottom:2px solid #000; border-left:2px solid #000; }',
      '.corner-br { bottom:0; right:0; border-bottom:2px solid #000; border-right:2px solid #000; }',
      '.qr-img { width:128px; height:128px; display:block; }',
      '.qr-svg { width:128px; height:128px; display:flex; align-items:center; justify-content:center; font-size:9px; }',
      '.qr-svg svg { width:128px; height:128px; display:block; }',
      '.qr-caption { font-family:"Courier New", monospace; font-size:8px; letter-spacing:0.5px; margin-top:4px; color:#000; }',

      /* ── Reprint banner ── */
      '.reprint-banner { text-align:center; font-family:"Times New Roman", serif; font-size:13px; font-weight:700;',
      '                  border-top:1px solid #000; border-bottom:1px solid #000; padding:5px; margin:8px 0 4px;',
      '                  letter-spacing:5px; }',

      /* ── Footer ── */
      '.fm { text-align:center; font-family:"Times New Roman", serif; font-style:italic; font-size:11px; font-weight:400; margin:3px 0; color:#000; }',
      '.lg { font-family:"Courier New", monospace; font-size:8px; font-weight:600; text-align:center; color:#000;',
      '      margin:2px 0; text-transform:uppercase; letter-spacing:0.8px; }',
      '.sy { font-family:"Courier New", monospace; font-size:8px; font-weight:600; text-align:center; color:#000;',
      '      margin:2px 0; text-transform:uppercase; letter-spacing:0.8px; }',
      '@media print { body { padding:0 3px 3px; } }'
    ].join('');

    var h = '<!DOCTYPE html><html><head><meta charset="UTF-8">';
    h += '<base href="' + window.location.origin + '/">';
    h += '<title>Bill #' + (d.bill_id || '') + '</title>';
    h += '<style>' + css + '</style></head><body>';

    if (isReprint) {
      h += '<div class="reprint-banner">REPRINT</div>';
    }

    /* ── HEADER ── */
    h += '<div class="logo-wrap" style="font-size:24px;font-weight:bold;">' + esc(d.restaurant_name || 'Hestia POS') + '</div>';
    if (d.address) {
      var addr = String(d.address || '').replace(/\\n/g, '\n');
      h += '<div class="ra">' + esc(addr) + '</div>';
    }
    if (d.phone)        h += '<div class="rs">Ph: ' + esc(d.phone) + '</div>';
    if (d.gst_number)   h += '<div class="rs">GSTIN: ' + esc(d.gst_number) + '</div>';
    if (d.fssai_number) h += '<div class="rs">FSSAI: ' + esc(d.fssai_number) + '</div>';
    if (d.restaurant_website) h += '<div class="rs">' + esc(d.restaurant_website.replace(/^https?:\/\//, '')) + '</div>';

    h += '<div class="ss"></div>';

    /* ── BILL IDENTITY ── */
    h += '<div class="kv"><span class="k">Bill No</span><span class="v">#' + esc(d.bill_id || '—') + '</span></div>';
    h += '<div class="kv"><span class="k">Date</span><span class="v">' + dt.date + ' &middot; ' + dt.time + '</span></div>';
    h += '<div class="perf tight"></div>';

    if (d.bill_comment) {
      h += '<div class="note">' + esc(d.bill_comment) + '</div>';
      h += '<div class="perf tight"></div>';
    }

    /* ── ORDER DETAILS ── */
    var oType = d.session_type === 'takeout' ? 'Takeout' : 'Dine-in';
    var tlText = oType;
    if (d.table_number) tlText += ' &middot; Table ' + esc(d.table_number);
    else if (d.parcel_number) tlText += ' &middot; Parcel ' + esc(d.parcel_number);
    else if (d.pickup_code) tlText += ' &middot; Code ' + esc(d.pickup_code);
    h += '<div class="tl">' + tlText + '</div>';

    h += '<div class="perf tight"></div>';

    /* ── ITEMS ── */
    h += '<div class="ih"><div class="item-col">Item</div><div class="qty-col">Qty</div><div class="rate-col">Rate</div><div class="amt-col">Amt</div></div>';

    var items = groupItemsForBill(d.items || []);
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var lineTotal = it.quantity * it.price;
      var sr = (i + 1) + '. ';
      
      h += '<div class="ir">';
      h += '<div class="item-col">' + esc(sr + (it.name || '')) + '</div>';
      h += '<div class="qty-col">' + esc(it.quantity) + '</div>';
      h += '<div class="rate-col">' + cur(it.price) + '</div>';
      h += '<div class="amt-col">' + cur(lineTotal) + '</div>';
      h += '</div>';
      
      var variants = it.variants || [];
      variants.forEach(function(v) {
        var label = (v.label || '').trim();
        if (!label) return;
        
        // Show if it has extra price OR if its label is in the visible list (case-insensitive)
        var isVisible = (v.extra && v.extra > 0) || visibleConfigs.indexOf(label.toLowerCase()) !== -1;
        
        if (isVisible) {
          h += '<div class="im">' + esc(label) + '</div>';
        }
      });
      
      /* Legacy fallback for old snapshots - show only if it seems like a variant choice */
      if (!variants.length) {
        var choices = it.config_choices || {};
        for (var k in choices) { 
          if (choices.hasOwnProperty(k)) {
            // Note: In older data we might not have 'extra' in this object, 
            // but the user wants ONLY price changing ones.
            // If we can't determine, it's safer to hide as requested.
          }
        }
      }
    }
    h += '<div class="perf tight"></div>';

    /* ── TOTALS ── */
    if ((d.total_discount || 0) > 0 || (d.gross_subtotal || 0) > 0) {
      h += '<div class="tr"><span>Gross Subtotal</span><span>&#8377;' + tax(d.gross_subtotal) + '</span></div>';
    }
    if ((d.included_due_payable || 0) > 0) {
      h += '<div class="tr"><span>Previous Due</span><span>&#8377;' + tax(d.included_due_payable) + '</span></div>';
    }
    if ((d.total_discount || 0) > 0) {
      h += '<div class="tr"><span>Discount</span><span>&minus;&#8377;' + cur(d.total_discount) + '</span></div>';
    }
    if ((d.cgst || 0) > 0)
      h += '<div class="tr"><span>CGST @ ' + ((d.cgst_rate || 0) * 100).toFixed(1) + '%</span><span>&#8377;' + tax(d.cgst) + '</span></div>';
    if ((d.sgst || 0) > 0)
      h += '<div class="tr"><span>SGST @ ' + ((d.sgst_rate || 0) * 100).toFixed(1) + '%</span><span>&#8377;' + tax(d.sgst) + '</span></div>';

    var exactTotal = Number(d.total || 0);
    var roundedTotal = Math.round(exactTotal);
    var roundOff = roundedTotal - exactTotal;
    var roundOffText = (roundOff >= 0 ? '+' : '-') + Math.abs(roundOff).toFixed(2);
    var totalInWords = amountInWords(roundedTotal);

    h += '<div class="tr"><span>Round Off</span><span>' + roundOffText + '</span></div>';

    h += '<div class="tg"><span>Grand Total</span><span>&#8377;' + cur(roundedTotal) + '</span></div>';
    h += '<div class="aw">' + esc(totalInWords) + '</div>';
    h += '<div class="eo"><span>E. &amp; O. E.</span><span>Manager</span></div>';

    /* ── PAYMENT ── */
    var splits = d.split_payments;
    var hasPayment = (splits && splits.length) || d.payment_method;
    if (hasPayment) {
      if (splits && splits.length) {
        for (var s = 0; s < splits.length; s++) {
          var lbl = payLabels[splits[s].method] || splits[s].method;
          h += '<div class="kv"><span class="k">' + esc(lbl) + '</span><span class="v">&#8377;' + cur(splits[s].amount) + '</span></div>';
        }
      } else {
        var ml = payLabels[d.payment_method] || d.payment_method;
        h += '<div class="kv"><span class="k">Payment</span><span class="v">' + esc(ml) + ' &middot; &#8377;' + cur(d.total) + '</span></div>';
      }
      if ((d.tip || 0) > 0)
        h += '<div class="kv"><span class="k">Tip</span><span class="v">&#8377;' + cur(d.tip) + '</span></div>';
    }

    /* ── FOOTER ── */
    if (d.footer_msg || d.instagram || d.google_listing) {
      h += '<div class="perf tight"></div>';
      if (d.footer_msg)     h += '<div class="fm">' + esc(d.footer_msg) + '</div>';
      if (d.instagram)      h += '<div class="fm" style="font-size:10px;">Instagram: ' + esc(d.instagram) + '</div>';
      if (d.google_listing) h += '<div class="fm" style="font-size:10px;">Google: ' + esc(d.google_listing) + '</div>';
    }

    h += '</body></html>';
    return h;
  }

  window.buildBillHTML = buildHTML;

  /* ══ Raw ESC/POS printing ════════════════════════════════════════════════
     Printing HTML through Windows makes the *printer driver* render the page
     inside spoolsv.exe. When a thermal driver faults there it takes the whole
     spooler down, and every printer on the machine vanishes until the service
     is restarted. So by default we rasterize the bill here and send the printer
     nothing but ESC/POS bytes — the driver is never asked to draw anything.

     The browser print dialog remains available via { raw: false }, and is used
     automatically if the raw path is unavailable or fails.                    */

  var RAW_ENDPOINT = '/api/print/bill-raw';
  var PRINTERS_ENDPOINT = '/api/print/printers';
  var LS_PRINTER = 'billPrinterName';
  var LS_DOTS = 'billPrinterDots';

  var rasterLibLoading = false;
  var rasterLibWaiters = [];

  function ensureRasterLib(done) {
    if (window.EscPosRaster) { done(true); return; }
    rasterLibWaiters.push(done);
    if (rasterLibLoading) return;
    rasterLibLoading = true;
    var s = document.createElement('script');
    s.src = '/escpos-raster.js';
    s.onload = s.onerror = function () {
      var waiters = rasterLibWaiters.slice();
      rasterLibWaiters = [];
      rasterLibLoading = false;
      var ok = !!window.EscPosRaster;
      for (var i = 0; i < waiters.length; i++) {
        try { waiters[i](ok); } catch (e) {}
      }
    };
    document.head.appendChild(s);
  }

  /* Printer dot width: 576 for 80mm paper, 384 for 58mm. Per-machine, so dev
     and prod stations each keep their own without touching config. */
  function rasterDots() {
    var v = parseInt(localStorage.getItem(LS_DOTS), 10);
    return v > 0 ? v : 576;
  }

  function rememberedPrinter() {
    try { return localStorage.getItem(LS_PRINTER) || ''; } catch (e) { return ''; }
  }

  /* Render the bill into a hidden iframe and wait until images and QR settle. */
  function renderBillFrame(html, done) {
    var f = document.createElement('iframe');
    f.style.cssText = 'position:fixed;right:-9999px;top:0;width:290px;height:700px;border:0;visibility:visible;';
    document.body.appendChild(f);
    f.contentWindow.document.open();
    f.contentWindow.document.write(html);
    f.contentWindow.document.close();
    var w = f.contentWindow;
    var started = Date.now();
    var qrReady = false;

    ensureQrLib(function (ok) {
      if (!ok) {
        markQrUnavailable(w.document);
        qrReady = true;
        return;
      }
      renderReceiptQr(w, function () { qrReady = true; });
    });

    function imgsReady() {
      try {
        var imgs = w.document.images || [];
        for (var i = 0; i < imgs.length; i++) {
          if (!imgs[i].complete) return false;
          if (typeof imgs[i].naturalWidth === 'number' && imgs[i].naturalWidth === 0) return false;
        }
        return qrReady;
      } catch (e) {
        return true;
      }
    }

    (function tick() {
      // Proceeds the instant everything is ready, so a cached logo costs
      // nothing. The cap is generous because giving up early on a slow phone
      // connection is what silently dropped the logo from the printed bill.
      if (imgsReady() || (Date.now() - started) > 8000) return done(f, w);
      setTimeout(tick, 80);
    })();
  }

  function dropFrame(f, delay) {
    setTimeout(function () {
      try { document.body.removeChild(f); } catch (e) {}
    }, delay || 0);
  }

  function browserPrint(f, w) {
    w.focus();
    w.print();
    dropFrame(f, 1200);
  }

  function newJobId() {
    try {
      if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    } catch (e) {}
    return 'j-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  }

  function postRaster(bytes, d, printerName, opts) {
    return fetch(RAW_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        raster: window.EscPosRaster.bytesToBase64(bytes),
        bill_id: d.bill_id,
        // One id per print attempt. The server dedups on this, so a retried
        // request is dropped while a deliberate second press always prints.
        job_id: opts.jobId,
        reprint: opts.reprint === true || d.is_reprint === true,
        printer_name: printerName || undefined
      })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok || body.error) {
          throw new Error(body.error || ('bill-raw returned HTTP ' + r.status));
        }
        return body;
      });
    });
  }

  /* ── Printer picker ────────────────────────────────────────────────────
     Replaces the browser print dialog for the raw path: same "choose where
     this prints" moment, but the bytes still bypass the driver. Preview shows
     the actual 1-bit bitmap being sent, not an approximation of it.          */

  function fetchPrinters() {
    return fetch(PRINTERS_ENDPOINT, { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (body.error) throw new Error(body.error);
        return body;
      });
  }

  function showPrinterPicker(canvas, info) {
    return new Promise(function (resolve) {
      var printers = (info && info.printers) || [];
      var current = rememberedPrinter() || (info && info.configured) || '';

      var overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483000;background:rgba(0,0,0,.55);'
        + 'display:flex;align-items:center;justify-content:center;font-family:system-ui,Segoe UI,sans-serif;';

      var panel = document.createElement('div');
      panel.style.cssText = 'background:#fff;color:#111;border-radius:10px;max-width:640px;width:92%;'
        + 'max-height:88vh;overflow:auto;padding:18px 20px;box-shadow:0 18px 50px rgba(0,0,0,.4);';

      var title = document.createElement('div');
      title.textContent = 'Print bill';
      title.style.cssText = 'font-size:17px;font-weight:700;margin-bottom:2px;';

      var sub = document.createElement('div');
      sub.textContent = 'Sent as raw ESC/POS — the printer driver is not used.';
      sub.style.cssText = 'font-size:12px;color:#666;margin-bottom:14px;';

      var body = document.createElement('div');
      body.style.cssText = 'display:flex;gap:16px;flex-wrap:wrap;';

      var preview = document.createElement('div');
      preview.style.cssText = 'flex:0 0 auto;';
      canvas.style.cssText = 'width:200px;height:auto;image-rendering:pixelated;'
        + 'border:1px solid #ddd;background:#fff;max-height:52vh;object-fit:contain;';
      var pcap = document.createElement('div');
      pcap.textContent = canvas.width + ' x ' + canvas.height + ' dots';
      pcap.style.cssText = 'font-size:11px;color:#888;margin-top:5px;text-align:center;';
      preview.appendChild(canvas);
      preview.appendChild(pcap);

      var list = document.createElement('div');
      list.style.cssText = 'flex:1 1 260px;min-width:240px;';

      if (!printers.length) {
        var none = document.createElement('div');
        none.textContent = 'No printers found on the server.';
        none.style.cssText = 'font-size:13px;color:#a00;padding:8px 0;';
        list.appendChild(none);
      }

      var selected = current;
      var rows = [];
      printers.forEach(function (p) {
        var row = document.createElement('label');
        row.style.cssText = 'display:flex;align-items:flex-start;gap:9px;padding:8px 9px;border:1px solid #e3e3e3;'
          + 'border-radius:7px;margin-bottom:6px;cursor:pointer;font-size:13px;';
        var radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = '__billprinter';
        radio.value = p.name;
        radio.checked = p.name === current;
        radio.style.cssText = 'margin-top:2px;';
        radio.onchange = function () { selected = p.name; paint(); };
        var meta = document.createElement('div');
        var nm = document.createElement('div');
        nm.textContent = p.name + (p.is_default ? '  (system default)' : '');
        nm.style.cssText = 'font-weight:600;';
        var det = document.createElement('div');
        det.textContent = p.driver + '  ·  ' + p.port;
        det.style.cssText = 'font-size:11px;color:#777;margin-top:1px;';
        meta.appendChild(nm);
        meta.appendChild(det);
        if (!p.likely_thermal) {
          var warn = document.createElement('div');
          warn.textContent = 'Not a receipt printer — raw bytes will print as garbage.';
          warn.style.cssText = 'font-size:11px;color:#b45309;margin-top:2px;';
          meta.appendChild(warn);
        }
        row.appendChild(radio);
        row.appendChild(meta);
        rows.push({ el: row, p: p, radio: radio });
        list.appendChild(row);
      });

      function paint() {
        rows.forEach(function (r) {
          var on = r.p.name === selected;
          r.radio.checked = on;
          r.el.style.borderColor = on ? '#2563eb' : '#e3e3e3';
          r.el.style.background = on ? '#eff6ff' : '#fff';
        });
        printBtn.disabled = !selected;
        printBtn.style.opacity = selected ? '1' : '.5';
        printBtn.style.cursor = selected ? 'pointer' : 'not-allowed';
      }

      var actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:9px;justify-content:flex-end;align-items:center;margin-top:16px;flex-wrap:wrap;';

      var dialogBtn = document.createElement('button');
      dialogBtn.textContent = 'Other printer / Save as PDF…';
      dialogBtn.style.cssText = 'margin-right:auto;background:none;border:0;color:#2563eb;font-size:12.5px;'
        + 'cursor:pointer;text-decoration:underline;padding:8px 0;';

      var cancelBtn = document.createElement('button');
      cancelBtn.textContent = 'Cancel';
      cancelBtn.style.cssText = 'padding:9px 15px;border:1px solid #ccc;background:#fff;border-radius:6px;'
        + 'font-size:13px;cursor:pointer;';

      var printBtn = document.createElement('button');
      printBtn.textContent = 'Print';
      printBtn.style.cssText = 'padding:9px 20px;border:0;background:#111;color:#fff;border-radius:6px;'
        + 'font-size:13px;font-weight:600;cursor:pointer;';

      function close(result) {
        document.removeEventListener('keydown', onKey, true);
        try { document.body.removeChild(overlay); } catch (e) {}
        resolve(result);
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.stopPropagation(); close({ action: 'cancel' }); }
        else if (e.key === 'Enter' && selected) { e.stopPropagation(); close({ action: 'print', printer: selected }); }
      }

      dialogBtn.onclick = function () { close({ action: 'dialog' }); };
      cancelBtn.onclick = function () { close({ action: 'cancel' }); };
      printBtn.onclick = function () {
        if (!selected) return;
        try { localStorage.setItem(LS_PRINTER, selected); } catch (e) {}
        close({ action: 'print', printer: selected });
      };
      overlay.onclick = function (e) { if (e.target === overlay) close({ action: 'cancel' }); };
      document.addEventListener('keydown', onKey, true);

      actions.appendChild(dialogBtn);
      actions.appendChild(cancelBtn);
      actions.appendChild(printBtn);
      body.appendChild(preview);
      body.appendChild(list);
      panel.appendChild(title);
      panel.appendChild(sub);
      panel.appendChild(body);
      panel.appendChild(actions);
      overlay.appendChild(panel);
      document.body.appendChild(overlay);
      paint();
      printBtn.focus();
    });
  }

  /**
   * Print a bill.
   * @param {Object} d     bill data
   * @param {Object} opts  { raw:false } to force the browser print dialog,
   *                       { chooser:true } to pick the printer first,
   *                       { reprint:true } for an operator-initiated reprint,
   *                       which is never suppressed by duplicate detection.
   */
  window.printBillReceipt = function (d, opts) {
    opts = opts || {};
    var jobId = newJobId();
    var html = buildHTML(d);

    // Explicit opt-out: captain-pc prints to a USB printer on its own machine,
    // which the server cannot reach, so it stays on the browser dialog.
    if (opts.raw === false) {
      renderBillFrame(html, browserPrint);
      return;
    }

    ensureRasterLib(function (libOk) {
      renderBillFrame(html, function (f, w) {
        if (!libOk) {
          console.warn('[bill] escpos-raster.js unavailable, using browser dialog');
          browserPrint(f, w);
          return;
        }

        // Falls back by re-rendering: rasterizing mutates the document.
        function fallback(reason) {
          console.warn('[bill] falling back to browser print dialog:', reason);
          dropFrame(f);
          renderBillFrame(html, browserPrint);
        }

        var rasterized;
        window.EscPosRaster.rasterizeDocument(w.document, { dots: rasterDots() })
          .then(function (result) {
            rasterized = result;
            if (!opts.chooser) return { action: 'print', printer: rememberedPrinter() };
            return fetchPrinters()
              .then(function (info) { return showPrinterPicker(result.canvas, info); })
              .catch(function (err) {
                console.warn('[bill] could not list printers:', err);
                return { action: 'print', printer: rememberedPrinter() };
              });
          })
          .then(function (choice) {
            if (choice.action === 'cancel') { dropFrame(f); return null; }
            if (choice.action === 'dialog') { fallback('operator chose the system dialog'); return null; }
            return postRaster(rasterized.bytes, d, choice.printer, {
              jobId: jobId,
              reprint: opts.reprint
            }).then(function (body) {
              console.log('[bill] printed raw:', body.bytes, 'bytes to', body.printer);
              dropFrame(f);
            });
          })
          .catch(function (err) { fallback(err && err.message ? err.message : err); });
      });
    });
  };

  /** Open the printer picker on its own, e.g. from a settings button. */
  window.chooseBillPrinter = function () {
    var canvas = document.createElement('canvas');
    canvas.width = rasterDots();
    canvas.height = 1;
    return fetchPrinters().then(function (info) {
      return showPrinterPicker(canvas, info);
    });
  };

})();
