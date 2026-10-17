import { useEffect, useRef, useState } from 'react'
import * as tus from 'tus-js-client'
import { api } from '../api.js'
import ComposerJalaliPicker, { tehranChoice } from './ComposerJalaliPicker.jsx'
import './composer.css'

const BLANK = { caption: '', caption_moved_to_comment: false, visible_caption: '',
  collaborators: [], userTags: [], scheduled: null }
const MIME = { 'image/jpeg': true, 'image/png': true, 'video/mp4': true,
  'video/quicktime': true }
const isVideo = (file) => file.type.startsWith('video/')
const label = (mode, kind, amount) => mode === 'story' ? 'دنبالهٔ استوری' :
  amount > 1 ? 'کاروسل' : kind === 'video' ? 'ریلز · نمایش در فید' : 'پست فید'
const cleanName = (name) => name.trim().replace(/^@/, '').toLowerCase()
const usernameOK = (name) => /^[a-z0-9._]{1,30}$/.test(name) &&
  !name.startsWith('.') && !name.endsWith('.') && !name.includes('..')

function fileMetadata(file) {
  return new Promise((resolve, reject) => {
    if (!MIME[file.type]) { reject(new Error('فقط عکس JPEG/PNG یا ویدیوی MP4/MOV انتخاب کنید.')); return }
    if (file.size <= 0 || file.size > (isVideo(file) ? 50 : 8) * 1024 * 1024) {
      reject(new Error('سقف عکس 8 MB و سقف ویدیو در Storage فعلی 50 MB است.')); return
    }
    const objectUrl = URL.createObjectURL(file)
    const media = document.createElement(isVideo(file) ? 'video' : 'img')
    const finish = (error, metadata) => {
      media.onload = null; media.onerror = null; media.onloadedmetadata = null
      URL.revokeObjectURL(objectUrl)
      error ? reject(error) : resolve(metadata)
    }
    media.onerror = () => finish(new Error('خواندن ابعاد رسانه ممکن نشد؛ فایل دیگری انتخاب کنید.'))
    const success = () => {
      const width = isVideo(file) ? media.videoWidth : media.naturalWidth
      const height = isVideo(file) ? media.videoHeight : media.naturalHeight
      const duration = isVideo(file) ? media.duration : null
      if (!width || !height || (isVideo(file) && !Number.isFinite(duration))) {
        finish(new Error('ابعاد یا مدت ویدیو معتبر نیست.')); return
      }
      finish(null, { mime_type: file.type, size_bytes: file.size,
        width, height, duration_seconds: duration })
    }
    if (isVideo(file)) { media.preload = 'metadata'; media.onloadedmetadata = success }
    else media.onload = success
    media.src = objectUrl
  })
}

function uploadSigned(file, signed, onProgress) {
  if (file.size > 6 * 1024 * 1024) {
    return new Promise((resolve, reject) => {
      const upload = new tus.Upload(file, {
        endpoint: signed.tus_endpoint,
        headers: { 'x-signature': signed.upload_token },
        fingerprint: () => Promise.resolve(`${signed.storage_path}:${file.name}:${file.size}:${file.lastModified}`),
        retryDelays: [0, 1000, 3000, 5000],
        chunkSize: 6 * 1024 * 1024,
        uploadDataDuringCreation: true,
        removeFingerprintOnSuccess: true,
        metadata: { bucketName: signed.bucket, objectName: signed.storage_path,
          contentType: file.type, cacheControl: '3600' },
        onProgress: (done, total) => onProgress(Math.round(done / total * 100)),
        onError: () => reject(new Error('آپلود ویدیو قطع شد؛ اتصال را بررسی کنید و همان فایل را دوباره انتخاب کنید.')),
        onSuccess: resolve,
      })
      upload.start()
    })
  }
  return fetch(signed.upload_url, {
    method: 'PUT', headers: { 'Content-Type': file.type }, body: file,
  }).then((response) => {
    if (!response.ok) throw new Error(`آپلود در Storage کامل نشد (کد ${response.status}).`)
    onProgress(100)
  })
}

