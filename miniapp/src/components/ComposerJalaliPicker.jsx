import { useMemo, useState } from 'react'
import moment from 'jalali-moment'

const WEEK = ['ش', 'ی', 'د', 'س', 'چ', 'پ', 'ج']
const MONTHS = ['فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
  'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند']
const pad = (n) => String(n).padStart(2, '0')

export function tehranChoice(offsetMinutes = 60) {
  const date = new Date(Date.now() + offsetMinutes * 60000)
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Tehran', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
  }).formatToParts(date)
  const value = (name) => Number(parts.find((p) => p.type === name)?.value || 0)
  const local = moment.from(`${value('year')}/${value('month')}/${value('day')}`, 'en', 'YYYY/M/D')
  return { year: local.jYear(), month: local.jMonth() + 1, day: local.jDate(),
    hour: value('hour'), minute: value('minute') }
}

function grid(year, month) {
  const first = moment.from(`${year}/${month}/1`, 'fa', 'jYYYY/jM/jD')
  const days = first.jDaysInMonth()
  const cells = [...Array((first.day() + 1) % 7).fill(null),
    ...Array.from({ length: days }, (_, index) => index + 1)]
  while (cells.length % 7) cells.push(null)
  return cells
}

function Wheel({ title, value, count, onChange }) {
  return <div className="compose-wheel">
    <span>{title}</span>
    <div className="compose-wheel-list" role="listbox" aria-label={title}>
      {Array.from({ length: count }, (_, v) => <button key={v} type="button"
        role="option" aria-selected={value === v} className={value === v ? 'selected' : ''}
        onClick={() => onChange(v)}>{pad(v)}</button>)}
    </div>
  </div>
}

export default function ComposerJalaliPicker({ value, onChange }) {
  const now = tehranChoice(0)
  const [view, setView] = useState(() => ({ year: value?.year || now.year,
    month: value?.month || now.month }))
  const cells = useMemo(() => grid(view.year, view.month), [view.year, view.month])
  const today = now.year * 10000 + now.month * 100 + now.day
  const selected = value.year * 10000 + value.month * 100 + value.day

  function move(delta) {
    let { year, month } = view
    month += delta
    if (month < 1) { year--; month = 12 }
    if (month > 12) { year++; month = 1 }
    if (year < now.year || year > now.year + 1) return
    setView({ year, month })
  }
  return <div className="compose-calendar" dir="rtl">
    <div className="compose-calendar-head">
      <button type="button" onClick={() => move(-1)} aria-label="ماه قبل">‹</button>
      <strong>{MONTHS[view.month - 1]} {view.year}</strong>
      <button type="button" onClick={() => move(1)} aria-label="ماه بعد">›</button>
    </div>
    <div className="compose-day-grid">
      {WEEK.map((name, index) => <span className="week" key={index}>{name}</span>)}
      {cells.map((day, index) => {
        if (day === null) return <span key={`blank-${index}`} />
        const key = view.year * 10000 + view.month * 100 + day
        return <button type="button" key={key} disabled={key < today}
          className={(key === selected ? 'selected ' : '') + (key === today ? 'today' : '')}
          onClick={() => onChange({ ...value, year: view.year, month: view.month, day })}>{day}</button>
      })}
    </div>
    <div className="compose-time-wheels">
      <Wheel title="ساعت تهران" value={value.hour} count={24}
        onChange={(hour) => onChange({ ...value, hour })} />
      <Wheel title="دقیقه" value={value.minute} count={60}
        onChange={(minute) => onChange({ ...value, minute })} />
    </div>
    <div className="compose-picked" dir="ltr">{value.year}/{pad(value.month)}/{pad(value.day)} · {pad(value.hour)}:{pad(value.minute)}</div>
    <p className="compose-hint">تقویم شمسی · ساعت تهران · با رقم لاتین. زمان باید حداقل 5 دقیقه در آینده باشد.</p>
  </div>
}
