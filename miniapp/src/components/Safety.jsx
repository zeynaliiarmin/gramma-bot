import { useEffect, useState } from 'react'
import { api } from '../api.js'

function fmt(n) {
  return Number(n || 0).toLocaleString('en-US')
}

export default function Safety() {
  const [usage, setUsage] = useState(null)
  const [queue, setQueue] = useState(null)
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')

  async function load() {
    try {
      const [u, q] = await Promise.all([
        api('/api/reply-usage'),
        api('/api/reply-queue'),
      ])
      setUsage(u)
      setQueue(q)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [])

  async function resolveAlert(id) {
    try {
      await api('/api/alerts/resolve', { method: 'POST', body: { alert_id: id } })
      setMsg('✅ هشدار بسته شد')
      setTimeout(() => setMsg(''), 2500)
      await load()
    } catch (e) {
      setError(e.message)
    }
  }

  if (error) return <div className="empty">⚠️ {error}</div>
  if (!usage || !queue) return <div className="skeleton" style={{ height: 200 }} />

  const t = usage.totals || {}
  const alertTypeLabel = (k) => ({
    oauth_exception: '🔑 مشکل توکن (190)',
    rate_limit: '⏱ محدودیت نرخ (4/17)',
    temporarily_blocked: '🚫 مسدودی موقت (368)',
    suspicious_activity: '⚠️ فعالیت مشکوک (1200)',
    high_error_rate: '📈 نرخ خطای بالا',
  }[k] || `🚨 ${k}`)

  return (
    <>
      {msg && <div className="banner ok">{msg}</div>}

      <div className="card">
        <h3>🛡 محافظت ضد بلاک — مصرف امروز</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10 }}>
          سقف پاسخ کامنت {fmt(usage.daily_limit_per_account)} در روز و {fmt(usage.hourly_limit)} در ساعت برای هر پیج · تاریخ {usage.date}
        </p>
        <div className="side-panel-holder">
          <div className="stat">
            <div className="num">{fmt(t.replies_today)}</div>
            <div className="lbl">پاسخ امروز (همه پیج‌ها)</div>
          </div>
          <div className="stat">
            <div className="num">{fmt(t.queued)}</div>
            <div className="lbl">در صف اجرا</div>
          </div>
          <div className="stat">
            <div className="num">{fmt(t.alerts)}</div>
            <div className="lbl">هشدار باز</div>
          </div>
        </div>
      </div>

      <div className="card">
        <h3>📊 وضعیت هر پیج</h3>
        {usage.schedules.length === 0 && <div className="empty">پیجی متصل نیست.</div>}
        {usage.schedules.map((a) => {
          const pct = a.daily_limit ? Math.min(100, Math.round((a.replies_today / a.daily_limit) * 100)) : 0
          return (
            <div key={a.account_id} className="list-item" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <b>@{a.username || a.name}</b>
                <span style={{ fontSize: 11, color: 'var(--muted)' }}>
                  {fmt(a.replies_today)} / {fmt(a.daily_limit)} · پروفایل {a.rate_profile}
                </span>
              </div>
              <div className="progress">
                <div className="progress-fill" style={{ width: pct + '%' }} />
              </div>
              <div style={{ fontSize: 11, color: 'var(--muted)', display: 'flex', justifyContent: 'space-between' }}>
                <span>⏳ ساعت جاری: {fmt(a.hourly_count)}/{fmt(a.hourly_limit)}</span>
                <span>صف: {fmt(a.queued)}</span>
                <span>باقی‌مانده: {fmt(a.remaining)}</span>
              </div>
            </div>
          )
        })}
      </div>

      <div className="card">
        <h3>🚨 هشدارهای ایمنی</h3>
        {usage.alerts.length === 0 && <div className="empty">هشداری ثبت نشده است ✅</div>}
        {usage.alerts.map((al) => (
          <div key={al.id} className="list-item">
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 700 }}>{alertTypeLabel(al.alert_type)}</div>
              <div style={{ fontSize: 11, color: 'var(--muted)' }}>{al.message}</div>
            </div>
            <button className="btn btn-sm" onClick={() => resolveAlert(al.id)}>بستن</button>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>⏳ صف پاسخ کامنت (FIFO)</h3>
        {queue.items.length === 0 && <div className="empty">صف خالی است.</div>}
        {queue.items.slice(0, 30).map((q) => (
          <div key={q.id} className="list-item">
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13 }}>{q.comment_text || '(بدون متن)'}</div>
              <div style={{ fontSize: 11, color: 'var(--muted)' }}>
                #{q.id} · {q.has_dm_followup ? 'با فالوآپ دایرکت 💬' : 'فقط پاسخ کامنت'} · وضعیت {q.status}
              </div>
            </div>
          </div>
        ))}
      </div>
    </>
  )
}
