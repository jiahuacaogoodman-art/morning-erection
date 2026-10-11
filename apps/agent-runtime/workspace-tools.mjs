import fs from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { spawn } from 'node:child_process'
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, createBashToolDefinition } from '@earendil-works/pi-coding-agent'

const protectedNames = new Set(['.secrets', '.ssh', '.aws', '.credentials', '.pulse', '.git'])
const inside = (root, candidate) => candidate === root || candidate.startsWith(root + path.sep)

export async function createWorkspaceTools(workspace) {
  const root = await fs.realpath(workspace)
  const taskTemp = await fs.mkdtemp(path.join(os.tmpdir(), 'morning-agent-'))
  const observed = new Set()
  const backups = []
  async function securePath(candidate) {
    const absolute = path.resolve(root, candidate)
    if (!inside(root, absolute)) throw new Error('Path is outside the selected workspace')
    const parts = path.relative(root, absolute).split(path.sep)
    if (parts.some(part => protectedNames.has(part) || part === '.env' || part.startsWith('.env.'))) {
      throw new Error('Credential and runtime state paths are protected')
    }
    let ancestor = absolute
    while (true) {
      try {
        const real = await fs.realpath(ancestor)
        if (!inside(root, real)) throw new Error('Symlink escapes the selected workspace')
        if (path.relative(root, real).split(path.sep).some(part => protectedNames.has(part) || part === '.env' || part.startsWith('.env.'))) {
          throw new Error('Symlink targets a protected credential or state path')
        }
        break
      } catch (error) {
        if (error.code !== 'ENOENT') throw error
        const parent = path.dirname(ancestor)
        if (parent === ancestor) throw error
        ancestor = parent
      }
    }
    return absolute
  }
  async function preserve(absolute) {
    try {
      const original = await fs.readFile(absolute)
      if (!observed.has(absolute)) throw new Error('Read the existing file before changing it')
      const backup = path.join(taskTemp, 'backup-' + backups.length)
      await fs.writeFile(backup, original, {mode: 0o600})
      backups.push({path: absolute, backup})
    } catch (error) {if (error.code !== 'ENOENT') throw error}
  }
  const read = createReadToolDefinition(root, {operations: {
    access: async candidate => {await fs.access(await securePath(candidate))},
    readFile: async candidate => {const absolute = await securePath(candidate); const data = await fs.readFile(absolute); observed.add(absolute); return data},
    detectImageMimeType: async () => null,
  }})
  const write = createWriteToolDefinition(root, {operations: {
    mkdir: async candidate => {await fs.mkdir(await securePath(candidate), {recursive: true})},
    writeFile: async (candidate, content) => {const absolute = await securePath(candidate); await preserve(absolute); await fs.writeFile(absolute, content)},
  }})
  const edit = createEditToolDefinition(root, {operations: {
    access: async candidate => {await fs.access(await securePath(candidate))},
    readFile: async candidate => {const absolute = await securePath(candidate); const data = await fs.readFile(absolute); observed.add(absolute); return data},
    writeFile: async (candidate, content) => {const absolute = await securePath(candidate); await preserve(absolute); await fs.writeFile(absolute, content)},
  }})
  const bash = createBashToolDefinition(root, {operations: {exec: async (command, cwd, {onData, signal, timeout}) => {
    if (process.platform !== 'darwin') throw new Error('Isolated bash executor is not configured on this platform')
    const readonly = [path.dirname(process.execPath), path.dirname(path.dirname(process.execPath)),
      path.join(os.homedir(), '.local/share/uv/python'), root, taskTemp, await fs.realpath(taskTemp)]
    const q = value => JSON.stringify(value)
    const pathPattern = suffix => `(regex (string-append "^" (regex-quote ${q(root)}) ${q(suffix)}))`
    const credentialPattern = pathPattern('/(.*/)?([.]secrets|[.]ssh|[.]aws|[.]credentials|[.]pulse|[.]env([.][^/]+)?)(/|$)')
    const gitPattern = pathPattern('/(.*/)?[.]git(/|$)')
    const policy = `(version 1) (deny default)\n(allow process* sysctl-read mach-lookup)\n` +
      `(allow file-read*)\n(deny file-read-data (subpath "/Users") (subpath "/Volumes") (subpath "/private/var/folders") (subpath "/private/tmp"))\n` +
      `(allow file-read-data ${readonly.map(p => `(require-all (subpath ${q(p)}) (require-not ${credentialPattern}))`).join(' ')})\n` +
      `(allow file-write* ${[root,taskTemp,await fs.realpath(taskTemp)].map(p => `(require-all (subpath ${q(p)}) (require-not ${credentialPattern}) (require-not ${gitPattern}))`).join(' ')} (literal "/dev/null"))\n` +
      `(allow file-read* file-write* (subpath "/dev/fd") (literal "/dev/tty"))\n` +
      `(deny file-read* file-write* ${credentialPattern})\n` +
      `(deny file-write* ${gitPattern})`
    await fs.writeFile(path.join(taskTemp, 'command.sb'), policy)
    const env = {PATH: [path.join(root, '.venv/bin'), path.dirname(process.execPath), '/opt/homebrew/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin'].join(':'),
      LANG: 'en_US.UTF-8', TMPDIR: taskTemp, PYTHONDONTWRITEBYTECODE: '1'}
    const child = spawn('/usr/bin/sandbox-exec', ['-p', policy, '/bin/bash', '-c', command], {cwd: root, env, detached: true, stdio: ['ignore','pipe','pipe']})
    child.stdout.on('data', onData); child.stderr.on('data', onData)
    let forceTimer
    const forceKill = () => {try {process.kill(-child.pid, 'SIGKILL')} catch {}}
    const kill = () => {try {process.kill(-child.pid, 'SIGTERM')} catch {}; forceTimer ??= setTimeout(forceKill, 1000)}
    const timer = setTimeout(kill, Math.min(timeout || 90, 300) * 1000)
    signal?.addEventListener('abort', kill, {once: true})
    try {
      return await new Promise((resolve, reject) => {child.once('error', reject); child.once('close', code => resolve({exitCode: code ?? 1}))})
    } finally {clearTimeout(timer); clearTimeout(forceTimer); forceKill(); signal?.removeEventListener('abort', kill)}
  }}})
  return {tools: [read, write, edit, bash], root, backups, taskTemp, securePath}
}
