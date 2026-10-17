import { useEffect, useMemo, useState } from 'react'
import moment from 'jalali-moment'
import { api } from '../api.js'

const KIND_EMOJI = { post: '🖼', photo: '🖼', video: '🎬', reel: '🎬', carousel: '🌀', story: '📸' }
const WEEK_HEADER = ['ش', 'ی', 'د', 'س', 'چ', 'پ', 'ج']
const KIND_LABEL = { post: 'عکس / پست فید', photo: 'عکس', reel: 'ریلز با نمایش در فید',
  video: 'ویدیو', carousel: 'آلبوم / کاروسل', story: 'استوری' }
const latin = (text) => String(text).replace(/[۰-۹]/g, (digit) => '۰۱۲۳۴۵۶۷۸۹'.indexOf(digit))
  .replace(/[٠-٩]/g, (digit) => '٠١٢٣٤٥٦٧٨٩'.indexOf(digit))

function tehranToday() {
  const parts = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Tehran',
    year: 'numeric', month: 'numeric', day: 'numeric' }).formatToParts(new Date())
  const value = (name) => Number(parts.find((part) => part.type === name)?.value || 0)
  const date = moment.from(`${value('year')}/${value('month')}/${value('day')}`, 'en', 'YYYY/M/D')
  return { jy: date.jYear(), jm: date.jMonth() + 1,
    key: latin(date.format('jYYYY-jMM-jDD')) }
}

function buildMonthGrid(jy, jm) {
  const first = moment.from(`${jy}/${jm}/1`, 'fa', 'jYYYY/jM/jD')
  const offset = (first.day() + 1) % 7 // Saturday=0
  const daysInMonth = first.jDaysInMonth()
  const cells = []
  for (let i = 0; i < offset; i++) cells.push(null)
  for (let d = 1; d <= daysInMonth; d++) cells.push(d)
  while (cells.length % 7 !== 0) cells.push(null)
  return cells
}

function Row({ post, expanded, onClick }) {
  return (
    <button className="list-item" onClick={onClick}
      style={{ width: '100%', textAlign: 'right', cursor: 'pointer',
        border: expanded ? '2px solid var(--accent, #5b5be0)' : '1px solid var(--line)' }}>
      <span style={{ fontSize: 20 }}>{KIND_EMOJI[post.kind] || '•'}</span>
      <span style={{ flex: 1 }}>
        <span style={{ display: 'block', fontSize: 13, fontWeight: 600 }}>{post.caption || 'بدون کپشن'}</span>
        <span style={{ display: 'block', fontSize: 11, color: 'var(--muted)' }}>
          نوع: {KIND_LABEL[post.kind] || post.kind} · ساعت {post.at} · جزئیات و ویرایش ›
        </span>
      </span>
      <span className={'badge ' + (post.status === 'scheduled' ? 'ok' : 'wait')}>
        {post.status === 'scheduled' ? 'زمان‌بندی‌شده' : 'پیش‌نویس'}
      </span>
    </button>
  )
}

