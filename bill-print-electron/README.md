# Bill Print Electron App

Silent bill printer agent for POS-Phone. Runs a local HTTP server on port 5002 that receives bill HTML and prints it silently to the configured bill printer.

## Setup

1. Install dependencies (one-time):
```
cd bill-print-electron
npm install
```

2. Set the bill printer name in `config.py` (same file the main app uses):
```python
BILL_PRINTER_NAME = "YOUR_PRINTER_NAME"
```

3. Start the app:
```
npm start
```

Or use the batch file:
```
start.bat
```

## How it works

1. POS-Phone user clicks "Print Bill"
2. Backend pushes `bill_print` SSE event
3. head-pos.js (on POS machine) catches the event
4. head-pos.js sends HTML to this Electron app at `http://127.0.0.1:5002/print-bill`
5. Electron app loads the HTML in a hidden window and prints silently
6. If Electron app is offline, head-pos falls back to browser print dialog

## Endpoints

- `POST /print-bill` - Receives `{ html, printer_name }` and prints silently
- `GET /health` - Returns status and configured printer name

## Build as .exe

To create a single portable `.exe` (no Node.js required):

```bash
npm run package
```

Or double-click `build.bat`.

The output will be in `dist/Hestia POS Bill Printer.exe` (or `dist/Hestia POS Bill Printer Setup.exe` for the installer).

## Auto-start with Windows

1. Build the portable executable: `npm run package`
2. Copy `config.py` into the same folder as the `.exe`
3. Add a shortcut to the `.exe` in your Windows Startup folder (`shell:startup`)

The app will read `BILL_PRINTER_NAME` from `config.py` automatically.
