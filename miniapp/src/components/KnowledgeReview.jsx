import { useEffect, useState } from 'react'
import { api } from '../api.js'

const channelNames = { dm: 'دایرکت', comment: 'کامنت', story_reply: 'ریپلای استوری', story_mention: 'منشن استوری' }
const reasons = {
  reply_window_expired: 'مهلت 24 ساعته دایرکت/استوری تمام شده؛ ارسال خودکار ممنوع است.',
  comment_window_expired: 'مهلت 7 روزهٔ پاسخ خصوصی به کامنت تمام شده است.',
  live_comment_unverified: 'فعال‌بودن پخش زنده قابل اثبات نیست؛ پاسخ خودکار داده نشد.',
  inbound_time_unverified: 'زمان اصلی پیام/کامنت از ارائه‌دهنده قابل تأیید نبود.',
  comment_context_unverified: 'شناسهٔ کامنت برای پاسخ خصوصی معتبر در دسترس نیست.',
  llm_retry_exhausted: 'تلاشِ یک‌بارهٔ موعد Hold نیز بی‌پاسخ ماند.',
  hold_retry_interrupted: 'تلاش موعد Hold ناتمام ماند؛ برای جلوگیری از تکرار نیاز به بررسی دارد.',
  hold_retry_exhausted: 'تعداد تلاش‌های Hold تمام شده است.',
  superseded_by_new_inbound: 'پیام تازه‌تری در این گفتگو آمده؛ پاسخ قدیمی خودکار فرستاده نشد.',
  owner_fact_required: 'پاسخ درباره اطلاعات کسب‌وکار به تأیید شما نیاز دارد.',
  personal_answer_required: 'درخواست شخصی است؛ پاسخ مشتری دیگری بازاستفاده نمی‌شود.',
  llm_needs_human: 'پاسخ مطمئن بر اساس دانش تأییدشده پیدا نشد.',
  automation_managed: 'برای این پیام یک اتوماسیون تعریف شده؛ LLM کنار گذاشته شد.',
  llm_unavailable: 'مدل رایگان معتبر پاسخ نداده؛ تا نوبت فردا نگه داشته شده است.',
}
const displayReason = (code) => reasons[code] || 'برای بررسی دستی نگه داشته شده است.'