function MediaStage({ media, url, tags = [], tagging, onPoint }) {
  if (!media) return <div className="compose-stage-empty">🖼<span>رسانه‌ای انتخاب نشده است</span></div>
  const width = Math.min(510, 430 * media.width / media.height)
  return <div className={'compose-media-stage' + (tagging ? ' tagging' : '')}
    style={{ aspectRatio: `${media.width} / ${media.height}`, maxWidth: width }}
    onClick={(event) => {
      if (!tagging || media.kind !== 'image') return
      const box = event.currentTarget.getBoundingClientRect()
      onPoint({ x: Math.round(Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)) * 1000) / 1000,
        y: Math.round(Math.min(1, Math.max(0, (event.clientY - box.top) / box.height)) * 1000) / 1000 })
    }} role={tagging ? 'button' : undefined} aria-label={tagging ? 'برای انتخاب محل تگ روی عکس کلیک کنید' : undefined}>
    {media.kind === 'video' ? <video src={url} controls playsInline preload="metadata" /> :
      <img src={url} alt={`پیش‌نمایش اسلاید ${media.position + 1}`} />}
    {media.kind === 'image' && tags.map((tag, index) => <div key={`${tag.username}-${index}`}
      className="compose-pin" style={{ left: `${tag.x * 100}%`, top: `${tag.y * 100}%` }}>
      <span>●</span><b>@{tag.username}</b>
    </div>)}
    {tagging && media.kind === 'image' && <span className="compose-tag-tip">برای گذاشتن تگ، عکس را لمس کنید</span>}
  </div>
}

