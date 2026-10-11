// Pi's CredentialStore contract; only persistence is delegated to the host's OS vault.
// These frames are private IPC, never conversation events or diagnostic logs.
export class VaultCredentialStore {
  constructor(initial, requestHost) {
    this.values = new Map(Object.entries(initial || {}))
    this.chains = new Map()
    this.requestHost = requestHost
  }
  async read(providerId, options) {
    options?.signal?.throwIfAborted()
    return structuredClone(this.values.get(providerId))
  }
  async list(options) {
    options?.signal?.throwIfAborted()
    return [...this.values].map(([providerId, value]) => ({providerId, type:value.type}))
  }
  enqueue(providerId, fn, options) {
    const previous = this.chains.get(providerId) || Promise.resolve()
    const task = previous.catch(() => {}).then(() => {options?.signal?.throwIfAborted(); return fn()})
    const tail = task.catch(() => {})
    this.chains.set(providerId, tail)
    void tail.then(() => {if (this.chains.get(providerId) === tail) this.chains.delete(providerId)})
    return task
  }
  modify(providerId, fn, options) {
    return this.enqueue(providerId, async () => {
      const previous = this.values.get(providerId)
      const next = await fn(structuredClone(previous))
      options?.signal?.throwIfAborted()
      if (next !== undefined) {
        await this.requestHost('credential_write', {providerId, credential:next}, {private:true})
        this.values.set(providerId, structuredClone(next))
      }
      return next ?? previous
    }, options)
  }
  delete(providerId, options) {
    return this.enqueue(providerId, async () => {
      await this.requestHost('credential_delete', {providerId}, {private:true})
      this.values.delete(providerId)
    }, options)
  }
  replace(snapshot) {
    if (this.chains.size) throw new Error('Credential operation is in progress')
    this.values = new Map(Object.entries(snapshot || {}))
  }
}

export function secretRedactor(credentials) {
  const values = new Set()
  const add = value => {
    if (value && typeof value === 'object') {
      for (const [key, item] of Object.entries(value)) {
        if (['key','apiKey','api_key','access','refresh','token'].includes(key) && typeof item === 'string' && item.length) values.add(item)
        else if (key === 'env' && item && typeof item === 'object') {for (const entry of Object.values(item)) if (typeof entry === 'string' && entry) values.add(entry)}
        else if (item && typeof item === 'object') add(item)
      }
    }
  }
  add(credentials)
  const text = value => {
    let result = String(value ?? '')
    for (const secret of [...values].sort((a,b) => b.length-a.length)) result = result.split(secret).join('[凭据已隐藏]')
    return result.replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[凭据已隐藏]').replace(/Bearer\s+[^\s"'<>]+/gi, 'Bearer [凭据已隐藏]')
  }
  const value = item => {
    if (typeof item === 'string') return text(item)
    if (Array.isArray(item)) return item.map(value)
    if (item instanceof Date) return item.toISOString()
    if (item && typeof item === 'object') return Object.fromEntries(Object.entries(item).map(([key, entry]) => [key, value(entry)]))
    return item
  }
  return {add, text, value}
}
