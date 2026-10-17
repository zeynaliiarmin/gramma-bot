import { useEffect, useMemo, useState } from 'react'
import moment from 'jalali-moment'
import { api } from '../api.js'

const WEEK = ['ش', 'ی', 'د', 'س', 'چ', 'پ', 'ج']
const MONTHS = ['فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
  'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند']
const numbers = (amount) => Array.from({ length: amount }, (_, index) => index)
const latin = (number, pad = 0) => String(number).padStart(pad, '0')

function tehranParts() {
  const fields = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Tehran', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
  }).formatToParts(new Date())
  const value = (name) => Number(fields.find((part) => part.type === name)?.value || 0)
  const iranDay = moment.from(`${value('year')}/${value('month')}/${value('day')}`, 'en', 'YYYY/M/D')
  return {
    year: iranDay.jYear(), month: iranDay.jMonth() + 1, day: iranDay.jDate(),
    hour: value('hour'), minute: value('minute'),
  }
}

function monthGrid(year, month) {
  const first = moment.from(`${year}/${month}/1`, 'fa', 'jYYYY/jM/jD')
  const firstWeekday = (first.day() + 1) % 7 // Saturday=0
  const days = first.jDaysInMonth()
  const cells = [ ...Array(firstWeekday).fill(null), ...numbers(days).map((d) => d + 1) ]
  while (cells.length % 7 !== 0) cells.push(null)
  return cells
}

function fromLabel(label) {
  const match = /^(\d{4})\/(\d{2})\/(\d{2}) (\d{2}):(\d{2})$/.exec(label || '')
  return match ? {
    year: Number(match[1]), month: Number(match[2]), day: Number(match[3]),
    hour: Number(match[4]), minute: Number(match[5]),
  } : null
}

function Wheel({ label, selected, range, onChange }) {
  return (
    <div style={{ flex: 1, minWidth: 100 }}>
      <div style={{ fontSize: 13, marginBottom: 8, fontWeight: 700 }}>{label}</div>
      <div role="listbox" aria-label={label} style={{ height: 152, overflowY: 'auto',
        border: '1px solid var(--line)', borderRadius: 12, padding: 5,
        background: 'var(--panel)', scrollSnapType: 'y proximity' }}>
        {range.map((value) => (
          <button key={value} type="button" role="option" aria-selected={value === selected}
            onClick={() => onChange(value)} style={{ display: 'block', width: '100%',
              border: value === selected ? '2px solid var(--accent, #5b5be0)' : '1px solid transparent',
              background: value === selected ? 'var(--bg, #eef2ff)' : 'transparent',
              color: 'var(--text, inherit)', fontWeight: value === selected ? 800 : 400,
              borderRadius: 8, cursor: 'pointer', height: 36, scrollSnapAlign: 'center' }}>
            {latin(value, 2)}
          </button>
        ))}
      </div>
    </div>
  )
}

