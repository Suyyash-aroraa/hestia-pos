/*
 * escpos-raster.js — turns a rendered bill into raw ESC/POS raster bytes.
 *
 * Why this exists: printing HTML through Windows makes the printer's own driver
 * render the page inside spoolsv.exe. When a cheap thermal driver faults there,
 * the whole spooler dies and every printer on the machine disappears until the
 * service is restarted. So we rasterize the bill ourselves, in the browser, and
 * send the printer nothing but ESC/POS bytes — the same path KOTs already use,
 * which has never taken the spooler down.
 *
 * Pipeline: bill DOM -> <foreignObject> SVG -> canvas at printer resolution
 *           -> 1-bit threshold -> GS v 0 raster bands.
 *
 * Exposes window.EscPosRaster.
 */
(function () {
  'use strict';

  var ESC = 0x1b, GS = 0x1d;

  // 80mm paper at 203dpi. 58mm paper is 384.
  var DEFAULT_DOTS = 576;
  // The bill stylesheet lays out at this CSS width (body max-width in print-bill.js).
  var DEFAULT_CSS_WIDTH = 290;
  // Rows per GS v 0 command. Some thermal firmwares choke on one huge raster.
  var BAND_ROWS = 128;
  // Luminance below this becomes ink. Antialiased text thresholds better than it dithers.
  var DEFAULT_THRESHOLD = 176;

  function concatBytes(chunks) {
    var total = 0, i;
    for (i = 0; i < chunks.length; i++) total += chunks[i].length;
    var out = new Uint8Array(total);
    var off = 0;
    for (i = 0; i < chunks.length; i++) { out.set(chunks[i], off); off += chunks[i].length; }
    return out;
  }

  /* ── DOM -> canvas ─────────────────────────────────────────────────────── */

  function fetchAsDataUrl(url) {
    return fetch(url, { cache: 'force-cache' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.blob();
      })
      .then(function (blob) {
        return new Promise(function (resolve, reject) {
          var fr = new FileReader();
          fr.onload = function () { resolve(fr.result); };
          fr.onerror = reject;
          fr.readAsDataURL(blob);
        });
      });
  }

  /* Redraw an already-loaded <img> as a data URL, scaled to the size the bill
     actually prints it at. Same-origin images do not taint the canvas. */
  function imageToDataUrl(img, targetWidthPx) {
    var w = img.naturalWidth, h = img.naturalHeight;
    if (!w || !h) return null;
    var scale = Math.min(1, (targetWidthPx || w) / w);
    var cw = Math.max(1, Math.round(w * scale));
    var ch = Math.max(1, Math.round(h * scale));
    var canvas = document.createElement('canvas');
    canvas.width = cw;
    canvas.height = ch;
    var ctx = canvas.getContext('2d');
    // Flatten transparency onto paper white; a transparent logo would
    // otherwise threshold to a solid black block.
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, cw, ch);
    ctx.drawImage(img, 0, 0, cw, ch);
    return canvas.toDataURL('image/png');
  }

  /* An <img src="/assets/Logo.png"> inside a foreignObject never loads — the SVG
     is rasterized from a data: URL and cannot reach back out for subresources.
     Every image has to be inlined first. QR codes are already data URLs.

     Preferred route is redrawing the element the page already loaded: it needs
     no network at all, and it downscales. Re-fetching pulled the full 356KB
     logo a second time and embedded all of it in the SVG data URL — slow over
     a phone connection, and if that fetch failed the logo was dropped from the
     bill entirely, which is exactly how bills printed from pos-phone lost it. */
  function inlineImages(doc, dots, cssWidth) {
    var imgs = Array.prototype.slice.call(doc.images || []);
    var scale = (dots && cssWidth) ? (dots / cssWidth) : 2;

    return Promise.all(imgs.map(function (img) {
      var src = img.getAttribute('src') || '';
      if (!src || src.indexOf('data:') === 0) return Promise.resolve();

      if (img.complete && img.naturalWidth > 0) {
        try {
          var displayWidth = img.clientWidth || img.naturalWidth;
          var url = imageToDataUrl(img, Math.round(displayWidth * scale));
          if (url) {
            img.setAttribute('src', url);
            return Promise.resolve();
          }
        } catch (e) {
          // Canvas unavailable or tainted — fall through to fetching.
        }
      }

      return fetchAsDataUrl(img.src)
        .then(function (dataUrl) { img.setAttribute('src', dataUrl); })
        .catch(function () {
          // A missing logo should cost us the logo, not the bill.
          if (img.parentNode) img.parentNode.removeChild(img);
        });
    }));
  }

  /* The bill stylesheet targets `body`, but a foreignObject subtree renders as a
     plain element. Rehome those rules onto a class we control, and fold in the
     @media print block since this *is* the print render. */
  function adaptCss(cssText) {
    var printRules = '';
    var css = cssText.replace(/@media\s+print\s*\{([\s\S]*?)\}\s*\}/g, function (_m, inner) {
      printRules += inner + '}';
      return '';
    });
    css += printRules;
    return css.replace(/(^|[},;\s])body\s*\{/g, '$1.__billroot{');
  }

  function serializeForeignObject(doc, cssWidth) {
    var css = Array.prototype.slice.call(doc.querySelectorAll('style'))
      .map(function (s) { return s.textContent || ''; })
      .join('\n');

    var root = doc.createElement('div');
    root.setAttribute('class', '__billroot');
    root.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
    while (doc.body.firstChild) root.appendChild(doc.body.firstChild);
    doc.body.appendChild(root);

    var markup = new XMLSerializer().serializeToString(root);
    var style = '<style xmlns="http://www.w3.org/1999/xhtml">'
      + adaptCss(css).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      + '\n.__billroot{width:' + cssWidth + 'px;background:#fff;}</style>';

    return style + markup;
  }

  function svgToCanvas(svgMarkup, cssWidth, cssHeight, dots) {
    var scale = dots / cssWidth;
    var height = Math.max(1, Math.ceil(cssHeight * scale));

    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + cssWidth
      + '" height="' + Math.ceil(cssHeight) + '">'
      + '<foreignObject x="0" y="0" width="' + cssWidth + '" height="' + Math.ceil(cssHeight) + '">'
      + svgMarkup
      + '</foreignObject></svg>';

    var url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);

    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        var canvas = document.createElement('canvas');
        canvas.width = dots;
        canvas.height = height;
        var ctx = canvas.getContext('2d');
        // Unpainted canvas is transparent black, which thresholds to solid ink.
        ctx.fillStyle = '#fff';
        ctx.fillRect(0, 0, dots, height);
        // The SVG is vector, so drawing it at 2x renders text at 2x — not upscaled.
        ctx.drawImage(img, 0, 0, dots, height);
        resolve(canvas);
      };
      img.onerror = function () { reject(new Error('bill raster failed: SVG did not load')); };
      img.src = url;
    });
  }

  /* ── canvas -> ESC/POS ─────────────────────────────────────────────────── */

  function canvasToBitmap(canvas, threshold) {
    var w = canvas.width, h = canvas.height;
    var data = canvas.getContext('2d').getImageData(0, 0, w, h).data;
    var widthBytes = Math.ceil(w / 8);
    var bits = new Uint8Array(widthBytes * h);
    var rowHasInk = new Uint8Array(h);

    for (var y = 0; y < h; y++) {
      var rowOff = y * widthBytes;
      var px = y * w * 4;
      for (var x = 0; x < w; x++, px += 4) {
        var a = data[px + 3];
        // Composite against white so transparent areas read as paper, not ink.
        var lum = a === 0 ? 255
          : (0.299 * data[px] + 0.587 * data[px + 1] + 0.114 * data[px + 2]) * (a / 255)
            + 255 * (1 - a / 255);
        if (lum < threshold) {
          bits[rowOff + (x >> 3)] |= 0x80 >> (x & 7);
          rowHasInk[y] = 1;
        }
      }
    }
    return { bits: bits, widthBytes: widthBytes, height: h, rowHasInk: rowHasInk };
  }

  /* Paint the thresholded bits back over the canvas, so previews show exactly
     what the printer will put on paper rather than the colour render. */
  function bitmapToCanvas(bmp, canvas) {
    var ctx = canvas.getContext('2d');
    var img = ctx.createImageData(canvas.width, canvas.height);
    var out = img.data;
    for (var y = 0; y < bmp.height; y++) {
      var rowOff = y * bmp.widthBytes;
      var px = y * canvas.width * 4;
      for (var x = 0; x < canvas.width; x++, px += 4) {
        var ink = bmp.bits[rowOff + (x >> 3)] & (0x80 >> (x & 7));
        var v = ink ? 0 : 255;
        out[px] = out[px + 1] = out[px + 2] = v;
        out[px + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    return canvas;
  }

  function bitmapToEscPos(bmp) {
    var chunks = [];
    var wb = bmp.widthBytes;
    var y = 0;

    // Walk runs of blank/inked rows rather than fixed-size bands. A receipt is
    // mostly whitespace, but on fixed bands nearly every band catches a stray
    // row of ink and gets sent in full — which is how a bill that is 90% paper
    // still costs a full uncompressed raster on the wire.
    while (y < bmp.height) {
      var start = y;
      var inked = !!bmp.rowHasInk[y];
      while (y < bmp.height && !!bmp.rowHasInk[y] === inked) y++;
      var rows = y - start;

      if (!inked) {
        // Feed blank space as a 1-byte-wide strip of white raster rather than
        // with ESC J. ESC J advances in the printer's *vertical motion unit*,
        // which ESC @ resets to the firmware default — commonly 1/360" instead
        // of the 1/203" we rasterize at, so blank runs came out ~56% short and
        // the whole bill printed vertically squashed. GS v 0 is dot-addressed:
        // it always advances by exactly its pixel height. Costs 1 byte per row
        // instead of 72, so the saving over a full-width raster is nearly intact.
        var left = rows;
        var blankRow = null;
        while (left > 0) {
          var n = Math.min(BAND_ROWS, left);
          if (!blankRow || blankRow.length !== n) blankRow = new Uint8Array(n);
          chunks.push(new Uint8Array([
            GS, 0x76, 0x30, 0x00,
            1, 0,
            n & 0xff, (n >> 8) & 0xff
          ]));
          chunks.push(blankRow);
          left -= n;
        }
      } else {
        // Still cap each raster command: some firmwares choke on a huge one.
        var off = 0;
        while (off < rows) {
          var take = Math.min(BAND_ROWS, rows - off);
          chunks.push(new Uint8Array([
            GS, 0x76, 0x30, 0x00,
            wb & 0xff, (wb >> 8) & 0xff,
            take & 0xff, (take >> 8) & 0xff
          ]));
          chunks.push(bmp.bits.subarray((start + off) * wb, (start + off + take) * wb));
          off += take;
        }
      }
    }
    return chunks;
  }

  /* ── public API ────────────────────────────────────────────────────────── */

  /**
   * Rasterize a rendered bill document into ESC/POS bytes.
   * @param {Document} doc  fully rendered bill document (images + QR settled)
   * @param {Object}   opts { dots, cssWidth, threshold, cut, feed }
   * @returns {Promise<{bytes: Uint8Array, canvas: HTMLCanvasElement}>}
   */
  function rasterizeDocument(doc, opts) {
    opts = opts || {};
    var dots = opts.dots || DEFAULT_DOTS;
    var cssWidth = opts.cssWidth || DEFAULT_CSS_WIDTH;
    var threshold = opts.threshold == null ? DEFAULT_THRESHOLD : opts.threshold;
    var feed = opts.feed == null ? 3 : opts.feed;

    return inlineImages(doc, dots, cssWidth).then(function () {
      var height = Math.max(
        doc.body.scrollHeight,
        doc.documentElement ? doc.documentElement.scrollHeight : 0
      );
      var markup = serializeForeignObject(doc, cssWidth);
      return svgToCanvas(markup, cssWidth, height, dots);
    }).then(function (canvas) {
      var bmp = canvasToBitmap(canvas, threshold);
      var chunks = [new Uint8Array([ESC, 0x40])]; // ESC @ — reset to a known state
      chunks = chunks.concat(bitmapToEscPos(bmp));
      for (var i = 0; i < feed; i++) chunks.push(new Uint8Array([0x0a]));
      if (opts.cut !== false) chunks.push(new Uint8Array([GS, 0x56, 0x01]));
      return { bytes: concatBytes(chunks), canvas: bitmapToCanvas(bmp, canvas) };
    });
  }

  function bytesToBase64(bytes) {
    var CHUNK = 0x8000, parts = [];
    for (var i = 0; i < bytes.length; i += CHUNK) {
      parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK)));
    }
    return btoa(parts.join(''));
  }

  window.EscPosRaster = {
    rasterizeDocument: rasterizeDocument,
    bytesToBase64: bytesToBase64,
    DEFAULT_DOTS: DEFAULT_DOTS
  };
})();
