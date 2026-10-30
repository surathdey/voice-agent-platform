import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Tenants — Super Admin only (Epic C). List tenants, provision a new
   one, and edit an existing one. Creation is invite-based: the form
   takes the first Tenant Admin's name + email, the account is created
   pending setup, and the invite result (setup link + OTP, for manual
   forwarding — email delivery is log-mode until a provider is wired)
   is shown here, super_admin only. Tenant cards show the first
   Tenant Admin's invite status with a Resend action for pending
   invites. The "Platform" scope row is not a customer tenant and is
   not editable. A tenant's slug/tenant_id is identity (box name,
   database name, subdomain routing) and is never edited here; phone
   numbers stay tenant-side (tenant Settings). Invite tokens, links
   and OTPs never render in any tenant-visible view. */
export default function Tenants() {
  const [tenants, setTenants] = useState([])
  const [invites, setInvites] = useState([])
  const [inviteResult, setInviteResult] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({
    name: '', market: 'IN', timezone: '',
    provider_mode_default: 'managed', concurrency_limit: 20,
    admin_name: '', admin_email: '',
  })
  const [editing, setEditing] = useState(null)
  const [editForm, setEditForm] = useState({
    name: '', market: 'IN', timezone: '',
    provider_mode_default: 'managed', concurrency_limit: 20,
  })

  async function load() {
    try { setTenants(await api('/api/tenants')) }
    catch (e) { setError(e.message) }
    try { setInvites(await api('/api/users/invites')) }
    catch { setInvites([]) }
  }
  useEffect(() => { load() }, [])

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))
  const setEdit = (k, v) => setEditForm((f) => ({ ...f, [k]: v }))

  async function submit(e) {
    e.preventDefault()
    setError(''); setNotice(''); setInviteResult(null); setBusy(true)
    try {
      const adminEmail = form.admin_email.trim()
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(adminEmail)) {
        throw new Error('Tenant Admin email must be a valid email address.')
      }
      const body = {
        name: form.name.trim(),
        market: form.market.trim() || 'IN',
        timezone: form.timezone.trim(),
        provider_mode_default: form.provider_mode_default.trim() || 'managed',
        concurrency_limit: parseInt(form.concurrency_limit, 10) || 20,
        from_numbers: [],
        admin_name: form.admin_name.trim(),
        admin_email: adminEmail,
      }
      const data = await api('/api/tenants', { method: 'POST', body })
      setNotice(`Tenant “${data.tenant.name}” created.`)
      setInviteResult({
        email: data.admin_email, name: data.admin_name || form.admin_name.trim(),
        invite_id: data.invite_id, setup_url: data.setup_url,
        dev_otp: data.dev_otp || null, status: 'pending',
      })
      setForm({ name: '', market: 'IN', timezone: '', provider_mode_default: 'managed', concurrency_limit: 20, admin_name: '', admin_email: '' })
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function resendInvite(inviteId) {
    setError(''); setNotice(''); setInviteResult(null); setBusy(true)
    try {
      const data = await api(`/api/users/invites/${inviteId}/resend`, { method: 'POST', body: {} })
      setNotice(`Invite resent to ${data.email} — the old link and OTP no longer work.`)
      setInviteResult({
        email: data.email, name: data.name,
        invite_id: data.invite_id, setup_url: data.setup_url,
        dev_otp: data.dev_otp || null, status: 'pending',
      })
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  // Latest Tenant Admin invite per tenant (the invites list arrives
  // newest-first, so the first tenant_admin match is the current one).
  const adminInviteFor = (tenantId) =>
    invites.find((v) => v.tenant_id === tenantId && v.role === 'tenant_admin') || null

  function openEdit(t) {
    setEditing(t)
    setEditForm({
      name: t.name, market: t.market, timezone: t.timezone,
      provider_mode_default: t.provider_mode_default, concurrency_limit: t.concurrency_limit,
    })
    setError(''); setNotice('')
  }

  function cancelEdit() {
    setEditing(null)
    setError('')
  }

  async function saveEdit(e) {
    e.preventDefault()
    setError(''); setNotice(''); setBusy(true)
    try {
      const body = {
        name: editForm.name.trim(),
        market: editForm.market,
        timezone: editForm.timezone.trim(),
        provider_mode_default: editForm.provider_mode_default,
        concurrency_limit: parseInt(editForm.concurrency_limit, 10) || 20,
      }
      const data = await api(`/api/tenants/${editing.tenant_id}`, { method: 'PATCH', body })
      setNotice(`Tenant “${data.name}” updated.`)
      setEditing(null)
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  const isPlatform = (t) => t.name === 'Platform'
  const modeChanged = editing && editForm.provider_mode_default !== editing.provider_mode_default

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Tenants</h1>
          <div className="sub">
            Every tenant on the platform — metadata only, never customer data.
            Creating a tenant provisions its first Tenant Admin by invite.
            Click a tenant to edit it.
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
          <div className="field"><label>Tenant Admin name *</label>
            <input value={form.admin_name} onChange={(e) => set('admin_name', e.target.value)} required /></div>
          <div className="field"><label>Tenant Admin email *</label>
            <input type="email" value={form.admin_email} onChange={(e) => set('admin_email', e.target.value)} required /></div>
        </div>
        <button className="btn" style={{ marginTop: 14 }} disabled={busy || !form.name.trim() || !form.admin_name.trim() || !form.admin_email.trim()}>
          {busy ? 'Creating…' : 'Create tenant'}</button>
      </form>

      {inviteResult && (
        <div className="card" style={{ marginTop: 18 }}>
          <h3 style={{ marginTop: 0 }}>Tenant Admin invite — {inviteResult.name} ({inviteResult.email})</h3>
          <p style={{ color: 'var(--slate)' }}>
            Status: {inviteResult.status}. Email delivery is not wired yet (log-mode) —
            forward the setup link below to the new admin yourself. No password was generated;
            they set their own at setup.
          </p>
          <div className="field"><label>Setup link (forward this)</label>
            <input readOnly value={`${window.location.origin}${inviteResult.setup_url}`} onFocus={(e) => e.target.select()} /></div>
          {inviteResult.dev_otp
            ? <p style={{ marginTop: 10 }}>OTP for manual forwarding: <b>{inviteResult.dev_otp}</b></p>
            : <p style={{ color: 'var(--slate)', marginTop: 10 }}>
                The OTP travels over the invite channel (currently log-mode). On this test box it
                is shown here when the INVITE_DEV_OTP test gate is enabled.</p>}
        </div>
      )}

      {editing && (
        <div>
          <h3 className="section-title">Edit tenant: {editing.name}</h3>
          <form onSubmit={saveEdit}>
            <div className="form-grid">
              <div className="field"><label>Name *</label>
                <input value={editForm.name} onChange={(e) => setEdit('name', e.target.value)} required /></div>
              <div className="field"><label>Market</label>
                <select value={editForm.market} onChange={(e) => setEdit('market', e.target.value)}>
                  <option value="IN">IN</option>
                  <option value="NA">NA</option>
                </select></div>
              <div className="field"><label>Timezone (blank = derive from market)</label>
                <input value={editForm.timezone} onChange={(e) => setEdit('timezone', e.target.value)} placeholder="e.g. Asia/Kolkata" /></div>
              <div className="field"><label>Provider mode default</label>
                <select value={editForm.provider_mode_default} onChange={(e) => setEdit('provider_mode_default', e.target.value)}>
                  <option value="managed">managed</option>
                  <option value="self_hosted">self_hosted</option>
                </select></div>
              <div className="field"><label>Concurrency limit</label>
                <input type="number" value={editForm.concurrency_limit} onChange={(e) => setEdit('concurrency_limit', e.target.value)} /></div>
            </div>
            {modeChanged && (
              <div className="card" style={{ marginTop: 14 }}>
                Note: changing mode does NOT re-adopt or change any adopted version automatically —
                adopted versions stay pinned. The tenant must adopt catalog versions matching the new mode.
              </div>
            )}
            <div style={{ marginTop: 14, display: 'flex', gap: 10 }}>
              <button className="btn" disabled={busy || !editForm.name.trim()}>
                {busy ? 'Saving…' : 'Save changes'}</button>
              <button type="button" className="btn secondary" onClick={cancelEdit}>Cancel</button>
            </div>
          </form>
        </div>
      )}

      <h3 className="section-title">Tenant list</h3>
      {tenants.length === 0 && !error && <p style={{ color: 'var(--slate)' }}>No tenants yet.</p>}
      <div className="grid">
        {tenants.map((t) => {
          const inv = isPlatform(t) ? null : adminInviteFor(t.tenant_id)
          return (
          <div
            key={t.tenant_id}
            className="card"
            onClick={() => { if (!isPlatform(t)) openEdit(t) }}
            style={isPlatform(t) ? {} : { cursor: 'pointer' }}
          >
            <h3>{t.name}</h3>
            <dl className="kv">
              <dt>Market</dt><dd>{t.market}</dd>
              <dt>Timezone</dt><dd>{t.timezone}</dd>
              <dt>Mode</dt><dd>{t.provider_mode_default}</dd>
              <dt>Concurrency</dt><dd>{t.concurrency_limit}</dd>
              <dt>Retention</dt><dd>{t.recording_retention_days} days</dd>
              {inv && (<><dt>Admin invite</dt><dd>{inv.email} — {inv.status}</dd></>)}
            </dl>
            {isPlatform(t)
              ? <p style={{ color: 'var(--slate)', marginTop: 10 }}>Platform scope — not editable.</p>
              : <div style={{ marginTop: 10, display: 'flex', gap: 10 }}>
                  <button
                    className="btn small secondary"
                    onClick={(e) => { e.stopPropagation(); openEdit(t) }}
                  >Edit</button>
                  {inv && (inv.status === 'pending' || inv.status === 'expired') && (
                    <button
                      className="btn small secondary"
                      disabled={busy}
                      onClick={(e) => { e.stopPropagation(); resendInvite(inv.invite_id) }}
                    >{inv.status === 'expired' ? 'Resend invite (expired — renew)' : 'Resend invite'}</button>
                  )}
                </div>}
          </div>
          )
        })}
      </div>
    </div>
  )
}
