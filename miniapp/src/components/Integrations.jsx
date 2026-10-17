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

  const [zAccounts, setZAccounts] = useState([])
  const [zLoading, setZLoading] = useState(false)
  const [zSaving, setZSaving] = useState(false)
  const [zError, setZError] = useState('')
  const [zResult, setZResult] = useState(null)
  const [zForm, setZForm] = useState({
    zernio_account_id: '', media_type: 'post', media_urls: '', caption: '',
    mode: 'draft', scheduled_jalali: ''
  })

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
      if (s.zernio?.configured) {
        setZLoading(true)
        setZError('')
        try {
          const response = await api('/api/zernio/accounts')
          const accounts = response.accounts || []
          setZAccounts(accounts)
          setZForm(f => ({
            ...f,
            zernio_account_id: accounts.some(a => a.id === f.zernio_account_id && a.active)
              ? f.zernio_account_id : (accounts.find(a => a.active)?.id || '')
          }))
        } catch (e) {
          setZError(e.message)
        } finally {
          setZLoading(false)
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

  async function handleZernioSubmit(e) {
    e.preventDefault()
    const selected = zAccounts.find(a => a.id === zForm.zernio_account_id && a.active)
    if (!selected) {
      setZError('ابتدا پیج متصل را انتخاب کنید.')
      return
    }
    if (zForm.mode === 'schedule' && !window.confirm(`پست برای ${zForm.scheduled_jalali} در Zernio زمان‌بندی شود؟ این پست بعداً به‌صورت واقعی منتشر می‌شود.`)) return
    setZSaving(true)
    setZError('')
    setZResult(null)
    try {
      const result = await api('/api/zernio/posts', {
        method: 'POST',
        body: {
          account_id: selected.local_account_id,
          zernio_account_id: selected.id,
          media_type: zForm.media_type,
          media_urls: zForm.media_urls.split(/\r?\n/).map(url => url.trim()).filter(Boolean),
          caption: zForm.caption,
          mode: zForm.mode,
          scheduled_jalali: zForm.scheduled_jalali,
          confirm_schedule: zForm.mode === 'schedule',
        },
      })
      setZResult(result)
    } catch (err) {
      setZError(err.message)
    } finally {
      setZSaving(false)
    }
  }

  if (error) return <div className="empty">⚠️ {error}</div>
  if (!status) return <div className="skeleton" style={{ height: 200 }} />

  const cbx = status.chatbotx || {}
  const zernio = status.zernio || {}
  const pub = status.instagram_publisher || {}
  const oc = status.openclaw || {}
  const limits = status.reply_limits || {}
  const meta = status.meta_app || {}

  // OpenClaw gateway health → badge for the publishing card
  const gwConfigured = !!(pub.gateway && pub.gateway.configured) || !!oc.configured
  const gwReachable = oc.reachable === true
  const gwBadge = gwConfigured
    ? (gwReachable ? '✅ درگاه متصل' : (oc.reachable === false ? '⚠️ درگاه قطع' : '🛰 درگاه فعال'))
    : '❌ بدون درگاه'
  const gwColor = gwConfigured ? (gwReachable ? 'green' : '#856404') : '#721c24'
  const gwBg = gwConfigured ? (gwReachable ? '#e6f9ed' : '#fff3cd') : '#f8d7da'

  return (
    <>
      <div className="card">
        <h3>🔗 وضعیت یکپارچه‌سازی</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 12 }}>
          ChatbotX برای پیام‌رسانی · Zernio برای انتشار/زمان‌بندی اختیاری · مسیر قبلی OpenClaw جداگانه حفظ شده است.
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
                پیج: <b>{cbx.username || '@zeynalikids'}</b> · Workspace: {cbx.workspace_id || '11706290428788736'}<br />
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
              background: gwBg,
              color: gwColor
            }}>
              {gwBadge}
            </span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            مسیر انتشار: {gwConfigured ? <>درگاه OpenClaw + Browser Use Cloud (<b>{(pub.gateway && pub.gateway.host) || oc.gateway_host || '—'}</b>)</> : 'درگاه تنظیم نشده'}<br />
            Browser Use: {(pub.gateway && pub.gateway.browser_use_key_set) || oc.browser_use_key_set ? '✅ کلید ست شده' : '⚠️ کلید ست نشده'}<br />
            محدودیت: {fmt(pub.daily_limit || 3)} پست/روز · حداقل {fmt(pub.min_interval_minutes || 30)} دقیقه فاصله بین انتشارها<br />
            حالت پشتیبان: {pub.instagrapi_installed ? 'instagrapi نصب است' : 'بدون instagrapi (فقط درگاه)'}<br />
            ⚠️ تونل فعلی موقتی است؛ اگر ترموکس بسته شود، درگاه قطع می‌شود.
            {oc.reachable === false && oc.error && <><br />⚠️ خطای درگاه: {oc.error}</>}
            {pub.error && <><br />⚠️ {pub.error}</>}
          </div>
        </div>

        {zernio.admin && (
          <div className="list-item" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6, marginTop: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <b>🗓 Zernio — انتشار و زمان‌بندی</b>
              <span style={{ fontSize: 11, color: zAccounts.some(a => a.active) ? 'green' : '#856404' }}>
                {zAccounts.some(a => a.active) ? '✅ پیج متصل' : zernio.configured ? '⚠️ بدون پیج' : '❌ کلید تنظیم نشده'}
              </span>
            </div>
            <div style={{ fontSize: 11, color: 'var(--muted)' }}>
              مستقل از ChatbotX · {fmt(zAccounts.filter(a => a.active).length)} پیج مجاز · فقط پیش‌نویس یا زمان‌بندی دستی
            </div>
          </div>
        )}

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

      {zernio.admin && (
        <div className="card">
          <h3>🗓 Zernio — پیش‌نویس و زمان‌بندی اینستاگرام</h3>
          <p style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 2 }}>
            این مسیر از ChatbotX و انتشار OpenClaw جداست؛ هیچ پاسخ خودکاری یا انتشار فوری انجام نمی‌دهد.
            برای انتشار، پیج حرفه‌ای خود را مستقلاً در <a href="https://zernio.com" target="_blank" rel="noreferrer">داشبورد Zernio</a> متصل کنید.
            زمان‌بندی‌ها در Zernio نگهداری می‌شوند، نه تقویم داخلی گراما.
          </p>
          {!zernio.configured ? (
            <div className="banner err">کلید Zernio در سرور تنظیم نشده است.</div>
          ) : (
            <>
              <button className="btn btn-sm" type="button" onClick={load} disabled={zLoading} style={{ marginBottom: 10 }}>
                {zLoading ? 'در حال بررسی...' : 'بازخوانی پیج‌های Zernio'}
              </button>
              {zError && <div className="banner err" role="alert">⚠️ {zError}</div>}
              {!zLoading && zAccounts.length === 0 && (
                <div className="banner">هنوز پیج اینستاگرام مشترک با حساب گراما در Zernio دیده نمی‌شود؛ پس از اتصال، بازخوانی کنید. اتصال ChatbotX به‌طور خودکار منتقل نمی‌شود.</div>
              )}
              {zAccounts.length > 0 && !zAccounts.some(a => a.active) && (
                <div className="banner err">پیج Zernio غیرفعال است؛ لطفاً در داشبورد Zernio دوباره متصل کنید.</div>
              )}
              {zAccounts.some(a => a.active) && (
                <form onSubmit={handleZernioSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <label style={{ fontSize: 12 }}>پیج متصل</label>
                  <select value={zForm.zernio_account_id} onChange={e => setZForm({ ...zForm, zernio_account_id: e.target.value })} required>
                    {zAccounts.filter(a => a.active).map(a => <option key={a.id} value={a.id}>@{a.username}</option>)}
                  </select>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <select value={zForm.media_type} onChange={e => setZForm({ ...zForm, media_type: e.target.value })} style={{ flex: 1 }}>
                      <option value="post">پست تصویری</option>
                      <option value="story">استوری</option>
                      <option value="reel">ریلز ویدیویی</option>
                      <option value="carousel">کاروسل</option>
                    </select>
                    <select value={zForm.mode} onChange={e => setZForm({ ...zForm, mode: e.target.value })} style={{ flex: 1 }}>
                      <option value="draft">ذخیره پیش‌نویس</option>
                      <option value="schedule">زمان‌بندی انتشار</option>
                    </select>
                  </div>
                  <textarea
                    placeholder="آدرس عمومی HTTPS رسانه؛ برای کاروسل هر URL در یک خط"
                    value={zForm.media_urls}
                    onChange={e => setZForm({ ...zForm, media_urls: e.target.value })}
                    rows={2}
                    required
                  />
                  <textarea
                    placeholder={zForm.media_type === 'story' ? 'یادداشت استوری (در اینستاگرام نشان داده نمی‌شود)' : 'کپشن (تا 2200 نویسه)'}
                    value={zForm.caption}
                    onChange={e => setZForm({ ...zForm, caption: e.target.value })}
                    maxLength={2200}
                    rows={3}
                    required={zForm.media_type !== 'story'}
                  />
                  {zForm.mode === 'schedule' && (
                    <input
                      type="text"
                      inputMode="numeric"
                      placeholder="تاریخ شمسی: 1405/08/01 10:00"
                      aria-label="تاریخ و ساعت شمسی انتشار (تهران)"
                      value={zForm.scheduled_jalali}
                      onChange={e => setZForm({ ...zForm, scheduled_jalali: e.target.value })}
                      required
                    />
                  )}
                  <button className="btn" type="submit" disabled={zSaving || zLoading}>
                    {zSaving ? 'در حال ثبت...' : zForm.mode === 'draft' ? 'ذخیره پیش‌نویس در Zernio' : 'تأیید و زمان‌بندی در Zernio'}
                  </button>
                </form>
              )}
              {zResult && (
                <div className="banner ok" role="status" style={{ marginTop: 10 }}>
                  ✅ {zResult.mode === 'draft' ? 'پیش‌نویس ذخیره شد' : 'زمان‌بندی ثبت شد'} · وضعیت: {zResult.status}
                  <br />شناسه: {zResult.post_id}
                  {zResult.scheduled_jalali && <><br />زمان انتشار (تهران): {zResult.scheduled_jalali}</>}
                </div>
              )}
            </>
          )}
        </div>
      )}

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
