import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Tenants — Super Admin only (Epic C). List tenants and provision a
   new one. Creation is invite-based: the new Tenant Admin account is
   created pending setup, so no password is ever generated or shown
   here — the setup link/OTP travel through the invite channel. */
export default function Tenants() {
  const [tenants, setTenants] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({
    name: '', market: 'IN', timezone: '',
    provider_mode_default: 'managed', concurrency_limit: 20,
  })

  async function load() {
    try { setTenants(await api('/api/tenants')) }
    catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  async function submit(e) {
    e.preventDefault()
    setError(''); setNotice(''); setBusy(true)
    try {
      const body = {
        name: form.name.trim(),
        market: form.market.trim() || 'IN',
        timezone: form.timezone.trim(),
        provider_mode_default: form.provider_mode_default.trim() || 'managed',
        concurrency_limit: parseInt(form.concurrency_limit, 10) || 20,
        from_numbers: [],
      }
      const data = await api('/api/tenants', { method: 'POST', body })
      setNotice(`Tenant “${data.tenant.name}” created. Its Tenant Admin (${data.admin_email}) is invite-based and pending setup — no password was generated.`)
      setForm({ name: '', market: 'IN', timezone: '', provider_mode_default: 'managed', concurrency_limit: 20 })
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Tenants</h1>
          <div className="sub">
            Every tenant on the platform — metadata only, never customer data.
            Creating a tenant provisions its first Tenant Admin by invite.
          </div>
        </div>
        <button className="btn small secondary" onClick={load}>Refresh</button>
      </div>

      {error && <div className="error">{error}</div>}
      {notice && <div className="card" style={{ marginBottom: 18 }}>{notice}</div>}

      <h3 className="section-title">Create a tenant</h3>
      <form onSubmit={submit}>
        <div className="form-grid">
          <div className="field"><label>Name *</label>
            <input value={form.name} onChange={(e) => set('name', e.target.value)} required /></div>
          <div className="field"><label>Market</label>
            <input value={form.market} onChange={(e) => set('market', e.target.value)} /></div>
          <div className="field"><label>Timezone (blank = derive from market)</label>
            <input value={form.timezone} onChange={(e) => set('timezone', e.target.value)} placeholder="e.g. Asia/Kolkata" /></div>
          <div className="field"><label>Provider mode default</label>
            <input value={form.provider_mode_default} onChange={(e) => set('provider_mode_default', e.target.value)} /></div>
          <div className="field"><label>Concurrency limit</label>
            <input type="number" value={form.concurrency_limit} onChange={(e) => set('concurrency_limit', e.target.value)} /></div>
        </div>
        <button className="btn" style={{ marginTop: 14 }} disabled={busy || !form.name.trim()}>
          {busy ? 'Creating…' : 'Create tenant'}</button>
      </form>

      <h3 className="section-title">Tenant list</h3>
      {tenants.length === 0 && !error && <p style={{ color: 'var(--slate)' }}>No tenants yet.</p>}
      <div className="grid">
        {tenants.map((t) => (
          <div key={t.tenant_id} className="card">
            <h3>{t.name}</h3>
            <dl className="kv">
              <dt>Market</dt><dd>{t.market}</dd>
              <dt>Timezone</dt><dd>{t.timezone}</dd>
              <dt>Mode</dt><dd>{t.provider_mode_default}</dd>
              <dt>Concurrency</dt><dd>{t.concurrency_limit}</dd>
              <dt>Retention</dt><dd>{t.recording_retention_days} days</dd>
            </dl>
          </div>
        ))}
      </div>
    </div>
  )
}
