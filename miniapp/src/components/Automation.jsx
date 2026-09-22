import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'

/*
 * Automation.jsx — سازنده Auto-Reply (مرحله ۱ v4).
 *
 * به کاربر اجازه می‌دهد سناریوهای چندمرحله‌ای (مثل ManyChat / vardast) بسازد:
 *   - انتخاب کانال (دایرکت / کامنت / ریپلای استوری)
 *   - حالت تطابق (کلمه کلیدی / هوش مصنوعی / همیشه)
 *   - گام‌ها: send (ارسال پیام) / wait (منتظر پاسخ بعدی) / collect (ثبت متغیر)
 *             goto (پرش) / end (پایان)
 *   - پاسخ هوشمند با AvalAI / OpenClaw (سوییچ use_ai) + پیام جایگزین (fallback)
 *   - پیش‌نمایش زنده سناریو قبل از ذخیره
 *   - فعال/غیرفعال کردن سناریو و حذف
 */

const CHANNELS = [
  { id: 'dm', label: '📥 دایرکت' },
  { id: 'comment', label: '💬 کامنت' },
  { id: 'story_reply', label: '📸 ریپلای استوری' },
]
const MATCH_MODES = [
  { id: 'keyword', label: '🔑 کلمات کلیدی' },
  { id: 'ai', label: '🤖 هوش مصنوعی' },
  { id: 'always', label: '⚡ همیشه' },
]
const ACTIONS = [
  { id: 'send', label: '✉️ ارسال پیام' },
  { id: 'wait', label: '⏸ انتظار پاسخ کاربر' },
  { id: 'collect', label: '🧷 ثبت متغیر' },
  { id: 'goto', label: '🔀 پرش به گام' },
  { id: 'end', label: '🏁 پایان' },
]

const emptyStep = () => ({ action: 'send', text: '', use_ai: false, next_step_order: null, variable: '' })

