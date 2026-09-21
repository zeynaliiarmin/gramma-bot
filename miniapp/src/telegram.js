// Gramma Mini-App — Telegram WebApp bootstrap.
//
// The Mini-App is entered via the bot's WebApp button whose URL carries a
// signed ticket (`?_token=`) minted by our backend; when absent, the SPA
// falls back to validating Telegram's signed initData server-side (never
// trusting initDataUnsafe). See auth.js for the full flow.

export function initTelegram() {
  let tg = null
  if (typeof window !== 'undefined' && typeof window.Telegram !== 'undefined' && window.Telegram.WebApp) {
    tg = window.Telegram.WebApp
    try { tg.ready() } catch (_) { /* noop */ }
    try { tg.expand() } catch (_) { /* noop */ }
  }
  return tg
}

export function tgColorScheme() {
  try {
    const tg = window.Telegram && window.Telegram.WebApp
    return (tg && tg.colorScheme) || 'light'
  } catch (_) {
    return 'light'
  }
}