export default function Composer({ me }) {
  const accounts = (me?.accounts || []).filter((account) => account.status === 'connected')
  const [mode, setMode] = useState('post')
  const [page, setPage] = useState(accounts[0]?.id || '')
  const [draft, setDraft] = useState(null)
  const [form, setForm] = useState(BLANK)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState('')
  const [percent, setPercent] = useState(null)
  const [selected, setSelected] = useState(0)
  const [tagMode, setTagMode] = useState(false)
  const [pendingPoint, setPendingPoint] = useState(null)
  const [tagName, setTagName] = useState('')
  const [collabName, setCollabName] = useState('')
  const [previews, setPreviews] = useState({})
  const previewRef = useRef({})
  const lastSavedRef = useRef('')

  useEffect(() => () => Object.values(previewRef.current).forEach(URL.revokeObjectURL), [])

  async function loadMode(next) {
    setLoading(true); setNotice(''); setMode(next); setSelected(0); setTagMode(false)
    try {
      const data = await api(`/api/composer/active?kind=${next}`)
      setDraft(data.draft)
      const settings = { ...BLANK, ...(data.draft?.settings || {}) }
      setForm(settings)
      lastSavedRef.current = JSON.stringify(settings)
      if (data.draft) setPage(data.draft.account_id)
    } catch (error) { setNotice('⚠️ ' + error.message) }
    finally { setLoading(false) }
  }

  useEffect(() => { loadMode('post') }, []) // rehydrate persisted post draft after navigation

  // Debounced, durable UI choices. This endpoint NEVER inserts queue rows.
  useEffect(() => {
    if (!draft || draft.kind !== 'post' || draft.status !== 'collecting' || busy) return
    const encoded = JSON.stringify(form)
    if (encoded === lastSavedRef.current) return
    const timer = setTimeout(() => {
      api(`/api/composer/drafts/${draft.id}/settings`, { method: 'POST', body: form })
        .then(() => { lastSavedRef.current = encoded })
        .catch((error) => setNotice('⚠️ ذخیرهٔ تنظیمات: ' + error.message))
    }, 800)
    return () => clearTimeout(timer)
  }, [form, draft?.id, draft?.status, busy])

  async function start(kind = mode) {
    if (!page) throw new Error('ابتدا یک پیج متصل انتخاب کنید.')
    const data = await api('/api/composer/drafts', { method: 'POST',
      body: { account_id: Number(page), kind } })
    const value = data.draft
    setDraft(value); setPage(value.account_id)
    setForm({ ...BLANK, ...(value.settings || {}) })
    lastSavedRef.current = JSON.stringify({ ...BLANK, ...(value.settings || {}) })
    return value
  }

  async function refresh(id) {
    const data = await api(`/api/composer/drafts/${id}`)
    setDraft(data.draft)
    return data.draft
  }

  async function addFiles(event) {
    const files = Array.from(event.target.files || [])
    event.target.value = ''
    if (!files.length || busy) return
    setNotice(''); setBusy(true); setPercent(0)
    try {
      let current = draft?.status === 'collecting' && draft.kind === mode ?
        await refresh(draft.id) : await start()
      let position = current.media.find((m) => m.status === 'uploading')?.position ?? current.media.length
      const maximum = mode === 'story' ? 30 : 10
      if (position + files.length > maximum) throw new Error(`حداکثر ${maximum} اسلاید مجاز است.`)
      for (const file of files) {
        const info = await fileMetadata(file)
        const signed = await api(`/api/composer/drafts/${current.id}/media/prepare`, {
          method: 'POST', body: { ...info, position },
        })
        if (!signed.ready) {
          await uploadSigned(file, signed, setPercent)
          await api(`/api/composer/drafts/${current.id}/media/${signed.media.id}/complete`, { method: 'POST' })
        }
        const existingUrl = previewRef.current[signed.media.id]
        if (existingUrl) URL.revokeObjectURL(existingUrl)
        previewRef.current[signed.media.id] = URL.createObjectURL(file)
        setPreviews({ ...previewRef.current })
        current = await refresh(current.id)
        setSelected(position)
        position++
        setPercent(0)
      }
      setNotice(`✅ ${files.length} رسانه ثبت شد. هنوز هیچ‌چیز به صف انتشار نرفته است.`)
    } catch (error) {
      setNotice('⚠️ ' + error.message + ' هیچ درخواست انتشاری ثبت نشده است.')
      if (draft?.id) try { await refresh(draft.id) } catch (_) { /* keep local state */ }
    } finally { setBusy(false); setPercent(null) }
  }

  async function confirmIncomplete() {
    const incomplete = draft?.media.find((item) => item.status === 'uploading')
    if (!incomplete) return
    setBusy(true); setNotice('')
    try {
      await api(`/api/composer/drafts/${draft.id}/media/${incomplete.id}/complete`, { method: 'POST' })
      await refresh(draft.id)
      setNotice('✅ آپلود قبلی تأیید شد؛ هنوز در صف انتشار نیست.')
    } catch (error) { setNotice('⚠️ ' + error.message + ' اگر فایل نرسیده است، همان فایل را دوباره انتخاب کنید.') }
    finally { setBusy(false) }
  }

  function addTag() {
    const name = cleanName(tagName)
    const media = draft?.media[selected]
    if (!media || !usernameOK(name)) { setNotice('⚠️ نام کاربری تگ معتبر نیست.'); return }
    if (form.userTags.length >= 5 || form.userTags.some((t) =>
      t.username === name && (t.mediaIndex ?? 0) === selected)) {
      setNotice('⚠️ حداکثر 5 تگ مجاز است و تگ تکراری برای یک اسلاید نداریم.'); return
    }
    if (media.kind === 'image' && !pendingPoint) { setNotice('⚠️ ابتدا محل تگ را روی عکس لمس کنید.'); return }
    const tag = { username: name, mediaIndex: selected,
      ...(media.kind === 'image' ? pendingPoint : {}) }
    setForm((old) => ({ ...old, userTags: [...old.userTags, tag] }))
    setTagName(''); setPendingPoint(null); setTagMode(false); setNotice('')
  }

  function addCollab() {
    const name = cleanName(collabName)
    if (!usernameOK(name) || form.collaborators.includes(name) || form.collaborators.length >= 3) {
      setNotice('⚠️ نام همکار معتبر یا یکتا نیست؛ حداکثر 3 همکار مجاز است.'); return
    }
    setForm((old) => ({ ...old, collaborators: [...old.collaborators, name] }))
    setCollabName(''); setNotice('')
  }

  async function finish() {
    if (!draft || busy) return
    setBusy(true); setNotice('')
    try {
      const normalizedTags = form.userTags.map((tag) => {
        if (draft.media.length > 1) return tag
        const { mediaIndex, ...single } = tag
        return single
      })
      const settings = mode === 'story' ? {} : { ...form, userTags: normalizedTags }
      const result = await api(`/api/composer/drafts/${draft.id}/submit`, {
        method: 'POST', body: settings,
      })
      setDraft((old) => ({ ...old, status: 'queued', queue_ids: result.queue_ids }))
      setNotice(mode === 'story'
        ? `✅ ${result.queue_ids.length} اسلاید استوری در صف مستقل ثبت شد؛ اولین اسلاید در نوبت Worker و بقیه پس از تأیید انتشار قبلی با فاصلهٔ 5 تا 12 دقیقه.`
        : `✅ پست فقط به صف امن افزوده شد. شناسهٔ صف: ${result.queue_ids[0]}. انتشار واقعی طبق محدودیت‌ها بعداً انجام می‌شود.`)
    } catch (error) { setNotice('⚠️ ' + error.message + ' پیش از تکرار، وضعیت صف را بررسی کنید.') }
    finally { setBusy(false) }
  }

  async function cancel() {
    if (!draft || busy) return
    setBusy(true); setNotice('')
    try {
      await api(`/api/composer/drafts/${draft.id}/cancel`, { method: 'POST' })
      Object.values(previewRef.current).forEach(URL.revokeObjectURL)
      previewRef.current = {}; setPreviews({}); setDraft(null); setForm(BLANK)
      lastSavedRef.current = JSON.stringify(BLANK)
      setNotice('🗑 پیش‌نویس لغو شد؛ هیچ رسانه‌ای به صف انتشار نرفت.')
    } catch (error) { setNotice('⚠️ ' + error.message) }
    finally { setBusy(false) }
  }

  const media = draft?.media || []
  const current = media[selected] || media[0]
  const mediaType = mode === 'story' ? 'story' : media.length > 1 ? 'carousel' :
    current?.kind === 'video' ? 'reel' : 'post'
  const eligibleForComment = mediaType === 'post' || mediaType === 'carousel'
  const formTags = form.userTags.filter((tag) => (tag.mediaIndex ?? 0) === (current?.position ?? 0))
  const ready = media.length > 0 && media.every((item) => item.status === 'ready')
  const pageName = accounts.find((p) => p.id === Number(page))?.username || draft?.username || '—'

  return <div className="composer" dir="rtl">
    <div className="compose-hero">
      <div><div className="compose-eyebrow">GRAMMA · CREATOR STUDIO</div>
        <h2>از گالری تا صف انتشار</h2>
        <p>رسانه و تنظیمات را آماده کنید؛ فقط دکمهٔ نهایی درخواست را ثبت می‌کند.</p>
      </div><span className="compose-hero-icon">✦</span>
    </div>

    <div className="compose-mode" role="tablist" aria-label="انتخاب نوع محتوا">
      <button type="button" role="tab" aria-selected={mode === 'post'}
        className={mode === 'post' ? 'active' : ''} disabled={busy}
        onClick={() => mode !== 'post' && loadMode('post')}>✦ پست / ریلز / کاروسل</button>
      <button type="button" role="tab" aria-selected={mode === 'story'}
        className={mode === 'story' ? 'active story' : ''} disabled={busy}
        onClick={() => mode !== 'story' && loadMode('story')}>◉ ساخت استوری</button>
    </div>

    {loading ? <div className="card">در حال بازیابی پیش‌نویس…</div> : <>
      <section className="compose-panel">
        <div className="compose-section-head"><div><span className="compose-step">01</span>
          <h3>انتخاب محتوا</h3></div><span className="compose-muted">فقط پیج متصل</span></div>
        <label className="compose-field-title" htmlFor="compose-page">پیج مقصد</label>
        <select id="compose-page" value={page} className="input" disabled={Boolean(draft) || busy}
          onChange={(event) => setPage(Number(event.target.value))}>
          {!accounts.length && <option value="">پیج متصل پیدا نشد</option>}
          {accounts.map((account) => <option value={account.id} key={account.id}>@{account.username}</option>)}
        </select>
        {draft && <p className="compose-hint">پیش‌نویس #{draft.id} برای @{pageName} محفوظ است. تغییر پیج فقط با لغو صریحِ این پیش‌نویس ممکن است.</p>}

        <div className="compose-content-kind"><strong>{label(mode, current?.kind, media.length)}</strong>
          <span>{mode === 'story' ? `${media.length} / 30 اسلاید` : `${media.length} / 10 رسانه`}</span></div>
        <div className="compose-preview">
          <MediaStage media={current} url={current ? (previews[current.id] || current.url) : ''}
            tags={mode === 'post' ? formTags : []} tagging={tagMode}
            onPoint={(point) => { setPendingPoint(point); setNotice('') }} />
        </div>
        {media.length > 1 && <div className="compose-thumbs" role="tablist" aria-label="اسلایدها">
          {media.map((item, index) => <button type="button" key={item.id}
            role="tab" aria-selected={selected === index}
            className={selected === index ? 'selected' : ''}
            onClick={() => { setSelected(index); setTagMode(false); setPendingPoint(null) }}>
            {item.url ? (item.kind === 'video' ? <video src={previews[item.id] || item.url} muted playsInline /> :
              <img src={previews[item.id] || item.url} alt="" />) : <span>⬆️</span>}
            <b>{index + 1}</b>
          </button>)}
        </div>}
        {media.some((item) => item.status === 'uploading') && <div className="compose-warning">
          یک اسلاید هنوز تأیید نشده است؛ تا آن زمان دکمهٔ صف فعال نمی‌شود.
          <button className="btn ghost btn-sm" type="button" disabled={busy} onClick={confirmIncomplete}>بررسی آپلود قبلی</button>
        </div>}
        {percent !== null && <div className="compose-upload-status" role="status">
          <div className="progress"><div className="progress-fill" style={{ width: `${percent}%` }} /></div>
          <span>در حال آپلود امن… {percent}%</span></div>}
        {draft?.status !== 'queued' && <label className={'compose-upload-btn' + (busy || !accounts.length ? ' disabled' : '')}>
          <span>＋</span><strong>{media.some((item) => item.status === 'uploading') ? 'انتخاب دوبارهٔ همان فایل' :
            media.length ? 'افزودن اسلاید بعدی' : 'انتخاب از گالری'}</strong>
          <small>JPEG · PNG · MP4 · MOV</small>
          <input type="file" accept="image/jpeg,image/png,video/mp4,video/quicktime,.mov" multiple
            disabled={busy || !accounts.length || (media.length >= (mode === 'story' ? 30 : 10)
              && !media.some((item) => item.status === 'uploading'))} onChange={addFiles} />
        </label>}
        <p className="compose-hint">یک عکس ← فید · یک ویدیوی عمودی ← ریلز · چند رسانه ← کاروسل.
          {mode === 'story' && ' ترتیب اسلایدها هنگام انتخاب محفوظ می‌ماند؛ تعداد اعلامی محدودیتِ پایان نیست.'}</p>
      </section>

      {mode === 'post' && draft?.status !== 'queued' && <section className="compose-panel">
        <div className="compose-section-head"><div><span className="compose-step">02</span><h3>تنظیمات و پیش‌نمایش</h3></div>
          <span className="compose-muted">{mediaType === 'reel' ? 'ریلز · فید روشن' : mediaType === 'carousel' ? 'آلبوم' : 'پست'}</span></div>
        <label className="compose-field-title" htmlFor="compose-caption">کپشن اصلی <span>{form.caption.length} / 2200</span></label>
        <textarea className="input compose-caption" id="compose-caption" maxLength={2200}
          value={form.caption} placeholder="داستان این پست را بنویسید…"
          onChange={(e) => setForm((old) => ({ ...old, caption: e.target.value }))} />
        {eligibleForComment && <div className="compose-feature">
          <div><strong>↪ انتقال کپشن به اولین کامنت</strong><p>متن اصلی به firstComment می‌رود؛ کپشن روی پست می‌تواند خالی یا کوتاه باشد.</p></div>
          <input type="checkbox" aria-label="انتقال کپشن به اولین کامنت"
            checked={form.caption_moved_to_comment} onChange={(e) => setForm((old) => ({ ...old,
              caption_moved_to_comment: e.target.checked,
              visible_caption: e.target.checked ? old.visible_caption : '',
            }))} />
        </div>}
        {eligibleForComment && form.caption_moved_to_comment && <div className="compose-subfield">
          <label className="compose-field-title" htmlFor="compose-visible">کپشن کوتاه روی خود پست (اختیاری)</label>
          <input className="input" id="compose-visible" maxLength={150}
            placeholder="می‌تواند خالی بماند" value={form.visible_caption}
            onChange={(e) => setForm((old) => ({ ...old, visible_caption: e.target.value }))} />
          <small>درج کامنت اول به مجوز comments اتصال Zernio وابسته است.</small>
        </div>}
        <div className="compose-divider" />
        <div className="compose-feature"><div><strong>👤 تگ کردن افراد روی رسانه</strong>
          <p>عکس: لمس تصویر برای انتخاب نقطه؛ کاروسل: ابتدا اسلاید را انتخاب کنید.</p></div>
          {current?.kind === 'image' && <button className="compose-text-btn" type="button"
            onClick={() => { setTagMode(!tagMode); setPendingPoint(null) }}>
            {tagMode ? 'بستن' : '＋ تگ'}</button>}</div>
        {current?.kind === 'video' && <p className="compose-hint">برای ویدیو، تگ نام کاربری ثبت می‌شود و مختصات عکس ندارد.</p>}
        {(tagMode || current?.kind === 'video' || pendingPoint) && current && <div className="compose-inline-editor">
          {current.kind === 'image' && <span>{pendingPoint ? `نقطه: X ${pendingPoint.x} · Y ${pendingPoint.y}` : 'نقطه را روی عکس انتخاب کنید.'}</span>}
          <div className="compose-inline-row"><input className="input" dir="ltr" value={tagName}
            placeholder="username@" onChange={(e) => setTagName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addTag())} />
            <button className="btn btn-sm" type="button" onClick={addTag}>ثبت تگ</button></div>
        </div>}
        {formTags.length > 0 && <div className="compose-chips">{formTags.map((tag, index) => <button
          type="button" key={`${tag.username}-${index}`} onClick={() => setForm((old) => ({ ...old,
            userTags: old.userTags.filter((item) => item !== tag) }))}>@{tag.username} ×</button>)}</div>}
        <div className="compose-divider" />
        <div className="compose-feature"><div><strong>🤝 دعوت همکار</strong>
          <p>با تگ کاربر متفاوت است؛ دعوت برای حساب عمومی حرفه‌ای ارسال می‌شود.</p></div></div>
        <div className="compose-inline-row"><input className="input" dir="ltr"
          placeholder="instagram_username" value={collabName}
          onChange={(e) => setCollabName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addCollab())} />
          <button className="btn ghost btn-sm" type="button" onClick={addCollab}>افزودن</button></div>
        {form.collaborators.length > 0 && <div className="compose-chips">{form.collaborators.map((name) =>
          <button key={name} type="button" onClick={() => setForm((old) => ({ ...old,
            collaborators: old.collaborators.filter((item) => item !== name) }))}>@{name} ×</button>)}</div>}
      </section>}

      {mode === 'post' && draft?.status !== 'queued' && <section className="compose-panel">
        <div className="compose-section-head"><div><span className="compose-step">03</span><h3>زمان انتشار</h3></div>
          <span className="compose-muted">تهران · هجری شمسی</span></div>
        <div className="compose-schedule-modes">
          <button type="button" className={!form.scheduled ? 'active' : ''}
            onClick={() => setForm((old) => ({ ...old, scheduled: null }))}>در اولین نوبت صف</button>
          <button type="button" className={form.scheduled ? 'active' : ''}
            onClick={() => setForm((old) => ({ ...old, scheduled: old.scheduled || tehranChoice() }))}>
            تقویم گرافیکی شمسی</button>
        </div>
        {form.scheduled ? <ComposerJalaliPicker value={form.scheduled}
          onChange={(scheduled) => setForm((old) => ({ ...old, scheduled }))} /> :
          <p className="compose-hint">افزودن به صف، انتشار فوری را تضمین نمی‌کند؛ Worker پست هر 5 دقیقه با محدودیت نرخ آن را بررسی می‌کند.</p>}
      </section>}

      {mode === 'story' && media.length > 0 && <section className="compose-panel compose-story-note">
        <div className="compose-section-head"><div><span className="compose-step">02</span><h3>ترتیب استوری‌ها</h3></div></div>
        <p>پس از زدن «پایان و صف»، اسلاید اول برای نوبت بعدی Worker آماده می‌شود.
          هر اسلاید بعدی فقط پس از تأیید انتشار قبلی با فاصلهٔ تصادفی 5 تا 12 دقیقه آزاد می‌شود.</p>
        <p className="compose-hint">زمان آپلود جزو این فاصلهٔ عمدی نیست و تا پیش از پایان و صف، هیچ استوری‌ای صف نمی‌شود.</p>
      </section>}

      {notice && <div className={'compose-message' + (notice.startsWith('⚠️') ? ' error' : '')}
        role="status">{notice}</div>}
      {draft?.status === 'queued' ? <div className="compose-finish compose-complete">
        <strong>✓ درخواست ثبت شد</strong>
        <p>شناسه‌های صف: {(draft.queue_ids || []).join('، ')}</p>
        <p>پیش‌نویس و تاریخچه محفوظ است؛ انتشار فقط توسط Worker مربوط انجام می‌شود.</p>
        <button className="btn" type="button" onClick={() => { setDraft(null); setForm(BLANK);
          lastSavedRef.current = JSON.stringify(BLANK); setNotice('') }}>ساخت محتوای جدید</button>
      </div> : <div className="compose-finish">
        <div><strong>آمادهٔ ثبت درخواست؟</strong>
          <p>تا قبل از این دکمه هیچ پست یا استوری‌ای به صف انتشار وارد نمی‌شود.</p></div>
        <button className="compose-primary" type="button" disabled={!ready || busy || !draft}
          onClick={finish}>{busy ? 'لطفاً صبر کنید…' : mode === 'story' ? '🏁 پایان و صف' :
            form.scheduled ? '⏰ زمان‌بندی و افزودن به صف' : '🚀 تأیید نهایی؛ افزودن به صف'}</button>
        {draft && <button className="compose-cancel" type="button" disabled={busy} onClick={cancel}>لغو پیش‌نویس</button>}
      </div>}
    </>}
  </div>
}