export default function Automation() {
  const [scenarios, setScenarios] = useState(null)
  const [simpleRules, setSimpleRules] = useState(null)
  const [usage, setUsage] = useState(null)
  const [cbxStatus, setCbxStatus] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState(null) // scenario being edited (null = closed)
  const [steps, setSteps] = useState([])
  const [preview, setPreview] = useState('')
  const [sample, setSample] = useState('')
  const [previewLoading, setPreviewLoading] = useState(false)
  const [messages, setMessages] = useState(null) // {account_id}
  const [simpleForm, setSimpleForm] = useState({ keywords: '', reply: '', dm_followup: '', account_id: '' })
  const [testMsg, setTestMsg] = useState('')
  const [testResult, setTestResult] = useState(null)

  const load = useCallback(() => {
    Promise.all([
      api('/api/automation/scenarios'),
      api('/api/auto-replies'),
      api('/api/reply-usage'),
      api('/api/chatbotx/status').catch(() => ({ enabled: false })),
    ])
      .then(([sc, rules, u, cbx]) => {
        setScenarios(sc.scenarios || [])
        setSimpleRules(rules.rules || [])
        setUsage(u)
        setCbxStatus(cbx)
      })
      .catch((e) => setError(e.message))
  }, [])

  useEffect(() => { load() }, [load])

  async function loadAccounts() {
    try {
      const m = await api('/api/me')
      setMessages(m)
    } catch (_) { /* noop */ }
  }
  useEffect(() => { loadAccounts() }, [])

  function openNew() {
    setForm({ name: '', channel: 'dm', match_mode: 'keyword', trigger_text: '', ai_instruction: '', fallback_reply: '', use_ai: true, enabled: true, priority: 0 })
    setSteps([emptyStep()])
    setPreview('')
    setSample('')
  }

  function openEdit(sc) {
    setForm({ ...sc })
    setSteps((sc.steps && sc.steps.length ? sc.steps : [emptyStep()]).map((s) => ({ ...s })))
    setPreview('')
    setSample('')
  }

  async function save() {
    if (!form || !form.name.trim()) { alert('نام سناریو را وارد کنید'); return }
    setBusy(true)
    const payload = {
      ...form,
      // فقط گام‌های معتبر را بفرست
      steps: steps.map((s, i) => ({
        action: s.action, text: s.text || '', use_ai: !!s.use_ai,
        next_step_order: s.next_step_order == null ? null : Number(s.next_step_order),
        variable: s.variable || '',
      })),
    }
    try {
      if (form.id) {
        await api(`/api/automation/scenarios/${form.id}`, { method: 'PATCH', body: payload })
      } else {
        await api('/api/automation/scenarios', { method: 'POST', body: payload })
      }
      setForm(null); setSteps([])
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function toggle(sc) {
    try {
      await api(`/api/automation/scenarios/${sc.id}`, { method: 'PATCH', body: { enabled: !sc.enabled } })
      await load()
    } catch (e) { setError(e.message) }
  }

  async function remove(sc) {
    if (!window.confirm(`سناریوی «${sc.name}» حذف شود؟`)) return
    try {
      await api(`/api/automation/scenarios/${sc.id}`, { method: 'DELETE' })
      await load()
    } catch (e) { setError(e.message) }
  }

  async function runPreview() {
    if (!form) return
    setPreviewLoading(true)
    try {
      const d = await api('/api/automation/preview', {
        method: 'POST',
        body: { ...form, steps, sample: sample || 'قیمت؟' },
      })
      setPreview(d)
    } catch (e) {
      setError(e.message)
    } finally {
      setPreviewLoading(false)
    }
  }

  if (error && !scenarios) return <div className="empty">⚠️ {error}</div>
  if (!scenarios) return <div className="skeleton" style={{ height: 160 }} />

  if (form) return (
    <Editor
      form={form} setForm={setForm} steps={steps} setSteps={setSteps}
      save={save} busy={busy} close={() => setForm(null)}
      preview={preview} sample={sample} setSample={setSample}
      runPreview={runPreview} previewLoading={previewLoading}
      accountId={(messages && messages.accounts && messages.accounts[0]) ? messages.accounts[0].id : null}
      onApplyAccount={(f) => setForm(f)}
    />
  )

  async function addSimpleRule(e) {
    e.preventDefault()
    if (!simpleForm.keywords.trim() || !simpleForm.reply.trim()) {
      alert('کلمات کلیدی و پاسخ را وارد کنید')
      return
    }
    setBusy(true)
    try {
      await api('/api/auto-replies', {
        method: 'POST',
        body: {
          keywords: simpleForm.keywords,
          reply: simpleForm.reply,
          dm_followup: simpleForm.dm_followup,
          account_id: simpleForm.account_id ? Number(simpleForm.account_id) : undefined,
        }
      })
      setSimpleForm({ keywords: '', reply: '', dm_followup: '', account_id: simpleForm.account_id })
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function deleteSimpleRule(id) {
    if (!confirm('این قانون حذف شود؟')) return
    try {
      await api(`/api/auto-replies/${id}`, { method: 'DELETE' }).catch(async () => {
        // Fallback: try via generic delete if endpoint not exists, use direct DB via custom endpoint
        await api('/api/auto-replies/delete', { method: 'POST', body: { id } })
      })
      await load()
    } catch (err) {
      // If no delete endpoint, just reload and show message
      setError('حذف از طریق API پیاده‌سازی نشده — از Supabase حذف کنید. ' + err.message)
    }
  }

  async function testAutomation() {
    if (!testMsg.trim()) {
      alert('پیام تست را وارد کنید')
      return
    }
    setTestResult(null)
    try {
      const res = await api('/api/chatbotx-webhook', {
        method: 'POST',
        body: {
          data: {
            conversation_id: 'test_conv_' + Date.now(),
            contact_id: 'test_contact',
            message: { text: testMsg },
            type: 'dm',
            channel: 'instagram',
            contact: { name: 'تست' }
          }
        }
      })
      setTestResult(res)
    } catch (err) {
      setTestResult({ ok: false, error: err.message })
    }
  }

  return (
    <>
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h3 style={{ margin: 0 }}>🤖 اتوماسیون پاسخ‌گویی (Auto-Reply)</h3>
          <button className="btn" onClick={openNew}>+ سناریوی جدید</button>
        </div>
        <p style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.9 }}>
          سناریوهای چندمرحله‌ای بسازید: کاربر «قیمت» بفرستد → ربات لیست قیمت را بفرستد →
          کاربر انتخاب کند → سفارش ثبت شود. پاسخ‌ها می‌توانند با AvalAI / OpenClaw هوشمند شوند.
          اولویت: کلمات کلیدی بالاتر از هوش مصنوعی و «همیشه» است.
          <br />
          <b>معماری جدید:</b> اینستاگرام → ChatbotX API Channel → Gramma Webhook → بررسی قوانین (Supabase) → پاسخ via ChatbotX API
        </p>
      </div>

      <div className="card">
        <h3>📡 وضعیت کانال ChatbotX API</h3>
        {cbxStatus ? (
          <div style={{ fontSize: 12, lineHeight: 1.8 }}>
            <div>فعال: {cbxStatus.enabled ? '✅ بله' : '❌ خیر'} · پیکربندی: {cbxStatus.configured ? '✅' : '❌'}</div>
            <div>متصل: {cbxStatus.connected ? `✅ ${cbxStatus.username || '@zeynalikids'}` : '❌ غیرمتصل'}</div>
            <div>Workspace: {cbxStatus.workspace_id || '11706290428788736'} · Base: {cbxStatus.base_url || 'https://app.chatbotx.io/api'}</div>
            {cbxStatus.error && <div style={{ color: 'var(--danger)' }}>⚠️ {cbxStatus.error}</div>}
            <div style={{ marginTop: 8, fontSize: 11, color: 'var(--muted)' }}>
              ⚠️ نکته: در پنل ChatbotX، AI Auto Reply و Flows داخلی را <b>خاموش</b> کنید تا دو بار پاسخ ندهد (ریسک بلاک).
              <br />
              Callback URL: <code>https://miniapp-five-inky.vercel.app/api/chatbotx-webhook</code>
            </div>
          </div>
        ) : (
          <div className="skeleton" style={{ height: 60 }} />
        )}
      </div>

      {usage && (
        <div className="card">
          <h3>📊 آمار اتوماسیون</h3>
          <div className="side-panel-holder">
            <div className="stat">
              <div className="num">{usage.totals?.replies_today ?? 0}</div>
              <div className="lbl">پاسخ امروز</div>
            </div>
            <div className="stat">
              <div className="num">{usage.totals?.queued ?? 0}</div>
              <div className="lbl">در صف</div>
            </div>
            <div className="stat">
              <div className="num">{simpleRules?.length ?? 0}</div>
              <div className="lbl">قوانین ساده</div>
            </div>
            <div className="stat">
              <div className="num">{scenarios?.length ?? 0}</div>
              <div className="lbl">سناریو</div>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <h3>🔑 قوانین ساده (auto_replies)</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)' }}>
          این قوانین در Supabase ذخیره می‌شوند و توسط ربات (نه ChatbotX) بررسی می‌شوند. کلمات کلیدی با کاما جدا کنید.
        </p>
        <form onSubmit={addSimpleRule} style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>
          <div style={{ display: 'flex', gap: 8 }}>
            <input className="input" placeholder="کلمات کلیدی: قیمت, هزینه, خرید" value={simpleForm.keywords} onChange={e => setSimpleForm({ ...simpleForm, keywords: e.target.value })} style={{ flex: 2 }} />
            <input className="input" placeholder="ID پیج (اختیاری)" value={simpleForm.account_id} onChange={e => setSimpleForm({ ...simpleForm, account_id: e.target.value })} style={{ flex: 1 }} />
          </div>
          <textarea className="input" placeholder="پاسخ آماده..." value={simpleForm.reply} onChange={e => setSimpleForm({ ...simpleForm, reply: e.target.value })} rows={2} />
          <input className="input" placeholder="فالوآپ دایرکت (اختیاری) — بعد از پاسخ کامنت" value={simpleForm.dm_followup} onChange={e => setSimpleForm({ ...simpleForm, dm_followup: e.target.value })} />
          <button className="btn" type="submit" disabled={busy}>+ افزودن قانون</button>
        </form>
        {simpleRules && simpleRules.length === 0 && <div className="empty">قانونی تعریف نشده</div>}
        {simpleRules && simpleRules.map(r => (
          <div key={r.id} className="list-item">
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 700, fontSize: 12 }}>🔑 {r.keywords}</div>
              <div style={{ fontSize: 11, color: 'var(--muted)' }}>{r.reply.slice(0, 120)}</div>
              <div style={{ fontSize: 10, color: 'var(--muted)' }}>ID:{r.id} · اکانت:{r.account_id} · تطابق:{r.matches} · {r.enabled ? 'فعال' : 'غیرفعال'}</div>
            </div>
            <button className="btn small danger" onClick={() => deleteSimpleRule(r.id)}>🗑</button>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>🧪 تست اتوماسیون</h3>
        <p style={{ fontSize: 11, color: 'var(--muted)' }}>یک پیام تستی بفرستید تا ببینید ربات چطور جواب می‌دهد (از طریق ChatbotX Webhook شبیه‌سازی می‌شود)</p>
        <div style={{ display: 'flex', gap: 8 }}>
          <input className="input" placeholder="پیام تست: قیمت؟" value={testMsg} onChange={e => setTestMsg(e.target.value)} style={{ flex: 1 }} />
          <button className="btn" onClick={testAutomation}>تست</button>
        </div>
        {testResult && (
          <div className={`banner ${testResult.ok ? 'ok' : 'err'}`} style={{ marginTop: 8, fontSize: 12 }}>
            <pre style={{ whiteSpace: 'pre-wrap', fontSize: 11 }}>{JSON.stringify(testResult, null, 2)}</pre>
          </div>
        )}
      </div>

      {scenarios.length === 0 && (
        <div className="card">
          <div className="empty">هنوز سناریویی تعریف نشده است.<br />از دکمه «+ سناریوی جدید» شروع کنید.</div>
        </div>
      )}

      {scenarios.map((sc) => (
        <div className="card" key={sc.id}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div>
              <div style={{ fontWeight: 700 }}>{sc.name}</div>
              <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>
                {(CHANNELS.find((c) => c.id === sc.channel) || {}).label}
                {' · '}
                {(MATCH_MODES.find((m) => m.id === sc.match_mode) || {}).label}
                {' · '}{sc.steps.length} گام · {sc.hits || 0} بار اجرا
              </div>
            </div>
            <span className={'badge ' + (sc.enabled ? 'ok' : 'err')}>
              {sc.enabled ? 'فعال' : 'غیرفعال'}
            </span>
          </div>

          {sc.match_mode === 'keyword' && sc.trigger_text && (
            <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>
              🔑 {sc.trigger_text}
            </div>
          )}

          <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
            <button className="btn small" onClick={() => openEdit(sc)}>✏️ ویرایش</button>
            <button className="btn small ghost" onClick={() => toggle(sc)}>
              {sc.enabled ? '⏸ غیرفعال' : '▶️ فعال'}
            </button>
            <button className="btn small danger" onClick={() => remove(sc)}>🗑 حذف</button>
          </div>
        </div>
      ))}
    </>
  )
}

function Editor({ form, setForm, steps, setSteps, save, busy, close, preview, sample, setSample, runPreview, previewLoading, accountId, onApplyAccount }) {
  function patch(key, value) { setForm((f) => ({ ...f, [key]: value })) }

  function patchStep(i, key, value) {
    setSteps((arr) => arr.map((s, idx) => (idx === i ? { ...s, [key]: value } : s)))
  }
  function addStep() { setSteps((arr) => [...arr, emptyStep()]) }
  function removeStep(i) { setSteps((arr) => arr.filter((_, idx) => idx !== i)) }
  function moveStep(i, dir) {
    setSteps((arr) => {
      const j = i + dir
      if (j < 0 || j >= arr.length) return arr
      const c = [...arr]
      ;[c[i], c[j]] = [c[j], c[i]]
      return c
    })
  }

  // هنگام باز شدن از یک سناریوی موجود، account_id مساوی id حساب اصلی user است.
  if (accountId && form.account_id == null) {
    setTimeout(() => onApplyAccount({ ...form, account_id: accountId }), 0)
  }

  return (
    <>
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h3 style={{ margin: 0 }}>✏️ {form.id ? 'ویرایش سناریو' : 'سناریوی جدید'}</h3>
          <button className="icon-btn" onClick={close}>✕</button>
        </div>

        <div style={{ display: 'grid', gap: 10, marginTop: 12 }}>
          <input className="input" placeholder="نام سناریو (مثلاً: فروش و ثبت سفارش)"
            value={form.name} onChange={(e) => patch('name', e.target.value)} />

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {CHANNELS.map((c) => (
              <button key={c.id} className={'tab ' + (form.channel === c.id ? 'active' : '')}
                onClick={() => patch('channel', c.id)}>{c.label}</button>
            ))}
          </div>

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {MATCH_MODES.map((m) => (
              <button key={m.id} className={'tab ' + (form.match_mode === m.id ? 'active' : '')}
                onClick={() => patch('match_mode', m.id)}>{m.label}</button>
            ))}
          </div>

          {form.match_mode === 'keyword' && (
            <input className="input" placeholder="کلمات کلیدی (با کاما جدا کنید): قیمت, هزینه, خرید"
              value={form.trigger_text} onChange={(e) => patch('trigger_text', e.target.value)} />
          )}

          <textarea className="input" placeholder="دستور هوش مصنوعی (اختیاری) — مثلاً: تو فروشنده پیج ما هستی و مودبانه به سوالات پاسخ می‌دهی"
            value={form.ai_instruction} onChange={(e) => patch('ai_instruction', e.target.value)} rows={2} />

          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input type="checkbox" checked={!!form.use_ai} onChange={(e) => patch('use_ai', e.target.checked)} />
            <span style={{ fontSize: 12 }}>پاسخ هوشمند با AvalAI / OpenClaw (در صورت در دسترس بودن)</span>
          </div>

          <input className="input" placeholder="پیام جایگزین (fallback) وقتی AI در دسترس نیست"
            value={form.fallback_reply} onChange={(e) => patch('fallback_reply', e.target.value)} />

          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input type="checkbox" checked={!!form.enabled} onChange={(e) => patch('enabled', e.target.checked)} />
            <span style={{ fontSize: 12 }}>فعال باشد</span>
          </div>
        </div>
      </div>

      {/* گام‌ها */}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>🧩 گام‌های سناریو</h3>
        {steps.map((st, i) => (
          <div key={i} className="step-box">
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
              <span className="step-num">{i + 1}</span>
              <select className="input" style={{ width: 'auto', flex: 1 }}
                value={st.action} onChange={(e) => patchStep(i, 'action', e.target.value)}>
                {ACTIONS.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
              </select>
              <button className="icon-btn" onClick={() => moveStep(i, -1)}>↑</button>
              <button className="icon-btn" onClick={() => moveStep(i, 1)}>↓</button>
              <button className="icon-btn" onClick={() => removeStep(i)}>✕</button>
            </div>

            {(st.action === 'send' || st.action === 'wait') && (
              <textarea className="input" style={{ marginTop: 8 }} rows={2}
                placeholder={st.action === 'send' ? 'متن پیام برای کاربر…' : 'پیام قبل از انتظار (اختیاری)…'}
                value={st.text} onChange={(e) => patchStep(i, 'text', e.target.value)} />
            )}
            {st.action === 'collect' && (
              <input className="input" style={{ marginTop: 8 }} placeholder="نام متغیر (مثلاً choice)"
                value={st.variable} onChange={(e) => patchStep(i, 'variable', e.target.value)} />
            )}
            {st.action === 'goto' && (
              <input className="input" style={{ marginTop: 8 }} type="number" placeholder="شماره گام مقصد"
                value={st.next_step_order == null ? '' : st.next_step_order}
                onChange={(e) => patchStep(i, 'next_step_order', e.target.value)} />
            )}

            {(st.action === 'send' || st.action === 'wait') && (
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, fontSize: 11.5, color: 'var(--muted)' }}>
                <input type="checkbox" checked={!!st.use_ai} onChange={(e) => patchStep(i, 'use_ai', e.target.checked)} />
                این گام را هوشمند بنویس (AI)
              </label>
            )}
          </div>
        ))}
        <button className="btn ghost" style={{ marginTop: 8 }} onClick={addStep}>+ افزودن گام</button>
      </div>

      {/* پیش‌نمایش */}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>🔍 پیش‌نمایش زنده</h3>
        <div style={{ display: 'flex', gap: 8 }}>
          <input className="input" placeholder="پیام نمونه (مثلاً: قیمت؟)" value={sample}
            onChange={(e) => setSample(e.target.value)} />
          <button className="btn" disabled={previewLoading} onClick={runPreview}>
            {previewLoading ? '…' : 'تست'}
          </button>
        </div>
        {preview && (
          <div className="preview-bubble" style={{ marginTop: 10 }}>
            {preview.reply
              ? <div className="bubble bot">{preview.reply}</div>
              : (preview.final
                ? <div className="empty" style={{ padding: 10 }}>پایان سناریو — پیامی ارسال نمی‌شود.</div>
                : null)}
            {(preview.reply || preview.final) && (
              <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 6 }}>
                {preview.waiting ? '⏸ منتظر پاسخ بعدی کاربر' : preview.final ? '🏁 پایان' : '✅ پاسخ ارسال شد'}
              </div>
            )}
          </div>
        )}
      </div>

      <div style={{ display: 'flex', gap: 10 }}>
        <button className="btn" style={{ flex: 1 }} disabled={busy} onClick={save}>
          {busy ? 'در حال ذخیره…' : '💾 ذخیره سناریو'}
        </button>
        <button className="btn ghost" onClick={close}>انصراف</button>
      </div>
    </>
  )
}
