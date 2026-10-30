import React, { useState } from 'react'
import { api, setSession } from '../api'

/* Public account setup (Epic C invite flow): the invitee arrives via
   the setup link (/setup?invite=...&token=...), confirms the OTP from
   the invite channel, and sets their own password. No login required.
   The backend verifies the single-use link token + OTP against
   salted hashes and enforces the 24h expiry; failures surface as one
   indistinguishable 401 ("Invalid or expired invite"). There is no
   password policy in the codebase (security.py is hashing only) —
   this form enforces non-empty + confirmation match, nothing more. */
export default function Setup({ inviteId, token, onDone, onBack }) {
  const [otp, setOtp] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setError('')
    if (!otp.trim()) { setError('Enter the OTP from your invite.'); return }
    if (!password) { setError('Choose a password.'); return }
    if (password !== confirm) { setError('Passwords do not match.'); return }
    setBusy(true)
    try {
      const data = await api('/api/auth/setup', {
        method: 'POST',
        body: { invite_id: inviteId, token, otp: otp.trim(), password },
      })
      setSession(data)
      onDone({ ...data.user, tenant_name: data.tenant_name })
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <h1>Set up your account</h1>
        <div className="sub">Confirm the OTP from your invite, then choose your own password.</div>
        {error && <div className="error">{error}</div>}
        <form onSubmit={submit}>
          <div className="field"><label>OTP</label>
            <input value={otp} onChange={(e) => setOtp(e.target.value)} autoComplete="one-time-code" /></div>
          <div className="field" style={{ marginTop: 12 }}><label>New password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" /></div>
          <div className="field" style={{ marginTop: 12 }}><label>Confirm password</label>
            <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" /></div>
          <button className="btn" style={{ width: '100%', marginTop: 18 }} disabled={busy}>
            {busy ? 'Setting up…' : 'Set password and sign in'}</button>
        </form>
        <p className="footnote" style={{ marginTop: 16 }}>
          <button className="btn small secondary" onClick={onBack}>Back to sign in</button>
        </p>
      </div>
    </div>
  )
}
