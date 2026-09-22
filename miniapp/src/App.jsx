import { useEffect, useState } from 'react'
import { initTelegram, tgColorScheme } from './telegram.js'
import { bootstrapToken, clearToken } from './auth.js'
import { api, openWebSocket, resolveApiBase } from './api.js'
import Dashboard from './components/Dashboard.jsx'
import Pages from './components/Pages.jsx'
import Calendar from './components/Calendar.jsx'
import Collab from './components/Collab.jsx'
import Automation from './components/Automation.jsx'
import Settings from './components/Settings.jsx'

const TABS = [
  { id: 'dashboard', label: 'داشبورد', icon: '📊' },
  { id: 'pages', label: 'پیج‌ها', icon: '🔗' },
  { id: 'calendar', label: 'تقویم', icon: '📅' },
  { id: 'collab', label: 'کلبریشن', icon: '🤝' },
  { id: 'automation', label: 'اتوماسیون', icon: '🤖' },
]

export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [tg] = useState(() => initTelegram())
  const [theme, setTheme] = useState(() => {
    try {
      const saved = window.localStorage.getItem('gramma_theme')
      if (saved) return saved
    } catch (_) { /* noop */ }
    // follow Telegram's colorScheme when available
    return tgColorScheme() === 'dark' ? 'dark' : 'light'
  })
  const [me, setMe] = useState(null)
  const [status, setStatus] = useState('loading') // loading | ready | noauth

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try { window.localStorage.setItem('gramma_theme', theme) } catch (_) { /* noop */ }
  }, [theme])

  function toggleTheme() {
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'))
  }

  // 1) Bootstrap: resolve a session ticket (SSO from bot OR Telegram initData
  //    verification). Without valid auth we never call /api/me and show the
  //    «دسترسی ممکن نیست» screen with no data exposed.
  useEffect(() => {
    bootstrapToken(resolveApiBase())
      .then(() => api('/api/me'))
      .then((data) => {
        setMe(data)
        setStatus('ready')
      })
      .catch(() => setStatus('noauth'))
  }, [])

  // 2) Live refresh socket keeps dashboards in sync.
  useEffect(() => {
    if (status !== 'ready') return
    let ws = null
    try { ws = openWebSocket(() => {/* ping → lightweight refetch is enough */}) } catch (_) { /* noop */ }
    return () => { if (ws) ws.close() }
  }, [status])

  if (status === 'loading') {
    return <div className="app"><Loading /></div>
  }

  if (status === 'noauth') {
    return (
      <div className="app">
        <div className="card" style={{ textAlign: 'center', paddingTop: 60, paddingBottom: 60 }}>
          <div style={{ fontSize: 48 }}>🔐</div>
          <h3 style={{ marginTop: 10 }}>دسترسی ممکن نیست</h3>
          <p style={{ fontSize: 13, color: 'var(--muted)', lineHeight: 2 }}>
            این پنل مخصوص کاربران ربات Gramma است.<br />
            لطفاً از داخل ربات و دکمه «پنل مدیریت» وارد شوید.
          </p>
          <button className="btn" onClick={() => { clearToken(); window.location.reload() }}>
            تلاش دوباره
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <div style={{ fontWeight: 800, fontSize: 18 }}>گراما — پنل مدیریت</div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>
            {me ? me.name : ''} {me?.username ? '· @' + me.username : ''}
          </div>
        </div>
        <div style={{ fontSize: 26 }}>🌈</div>
      </header>

      <button
        onClick={toggleTheme}
        style={{
          position: 'absolute', top: 14, left: 14,
          width: 38, height: 38, borderRadius: 12,
          border: '1px solid var(--line)', background: 'var(--panel)',
          fontSize: 18, cursor: 'pointer',
        }}
        aria-label="تغییر حالت رنگی"
      >
        {theme === 'dark' ? '☀️' : '🌙'}
      </button>

      <div className="tabbar" style={{ display: tab === 'settings' ? 'none' : 'flex' }}>
        {TABS.map((t) => (
          <button key={t.id} className={'tab ' + (tab === t.id ? 'active' : '')} onClick={() => setTab(t.id)}>
            {t.icon} {t.label}
          </button>
        ))}
        <button className={'tab ' + (tab === 'settings' ? 'active' : '')} onClick={() => setTab('settings')}>
          ⚙️ تنظیمات
        </button>
      </div>

      {tab === 'dashboard' && <Dashboard />}
      {tab === 'pages' && <Pages />}
      {tab === 'calendar' && <Calendar />}
      {tab === 'collab' && <Collab />}
      {tab === 'automation' && <Automation />}
      {tab === 'settings' && <Settings />}

      <nav className="bottom-nav">
        {TABS.map((t) => (
          <button key={t.id} className={'nav-btn ' + (tab === t.id ? 'active' : '')} onClick={() => setTab(t.id)}>
            <span className="ic">{t.icon}</span>
            <span>{t.label}</span>
          </button>
        ))}
        <button className={'nav-btn ' + (tab === 'settings' ? 'active' : '')} onClick={() => setTab('settings')}>
          <span className="ic">⚙️</span>
          <span>تنظیمات</span>
        </button>
      </nav>
    </div>
  )
}

function Loading() {
  return (
    <>
      <div className="skeleton" style={{ height: 48, marginBottom: 14 }} />
      <div className="side-panel-holder">
        {[0, 1, 2, 3].map((i) => <div className="skeleton" key={i} style={{ height: 64 }} />)}
      </div>
      <div className="skeleton" style={{ height: 200, marginBottom: 14 }} />
      <div className="skeleton" style={{ height: 140 }} />
    </>
  )
}
