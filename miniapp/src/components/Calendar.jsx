import { useEffect, useMemo, useState } from 'react'
import moment from 'jalali-moment'
import { api } from '../api.js'

const KIND_EMOJI = { photo: '🖼', video: '🎬', reel: '🛰', carousel: '🌀', story: '📸' }
const WEEK_HEADER = ['ش', 'ی', 'د', 'س', 'چ', 'پ', 'ج']

// Build a Jalali month grid (weeks start on Saturday) for a (jy, jm).
function buildMonthGrid(jy, jm) {
  const first = moment.from(`${jy}/${jm}/1`, 'fa', 'jYYYY/jM/jD')
  // Jalali weekday: Saturday=6 in moment's ISO numbering (Mon=1..Sun=7).
  // moment.day(): Sun=0..Sat=6. Saturday → we want index 0.
  const offset = (first.day() + 1) % 7
  const daysInMonth = first.jDaysInMonth()

  const cells = []
  for (let i = 0; i < offset; i++) cells.push(null)
  for (let d = 1; d <= daysInMonth; d++) cells.push(d)
  while (cells.length % 7 !== 0) cells.push(null)
  return cells
}

export default function Calendar() {
  const [days, setDays] = useState(null)      // posts grouped by Jalali date
  const [error, setError] = useState('')
  const now = moment()
  const [view, setView] = useState({ jy: now.jYear(), jm: now.jMonth() + 1 })
  const [selected, setSelected] = useState(null)  // 'jy-jm-jd'

  const load = () =>
    api('/api/calendar')
      .then((d) => setDays(d.days))
      .catch((e) => setError(e.message))

  useEffect(() => { load() }, [])

  const postsByDate = useMemo(() => {
    const map = {}
    ;(days || []).forEach((day) => {
      // backend label uses 'y-m-d' (e.g. 1405-06-30)
      map[day.date] = day
    })
    return map
  }, [days])

  const cells = useMemo(() => buildMonthGrid(view.jy, view.jm), [view])
  const monthLabel = moment.from(`${view.jy}/${view.jm}/1`, 'fa', 'jYYYY/jM/jD')
    .format('jMMMM jYYYY')

  function nav(delta) {
    let { jy, jm } = view
    jm += delta
    if (jm < 1) { jm = 12; jy -= 1 }
    if (jm > 12) { jm = 1; jy += 1 }
    setView({ jy, jm })
    setSelected(null)
  }

  function cellKey(d) {
    if (!d) return null
    return `${view.jy}-${String(view.jm).padStart(2, '0')}-${String(d).padStart(2, '0')}`
  }

  const selectedPosts = selected ? (postsByDate[selected] || { posts: [] }).posts : []

  if (error) return <div className="empty">⚠️ {error}</div>
  if (!days) return <div className="skeleton" style={{ height: 200 }} />

  return (
    <>
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <button className="icon-btn" onClick={() => nav(-1)}>‹</button>
          <h3 style={{ margin: 0 }}>{monthLabel}</h3>
          <button className="icon-btn" onClick={() => nav(1)}>›</button>
        </div>

        <div className="cal-grid">
          {WEEK_HEADER.map((w) => (
            <div className="cal-head" key={w}>{w}</div>
          ))}
          {cells.map((d, i) => {
            if (!d) return <div className="cal-cell empty" key={'e' + i} />
            const key = cellKey(d)
            const dayInfo = postsByDate[key]
            const hasPosts = dayInfo && dayInfo.posts.length > 0
            const isToday = key === moment().format('jYYYY-jMM-jDD')
            const isSelected = key === selected
            return (
              <button
                key={key}
                className={'cal-cell' + (hasPosts ? ' has' : '') + (isToday ? ' today' : '') + (isSelected ? ' sel' : '')}
                onClick={() => setSelected(isSelected ? null : key)}
              >
                <span>{d.toLocaleString('en-US')}</span>
                {hasPosts && <i className="dot" />}
              </button>
            )
          })}
        </div>
        <p style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
          تقویم شمسی (جلالی) — نقطه‌ها یعنی آن روز پست زمان‌بندی‌شده دارید.
        </p>
      </div>

      {selected ? (
        <div className="card">
          <h3>▫️ {selected}</h3>
          {selectedPosts.length === 0 && (
            <div className="empty">برای این روز پستی زمان‌بندی نشده است.</div>
          )}
          {selectedPosts.map((p) => (
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
      ) : (
        (days || []).slice(0, 14).map((day) => (
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
        ))
      )}
    </>
  )
}
