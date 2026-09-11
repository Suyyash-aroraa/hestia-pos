"""
Launch the local Hestia POS server and open it in the default browser.
The small server control window stops the process started by this launcher.
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request
import traceback

# Match app.py: when frozen, prefer config.py next to the exe over bundled config.
if getattr(sys, 'frozen', False):
    _exe_dir = os.path.dirname(sys.executable)
    _ext_config = os.path.join(_exe_dir, 'config.py')
    if os.path.exists(_ext_config):
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location('config', _ext_config)
        _cfg_mod = _ilu.module_from_spec(_spec)
        sys.modules['config'] = _cfg_mod
        _spec.loader.exec_module(_cfg_mod)

try:
    from config import RESTAURANT_NAME as _RNAME
except Exception:
    _RNAME = 'POS'


try:
    from config import HOST_IP as _HOST_IP
except Exception:
    _HOST_IP = '127.0.0.1'

# Use 127.0.0.1 for browser launch (server still binds to configured HOST_IP)
from config import PORT
BASE_URL = f"http://127.0.0.1:{PORT}"
_LAST_START_ERROR = None


def _runtime_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _log_path():
    return os.path.join(_runtime_dir(), "launcher-error.log")


def _write_log(message):
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def _show_error(title, message):
    _write_log(f"{title}: {message}")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def _server_ready(url, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "/api/config", timeout=2)
            return True
        except Exception:
            time.sleep(0.4)
    return False


def _server_already_running():
    try:
        urllib.request.urlopen(BASE_URL + "/api/config", timeout=1)
        return True
    except Exception:
        return False






def _start_flask():
    """Start Flask server."""
    if getattr(sys, 'frozen', False):
        # Bundled exe — run Waitress in a background daemon thread
        def _run():
            global _LAST_START_ERROR
            exe_dir = os.path.dirname(sys.executable)
            if exe_dir not in sys.path:
                sys.path.insert(0, exe_dir)
            try:
                from app import app as flask_app
                from config import HOST_IP
                from waitress import serve
                _write_log(f"Starting bundled server on {HOST_IP}:{PORT}")
                serve(flask_app, host=HOST_IP, port=PORT, threads=32,
                      channel_timeout=600)
            except Exception:
                _LAST_START_ERROR = traceback.format_exc()
                _write_log(_LAST_START_ERROR)
        threading.Thread(target=_run, daemon=True).start()
        return None
    else:
        python = sys.executable
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        return subprocess.Popen(
            [python, script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )


def main():

    already_up = _server_already_running()
    proc = None if already_up else _start_flask()

    # Wait for server to be ready
    if not already_up:
        print("Waiting for server to start...")
        if not _server_ready(BASE_URL):
            extra = ""
            if _LAST_START_ERROR:
                extra = "\n\nLast startup error was written to launcher-error.log."
            print("ERROR: Server failed to start")
            if proc:
                try: proc.terminate()
                except: pass
            _show_error(
                "POS Failed To Start",
                "The POS server could not start." + extra +
                "\n\nCheck `config.py` and make sure the database is reachable from this PC."
            )
            return

    # Launch browser with tkinter control window
    print("Launching browser...")
    try:
        import webbrowser
        import tkinter as tk
        from tkinter import font as tkfont

        # Open browser
        webbrowser.open_new(BASE_URL)

        # Small tkinter window to stop server
        BG = "#2C1810"
        FG = "#E8A84E"
        BTN_BG = "#4A2C1A"

        root = tk.Tk()
        root.title(f"{_RNAME} - Server Control")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.geometry("300x120")

        # Center on screen
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"300x120+{(sw-300)//2}+{(sh-120)//2}")

        btn_font = tkfont.Font(family="Segoe UI", size=10)

        tk.Label(root, text="POS running in browser", font=btn_font,
                 bg=BG, fg=FG, pady=20).pack()

        def _on_close():
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except:
                    try: proc.kill()
                    except: pass
            root.destroy()

        tk.Button(
            root, text="Stop Server", font=btn_font,
            bg=BTN_BG, fg="#E87070", activebackground="#5E3820", activeforeground="#E87070",
            relief="flat", padx=20, pady=8, cursor="hand2",
            command=_on_close,
        ).pack()

        root.protocol("WM_DELETE_WINDOW", _on_close)
        root.mainloop()

    except Exception as e:
        _write_log(traceback.format_exc())
        _show_error("POS Launch Failed", str(e))
        print(f"ERROR: Failed to launch: {e}")
        if proc:
            try: proc.terminate()
            except: pass


if __name__ == "__main__":
    main()
