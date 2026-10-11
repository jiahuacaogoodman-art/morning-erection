export class ApiError extends Error {
  constructor(message: string, public status: number, public code: string) {super(message)}
}
export async function api<T>(path: string, body?: unknown, signal?: AbortSignal, key?: string): Promise<T> {
  const options: RequestInit = {method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin', cache: 'no-store', signal,
    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Pulse-Client': '1', 'Idempotency-Key': key || crypto.randomUUID()},
    body: body === undefined ? undefined : JSON.stringify(body)}
  let response = await fetch('/api' + path, options)
  if (response.status === 401 && path !== '/bootstrap') {
    const connected = await fetch('/api/bootstrap', {credentials: 'same-origin', cache: 'no-store', signal})
    if (connected.ok) response = await fetch('/api' + path, options)
  }
  const value = await response.json()
  if (!response.ok) throw new ApiError(value.error?.message || '连接暂时不可用', response.status, value.error?.code || 'CONNECTION')
  return value as T
}
