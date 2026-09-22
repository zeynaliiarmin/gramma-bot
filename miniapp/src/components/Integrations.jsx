import { useEffect, useState } from 'react'
import { api } from '../api.js'

function fmt(n) {
  return Number(n || 0).toLocaleString('en-US')
}

export default function Integrations() {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState('')
  const [publishing, setPublishing] = useState(false)
  const [pubForm, setPubForm] = useState({ account_id: '', image_url: '', caption: '', media_type: 'post' })
  const [pubResult, setPubResult] = useState(null)

  async function load() {
    try {
      const s = await api('/api/integrations/status')
      setStatus(s)
      if (s.reply_limits && s.reply_limits.accounts_count > 0) {
        // Try to get accounts for publish form
        const me = await api('/api/me')
        if (me.accounts && me.accounts.length > 0) {
          setPubForm(f => ({ ...f, account_id: f.account_id || me.accounts[0].id }))
        }
      }
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [])

  async function handlePublish(e) {
    e.preventDefault()
    setPublishing(true)
    setPubResult(null)
    try {
      const res = await api('/api/instagram-publisher/publish', {
        method: 'POST',
        body: {
          account_id: Number(pubForm.account_id),
          image_url: pubForm.image_url,
          caption: pubForm.caption,
          media_type: pubForm.media_type,
        }
      })
      setPubResult(res)
    } catch (err) {
      setPubResult({ ok: false, error: err.message })
    } finally {
      setPublishing(false)
    }
  }

  if (error) return <div className="empty">⚠️ {error}</div>
  if (!status) return <div className="skeleton" style={{ height: 200 }} />

  const cbx = status.chatbotx || {}
  const pub = status.instagram_publisher || {}
  const limits = status.reply_limits || {}
  const meta = status.meta_app || {}

  return (
    <>
      <div className="card">
        <h3>🔗 وضعیت یکپارچه‌سازی</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 12 }}>
          معماری جدید: ChatbotX برای پیام‌رسانی (بدون تحریم) + OpenClaw برای انتشار محتوا + Gramma برای رابط کاربری
        </p>

        <div className="list-item" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <b>💬 ChatbotX — پیام‌رسانی</b>
            <span style={{
              fontSize: 11,
              padding: '2px 8px',
              borderRadius: 10,
              background: cbx.connected ? 'var(--ok-bg, #e6f9ed)' : 'var(--warn-bg, #fff3cd)',
              color: cbx.connected ? 'green' : '#856404'
            }}>
              {cbx.connected ? '✅ متصل' : cbx.enabled ? '⚠️ نیاز به اتصال' : '❌ غیرفعال'}
            </span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            {cbx.enabled ? (
              <>
                پیج: <b>{cbx.username || '@zeynalikids'}</b> · Workspace: {cbx.workspace_id || '1170629'}<br />
                قابلیت: کامنت (1000/روز) · دایرکت (نامحدود) · استوری ریپلای · AI AvalAI<br />
                آدرس: <a href="https://app.chatbotx.io" target="_blank" rel="noreferrer">app.chatbotx.io</a>
                {cbx.error && <><br />⚠️ {cbx.error}</>}
              </>
            ) : (
              <>توکن ChatbotX تنظیم نشده. در .env بگذارید: CHATBOTX_WORKSPACE_TOKEN, CHATBOTX_WORKSPACE_ID, CHATBOTX_ENABLED=true</>
            )}
          </div>
        </div>

        <div className="list-item" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6, marginTop: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <b>📤 OpenClaw — انتشار محتوا</b>
            <span style={{
              fontSize: 11,
              padding: '2px 8px',
              borderRadius: 10,
              background: pub.enabled ? (pub.instagrapi_installed ? '#e6f9ed' : '#fff3cd') : '#f8d7da',
              color: pub.enabled ? (pub.instagrapi_installed ? 'green' : '#856404') : '#721c24'
            }}>
              {pub.enabled ? (pub.instagrapi_installed ? '✅ آماده' : '🧪 شبیه‌سازی') : '❌ غیرفعال'}
            </span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            حالت: <b>{pub.mode || 'private-enabled'}</b> · سقف: {fmt(pub.daily_limit || 3)} پست/روز/پیج<br />
            instagrapi: {pub.instagrapi_installed ? '✅ نصب شده' : '❌ نصب نیست (حالت mock)'}<br />
            یوزرنیم: {pub.username || 'تنظیم نشده'} · اعتبار: {pub.credentials_set ? '✅ ست شده' : '⚠️ ست نشده'}<br />
            ⚠️ هشدار: API خصوصی اینستاگرام ریسک بلاک دارد — با محدودیت 3 پست/روز استفاده کنید.<br />
            نصب: <code>openclaw skills install clinstagram</code> سپس <code>clinstagram.config.mode = "private-enabled"</code>
            {pub.error && <><br />⚠️ {pub.error}</>}
          </div>
        </div>

        <div className="list-item" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6, marginTop: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <b>📊 سقف پاسخ کامنت</b>
            <span style={{ fontSize: 11, color: 'var(--muted)' }}>{limits.date}</span>
          </div>
          <div style={{ fontSize: 12 }}>
            روزانه: {fmt(limits.daily_limit)} · ساعتی: {fmt(limits.hourly_limit)} · پیج‌ها: {fmt(limits.accounts_count)} (متصل {fmt(limits.connected_count)})
          </div>
        </div>

        <div className="list-item" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6, marginTop: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <b>🔐 اپ متا</b>
            <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 10, background: meta.configured ? '#e6f9ed' : '#f8d7da', color: meta.configured ? 'green' : '#721c24' }}>
              {meta.configured ? '✅ پیکربندی شده' : '❌ ساخته نشده (تحریم)'}
            </span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--muted)' }}>
            حالت: {meta.mode} · {meta.configured ? 'OAuth واقعی فعال' : 'به دلیل تحریم ایران، از ChatbotX استفاده می‌شود (Login with Instagram)'}
          </div>
        </div>
      </div>

      <div className="card">
        <h3>📤 انتشار محتوا (OpenClaw)</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10 }}>
          حداکثر 3 پست در روز برای هر پیج — برای کاهش ریسک بلاک. در حالت mock، انتشار شبیه‌سازی می‌شود.
        </p>
        <form onSubmit={handlePublish} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="number"
              placeholder="ID پیج"
              value={pubForm.account_id}
              onChange={e => setPubForm({ ...pubForm, account_id: e.target.value })}
              style={{ flex: 1 }}
              required
            />
            <select
              value={pubForm.media_type}
              onChange={e => setPubForm({ ...pubForm, media_type: e.target.value })}
              style={{ flex: 1 }}
            >
              <option value="post">پست</option>
              <option value="story">استوری</option>
              <option value="reel">ریلز</option>
              <option value="carousel">کاروسل</option>
            </select>
          </div>
          <input
            type="url"
            placeholder="آدرس تصویر/ویدیو (https://...)"
            value={pubForm.image_url}
            onChange={e => setPubForm({ ...pubForm, image_url: e.target.value })}
            required
          />
          <textarea
            placeholder="کپشن..."
            value={pubForm.caption}
            onChange={e => setPubForm({ ...pubForm, caption: e.target.value })}
            rows={3}
          />
          <button className="btn" type="submit" disabled={publishing}>
            {publishing ? '⏳ در حال انتشار...' : '📤 انتشار'}
          </button>
        </form>
        {pubResult && (
          <div className={`banner ${pubResult.ok ? 'ok' : 'err'}`} style={{ marginTop: 10 }}>
            {pubResult.ok ? (
              <>
                ✅ منتشر شد (mock={String(pubResult.mock)})<br />
                ID: {pubResult.media_id}<br />
                لینک: <a href={pubResult.permalink} target="_blank" rel="noreferrer">{pubResult.permalink}</a><br />
                باقی‌مانده امروز: {pubResult.limit_info?.remaining_after ?? '?'}<br />
                {pubResult.warning && <><br />⚠️ {pubResult.warning}</>}
              </>
            ) : (
              <>❌ خطا: {pubResult.error}</>
            )}
          </div>
        )}
      </div>

      <div className="card">
        <h3>🧹 پاکسازی دستی</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10 }}>
          حذف چت‌های قدیمی و لاگ‌ها برای کاهش حجم دیتابیس
        </p>
        <button
          className="btn btn-sm"
          onClick={async () => {
            if (!confirm('چت‌های قدیمی‌تر از 30 روز حذف شوند؟')) return
            try {
              const res = await api('/api/cleanup/manual', { method: 'POST', body: { days: 30 } })
              alert(`✅ پاکسازی انجام شد: ${res.deleted_chats} چت و ${res.deleted_logs} لاگ حذف شد`)
              await load()
            } catch (e) {
              alert('❌ خطا: ' + e.message)
            }
          }}
        >
          🧹 پاکسازی 30 روزه
        </button>
      </div>
    </>
  )
}
