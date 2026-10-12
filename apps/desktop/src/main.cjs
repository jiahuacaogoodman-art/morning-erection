const { app, BrowserWindow, shell } = require('electron')
const { spawn } = require('child_process')
const path = require('path')
const http = require('http')
const net = require('net')

// Find repo root (works both in dev and packaged)
const isDev = !app.isPackaged
const REPO_ROOT = isDev
  ? path.resolve(__dirname, '../../..')
  : path.join(process.resourcesPath, 'app')

const PYTHON = path.join(REPO_ROOT, '.venv', 'bin', 'python')
const PULSE_SCRIPT = path.join(REPO_ROOT, 'scripts', 'pulse.py')
const STATE_DIR = path.join(app.getPath('userData'), 'pulse-state')

let backendProcess = null
let backendPort = null
let mainWindow = null

async function getFreePort() {
  return new Promise((resolve) => {
    const srv = net.createServer()
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port
      srv.close(() => resolve(port))
    })
  })
}

function waitForBackend(port, maxMs = 30000) {
  const start = Date.now()
  return new Promise((resolve, reject) => {
    function probe() {
      http.get(`http://127.0.0.1:${port}/pulse/pi`, (res) => {
        res.resume()
        resolve()
      }).on('error', () => {
        if (Date.now() - start > maxMs) {
          reject(new Error('Backend did not start in time'))
          return
        }
        setTimeout(probe, 300)
      })
    }
    probe()
  })
}

// Check if a LaunchAgent instance of the backend is already running on the
// well-known port. If so, reuse it instead of starting a second process.
// This is important for Accessibility permissions: the LaunchAgent's Python
// executable is the one macOS has granted permissions to; a freshly-spawned
// bundle process runs as a different binary and gets "no-permissions".
const LAUNCHAGENT_PORT = 8788

function probePort(port) {
  return new Promise((resolve) => {
    http.get(`http://127.0.0.1:${port}/pulse/pi`, (res) => {
      res.resume()
      resolve(true)
    }).on('error', () => resolve(false))
  })
}

async function startBackend() {
  // Prefer the already-running LaunchAgent so we inherit its permissions.
  if (await probePort(LAUNCHAGENT_PORT)) {
    console.log(`Reusing LaunchAgent backend on port ${LAUNCHAGENT_PORT}`)
    backendPort = LAUNCHAGENT_PORT
    return LAUNCHAGENT_PORT
  }

  const port = await getFreePort()
  backendPort = port

  const env = {
    ...process.env,
    PATH: [
      path.join(app.getPath('home'), '.local', 'node', 'bin'),
      '/usr/local/bin',
      '/usr/bin',
      '/bin',
      '/opt/homebrew/bin',
      path.join(REPO_ROOT, '.venv', 'bin'),
      process.env.PATH || ''
    ].join(':'),
    HOME: app.getPath('home'),
    PULSE_ELECTRON: '1',
    // Ensure wakecore and wakecore-ui-runtime packages are importable even
    // when the bundled .venv's pyvenv.cfg paths don't match the bundle layout.
    PYTHONPATH: [
      path.join(REPO_ROOT, 'packages', 'wakecore', 'src'),
      path.join(REPO_ROOT, 'packages', 'wakecore-ui-runtime', 'src'),
      process.env.PYTHONPATH || ''
    ].filter(Boolean).join(':'),
  }

  backendProcess = spawn(PYTHON, [
    PULSE_SCRIPT,
    '--port', String(port),
    '--state', STATE_DIR,
  ], { env, stdio: ['ignore', 'pipe', 'pipe'] })

  backendProcess.stdout.on('data', d => process.stdout.write(d))
  backendProcess.stderr.on('data', d => process.stderr.write(d))
  backendProcess.on('exit', (code) => {
    console.log(`Backend exited with code ${code}`)
    if (!app.isQuitting) app.quit()
  })

  await waitForBackend(port)
  return port
}

function createWindow(port) {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 800,
    minHeight: 600,
    titleBarStyle: 'hiddenInset',
    vibrancy: 'sidebar',
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  // Open external links in the system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http')) shell.openExternal(url)
    return { action: 'deny' }
  })

  const frontendDist = isDev
    ? `http://127.0.0.1:5173`  // vite dev server
    : `http://127.0.0.1:${port}`  // Python serves the built frontend

  mainWindow.loadURL(frontendDist)
  mainWindow.on('closed', () => { mainWindow = null })
}

app.on('before-quit', () => { app.isQuitting = true })

app.whenReady().then(async () => {
  try {
    const port = await startBackend()
    createWindow(port)
  } catch (err) {
    console.error('Failed to start backend:', err)
    app.quit()
  }
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (mainWindow === null && backendPort) createWindow(backendPort)
})

app.on('quit', () => {
  if (backendProcess) {
    backendProcess.kill('SIGTERM')
    backendProcess = null
  }
})
