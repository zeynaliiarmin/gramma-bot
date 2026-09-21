// Gramma Mini-App — tiny API client with auth header + graceful errors.

import { getToken } from './auth.js'

// The backend origin can be overridden at build time (VITE_API_BASE) or via
// localStorage; defaults to same-origin (backend serves the SPA in Docker).
export function resolveApiBase() {
  try {
    const saved = window.localStorage.getItem('gramma_api_base')
    if (saved) return saved
  } catch (_) { /* noop */ }
  return (typeof import.meta !== 'undefined' && import.meta.env?.VITE_API_BASE) || ''
}

export function setApiBase(url) {
  try { window.localStorage.setItem('gramma_api_base', url) } catch (_) { /* noop */ }
}

export async function api(path, options = {}) {
  const token = getToken()
  const base = resolveApiBase()
  const headers = {
    'X-Mini-App-Hash': token,
    ...(options.headers || {}),
  }
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(options.body)
  }
  const res = await fetch(base + path, { ...options, headers })
  if (res.status === 401) {
    throw new ApiError(401, 'نشست شما منقضی شده؛ لطفاً دوباره از ربات وارد شوید.')
  }
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch (_) { /* noop */ }
    throw new ApiError(res.status, detail)
  }
  return res.json()
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

export function openWebSocket(onMessage) {
  const base = resolveApiBase()
  let wsTarget = ''
  if (base) {
    wsTarget = base.replace(/^http/, 'ws').replace(/\/$/, '') + '/ws/broadcast'
  } else {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    wsTarget = `${proto}://${window.location.host}/ws/broadcast`
  }
  const ws = new WebSocket(wsTarget)
  ws.onmessage = (ev) => {
    try { onMessage(JSON.parse(ev.data)) } catch (_) { /* noop */ }
  }
  ws.onclose = () => { /* optionally retry */ }
  ws.onerror = () => ws.close()
  return ws
}
