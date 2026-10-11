import net from 'node:net'
import readline from 'node:readline'
import {randomUUID} from 'node:crypto'

// A separate inherited descriptor, not stdin/stdout. Pi owns the public RPC stream.
export class PrivateControl {
  constructor(fd) {
    this.socket = new net.Socket({fd, readable:true, writable:true})
    this.pending = new Map()
    this.handler = null
    this.configuration = new Promise(resolve => {this.configure = resolve})
    const lines = readline.createInterface({input:this.socket, terminal:false})
    lines.on('line', line => {
      let frame
      try {frame = JSON.parse(line)} catch {return}
      if (frame.type === 'configure') this.configure(frame.data)
      else if (frame.type === 'host_response') {
        const pending = this.pending.get(frame.id)
        if (!pending) return
        this.pending.delete(frame.id)
        pending.cleanup()
        frame.ok ? pending.resolve(frame.data) : pending.reject(new Error(frame.error || 'Host operation failed'))
      } else if (frame.type === 'control') {
        // Never await here: credential replies and UI responses must keep flowing.
        void this.dispatch(frame)
      }
    })
    this.socket.on('error', () => this.failPending())
    this.socket.on('end', () => {this.failPending(); process.kill(process.pid, 'SIGTERM')})
  }
  send(frame) {this.socket.write(JSON.stringify(frame) + '\n')}
  request(method, params, options = {}) {
    const id = randomUUID()
    return new Promise((resolve, reject) => {
      const cleanup = () => options.signal?.removeEventListener('abort', cancel)
      const cancel = () => {this.pending.delete(id); cleanup(); this.send({type:'host_cancel', id}); reject(new Error('Operation cancelled'))}
      this.pending.set(id, {resolve, reject, cleanup})
      if (options.signal?.aborted) {cancel(); return}
      options.signal?.addEventListener('abort', cancel, {once:true})
      this.send({type:'host_request', id, method, params})
    })
  }
  async dispatch(frame) {
    try {
      if (!this.handler) throw new Error('Agent is still starting')
      this.send({type:'control_response', id:frame.id, ok:true, data:await this.handler(frame.method, frame.params || {})})
    } catch (error) {
      this.send({type:'control_response', id:frame.id, ok:false, error:this.redact?.(error.message) || 'Agent control failed'})
    }
  }
  failPending() {
    for (const pending of this.pending.values()) {pending.cleanup(); pending.reject(new Error('Host connection closed'))}
    this.pending.clear()
  }
}
