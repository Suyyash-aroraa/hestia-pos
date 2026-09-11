/*!
 * shared-dialog.js — styled replacements for window.alert / window.confirm.
 *
 * Public API (all attached to window):
 *   uiAlert(message, opts)   -> Promise<void>     resolves when dismissed
 *   uiConfirm(message, opts) -> Promise<boolean>  true = primary action chosen
 *   uiToast(message, opts)   -> void              non-blocking corner notice
 *
 * opts: {
 *   title:       heading text (defaults per variant)
 *   variant:     'info' | 'success' | 'warn' | 'danger' | 'question'
 *   confirmText: primary button label   (default 'OK' / 'Yes')
 *   cancelText:  secondary button label (default 'Cancel')
 *   detail:      smaller line under the message
 *   duration:    uiToast only, ms before auto-dismiss (default 3200)
 * }
 *
 * Notes for callers:
 *  - uiAlert does NOT block like window.alert. Code after the call keeps
 *    running unless you `await` it. Every existing call site was checked to
 *    make sure nothing depended on the blocking behaviour.
 *  - uiConfirm MUST be awaited: `if (!await uiConfirm('...')) return;`
 *  - When no variant is given, one is inferred from the message text so the
 *    many bare `alert(d.error)` call sites still get error styling.
 */
