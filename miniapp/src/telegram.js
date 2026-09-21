// Gramma Mini-App — Telegram WebApp bootstrap + token plumbing.
//
// The Mini-App is entered via a WebApp button whose URL carries `?_token=`
// (an HMAC ticket minted by the backend). We also honour `tgWebAppData`
// (initData) from Telegram for display purposes, but we never trust it for
// auth — our own ticket is always required by the API.

export function initTelegram() {
  let tg = null
  if (typeof window !== 'undefined' && typeof window.Telegram !== 'undefined' && window.Telegram.WebApp) {
    tg = window.Telegram.WebApp
    try { tg.ready() } catch (_) { /* noop */ }
    try { tg.expand() } catch (_) { /* noop */ }
  }
  return tg
}
