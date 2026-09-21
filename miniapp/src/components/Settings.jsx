import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Settings() {
  const [rules, setRules] = useState(null)
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [form, setForm] = useState({ keywords: '', reply: '' })

  async function load() {
    try {
      const d = await api('/api/auto-replies')
      setRules(d.rules)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [])

  async function addRule(e) {
    e.preventDefault()
    if (!form.keywords.trim() || !form.reply.trim()) return
    try {
      await api('/api/auto-replies', { method: 'POST', body: form })
      setForm({ keywords: '', reply: '' })
      setMsg('✅ قانون جدید اضافه شد')
      await load()
      setTimeout(() => setMsg(''), 2500)
    } catch (err) {
      setError(err.message)
    }
  }

  if (error) return <div className="empty">⚠️ {error}</div>

  return (
    <>
      <div className="card">
        <h3>🤖 پاسخ‌های خودکار (Auto-Reply)</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10 }}>
          وقتی پیامی حاوی کلمات کلیدی برسد، این پاسخ خودکار ارسال می‌شود.
        </p>
        {rules && rules.length === 0 && (
          <div className="empty">هنوز قانونی تعریف نشده است.</div>
        )}
        {rules && rules.map((r) => (
          <div className="list-item" key={r.id}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, fontWeight: 600 }}>🔑 {r.keywords}</div>
              <div style={{ fontSize: 12, color: 'var(--muted)' }}>{r.reply}</div>
            </div>
            <span className="badge ok">{r.matches} پاسخ</span>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>➕ قانون جدید</h3>
        <form onSubmit={addRule} style={{ display: 'grid', gap: 10 }}>
          <input
            className="input"
            placeholder="کلمات کلیدی (با کاما جدا کنید): قیمت, سفارش"
            value={form.keywords}
            onChange={(e) => setForm({ ...form, keywords: e.target.value })}
          />
          <textarea
            className="input"
            placeholder="متن پاسخ خودکار…"
            value={form.reply}
            onChange={(e) => setForm({ ...form, reply: e.target.value })}
          />
          <button className="btn" type="submit">ذخیره قانون</button>
        </form>
      </div>

      <div className="card">
        <h3>⚙️ سقف‌های پلتفرم</h3>
        <p style={{ fontSize: 12, color: 'var(--muted)' }}>
          • هر کاربر: حداکثر <b>۳ پیج</b><br />
          • کل ربات (حالت توسعه): <b>۵ پیج</b><br />
          • توکن‌ها: رمزنگاری AES-256 در دیتابیس<br />
          • پشتیبان‌گیری خودکار: هر شب
        </p>
      </div>

      {msg && <div className="toast">{msg}</div>}
    </>
  )
}