/** All writes are knowledge decisions, never Instagram/Telegram sends. */
export default function KnowledgeReview() {
  const [accounts, setAccounts] = useState([])
  const [accountId, setAccountId] = useState(null)
  const [data, setData] = useState(null)
  const [busyId, setBusyId] = useState('')
  const [factById, setFactById] = useState({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api('/api/me').then((me) => {
      const pages = (me.accounts || []).filter((page) => page.username?.toLowerCase() === 'zeynalikids')
      setAccounts(pages)
      setAccountId(pages[0]?.id || null)
    }).catch((err) => setError(err.message)).finally(() => setLoading(false))
  }, [])

  async function refresh(id) {
    if (!id) { setData(null); return }
    try { setData(await api(`/api/knowledge/review?account_id=${encodeURIComponent(id)}`)) }
    catch (err) { setError(err.message) }
  }
  useEffect(() => { refresh(accountId) }, [accountId])

  async function decide(item, decision) {
    if (decision === 'reject' && !window.confirm('این نمونه از صف حذف شود؟')) return
    const fact = !!factById[item.id]
    if (fact && decision === 'approve' && !window.confirm(
      'شما تأیید می‌کنید اطلاعات تجاری این پاسخ اکنون دقیق است؟ اعتبار آن برای استناد 7 روز خواهد بود.'
    )) return
    setBusyId(`candidate-${item.id}`); setError(''); setNotice('')
    try {
      const result = await api(`/api/knowledge/candidates/${item.id}/decision`, {
        method: 'POST', body: { account_id: accountId, decision, fact_approved: fact && decision === 'approve' },
      })
      setNotice(result.message || 'تصمیم ثبت شد؛ هیچ پیامی ارسال نشد.')
      await refresh(accountId)
    } catch (err) { setError(err.message); await refresh(accountId) }
    finally { setBusyId('') }
  }

  async function removeExample(item) {
    if (!window.confirm('این نمونهٔ تأییدشده و اثر آن بر لحن حذف شود؟')) return
    setBusyId(`approved-${item.id}`); setError(''); setNotice('')
    try {
      const result = await api(`/api/knowledge/examples/${item.id}?account_id=${encodeURIComponent(accountId)}`, {
        method: 'DELETE',
      })
      setNotice(result.message); await refresh(accountId)
    } catch (err) { setError(err.message) }
    finally { setBusyId('') }
  }

  async function acknowledge(item) {
    setBusyId(`review-${item.id}`); setError(''); setNotice('')
    try {
      const result = await api(`/api/knowledge/manual-review/${item.id}/ack`, {
        method: 'POST', body: { account_id: accountId },
      })
      setNotice(result.message); await refresh(accountId)
    } catch (err) { setError(err.message) }
    finally { setBusyId('') }
  }

  if (loading) return <div className="card skeleton" style={{ minHeight: 180 }} />

  return <>
    <div className="card">
      <h3>🧠 صف تأیید پاسخ‌ها و یادگیری لحن</h3>
      <p style={{ fontSize: 12, lineHeight: 1.95, color: 'var(--muted)' }}>
        هر شب پاسخ‌های خروجیِ قابل‌دسترسی پیج از Zernio خوانده می‌شوند؛ خروجی‌های ChatbotX نیز در صورت داشتن رویداد معتبر وارد صف می‌شوند.
        نبودِ لاگ ربات به معنی «دستی بودن» نیست. فقط نمونه‌ای که شما تأیید کنید وارد دانش و تحلیل لحن می‌شود.
        تأیید و رد هیچ پیام جدیدی به اینستاگرام نمی‌فرستد.
      </p>
      {accounts.length ? <>
        <label htmlFor="knowledge-account" style={{ fontWeight: 700, fontSize: 12 }}>پیج</label>
        <select id="knowledge-account" className="input" value={accountId || ''} onChange={(event) => {
          setAccountId(Number(event.target.value)); setFactById({}); setError(''); setNotice('')
        }}>
          {accounts.map((page) => <option key={page.id} value={page.id}>@{page.username}</option>)}
        </select>
      </> : <div className="empty">پیج @zeynalikids باید به حساب شما متصل باشد.</div>}
      {data && <p style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.9 }}>
        همگام‌سازی شبانه: {data.sync?.status === 'ok' ? 'موفق' : data.sync?.status === 'not_started' ? 'هنوز شروع نشده' : 'نیازمند بررسی'}
        {data.sync?.last_jalali ? ` · ${data.sync.last_jalali} تهران` : ''}
        {data.sync?.error_code ? ` · علت: ${data.sync.error_code}` : ''}
      </p>}
    </div>

    {error && <div className="banner" style={{ background: '#ffe7ec', color: '#d21f47' }} role="alert">⚠️ {error}</div>}
    {notice && <div className="banner ok" role="status">✓ {notice}</div>}

    {accountId && data && <>
      <div className="card">
        <h3>📝 صف تأیید پاسخ‌ها ({data.pending_total || 0})</h3>
        <p style={{ fontSize: 12, color: 'var(--muted)' }}>
          پاسخِ بدون برچسب انسانی هم می‌تواند از بات دیگری باشد. متن پرسش و پاسخ را قبل از تأیید بخوانید.
          فقط پاسخ‌هایی که واقعاً خودتان نوشته‌اید را تأیید کنید.
        </p>
        {!data.candidates?.length ? <div className="empty">فعلاً نمونه‌ای در انتظار تأیید نیست. جمع‌آوری فقط بعد از دریافت دادهٔ واقعی انجام می‌شود.</div>
          : data.candidates.map((item) => <div key={item.id} className="list-item" style={{ display: 'block' }}>
            <div style={{ fontSize: 12, color: 'var(--muted)' }}>
              {channelNames[item.channel] || item.channel} · {item.created_jalali} · {item.origin === 'human' ? 'منشأ انسانیِ ثبت‌شده' : 'منشأ نامشخص؛ بررسی کنید'}
            </div>
            <p style={{ fontSize: 13, lineHeight: 1.8, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}><strong>پیام مخاطب:</strong> {item.question}</p>
            <p style={{ fontSize: 13, lineHeight: 1.8, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}><strong>پاسخ خروجی:</strong> {item.answer}</p>
            <label style={{ display: 'block', fontSize: 12, marginBottom: 12, lineHeight: 1.8 }}>
              <input type="checkbox" checked={!!factById[item.id]}
                onChange={(event) => setFactById({ ...factById, [item.id]: event.target.checked })} />
              {' '}اطلاعات تجاری این پاسخ را برای استنادِ حداکثر 7 روز تأیید می‌کنم
            </label>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button className="btn btn-sm" disabled={!!busyId} onClick={() => decide(item, 'approve')}>
                {busyId === `candidate-${item.id}` ? 'در حال ثبت…' : 'تأیید و یادگیری لحن'}
              </button>
              <button className="btn btn-sm danger" disabled={!!busyId} onClick={() => decide(item, 'reject')}>رد و حذف متن</button>
            </div>
          </div>)}
      </div>

      <div className="card">
        <h3>🎨 راهنمای لحن این پیج</h3>
        {data.style?.tone ? <div style={{ fontSize: 13, lineHeight: 2 }}>
          <div>لحن: <strong>{data.style.tone}</strong> · طول جمله: <strong>{data.style.sentence_length || '—'}</strong></div>
          <div>کلمات رایج: {(data.style.style_words || []).join('، ') || '—'}</div>
          <div>ایموجی‌ها: {(data.style.emojis || []).join(' ') || '—'}</div>
        </div> : <div className="empty">هنوز لحن تأییدشده‌ای استخراج نشده است.</div>}
        <p style={{ fontSize: 12, color: 'var(--muted)' }}>
          نمونه‌های سبک: {data.style_samples || 0} · تحلیل‌های منتظر مدل رایگان: {data.style_pending || 0}
        </p>
        <h4 style={{ marginBottom: 6 }}>دانش تأییدشدهٔ اخیر</h4>
        {!data.approved?.length ? <div className="empty">هنوز نمونهٔ تأییدشده‌ای ندارید.</div>
          : data.approved.map((item) => <div key={item.id} className="list-item" style={{ display: 'block' }}>
            <div style={{ fontSize: 12, color: 'var(--muted)' }}>
              {channelNames[item.channel] || item.channel} · تحلیل لحن: {item.style_status === 'ready' ? 'انجام شد' : 'در انتظار'}
              {item.fact_expires_jalali ? ` · اعتبار اطلاعات تجاری تا ${item.fact_expires_jalali}` : ''}
            </div>
            <p style={{ fontSize: 12, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{item.question} ← {item.answer}</p>
            <button className="btn btn-sm danger" disabled={!!busyId} onClick={() => removeExample(item)}>حذف از دانش</button>
          </div>)}
      </div>

      <div className="card">
        <h3>⏳ Hold و نیاز به بررسی دستی</h3>
        <p style={{ fontSize: 12, lineHeight: 1.9, color: 'var(--muted)' }}>
          کمبود مدل رایگان پیام را تا ساعت 13:00 روز بعد تهران نگه می‌دارد. پیام‌های تازهٔ امروز زودتر از Hold بررسی می‌شوند؛
          Hold تنها با سهمیهٔ باقیمانده یک بار تلاش می‌کند. مهلت دایرکت/استوری 24 ساعت و پاسخ خصوصی کامنت 7 روز است؛
          پس از انقضا فقط شما تصمیم می‌گیرید. دکمهٔ «ثبت بررسی» هیچ پیامی ارسال نمی‌کند.
        </p>
        {!data.held?.length && !data.manual_review?.length && <div className="empty">پیام معلق یا نیازمند بررسی ندارید.</div>}
        {(data.held || []).map((item) => <div key={`held-${item.id}`} className="list-item" style={{ display: 'block' }}>
          <div style={{ fontSize: 12 }}><strong>در حالت Hold</strong> · {channelNames[item.channel] || item.channel} · موعد: {item.scheduled_jalali}</div>
          <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>{item.text}</div>
        </div>)}
        {(data.manual_review || []).map((item) => <div key={`manual-${item.id}`} className="list-item" style={{ display: 'block' }}>
          <div style={{ fontSize: 12 }}><strong>نیاز به بررسی دستی</strong> · {channelNames[item.channel] || item.channel} · {item.received_jalali}</div>
          <div style={{ fontSize: 12, color: 'var(--muted)', margin: '6px 0' }}>{displayReason(item.reason)}</div>
          <p style={{ fontSize: 12, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{item.text}</p>
          <button className="btn btn-sm ghost" disabled={!!busyId} onClick={() => acknowledge(item)}>ثبت بررسی (بدون ارسال)</button>
        </div>)}
      </div>

      <div className="card">
        <h3>🔎 کشف مدل‌های بدون هزینهٔ اعلام‌شده</h3>
        <p style={{ fontSize: 12, lineHeight: 1.9, color: 'var(--muted)' }}>
          فهرست Mistral و Google حداکثر هر 6 ساعت در صورت نبود مدل معتبر، و پیش از استفاده از مدل تأییدشده دوباره بررسی می‌شود. مدلی که API برای ورودی و خروجی آن «0» اعلام نکند،
          حتی اگر اسمش Flash/Small باشد به‌صورت خودکار استفاده نمی‌شود. OpenRouter پیش از تلاش، فهرست زندهٔ :free و قیمت را بررسی می‌کند.
        </p>
        {['mistral', 'gemini'].map((provider) => <div key={provider} style={{ fontSize: 12, marginTop: 6 }}>
          {provider === 'mistral' ? 'Mistral' : 'Gemini'}: {data.models?.[provider]?.eligible || 0} مدل با قیمت صفرِ احرازشده
          {data.models?.[provider]?.checked_jalali ? ` · بررسی: ${data.models[provider].checked_jalali}` : ' · هنوز بررسی نشده'}
        </div>)}
        <p style={{ fontSize: 12, color: 'var(--muted)' }}>مدل‌های موقتاً بلاک‌شده به‌دلیل 402/403: {data.blacklisted_model_count || 0}</p>
      </div>
    </>}
  </>
}
