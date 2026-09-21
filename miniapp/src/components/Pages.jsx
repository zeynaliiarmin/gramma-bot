import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Pages() {
  const [me, setMe] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api('/api/me')
      .then(setMe)
      .catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="empty">⚠️ {error}</div>
  if (!me) return <div className="skeleton" style={{ height: 140 }} />

  const accounts = me.accounts || []

  return (
    <>
      <div className="card">
        <h3>👤 حساب کاربری</h3>
        <div className="list-item">
          <span style={{ fontSize: 26 }}>🪪</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 700 }}>{me.name}</div>
            <div style={{ fontSize: 11, color: 'var(--muted)' }}>
              {me.username ? '@' + me.username : 'بینام'} · شناسه {me.user_id}
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <h3>🔗 پیج‌های متصل ({me.connected_count} از {me.max_accounts})</h3>
        {accounts.length === 0 && (
          <div className="empty">
            پیجی متصل نیست. از داخل ربات، دستور /connect را بزنید.
          </div>
        )}
        {accounts.map((a) => (
          <div className="list-item" key={a.id}>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 700 }}>@{a.username}</div>
              <div style={{ fontSize: 11, color: 'var(--muted)' }}>
                {a.name} ·{' '}
                {a.token_encrypted
                  ? 'توکن رمزنگاری‌شده (AES-256) 🔒'
                  : 'بدون توکن'}
                {a.token_expires_jalali && (
                  <> · انقضا (شمسی): {a.token_expires_jalali}</>
                )}
              </div>
            </div>
            <span className={'badge ' + (a.status === 'connected' ? 'ok' : 'err')}>
              {a.status === 'connected' ? 'فعال' : a.status}
            </span>
          </div>
        ))}
        <p style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
          {me.limits ? (
            <>
              💡 حالت Development اپ متا: حداکثر{' '}
              <b>{me.limits.max_accounts_per_user}</b> پیج برای هر کاربر و{' '}
              <b>{me.limits.max_total_instagram_accounts}</b> پیج برای کل ربات؛
              حداکثر <b>{me.limits.max_total_users}</b> کاربر.
            </>
          ) : (
            '💡 حالت Development اپ متا: حداکثر 3 پیج برای هر کاربر و 24 پیج برای کل ربات.'
          )}
        </p>
      </div>
    </>
  )
}
