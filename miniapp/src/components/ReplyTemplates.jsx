import { useEffect, useState } from 'react'
import { api } from '../api.js'

const initial = { keyword: '', reply: '' }

/** Private, owner-scoped per-page keyword templates. This tab never sends a DM. */
export default function ReplyTemplates() {
  const [accounts, setAccounts] = useState([])
  const [accountId, setAccountId] = useState(null)
  const [items, setItems] = useState([])
  const [budget, setBudget] = useState(null)
  const [workerEnabled, setWorkerEnabled] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [form, setForm] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    api('/api/me').then((me) => {
      const pages = me.accounts || []
      setAccounts(pages)
      setAccountId(pages.length ? pages[0].id : null)
      setLoading(false)
    }).catch((err) => { setError(err.message); setLoading(false) })
  }, [])

  async function refresh(id) {
    if (!id) { setItems([]); setBudget(null); return }
    try {
      const [result, counts] = await Promise.all([
        api(`/api/reply-templates?account_id=${encodeURIComponent(id)}`),
        api(`/api/reply-templates/budget?account_id=${encodeURIComponent(id)}`),
      ])
      setItems(result.templates || [])
      setBudget(counts)
      setWorkerEnabled(!!result.worker_enabled)
    } catch (err) { setError(err.message) }
  }

  useEffect(() => { refresh(accountId) }, [accountId])

  function choosePage(event) {
    setAccountId(Number(event.target.value))
    setEditingId(null)
    setForm(initial)
    setNotice('')
    setError('')
  }

  async function save(event) {
    event.preventDefault()
    if (!accountId || !form.keyword.trim() || !form.reply.trim()) {
      setError('کلمهٔ کلیدی و متن پاسخ را وارد کنید.')
      return
    }
    setBusy(true); setError(''); setNotice('')
    try {
      const path = editingId ? `/api/reply-templates/${editingId}` : '/api/reply-templates'
      const body = editingId ? form : { ...form, account_id: accountId }
      await api(path, { method: editingId ? 'PATCH' : 'POST', body })
      setEditingId(null)
      setForm(initial)
      setNotice('قالب با موفقیت ذخیره شد؛ هیچ پیامی ارسال نشد.')
      await refresh(accountId)
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  function edit(item) {
    setEditingId(item.id)
    setForm({ keyword: item.keyword, reply: item.reply })
    setNotice('')
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  async function toggle(item) {
    setBusy(true); setError(''); setNotice('')
    try {
      await api(`/api/reply-templates/${item.id}`, { method: 'PATCH', body: { enabled: !item.enabled } })
      setNotice('وضعیت قالب ذخیره شد؛ هیچ پیامی ارسال نشد.')
      await refresh(accountId)
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  async function remove(item) {
    if (!window.confirm(`قالب «${item.keyword}» حذف شود؟`)) return
    setBusy(true); setError(''); setNotice('')
    try {
      await api(`/api/reply-templates/${item.id}`, { method: 'DELETE' })
      if (editingId === item.id) { setEditingId(null); setForm(initial) }
      setNotice('قالب حذف شد؛ هیچ پیامی ارسال نشد.')
      await refresh(accountId)
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  if (loading) return <div className="card skeleton" style={{ minHeight: 180 }} />

  return <>
    <div className="card">
      <h3>📝 قالب‌های پاسخ هر پیج</h3>
      <p style={{ lineHeight: 1.9, fontSize: 12, color: 'var(--muted)' }}>
        برای هر پیج، کلمهٔ کلیدی و پاسخ واقعی خودتان را ثبت کنید. هیچ پاسخ، قیمت، هزینه یا زمان ارسالی به‌صورت پیش‌فرض ساخته نشده است.
      </p>
      <div className={'banner ' + (workerEnabled ? 'ok' : 'warn')} style={{ lineHeight: 1.85 }}>
        {workerEnabled
          ? 'Worker جدید فعال است: ابتدا قالب‌های روشن، سپس قوانین قبلی و در صورت نیاز هوش مصنوعی بررسی می‌شوند.'
          : 'Worker ارسال جدید خاموش است. این صفحه فقط قالب‌ها را ذخیره می‌کند و پیام نمی‌فرستد؛ پاسخ‌گویی فعلی ChatbotX با سقف مشترک برقرار است. قالب‌های تازه پس از تأیید جداگانهٔ فعال‌سازی Worker در ارسال استفاده می‌شوند.'}
      </div>
      {budget && <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, margin: '10px 0', fontSize: 12 }}>
        <span className="badge ok">پاسخ خودکار امروز تهران: {budget.used_today} / {budget.daily_limit}</span>
        <span className="badge wait">ساعت شناور: {budget.used_hour} / {budget.hourly_limit}</span>
      </div>}
      {accounts.length === 0 ? <div className="empty">ابتدا یک پیج از داخل ربات متصل کنید.</div> : <>
        <label htmlFor="reply-template-page" style={{ fontSize: 12, fontWeight: 700 }}>پیج</label>
        <select id="reply-template-page" className="input" value={accountId || ''} onChange={choosePage} style={{ marginTop: 6 }}>
          {accounts.map((page) => <option value={page.id} key={page.id}>@{page.username}</option>)}
        </select>
      </>}
    </div>

    {error && <div className="banner" style={{ background: '#ffe7ec', color: '#d21f47' }} role="alert">⚠️ {error}</div>}
    {notice && <div className="banner ok" role="status">✓ {notice}</div>}

    {accountId && <div className="card">
      <h3>{editingId ? 'ویرایش قالب' : 'افزودن قالب'}</h3>
      <form onSubmit={save}>
        <label htmlFor="reply-keyword" style={{ fontSize: 12 }}>کلمه یا عبارت کلیدی</label>
        <input id="reply-keyword" className="input" maxLength={160} required value={form.keyword}
          onChange={(e) => setForm({ ...form, keyword: e.target.value })} placeholder="عبارت موردنظر شما" style={{ margin: '6px 0 12px' }} />
        <label htmlFor="reply-text" style={{ fontSize: 12 }}>متن پاسخ تأییدشدهٔ شما</label>
        <textarea id="reply-text" className="input" maxLength={2000} required value={form.reply}
          onChange={(e) => setForm({ ...form, reply: e.target.value })}
          placeholder="پاسخ دقیق و واقعی خودتان را بنویسید" style={{ margin: '6px 0 12px', minHeight: 110 }} />
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button type="submit" className="btn" disabled={busy}>{busy ? 'در حال ذخیره…' : editingId ? 'ذخیرهٔ تغییرات' : 'افزودن قالب'}</button>
          {editingId && <button type="button" className="btn ghost" onClick={() => { setEditingId(null); setForm(initial) }}>انصراف</button>}
        </div>
      </form>
    </div>}

    {accountId && <div className="card">
      <h3>قالب‌های این پیج ({items.length})</h3>
      {items.length === 0 ? <div className="empty">هنوز قالبی ثبت نکرده‌اید.</div> : items.map((item) => (
        <div key={item.id} className="list-item" style={{ alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <div style={{ flex: '1 1 170px', minWidth: 0 }}>
            <div style={{ fontWeight: 700, overflowWrap: 'anywhere' }}>{item.keyword} <span className={'badge ' + (item.enabled ? 'ok' : 'wait')}>{item.enabled ? 'روشن' : 'خاموش'}</span></div>
            <p style={{ fontSize: 12, lineHeight: 1.8, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: '5px 0' }}>{item.reply}</p>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
            <button className="btn btn-sm ghost" type="button" disabled={busy} onClick={() => edit(item)}>ویرایش</button>
            <button className="btn btn-sm ghost" type="button" disabled={busy} onClick={() => toggle(item)}>{item.enabled ? 'غیرفعال' : 'فعال'}</button>
            <button className="btn btn-sm danger" type="button" disabled={busy} onClick={() => remove(item)}>حذف</button>
          </div>
        </div>
      ))}
      <p style={{ fontSize: 11, lineHeight: 1.8, color: 'var(--muted)' }}>
        قوانین قدیمی همچنان در تب «اتوماسیون» هستند؛ قالب‌های جدید بدون ساخت پاسخ پیش‌فرض، بعد از تأیید راه‌اندازی Worker اولویت دارند.
      </p>
    </div>}
  </>
}
