// Gramma Mini-App — authentication (Telegram initData → HMAC ticket).
//
// Security model (never trust initDataUnsafe):
//   1. If the opening URL carries `?_token=` (an HMAC ticket minted by OUR
//      backend), keep it in sessionStorage and use it immediately.
//   2. Otherwise, take `window.Telegram.WebApp.initData` — the Telegram-
//      signed data — and POST it to `/api/auth/verify`, which validates the
//      HMAC-SHA256 hash against the bot token. Only then does the backend
//      mint a session ticket. `user.id` is NEVER trusted on its own.
//   3. If neither exists (direct browser visit, wrong bot, or forged data),
//      bootstrap fails and the app shows «دسترسی ممکن نیست» with no data.
//
// The session key lives in sessionStorage (dies with the tab) — never in
// localStorage and never a cookie. It is sent as the `X-Mini-App-Hash`
// header on every API request.

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
  // URL ticket first, then the session-scoped one.
  const fromUrl = pickTokenFromLocation()
  if (fromUrl) { saveToken(fromUrl); return fromUrl }
  return loadToken()
}

export function getInitData() {
  // The real Telegram WebApp bridge (injected by telegram-web-app.js).
  if (typeof window !== 'undefined' && window.Telegram && window.Telegram.WebApp) {
    return window.Telegram.WebApp.initData || ''
  }
  return ''
}

export function getUnsafeUser() {
  // DISPLAY-ONLY convenience. NEVER used for auth decisions.
  try {
    const tg = window.Telegram && window.Telegram.WebApp
    return tg && tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user : null
  } catch (_) {
    return null
  }
}

/**
 * Resolve a valid session ticket for this Mini-App session.
 * Returns a ticket string; throws Error('NO_AUTH') when the visit is not a
 * legitimate Telegram WebApp launch from our bot.
 */
export async function bootstrapToken(apiBase) {
  // 1) URL ticket from the WebApp button (fast path).
  const urlToken = pickTokenFromLocation()
  if (urlToken) { saveToken(urlToken); return urlToken }

  // 2) Telegram initData → backend verifies HMAC-SHA256, mints a ticket.
  const initData = getInitData()
  if (initData) {
    let res
    try {
      res = await fetch(apiBase + '/api/auth/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData }),
      })
    } catch (_) {
      throw new Error('NO_AUTH')
    }
    if (res.ok) {
      const data = await res.json().catch(() => ({}))
      if (data && data.ticket) { saveToken(data.ticket); return data.ticket }
    }
  }

  // 3) Direct visit / other bot / forged → refuse.
  clearToken()
  throw new Error('NO_AUTH')
}
