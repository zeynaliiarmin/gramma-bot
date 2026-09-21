import { useEffect, useState } from 'react'
import { api } from '../api.js'

const STATUS = { pending: 'در انتظار', accepted: 'تایید شده', declined: 'رد شده', published: 'منتشر شده', canceled: 'لغو' }
const STATUS_BADGE = { pending: 'wait', accepted: 'ok', declined: 'err', published: 'ok', canceled: 'err' }

export default function Collab() {
  const [requests, setRequests] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = () =>
    api('/api/collabs')
      .then((d) => setRequests(d.requests))
      .catch((e) => setError(e.message))

  useEffect(() => { load() }, [])

  async function respond(id, action) {
    setBusy(true)
    try {
      await api(`/api/collabs/${id}/respond`, {
        method: 'POST',
        body: { action },
      })
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (error) return <div className="empty">⚠️ {error}</div>
  if (!requests) return <div className="skeleton" style={{ height: 140 }} />

  const pending = requests.filter((r) => r.status === 'pending')

  return (
    <>
      <div className="card">
        <h3>🤝 پست‌های کلبریشن</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)' }}>
          {pending.length} درخواست در انتظار — برای ایجاد کلبریشن، از داخل ربات اقدام کنید.
        </p>
      </div>

      {pending.map((r) => (
        <div className="card" key={r.id}>
          <div style={{ fontWeight: 700, marginBottom: 6 }}>درخواست کلبریشن #{r.id}</div>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 10 }}>
            پیام: «{r.message || '—'}»
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn" disabled={busy} onClick={() => respond(r.id, 'accept')}>
              ✅ تایید
            </button>
            <button className="btn danger" disabled={busy} onClick={() => respond(r.id, 'decline')}>
              ❌ رد
            </button>
          </div>
        </div>
      ))}

      {requests.length > 0 && (
        <div className="card">
          <h3>📋 تاریخچه</h3>
          {requests.map((r) => (
            <div className="list-item" key={r.id}>
              <div style={{ flex: 1, fontSize: 12 }}>
                #{r.id} — «{r.message || '—'}»
              </div>
              <span className={'badge ' + (STATUS_BADGE[r.status] || 'wait')}>
                {STATUS[r.status] || r.status}
              </span>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
