const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const net = require('node:net')
const path = require('node:path')

let mainWindow = null
let backendProcess = null
let backendPort = null
let quitting = false

function availablePort(start = 5688, end = 5788) {
  return new Promise((resolve, reject) => {
    const tryPort = port => {
      if (port > end) return reject(new Error('没有可用的本地服务端口'))
      const server = net.createServer()
      server.unref()
      server.once('error', () => tryPort(port + 1))
      server.listen(port, '127.0.0.1', () => server.close(() => resolve(port)))
    }
    tryPort(start)
  })
}

function backendCommand() {
  if (app.isPackaged) {
    return {
      command: path.join(process.resourcesPath, 'backend', 'AILandscapeBackend.exe'),
      args: [],
      cwd: path.join(process.resourcesPath, 'backend'),
    }
  }
  const projectRoot = path.resolve(__dirname, '..')
  const configuredPython = process.env.AI_LANDSCAPE_PYTHON
  const localPython = path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
  const command = configuredPython || (fs.existsSync(localPython) ? localPython : 'python')
  return { command, args: [path.join(projectRoot, 'app.py')], cwd: projectRoot }
}

function waitForBackend(port, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const check = async () => {
      try {
        const response = await fetch(`http://127.0.0.1:${port}/api/health`)
        if (response.ok) return resolve()
      } catch {
        // The process can need several seconds to import OpenCV.
      }
      if (Date.now() >= deadline) return reject(new Error('后端服务启动超时'))
      setTimeout(check, 300)
    }
    check()
  })
}

async function startBackend() {
  backendPort = await availablePort()
  const target = backendCommand()
  const env = {
    ...process.env,
    AI_LANDSCAPE_PORT: String(backendPort),
    AI_LANDSCAPE_USER_DATA_DIR: app.getPath('userData'),
  }
  if (app.isPackaged) {
    env.AI_LANDSCAPE_FFMPEG = path.join(process.resourcesPath, 'ffmpeg', 'bin', 'ffmpeg.exe')
    env.AI_LANDSCAPE_FFPROBE = path.join(process.resourcesPath, 'ffmpeg', 'bin', 'ffprobe.exe')
  }
  backendProcess = spawn(target.command, target.args, {
    cwd: target.cwd,
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const logDirectory = path.join(app.getPath('userData'), 'logs')
  fs.mkdirSync(logDirectory, { recursive: true })
  const logStream = fs.createWriteStream(path.join(logDirectory, 'backend.log'), { flags: 'a' })
  backendProcess.stdout.pipe(logStream)
  backendProcess.stderr.pipe(logStream)
  backendProcess.once('exit', code => {
    backendProcess = null
    if (!quitting && mainWindow) {
      dialog.showErrorBox('AI Landscape', `后端服务已退出 (code ${code ?? 'unknown'})`)
    }
  })
  await waitForBackend(backendPort)
}

function stopBackend() {
  if (!backendProcess || backendProcess.killed) return
  if (process.platform === 'win32') {
    spawn('taskkill', ['/PID', String(backendProcess.pid), '/T', '/F'], { windowsHide: true })
  } else {
    backendProcess.kill('SIGTERM')
  }
  backendProcess = null
}

function registerIpc() {
  ipcMain.handle('dialog:videos', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile', 'multiSelections'],
      filters: [{ name: '视频', extensions: ['mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v'] }],
    })
    return result.canceled ? [] : result.filePaths
  })
  ipcMain.handle('dialog:subtitles', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile', 'multiSelections'],
      filters: [{ name: 'SRT 字幕', extensions: ['srt'] }],
    })
    return result.canceled ? [] : result.filePaths
  })
  ipcMain.handle('dialog:effects', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile', 'multiSelections'],
      filters: [{ name: '透明特效', extensions: ['gif', 'mov', 'webm'] }],
    })
    return result.canceled ? [] : result.filePaths
  })
  ipcMain.handle('dialog:output-directory', async () => {
    const result = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory', 'createDirectory'] })
    return result.canceled ? '' : result.filePaths[0]
  })
  ipcMain.handle('shell:show-item', (_event, value) => {
    if (typeof value === 'string' && value) shell.showItemInFolder(value)
  })
  ipcMain.handle('shell:open-path', (_event, value) => {
    if (typeof value === 'string' && value) return shell.openPath(value)
    return ''
  })
}

async function createWindow() {
  await startBackend()
  mainWindow = new BrowserWindow({
    width: 1460,
    height: 920,
    minWidth: 940,
    minHeight: 700,
    show: false,
    backgroundColor: '#eef0f2',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  const devUrl = process.env.AI_LANDSCAPE_DEV_SERVER_URL
  await mainWindow.loadURL(devUrl || `http://127.0.0.1:${backendPort}`)
  mainWindow.once('ready-to-show', () => mainWindow.show())
  mainWindow.on('closed', () => { mainWindow = null })
}

app.whenReady().then(async () => {
  registerIpc()
  try {
    await createWindow()
  } catch (error) {
    dialog.showErrorBox('AI Landscape 启动失败', error.stack || error.message)
    app.quit()
  }
})

app.on('activate', () => {
  if (!mainWindow) createWindow().catch(error => dialog.showErrorBox('启动失败', error.message))
})
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
app.on('before-quit', () => { quitting = true; stopBackend() })