(function () {
  'use strict';
  if (window.uiAlert) return; // already loaded

  var CSS = `
  .uidlg-overlay{position:fixed;inset:0;z-index:10000;display:flex;align-items:center;justify-content:center;
    padding:20px;background:rgba(28,15,8,.52);backdrop-filter:blur(2px);-webkit-backdrop-filter:blur(2px);
    opacity:0;transition:opacity .16s ease;font-family:'DM Sans',system-ui,-apple-system,sans-serif;}
  .uidlg-overlay.is-open{opacity:1;}
  .uidlg-card{width:100%;max-width:400px;background:var(--cream,#F9F5EE);color:var(--text,#2C1810);
    border-radius:18px;box-shadow:0 18px 56px rgba(28,15,8,.34),0 2px 8px rgba(28,15,8,.12);
    padding:26px 26px 22px;text-align:center;max-height:88vh;overflow-y:auto;
    transform:translateY(10px) scale(.97);transition:transform .18s cubic-bezier(.2,.9,.3,1.2);}
  .uidlg-overlay.is-open .uidlg-card{transform:none;}
  .uidlg-icon{width:52px;height:52px;border-radius:50%;margin:0 auto 16px;display:flex;
    align-items:center;justify-content:center;flex-shrink:0;}
  .uidlg-icon svg{width:26px;height:26px;stroke:currentColor;stroke-width:2.2;fill:none;
    stroke-linecap:round;stroke-linejoin:round;}
  .uidlg-icon--info{background:#e0edfb;color:#1d4ed8;}
  .uidlg-icon--success{background:#dcf5e4;color:#15803d;}
  .uidlg-icon--warn{background:#fdf0d5;color:#b45309;}
  .uidlg-icon--danger{background:#fde4e4;color:#b91c1c;}
  .uidlg-icon--question{background:#f0e6d8;color:var(--roast,#7B3F1E);}
  .uidlg-title{font-family:'DM Serif Display',Georgia,serif;font-size:21px;font-weight:400;
    line-height:1.25;margin:0 0 8px;color:var(--espresso,#2C1810);}
  .uidlg-msg{font-size:14.5px;line-height:1.5;margin:0;color:var(--muted,#8C7B6B);white-space:pre-line;
    overflow-wrap:anywhere;}
  .uidlg-detail{font-size:12.5px;line-height:1.45;margin:8px 0 0;color:var(--muted,#8C7B6B);opacity:.8;}
  .uidlg-actions{display:flex;gap:9px;margin-top:22px;}
  .uidlg-actions--stack{flex-direction:column-reverse;}
  .uidlg-btn{flex:1;padding:12px 16px;border-radius:11px;border:none;font-family:inherit;font-size:14.5px;
    font-weight:700;cursor:pointer;transition:background .12s,border-color .12s,opacity .12s;
    -webkit-tap-highlight-color:transparent;}
  .uidlg-btn:focus-visible{outline:2px solid var(--caramel,#C97B2E);outline-offset:2px;}
  .uidlg-btn--primary{background:var(--caramel,#C97B2E);color:#fff;}
  .uidlg-btn--primary:hover{background:var(--roast,#7B3F1E);}
  .uidlg-btn--danger{background:#dc2626;color:#fff;}
  .uidlg-btn--danger:hover{background:#b91c1c;}
  .uidlg-btn--ghost{background:var(--foam,#FDF8F0);color:var(--text,#2C1810);
    border:1.5px solid var(--border,rgba(44,24,16,.16));}
  .uidlg-btn--ghost:hover{background:var(--cream-dark,#EDE8DC);}

  .uidlg-toast-wrap{position:fixed;top:18px;left:50%;transform:translateX(-50%);z-index:10001;
    display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;
    font-family:'DM Sans',system-ui,-apple-system,sans-serif;}
  .uidlg-toast{display:flex;align-items:center;gap:9px;max-width:min(92vw,420px);padding:11px 17px;
    border-radius:11px;background:var(--espresso,#2C1810);color:#FDF8F0;font-size:14px;font-weight:600;
    box-shadow:0 8px 26px rgba(28,15,8,.3);opacity:0;transform:translateY(-9px);
    transition:opacity .16s ease,transform .16s ease;pointer-events:auto;}
  .uidlg-toast.is-open{opacity:1;transform:none;}
  .uidlg-toast--success{background:#15803d;}
  .uidlg-toast--danger{background:#b91c1c;}
  .uidlg-toast--warn{background:#b45309;}

  @media (max-width:420px){
    .uidlg-card{padding:22px 20px 18px;border-radius:16px;}
    .uidlg-title{font-size:19px;}
  }
  @media (prefers-reduced-motion:reduce){
    .uidlg-overlay,.uidlg-card,.uidlg-toast{transition:none;}
  }`;

  var ICONS = {
    info:     '<path d="M12 16v-5M12 8h.01"/><circle cx="12" cy="12" r="9"/>',
    success:  '<path d="M20 6 9 17l-5-5"/>',
    warn:     '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
    danger:   '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
    question: '<circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
    // Used for destructive confirms: "you are about to delete", not "an error happened".
    trash:    '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6"/>'
  };

  var TITLES = {
    info: 'Notice', success: 'Done', warn: 'Heads up',
    danger: 'Something went wrong', question: 'Please confirm'
  };

  // Most legacy call sites are bare `alert(d.error || 'Failed')`, so infer a
  // sensible look from the wording rather than editing all of them by hand.
  // Order matters: a genuine failure ("Failed to add table") must beat the
  // success keyword it happens to contain.
  function inferVariant(msg) {
    var s = String(msg || '');
    if (/\b(fail|failed|error|invalid|denied|unable|could not|cannot|not found|no session|not loaded|not printed|went wrong)/i.test(s)) return 'danger';
    if (/\b(required|must |please |first|before |already |disabled|not allowed|nothing to|no held|select )/i.test(s)) return 'warn';
    if (/\b(success|successful|saved|added|updated|deleted|removed|sent|printed|requested|settled|closed|complete|created)/i.test(s)) return 'success';
    return 'info';
  }

  var styleEl = null;
  function ensureStyle() {
    if (styleEl) return;
    styleEl = document.createElement('style');
    styleEl.setAttribute('data-uidlg', '');
    styleEl.textContent = CSS;
    (document.head || document.documentElement).appendChild(styleEl);
  }

  var queue = [];
  var active = false;

  function pump() {
    if (active || !queue.length) return;
    active = true;
    render(queue.shift());
  }

  function render(job) {
    ensureStyle();
    var o = job.opts || {};
    var variant = o.variant || inferVariant(job.message);
    var isConfirm = job.kind === 'confirm';
    if (isConfirm && !o.variant) variant = 'question';

    var prevFocus = document.activeElement;
    var uid = 'uidlg-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7);

    var overlay = document.createElement('div');
    overlay.className = 'uidlg-overlay';

    var confirmText = o.confirmText || (isConfirm ? 'Yes' : 'OK');
    var cancelText = o.cancelText || 'Cancel';
    // Long custom labels read better stacked than squeezed side by side.
    var stack = isConfirm && (confirmText.length + cancelText.length) > 26;
    var primaryClass = (variant === 'danger' && isConfirm) ? 'uidlg-btn--danger' : 'uidlg-btn--primary';

    var iconKey = (variant === 'danger' && isConfirm) ? 'trash' : variant;

    var card = document.createElement('div');
    card.className = 'uidlg-card';
    card.setAttribute('role', 'alertdialog');
    card.setAttribute('aria-modal', 'true');
    card.setAttribute('aria-labelledby', uid + '-t');
    card.setAttribute('aria-describedby', uid + '-m');
    card.innerHTML =
      '<div class="uidlg-icon uidlg-icon--' + variant + '">' +
        '<svg viewBox="0 0 24 24" aria-hidden="true">' + (ICONS[iconKey] || ICONS.info) + '</svg>' +
      '</div>' +
      '<h2 class="uidlg-title" id="' + uid + '-t"></h2>' +
      '<p class="uidlg-msg" id="' + uid + '-m"></p>' +
      (o.detail ? '<p class="uidlg-detail"></p>' : '') +
      '<div class="uidlg-actions' + (stack ? ' uidlg-actions--stack' : '') + '">' +
        (isConfirm ? '<button type="button" class="uidlg-btn uidlg-btn--ghost" data-act="cancel"></button>' : '') +
        '<button type="button" class="uidlg-btn ' + primaryClass + '" data-act="ok"></button>' +
      '</div>';

    // textContent everywhere — messages carry server text and item names.
    card.querySelector('.uidlg-title').textContent = o.title || TITLES[variant] || TITLES.info;
    card.querySelector('.uidlg-msg').textContent = String(job.message == null ? '' : job.message);
    if (o.detail) card.querySelector('.uidlg-detail').textContent = String(o.detail);
    card.querySelector('[data-act="ok"]').textContent = confirmText;
    if (isConfirm) card.querySelector('[data-act="cancel"]').textContent = cancelText;

    overlay.appendChild(card);
    document.body.appendChild(overlay);
    requestAnimationFrame(function () { overlay.classList.add('is-open'); });

    var done = false;
    function close(result) {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey, true);
      overlay.classList.remove('is-open');
      var remove = function () {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        try { if (prevFocus && prevFocus.focus) prevFocus.focus(); } catch (e) {}
        active = false;
        pump();
      };
      // Fall back to a timer in case transitionend never fires.
      var fired = false;
      overlay.addEventListener('transitionend', function () {
        if (!fired) { fired = true; remove(); }
      });
      setTimeout(function () { if (!fired) { fired = true; remove(); } }, 240);
      job.resolve(isConfirm ? !!result : undefined);
    }

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); }
      else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); close(true); }
      else if (e.key === 'Tab') {
        // Keep focus inside the dialog.
        var f = card.querySelectorAll('button');
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', onKey, true);

    card.querySelector('[data-act="ok"]').addEventListener('click', function () { close(true); });
    var cancelBtn = card.querySelector('[data-act="cancel"]');
    if (cancelBtn) cancelBtn.addEventListener('click', function () { close(false); });
    // Click-outside dismisses a confirm as "no"; an alert just closes.
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close(false); });

    setTimeout(function () {
      var btn = card.querySelector('[data-act="ok"]');
      if (btn) btn.focus();
    }, 30);
  }

  function enqueue(kind, message, opts) {
    return new Promise(function (resolve) {
      queue.push({ kind: kind, message: message, opts: opts || {}, resolve: resolve });
      if (document.body) pump();
      else document.addEventListener('DOMContentLoaded', pump, { once: true });
    });
  }

  window.uiAlert = function (message, opts) { return enqueue('alert', message, opts); };
  window.uiConfirm = function (message, opts) { return enqueue('confirm', message, opts); };

  var toastWrap = null;
  window.uiToast = function (message, opts) {
    ensureStyle();
    var o = opts || {};
    if (!toastWrap) {
      toastWrap = document.createElement('div');
      toastWrap.className = 'uidlg-toast-wrap';
      document.body.appendChild(toastWrap);
    }
    var el = document.createElement('div');
    el.className = 'uidlg-toast uidlg-toast--' + (o.variant || inferVariant(message));
    el.setAttribute('role', 'status');
    el.textContent = String(message == null ? '' : message);
    toastWrap.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('is-open'); });
    setTimeout(function () {
      el.classList.remove('is-open');
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }, o.duration || 3200);
  };
})();
