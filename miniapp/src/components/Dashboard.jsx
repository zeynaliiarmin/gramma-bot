import { useEffect, useState } from 'react'
import { api } from '../api.js'
import LineChart from './LineChart.jsx'

export default function Dashboard() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api('/api/dashboard')
      .then(setData)
      .catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="empty">⚠️ {error}</div>
  if (!data) return <DashboardSkeleton />

  const cards = data.cards || []
  const series = data.series || {}

  const totals = cards.reduce(
    (acc, c) => ({
      followers: acc.followers + (Number(c.followers) || 0),
      reach: acc.reach + (Number(c.reach_today) || 0),
      posts: acc.posts + (Number(c.media_count) || 0),
    }),
    { followers: 0, reach: 0, posts: 0 }
  )

  return (
    <>
      <div className="side-panel-holder">
        <div className="stat">
          <div className="num">{totals.followers.toLocaleString('fa-IR')}</div>
          <div className="lbl">👥 فالوور کل</div>
        </div>
        <div className="stat">
          <div className="num">{totals.reach.toLocaleString('fa-IR')}</div>
          <div className="lbl">📈 دسترسی امروز</div>
        </div>
        <div className="stat">
          <div className="num">{totals.posts.toLocaleString('fa-IR')}</div>
          <div className="lbl">🖼 پست‌ها</div>
        </div>
        <div className="stat">
          <div className="num">{cards.length}</div>
          <div className="lbl">🔗 پیج متصل</div>
        </div>
      </div>

      <LineChart
        title="📈 روند دسترسی (Reach) — ۷ روز اخیر"
        labels={series.labels || []}
        values={series.reach || []}
      />
      <LineChart
        title="👥 رشد دنبال‌کننده — ۷ روز اخیر"
        labels={series.labels || []}
        values={series.followers || []}
        color="rgba(13,201,189,0.9)"
      />

      <div className="card">
        <h3>🔗 پیج‌های شما</h3>
        {cards.map((c) => (
          <div className="list-item" key={c.account_id}>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 700 }}>@{c.username || '—'}</div>
              <div style={{ fontSize: 11, color: 'var(--muted)' }}>
                {c.name} · {Number(c.followers || 0).toLocaleString('fa-IR')} فالوور
              </div>
            </div>
            <span className={'badge ' + (c.status === 'connected' ? 'ok' : 'err')}>
              {c.status === 'connected' ? 'فعال' : c.status}
            </span>
          </div>
        ))}
      </div>
    </>
  )
}

function DashboardSkeleton() {
  return (
    <>
      <div className="side-panel-holder">
        {[0, 1, 2, 3].map((i) => (
          <div className="skeleton" style={{ height: 64 }} key={i} />
        ))}
      </div>
      {[0, 1].map((i) => (
        <div className="skeleton" style={{ height: 200, marginBottom: 14 }} key={i} />
      ))}
    </>
  )
}
