# Hestia POS

A free, local point-of-sale application for dine-in and counter takeout. This is an independent copy of the original application; its code and database stay inside `hestia-pos`.

## Start on Windows

1. Install Python 3.11 or newer, including the Python launcher and Tcl/Tk.
2. Double-click `start.bat`. The first run creates a virtual environment and installs dependencies using the internet.
3. Enter and confirm your own admin password at the hidden prompt.
4. The launcher opens **http://127.0.0.1:5010** in your browser.
5. Open Administration and log in with the password you just entered.

There is **no default admin password**. The password entered at startup is held in the running process and is not saved to disk. Enter it again on each launch, or supply `HESTIA_ADMIN_PASSWORD` through your environment before starting. A missing or blank password cannot start the backend directly.

After dependencies are installed, normal POS use works locally. Use **Stop Server** in the launcher window to stop the process it started.

Alternatively:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe start.py --server
```

This alternative prompts for the password and runs the server in your terminal. Open the address above yourself; press Ctrl+C to stop.

## First setup

- Enter restaurant name, address, contact details and invoice information in `config.py` or Administration > Settings.
- Set your applicable tax rates. Defaults are zero; rates use fractions, so `0.025` means 2.5%.
- Add your menu through Menu Manager. There are no bundled restaurant menu items or customer records.
- Ten empty tables are created on first startup. Manage tables and staff PINs through Administration.
- Restart after changing business settings, taxes or printers.

## Included

- Dine-in table orders and counter takeout parcels
- Menu items, sizes/variants, quantities, discounts, and held items
- Cash, externally collected UPI/card, split settlement, and customer dues
- Receipt previews, printing, bill history, item reports, and administration
- Optional physical KOT printer and live updates between staff screens

Customer ordering, remote tunnels, payment gateways, kitchen display screens, order special instructions, and service charges have been removed. Item sizes/variants remain because they determine the item's price. Bill notes remain for customer name, contact and invoice details.

## Credentials and local data

No API keys, database login credentials, merchant accounts, shared admin passwords or pre-generated signing keys are included. The application does not need an external account.

The administrator password comes only from the startup prompt or `HESTIA_ADMIN_PASSWORD`. Administration does not display it or write it into `config.py`. To change it, stop the server and enter a new password on the next launch, or change the environment variable.

A unique Flask session-signing key is generated on first startup in `data/.secret-key`. This is runtime data for that installation, not a bundled credential. Staff PINs are created by the operator and stored as hashes in the local database.

The SQLite database is `data/hestia.db`, entirely separate from the original application's database. Back up the `data` folder while the server is stopped. Restore that folder into the same location with the server stopped.

Optional environment variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `HESTIA_ADMIN_PASSWORD` | Supply the admin password without the startup prompt | No default |
| `HESTIA_PORT` | POS server port | `5010` |
| `HESTIA_DATA_DIR` | Location for the database and generated signing key | This installation's `data` folder |
| `HESTIA_DATABASE_URL` | Explicit database override | Local SQLite database |

PostgreSQL is optional and additionally needs `psycopg[binary]`. Supply any database credentials through your environment, not through distributed source files. The default server binds to `127.0.0.1`.

## Printing

Set `BILL_PRINTER_NAME` and, optionally, `KITCHEN_PRINTER_NAME` to your Windows printer names. KOT printing starts disabled. Receipts support a browser print preview and Windows thermal printing; the optional `bill-print-electron` helper retains the legacy silent print path. Physical printer output requires a connected printer and local verification.

The bill printer helper uses port `5002`; the main POS uses `5010`. UPI and card buttons record payments collected externally. No payment gateway or preconfigured merchant QR is included.

## Distributing a fresh copy

Include the source, frontend assets, `requirements.txt`, `start.py`, `start.bat` and this README. Exclude `data`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `artifacts`, logs, environment files and old build output. Each recipient chooses their own admin password and gets a new database and signing key on startup. These local and generated files are excluded by `.gitignore`.

## Troubleshooting

- **No admin password configured:** start with `start.bat` or `start.py`, or set `HESTIA_ADMIN_PASSWORD` in the environment used to launch the server.
- **Port already in use:** stop the other Hestia instance or choose a different `HESTIA_PORT`.
- **Dependencies missing:** rerun the `pip install -r requirements.txt` command above in this installation's virtual environment.
- **Cannot print:** check the Windows printer name and use browser printing to inspect the receipt. The launcher's startup errors are recorded in `launcher-error.log`.

## Development checks

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
npm install
npm test
node tools/smoke-browser.cjs
```

The browser smoke test uses installed Chrome, temporary credentials and a temporary database on port 5011. It captures screenshots in `artifacts` and intercepts physical printing. Automated tests generate their own passwords and signing keys outside the distribution's data folder.

The source was copied into this directory before edits. Installed dependencies, database snapshots, logs, old binaries and generated builds were excluded; obsolete feature files and restaurant-specific support material were removed from this edition. The parent application remains unchanged.
