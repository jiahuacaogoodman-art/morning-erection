// Transport adapter only: node-pty owns PTY creation, UTF-8, resize and terminal I/O.
import pty from 'node-pty';
import readline from 'node:readline';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';

// npm's macOS prebuild can arrive without the helper's executable bit.
if (process.platform === 'darwin') {
  const root = path.dirname(createRequire(import.meta.url).resolve('node-pty/package.json'));
  for (const relative of [`prebuilds/darwin-${process.arch}/spawn-helper`, 'build/Release/spawn-helper']) {
    const helper = path.join(root, relative);
    if (fs.existsSync(helper)) {
      const mode = fs.statSync(helper).mode;
      if (!(mode & 0o100)) fs.chmodSync(helper, mode | 0o111);
    }
  }
}

let terminal, ending = false, finished = false;
const send = value => {
  if (!process.stdout.write(JSON.stringify(value) + '\n')) terminal?.pause();
};
process.stdout.on('drain', () => terminal?.resume());
function finish(value) {
  if (finished) return;
  finished = true;
  process.stdout.write(JSON.stringify(value) + '\n', () => process.exit(0));
}
function stop() {
  if (ending || finished) return;
  ending = true;
  if (!terminal) return finish({type:'exit', exitCode:0});
  // Signal only this PTY's own process group, including its running command.
  try { process.kill(-terminal.pid, 'SIGHUP'); } catch { terminal.kill('SIGHUP'); }
  setTimeout(() => {
    try { process.kill(-terminal.pid, 'SIGKILL'); } catch { terminal.kill('SIGKILL'); }
  }, 400).unref();
}
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
process.stdout.on('error', stop);
const input = readline.createInterface({input:process.stdin, crlfDelay:Infinity});
input.on('close', stop);
input.on('line', line => {
  try {
    const message = JSON.parse(line);
    if (message.type === 'start' && !terminal) {
      terminal = pty.spawn(message.shell, message.args, {
        name:'xterm-256color', cols:message.cols, rows:message.rows,
        cwd:message.cwd, env:process.env,
      });
      send({type:'ready', pid:terminal.pid});
      terminal.onData(data => send({type:'data', data}));
      terminal.onExit(({exitCode,signal}) => finish({type:'exit', exitCode, signal}));
    } else if (message.type === 'input' && terminal && !ending) terminal.write(message.data);
    else if (message.type === 'resize' && terminal && !ending) terminal.resize(message.cols, message.rows);
    else if (message.type === 'stop') stop();
  } catch {
    // No command, environment or credential contents in diagnostics.
    if (!terminal) finish({type:'error', message:'PTY could not be started'});
    else stop();
  }
});
