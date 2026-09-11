"""Raw ESC/POS delivery to Windows printers, with print-spooler recovery.

Every byte we send this way is passed straight to the printer port with datatype
RAW, so the printer's own driver never renders anything. That matters: rendering
inside spoolsv.exe is what takes the whole spooler (and every installed printer)
down when a cheap thermal driver faults.

send_raw() additionally notices when the spooler has already died, restarts it,
and replays the job — so a crash caused by anything else on the machine costs a
couple of seconds instead of a manual "restart Print Spooler" trip.
"""

import subprocess
import sys
import time

SPOOLER_SERVICE = 'Spooler'

_SERVICE_RUNNING = 4
_SERVICE_STOPPED = 1

# Win32 error codes that mean "the spooler is not there / not answering".
_SPOOLER_DEAD_CODES = {
    1722,   # RPC_S_SERVER_UNAVAILABLE — spoolsv.exe is gone
    1723,   # RPC_S_SERVER_TOO_BUSY
    6,      # ERROR_INVALID_HANDLE — handle died mid-job
    1717,   # RPC_S_UNKNOWN_IF
    109,    # ERROR_BROKEN_PIPE
    1726,   # RPC_S_CALL_FAILED
}

_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0


def _log(msg):
    print(f'[raw_print] {msg}', file=sys.stderr)


def _err_code(exc):
    """Pull the Win32 error number out of a pywin32 exception."""
    for attr in ('winerror', 'errno'):
        code = getattr(exc, attr, None)
        if isinstance(code, int):
            return code
    args = getattr(exc, 'args', None)
    if args and isinstance(args[0], int):
        return args[0]
    return None


def looks_like_spooler_death(exc):
    code = _err_code(exc)
    if code in _SPOOLER_DEAD_CODES:
        return True
    text = str(exc).lower()
    return 'spooler' in text or 'rpc server is unavailable' in text


def spooler_state():
    """Return the Win32 service state for the spooler, or None if unknown."""
    try:
        import win32serviceutil
        return win32serviceutil.QueryServiceStatus(SPOOLER_SERVICE)[1]
    except Exception as exc:
        _log(f'could not query spooler state: {exc}')
        return None


def _sc(*args):
    """Run sc.exe without flashing a console window. Returns (rc, output)."""
    try:
        proc = subprocess.run(
            ('sc.exe',) + args,
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=_NO_WINDOW,
        )
        return proc.returncode, (proc.stdout or '') + (proc.stderr or '')
    except Exception as exc:
        return -1, str(exc)


def ensure_spooler_running(timeout=25.0):
    """Make sure the spooler service is running. Returns (ok, message).

    Starting a service needs administrator rights. If we don't have them we say
    so plainly rather than silently failing, because the operator needs to know
    the app has to be relaunched elevated.
    """
    state = spooler_state()
    if state == _SERVICE_RUNNING:
        return True, 'spooler already running'

    _log(f'spooler not running (state={state}), attempting restart')

    # A wedged spooler reports RUNNING but refuses calls; a stop first clears it.
    if state not in (None, _SERVICE_STOPPED):
        _sc('stop', SPOOLER_SERVICE)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if spooler_state() in (_SERVICE_STOPPED, None):
                break
            time.sleep(0.5)

    rc, out = _sc('start', SPOOLER_SERVICE)
    if rc != 0 and 'already running' not in out.lower():
        if 'access is denied' in out.lower() or rc == 5:
            return False, ('Print Spooler is stopped and could not be restarted: '
                           'access denied. Run the POS as administrator, or start '
                           'the "Print Spooler" service manually.')
        _log(f'sc start failed rc={rc}: {out.strip()}')

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if spooler_state() == _SERVICE_RUNNING:
            # The spooler needs a beat after start before it will accept jobs.
            time.sleep(1.5)
            _log('spooler restarted successfully')
            return True, 'spooler restarted'
        time.sleep(0.5)

    return False, 'Print Spooler did not come back up within the timeout'


def list_printers():
    """Return [{name, port, driver, is_default, likely_thermal}] for this machine.

    `likely_thermal` is a hint for the operator-facing picker: raw ESC/POS bytes
    only make sense on a receipt printer, and printing them to a laser or to
    "Microsoft Print to PDF" produces pages of garbage.
    """
    try:
        import win32print
    except ImportError:
        return []

    try:
        default_name = win32print.GetDefaultPrinter()
    except Exception:
        default_name = ''

    out = []
    try:
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS,
            None,
            2,
        )
    except Exception as exc:
        _log(f'EnumPrinters(level 2) failed: {exc}')
        return []

    for p in printers:
        name = p.get('pPrinterName') or ''
        port = p.get('pPortName') or ''
        driver = p.get('pDriverName') or ''
        out.append({
            'name': name,
            'port': port,
            'driver': driver,
            'is_default': name == default_name,
            'likely_thermal': _looks_thermal(name, port, driver),
        })
    return out


# Ports/drivers that clearly cannot take raw ESC/POS.
_NON_THERMAL_HINTS = ('microsoft print to pdf', 'onenote', 'xps document writer', 'fax')


def _looks_thermal(name, port, driver):
    blob = f'{name} {driver}'.lower()
    if any(h in blob for h in _NON_THERMAL_HINTS):
        return False
    port_l = (port or '').lower()
    # Receipt printers sit on USB/COM/LPT or a raw TCP port; virtual devices
    # sit on nul:, FILE: or PORTPROMPT:.
    if port_l.startswith(('nul', 'file:', 'portprompt')):
        return False
    return True


def printer_exists(printer_name):
    if not printer_name:
        return False
    try:
        import win32print
        for _flags, _desc, name, _comment in win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        ):
            if name == printer_name:
                return True
    except Exception as exc:
        _log(f'EnumPrinters failed: {exc}')
    return False


def _write_once(printer_name, data, doc_name):
    import win32print
    hp = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(hp, 1, (doc_name, None, 'RAW'))
        try:
            win32print.StartPagePrinter(hp)
            win32print.WritePrinter(hp, data)
            win32print.EndPagePrinter(hp)
        finally:
            # EndDocPrinter must run even if the write failed, or the spooler is
            # left holding an open job for this handle.
            win32print.EndDocPrinter(hp)
    finally:
        win32print.ClosePrinter(hp)


def send_raw(printer_name, data, doc_name='Document', retries=1):
    """Send raw bytes to a printer, recovering from a dead spooler.

    Raises the underlying exception if it still fails after recovery.
    """
    if not printer_name:
        raise ValueError('printer name is empty')
    if not data:
        raise ValueError('nothing to print')

    last_exc = None
    for attempt in range(retries + 1):
        try:
            _write_once(printer_name, data, doc_name)
            if attempt:
                _log(f'print succeeded on retry {attempt}')
            return
        except Exception as exc:
            last_exc = exc
            code = _err_code(exc)
            _log(f'print attempt {attempt + 1} failed (code={code}): {exc}')
            if attempt >= retries:
                break
            if looks_like_spooler_death(exc) or spooler_state() != _SERVICE_RUNNING:
                ok, msg = ensure_spooler_running()
                _log(f'spooler recovery: {msg}')
                if not ok:
                    raise RuntimeError(msg) from exc
            else:
                time.sleep(0.75)

    raise last_exc