export default function Calendar() {
  const [days, setDays] = useState(null)
  const [error, setError] = useState('')
  const [view, setView] = useState(() => ({ jy: tehranToday().jy, jm: tehranToday().jm }))
  const [selected, setSelected] = useState(null)
  const [expandedKey, setExpandedKey] = useState('')
  const [caption, setCaption] = useState('')
  const [comment, setComment] = useState('')
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState('')

  const load = () => api('/api/calendar').then((data) => setDays(data.days))
    .catch((exc) => setError(exc.message))

  useEffect(() => { load() }, [])

  const postsByDate = useMemo(() => {
    const map = {}
    ;(days || []).forEach((day) => { map[day.date] = day })
    return map
  }, [days])
  const cells = useMemo(() => buildMonthGrid(view.jy, view.jm), [view])
  const monthLabel = latin(moment.from(`${view.jy}/${view.jm}/1`, 'fa', 'jYYYY/jM/jD').format('jMMMM jYYYY'))
  const selectedPosts = selected ? (postsByDate[selected] || { posts: [] }).posts : []
  const expanded = useMemo(() => {
    for (const day of days || []) {
      const post = day.posts.find((p) => `${p.source || 'legacy'}:${p.id}` === expandedKey)
      if (post) return post
    }
    return null
  }, [days, expandedKey])

  function nav(delta) {
    let { jy, jm } = view
    jm += delta
    if (jm < 1) { jm = 12; jy -= 1 }
    if (jm > 12) { jm = 1; jy += 1 }
    setView({ jy, jm })
    setSelected(null)
  }

  function cellKey(day) {
    if (!day) return null
    return `${view.jy}-${String(view.jm).padStart(2, '0')}-${String(day).padStart(2, '0')}`
  }

  function choosePost(post) {
    const key = `${post.source || 'legacy'}:${post.id}`
    setExpandedKey((old) => old === key ? '' : key)
    setCaption(post.visible_caption ?? post.caption ?? '')
    setComment(post.first_comment || '')
    setNotice('')
  }

  function openTimePicker(post) {
    const url = new URL(window.location.href)
    url.searchParams.set('schedule', post.legacy_id ? 'legacy' : 'queue')
    url.searchParams.set('item', String(post.queue_id || post.legacy_id))
    window.location.assign(url.toString())
  }

  async function saveCaption(post, kind, value) {
    setSaving(true)
    setNotice('')
    try {
      await api('/api/telegram-schedule/edit-caption', {
        method: 'POST', body: { target: post.legacy_id ? 'legacy' : 'queue',
          item: post.queue_id || post.legacy_id, kind, value },
      })
      await load()
      setNotice('✅ کپشن پست زمان‌بندی‌شده ذخیره شد؛ انتشار تازه‌ای انجام نشد.')
    } catch (exc) {
      setNotice('⚠️ ' + exc.message)
    } finally {
      setSaving(false)
    }
  }

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
          {WEEK_HEADER.map((weekday, i) => <div className="cal-head" key={i}>{weekday}</div>)}
          {cells.map((day, index) => {
            if (!day) return <div className="cal-cell empty" key={'e' + index} />
            const key = cellKey(day)
            const hasPosts = (postsByDate[key]?.posts.length || 0) > 0
            return (
              <button key={key} className={'cal-cell' + (hasPosts ? ' has' : '') +
                (key === tehranToday().key ? ' today' : '') +
                (key === selected ? ' sel' : '')}
                onClick={() => setSelected(selected === key ? null : key)}>
                <span>{day.toLocaleString('en-US')}</span>
                {hasPosts && <i className="dot" />}
              </button>
            )
          })}
        </div>
        <p style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
          تقویم شمسی (جلالی) — نقطه‌ها روزهای دارای پست زمان‌بندی‌شده‌اند.
        </p>
      </div>

      {selected ? (
        <div className="card">
          <h3>▫️ {selected}</h3>
          {selectedPosts.length === 0 && <div className="empty">برای این روز پستی زمان‌بندی نشده است.</div>}
          {selectedPosts.map((post) => <Row key={`${post.source || 'legacy'}:${post.id}`}
            post={post} expanded={expandedKey === `${post.source || 'legacy'}:${post.id}`}
            onClick={() => choosePost(post)} />)}
        </div>
      ) : (days || []).slice(0, 14).map((day) => (
        <div className="card" key={day.date}>
          <h3>▫️ {day.label}</h3>
          {day.posts.map((post) => <Row key={`${post.source || 'legacy'}:${post.id}`}
            post={post} expanded={expandedKey === `${post.source || 'legacy'}:${post.id}`}
            onClick={() => choosePost(post)} />)}
        </div>
      ))}

      {expanded && <div className="card">
        <h3>📋 جزئیات پست {expanded.id}</h3>
        <p>نوع رسانه: {KIND_LABEL[expanded.kind] || expanded.kind}</p>
        <p>زمان انتشار (تهران، شمسی): {expanded.when_jalali || expanded.at}</p>
        <p>کپشن روی پست: {expanded.moved ? (expanded.visible_caption || 'خالی') : (expanded.caption || '—')}</p>
        {expanded.moved && <p>کپشن اصلی در اولین کامنت: {expanded.first_comment || '—'}<br />
          <small>برای درج کامنت، اتصال Zernio به مجوز comments نیاز دارد.</small></p>}
        {(expanded.queue_id || expanded.legacy_id) ? <>
          <h4>✏️ ویرایش</h4>
          <button className="btn btn-sm" type="button" onClick={() => openTimePicker(expanded)}>
            ⏰ تغییر زمان (تقویم شمسی)
          </button>
          <div style={{ marginTop: 14 }}>
            <label style={{ display: 'block' }}>{expanded.moved ? 'کپشن کوتاه یا خالیِ روی پست' : 'کپشن پست'}</label>
            <textarea rows={3} maxLength={expanded.moved ? 150 : 2200}
              value={caption} onChange={(event) => setCaption(event.target.value)}
              style={{ width: '100%', marginBottom: 6 }} />
            <button className="btn btn-sm" type="button" disabled={saving}
              onClick={() => saveCaption(expanded, expanded.moved ? 'visible' : 'caption', caption)}>
              ذخیرهٔ کپشن پست
            </button>
          </div>
          {expanded.moved && <div style={{ marginTop: 14 }}>
            <label style={{ display: 'block' }}>کپشن اصلیِ اولین کامنت</label>
            <textarea rows={3} maxLength={2200} value={comment}
              onChange={(event) => setComment(event.target.value)}
              style={{ width: '100%', marginBottom: 6 }} />
            <button className="btn btn-sm" type="button" disabled={saving}
              onClick={() => saveCaption(expanded, 'comment', comment)}>
              ذخیرهٔ متن اولین کامنت
            </button>
          </div>}
          <p style={{ fontSize: 11, color: 'var(--muted)' }}>
            ویرایش فقط پیش از برداشته‌شدن پست از صف ممکن است.
          </p>
          {notice && <div role="status" className="banner">{notice}</div>}
        </> : <p style={{ fontSize: 11, color: 'var(--muted)' }}>
          این مورد در حال حاضر قابل ویرایش نیست؛ وضعیت انتشار را از تلگرام بررسی کنید.
        </p>}
      </div>}
    </>
  )
}
