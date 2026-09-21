// Gramma Mini-App — auth token management.
//
// Security note: never persist raw credentials. The `_token` injected in the
// URL is an HMAC ticket with an expiry; we keep it only in memory + a
// session-scoped storage, and send it as the `X-Mini-App-Hash` header.

const storageKey = '__gramma_token__'

export function pickTokenFromLocation() {
  try {
    const u = new URL(window.location.href)
    return u.searchParams.get('_token') || ''
  } catch (_) {
    return ''
  }
}

export function saveToken(token) {
  try { window.sessionStorage.setItem(storageKey, token) } catch (_) { /* noop */ }
}

export function loadToken() {
  try { return window.sessionStorage.getItem(storageKey) || '' } catch (_) { return '' }
}

export function clearToken() {
  try { window.sessionStorage.removeItem(storageKey) } catch (_) { /* noop */ }
}

export function getToken() {
  const fromUrl = pickTokenFromLocation()
  if (fromUrl) { saveToken(fromUrl); return fromUrl }
  return loadToken()
}