export default function TelegramSchedulePicker({ target, item }) {
  const [picked, setPicked] = useState(() => tehranParts())
  const [view, setView] = useState(() => {
    const now = tehranParts(); return { year: now.year, month: now.month }
  })
  const [meta, setMeta] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(null)

  useEffect(() => {
    api(`/api/telegram-schedule/current?target=${encodeURIComponent(target)}&item=${encodeURIComponent(item)}`)
      .then((data) => {
        setMeta(data)
        const initial = fromLabel(data.scheduled_jalali)
        if (initial) {
          setPicked(initial)
          setView({ year: initial.year, month: initial.month })
        }
      }).catch((exc) => setError(exc.message)).finally(() => setLoading(false))
  }, [target, item])

  const cells = useMemo(() => monthGrid(view.year, view.month), [view.year, view.month])
  const today = tehranParts()
  const todayKey = today.year * 10000 + today.month * 100 + today.day
  const chosenKey = picked.year * 10000 + picked.month * 100 + picked.day

  function navigate(delta) {
    let { year, month } = view
    month += delta
    if (month < 1) { year -= 1; month = 12 }
    if (month > 12) { year += 1; month = 1 }
    if (year < today.year || year > today.year + 1) return
    setView({ year, month })
  }

  async function save() {
    setError('')
    setSaved(null)
    setSaving(true)
    try {
      const result = await api('/api/telegram-schedule/choose', {
        method: 'POST', body: { target, item, year: picked.year, month: picked.month,
          day: picked.day, hour: picked.hour, minute: picked.minute },
      })
      setSaved(result)
    } catch (exc) {
      setError(exc.message)
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="card">در حال آماده‌سازی تقویم شمسی…</div>
  if (error && !meta) return <div className="card banner err">⚠️ {error}</div>

  return (
    <div style={{ paddingBottom: 30 }}>
      <div className="card">
        <h3>🗓 زمان‌بندی با تقویم شمسی</h3>
        <p style={{ fontSize: 13 }}>{meta?.kind} {meta?.caption ? `· ${meta.caption}` : ''}</p>
        <p style={{ fontSize: 12, color: 'var(--muted)' }}>منطقهٔ زمانی: تهران · تاریخ‌ها و ساعت‌ها با رقم لاتین</p>
        {meta?.scheduled_jalali && <p>زمان فعلی: {meta.scheduled_jalali}</p>}
      </div>
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <button type="button" className="icon-btn" onClick={() => navigate(-1)} aria-label="ماه قبل">‹</button>
          <h3 style={{ margin: 0 }}>{MONTHS[view.month - 1]} {latin(view.year)}</h3>
          <button type="button" className="icon-btn" onClick={() => navigate(1)} aria-label="ماه بعد">›</button>
        </div>
        <div className="cal-grid">
          {WEEK.map((name, index) => <div className="cal-head" key={index}>{name}</div>)}
          {cells.map((day, index) => {
            if (day === null) return <div className="cal-cell empty" key={`blank-${index}`} />
            const key = view.year * 10000 + view.month * 100 + day
            const past = key < todayKey
            return (
              <button type="button" key={key} disabled={past}
                className={'cal-cell' + (key === chosenKey ? ' sel' : '') +
                  (key === todayKey ? ' today' : '')}
                onClick={() => setPicked((prev) => ({ ...prev,
                  year: view.year, month: view.month, day }))}>
                {latin(day)}
              </button>
            )
          })}
        </div>
        <p style={{ fontSize: 11, color: 'var(--muted)' }}>تاریخ گذشته غیرقابل انتخاب است؛ زمان باید حداقل 5 دقیقه در آینده باشد.</p>
      </div>
      <div className="card">
        <h3>🕰 انتخاب ساعت و دقیقه</h3>
        <p style={{ fontSize: 12, color: 'var(--muted)' }}>در هر ستون اسکرول کنید و عدد را لمس کنید.</p>
        <div style={{ display: 'flex', gap: 12 }}>
          <Wheel label="ساعت" selected={picked.hour} range={numbers(24)}
            onChange={(hour) => setPicked((prev) => ({ ...prev, hour }))} />
          <Wheel label="دقیقه" selected={picked.minute} range={numbers(60)}
            onChange={(minute) => setPicked((prev) => ({ ...prev, minute }))} />
        </div>
        <p style={{ marginTop: 14 }}>
          زمان انتخابی: {latin(picked.year)}/{latin(picked.month, 2)}/{latin(picked.day, 2)}{' '}
          {latin(picked.hour, 2)}:{latin(picked.minute, 2)} (تهران)
        </p>
      </div>
      {error && <div className="card banner err" role="alert">⚠️ {error}</div>}
      {saved && <div className="card banner ok" role="status">
        ✅ {saved.message}<br />زمان ثبت‌شده: {saved.scheduled_jalali}<br />
        {target === 'draft' ? 'در پنل تلگرام، «بازخوانی زمان» و سپس «تأیید نهایی» را بزنید.' :
          'در تقویم تلگرام، «جزئیات و بازخوانی زمان» را بزنید.'}
      </div>}
      <div className="card" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button className="btn" type="button" disabled={saving} onClick={save}>
          {saving ? 'در حال ذخیره…' : '✅ ذخیرهٔ زمان (بدون انتشار)'}
        </button>
        {saved && <button className="btn" type="button"
          onClick={() => window.Telegram?.WebApp?.close?.()}>بازگشت به تلگرام</button>}
      </div>
    </div>
  )
}
