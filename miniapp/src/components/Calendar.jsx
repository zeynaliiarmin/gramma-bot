import { useEffect, useState } from 'react'
import { api } from '../api.js'

const KIND_EMOJI = { photo: '🖼', video: '🎬', reel: '🛰', carousel: '🌀', story: '📸' }

export default function Calendar() {
  const [days, setDays] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api('/api/calendar')
      .then((d) => setDays(d.days))
      .catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="empty">⚠️ {error}</div>
  if (!days) return <div className="skeleton" style={{ height: 160 }} />

  if (!days.length) {
    return (
      <div className="card">
        <h3>📅 تقویم محتوا</h3>
        <div className="empty">هنوز پستی زمان‌بندی نشده است.<br />از ربات، یک پست بسازید و زمان‌بندی کنید.</div>
      </div>
    )
  }

  return (
    <>
      <div className="card">
        <h3>📅 تقویم محتوا</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)' }}>
          {days.length} روز دارای پست زمان‌بندی‌شده (تقویم شمسی)
        </p>
      </div>
      {days.map((day) => (
        <div className="card" key={day.date}>
          <h3>▫️ {day.label}</h3>
          {day.posts.map((p) => (
            <div className="list-item" key={p.id}>
              <span style={{ fontSize: 20 }}>{KIND_EMOJI[p.kind] || '•'}</span>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{p.caption || 'بدون کپشن'}</div>
                <div style={{ fontSize: 11, color: 'var(--muted)' }}>
                  نوع: {p.kind} · ساعت {p.at}
                </div>
              </div>
              <span className={'badge ' + (p.status === 'scheduled' ? 'ok' : 'wait')}>
                {p.status === 'scheduled' ? 'زمان‌بندی‌شده' : 'پیش‌نویس'}
              </span>
            </div>
          ))}
        </div>
      ))}
    </>
  )
}
