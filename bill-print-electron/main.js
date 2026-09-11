const { app, BrowserWindow, ipcMain } = require('electron');
const http = require('http');
const path = require('path');
const fs = require('fs');

let mainWindow;
let printQueue = [];
let isPrinting = false;
const recentBillIds = new Set();

const PORT = 5002;

function getBillPrinterName() {
  // Try to find config.py in multiple locations
  // For portable .exe, place config.py in the same folder as the .exe
  const possiblePaths = [
    path.join(path.dirname(process.execPath), 'config.py'), // Same folder as .exe (production)
    path.join(process.resourcesPath, '..', 'config.py'),     // Extracted app root
    path.join(__dirname, '..', 'config.py'),                 // Parent of electron dir (dev)
    path.join(__dirname, 'config.py'),                       // Same dir (dev)
    path.join(process.cwd(), 'config.py'),                   // Current working dir
  ];

  for (const configPath of possiblePaths) {
    try {
      if (fs.existsSync(configPath)) {
        const content = fs.readFileSync(configPath, 'utf8');
        const match = content.match(/BILL_PRINTER_NAME\s*=\s*['"]([^'"\r\n]*)['"]/);
        if (match && match[1]) {
          console.log('Found BILL_PRINTER_NAME in:', configPath, '=', match[1]);
          return match[1];
        }
      }
    } catch (e) {
      // Continue to next path
    }
  }

  // Fallback to environment variable
  const envPrinter = process.env.BILL_PRINTER_NAME;
  if (envPrinter) return envPrinter;

  console.warn('BILL_PRINTER_NAME not found in config.py or environment');
  return '';
}

const BILL_PRINTER_NAME = getBillPrinterName();

// Also set environment variable so any code checking it gets the same value
if (BILL_PRINTER_NAME) {
  process.env.BILL_PRINTER_NAME = BILL_PRINTER_NAME;
  console.log('Set BILL_PRINTER_NAME env var to:', BILL_PRINTER_NAME);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 800,
    height: 600,
    show: false, // Hidden window for printing
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });

  // Keep window hidden but ready for printing
  mainWindow.loadURL('about:blank');
}

async function processPrintQueue() {
  if (isPrinting || printQueue.length === 0) return;
  isPrinting = true;

  const job = printQueue.shift();
  const { html, res } = job;
  // Always use the printer name from our own config.py — Electron app is the source of truth
  const targetPrinter = BILL_PRINTER_NAME;

  try {
    // Load HTML content
    await mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);

    // Inject QR library from local file and render QR codes
    try {
      const possibleQrPaths = [
        path.join(__dirname, 'qrcode.min.js'),                    // Bundled in exe
        path.join(path.dirname(process.execPath), 'qrcode.min.js'), // Same folder as .exe (production)
        path.join(process.resourcesPath, '..', 'qrcode.min.js'),  // Extracted app root
        path.join(__dirname, '..', 'frontend', 'qrcode.min.js'),  // Dev mode
        path.join(process.cwd(), 'frontend', 'qrcode.min.js'),
      ];
      let qrLibPath = null;
      for (const p of possibleQrPaths) {
        if (fs.existsSync(p)) { qrLibPath = p; break; }
      }
      if (qrLibPath) {
        console.log('Loading QR library from:', qrLibPath);
        const qrCode = fs.readFileSync(qrLibPath, 'utf8');
        await mainWindow.webContents.executeJavaScript(qrCode);
        await mainWindow.webContents.executeJavaScript(`
          (function() {
            var slots = document.querySelectorAll('.qr-svg[data-upi-url]');
            slots.forEach(function(slot) {
              try {
                var url = slot.getAttribute('data-upi-url') || '';
                slot.innerHTML = '';
                new QRCode(slot, {
                  text: url,
                  width: 102,
                  height: 102,
                  correctLevel: QRCode.CorrectLevel.M
                });
              } catch (e) {
                slot.textContent = 'QR unavailable';
              }
            });
          })();
        `);
        console.log('QR codes rendered');
      }
    } catch (qrErr) {
      console.warn('QR rendering failed:', qrErr.message);
    }

    // Wait for content and QR to fully render
    await new Promise((resolve) => {
      setTimeout(resolve, 1000);
    });

    const doPrint = (opts, attempt) => {
      console.log(`Print attempt ${attempt}, printer:`, opts.deviceName || '(default)');
      mainWindow.webContents.print(opts, (success, failureReason) => {
        if (success) {
          console.log('Print succeeded');
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: true }));
          isPrinting = false;
          setTimeout(processPrintQueue, 100);
          return;
        }
        console.error(`Print attempt ${attempt} failed:`, failureReason);

        // No retry ladder here on purpose. Re-submitting hands the same page to
        // the same thermal driver again, and each pass is another chance to
        // fault inside spoolsv.exe and take every printer on the machine down.
        // Falling back to the system default was worse still — bills came out
        // on whatever printer happened to be default. Callers rasterize and use
        // /api/print/bill-raw now; this endpoint is a legacy fallback only.
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: failureReason || 'Print failed after all retries' }));
        isPrinting = false;
        setTimeout(processPrintQueue, 100);
      });
    };

    // First attempt — 80mm width thermal paper, auto height
    doPrint({
      silent: true,
      printBackground: true,
      deviceName: targetPrinter || undefined,
      margins: { marginType: 'none' },
      pageSize: { width: 72000, height: 297000 },
    }, 1);

  } catch (err) {
    console.error('Print error:', err);
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: err.message }));
    isPrinting = false;
    setTimeout(processPrintQueue, 100);
  }
}

async function startServer() {
  const server = http.createServer(async (req, res) => {
    // CORS headers
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    if (req.method === 'POST' && req.url === '/print-bill') {
      let body = '';
      req.on('data', (chunk) => { body += chunk; });
      req.on('end', () => {
        try {
          const data = JSON.parse(body);
          const html = data.html;
          const printerName = data.printer_name;
          const billId = data.bill_id;

          if (!html) {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'html is required' }));
            return;
          }

          // Dedup: skip duplicate bill prints within 30 seconds
          if (billId != null && recentBillIds.has(String(billId))) {
            console.log('Dedup: skipping duplicate print for bill', billId);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ok: true, dedup: true }));
            return;
          }
          if (billId != null) {
            recentBillIds.add(String(billId));
            setTimeout(() => recentBillIds.delete(String(billId)), 30000);
          }

          printQueue.push({ html, printerName, res });
          processPrintQueue();
        } catch (err) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Invalid JSON: ' + err.message }));
        }
      });
      return;
    }

    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, printer: BILL_PRINTER_NAME || 'not set' }));
      return;
    }

    if (req.method === 'GET' && req.url === '/printers') {
      try {
        const printers = mainWindow.webContents.getPrintersAsync
          ? await mainWindow.webContents.getPrintersAsync()
          : mainWindow.webContents.getPrinters();
        const list = printers.map(p => ({
          name: p.name,
          description: p.description,
          status: p.status,
          isDefault: p.isDefault
        }));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ configured: BILL_PRINTER_NAME, printers: list }));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
      }
      return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
  });

  server.listen(PORT, '0.0.0.0', () => {
    console.log(`Bill print server running on http://0.0.0.0:${PORT}`);
  });

  server.on('error', (err) => {
    console.error('Server error:', err);
  });
}

app.whenReady().then(() => {
  createWindow();
  startServer();
});

app.on('window-all-closed', () => {
  // Keep running in background
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});
