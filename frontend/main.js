// frontend/main.js
const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path  = require('path');
const { spawn } = require('child_process');
const http  = require('http');
const fs    = require('fs');

let mainWindow;
let backendProcess;

// ── Find Python ───────────────────────────────────────────────────────────
function findPython() {
  const { execSync } = require('child_process');

  const bundled = [
    path.join(process.resourcesPath, 'python', 'python.exe'),
    path.join(process.resourcesPath, 'python', 'python'),
  ];
  for (const c of bundled) {
    try { if (fs.existsSync(c)) return c; } catch (_) {}
  }

  const candidates = process.platform === 'win32'
    ? ['python']
    : ['python3', 'python'];

  for (const c of candidates) {
    try {
      const out = execSync(`${c} --version`, { timeout: 3000 }).toString();
      if (out.toLowerCase().includes('python 3')) return c;
    } catch (_) {}
  }

  try {
    const out = execSync('py -3 --version', { timeout: 3000 }).toString();
    if (out.toLowerCase().includes('python 3')) return 'py -3';
  } catch (_) {}

  return 'python';
}

// ── Start FastAPI backend ─────────────────────────────────────────────────
function startBackend() {
  const python = findPython();

  const backendDir = app.isPackaged
    ? path.join(process.resourcesPath, 'backend')
    : path.join(__dirname, '..', 'backend');

  const script = path.join(backendDir, 'without_face_detection.py');

  const envFile = path.join(backendDir, '.env');
  const env = { ...process.env };
  env['PYTHONIOENCODING'] = 'utf-8';
  env['PYTHONUTF8'] = '1';

  if (fs.existsSync(envFile)) {
    // FIX: strip BOM from .env file read in JS too
    let raw = fs.readFileSync(envFile, 'utf8');
    if (raw.charCodeAt(0) === 0xFEFF) raw = raw.slice(1); // strip UTF-8 BOM
    const lines = raw.split('\n');
    for (const line of lines) {
      const trimmed = line.trim().replace(/\r/g, '');
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eqIdx = trimmed.indexOf('=');
      if (eqIdx === -1) continue;
      const k = trimmed.slice(0, eqIdx).trim();
      const v = trimmed.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, '');
      if (k) env[k] = v;
    }
  }

  console.log('Starting backend:', python, script);

  const pythonArgs = python.includes(' ')
    ? [...python.split(' '), '-u', script]
    : ['-u', script];
  const pythonCmd = python.includes(' ') ? python.split(' ')[0] : python;

  backendProcess = spawn(pythonCmd, pythonArgs, {
    cwd: backendDir,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  backendProcess.stdout.on('data', d => console.log('[backend]', d.toString().trim()));
  backendProcess.stderr.on('data', d => console.error('[backend-err]', d.toString().trim()));

  backendProcess.on('exit', code => {
    console.log(`Backend exited with code ${code}`);
    backendProcess = null;
  });
}

// ── Wait until backend is up (max 30s) ───────────────────────────────────
function waitForBackend(retries = 30) {
  return new Promise((resolve, reject) => {
    const attempt = (n) => {
      http.get('http://localhost:8000/health', res => {
        if (res.statusCode === 200) resolve();
        else setTimeout(() => attempt(n - 1), 1000);
      }).on('error', () => {
        if (n <= 0) reject(new Error('Backend never started'));
        else setTimeout(() => attempt(n - 1), 1000);
      });
    };
    attempt(retries);
  });
}

// ── Create the main window ────────────────────────────────────────────────
async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 720,
    minWidth: 700,
    minHeight: 500,
    title: 'Bengali Subtitles',
    backgroundColor: '#0f0f0f',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'index.html'));

  if (!app.isPackaged) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }
}

// ── App lifecycle ─────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  startBackend();

  // FIX: register IPC handlers BEFORE createWindow so preload bridge is ready
  registerIpcHandlers();

  let backendReady = false;
  try {
    await waitForBackend();
    backendReady = true;
    console.log('✅ Backend ready');
  } catch (e) {
    console.error('Backend failed to start:', e.message);
    // FIX: don't block window creation — renderer shows its own error state
    // Still show an error dialog, but don't prevent the window from opening
    dialog.showErrorBox(
      'Backend failed to start',
      'Could not start the subtitle engine.\n\n' +
      'Make sure:\n' +
      '  • Python 3 is installed (python --version)\n' +
      '  • All pip packages are installed (pip install -r requirements.txt)\n' +
      '  • GROQ_API_KEY is set in backend/.env (needed for online mode)\n' +
      '  • FFmpeg is installed and on your PATH\n\n' +
      'The app will open but processing will not work until the backend is running.'
    );
  }

  createWindow();
});

app.on('window-all-closed', () => {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

// ── IPC handlers ──────────────────────────────────────────────────────────
function registerIpcHandlers() {
  // Open native file picker — returns the chosen file path
  ipcMain.handle('open-file-dialog', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: 'Choose a video file',
      filters: [
        { name: 'Videos', extensions: ['mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', 'ts'] },
        { name: 'All Files', extensions: ['*'] },
      ],
      properties: ['openFile'],
    });
    return result.canceled ? null : result.filePaths[0];
  });

  /**
   * FIX: Read a local file as a Base64 string via IPC instead of
   * using fetch('file:///...') in the renderer, which is blocked by
   * Electron's CSP and protocol restrictions.
   *
   * renderer calls: window.electronAPI.readFileAsBase64(filePath)
   * returns: { base64: string, mimeType: string } | { error: string }
   */
  ipcMain.handle('read-file-as-base64', async (_event, filePath) => {
    try {
      const buffer = fs.readFileSync(filePath);
      const ext = path.extname(filePath).toLowerCase().replace('.', '');
      const mimeMap = {
        mp4: 'video/mp4', mkv: 'video/x-matroska', avi: 'video/x-msvideo',
        mov: 'video/quicktime', webm: 'video/webm', flv: 'video/x-flv',
        ts: 'video/mp2t',
      };
      const mimeType = mimeMap[ext] || 'application/octet-stream';
      return { base64: buffer.toString('base64'), mimeType };
    } catch (err) {
      return { error: err.message };
    }
  });
}