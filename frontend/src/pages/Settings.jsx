import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Provider Settings (A4) — per-tenant credentials for both orchestration
   modes. Secrets are write-only: the API returns masked forms only, so the
   form starts secret fields blank ("leave blank to keep") and never holds
   a full secret in the browser. */
const STATUS_PILL = {
  connected: 'teal', failed: 'red', stub: 'gold',
  not_configured: 'slate', configured: 'navy',
}
const STATUS_LABEL = {
  connected: 'Connected', failed: 'Failed', stub: 'Stub',
  not_configured: 'Not configured', configured: 'Configured — untested',
}
const MODE_TITLE = {
  managed: 'Managed mode',
  self_hosted: 'Self-hosted mode',
}
const MODE_SUB = {
  managed: 'The managed platform runs the call; Twilio is an optional side account.',
  self_hosted: 'Our orchestrator runs the call: one telephony provider (Plivo or Exotel) plus Sarvam for STT / LLM / TTS.',
}

/* Vapi voice-profile fields (en_ / hi_ / bn_ …) render under a per-language
   subheading in the edit form; everything else is one unlabelled group. */
const FIELD_GROUPS = [
  ['en_', 'Voice profile — English'],
  ['hi_', 'Voice profile — Hindi'],
  ['bn_', 'Voice profile — Bengali'],
  ['rate_', 'Rate card — per-minute rates for the cost breakdown'],
]
function groupFields(fields) {
  const groups = []
  let current = null
  for (const f of fields) {
    const hit = FIELD_GROUPS.find(([pre]) => f.name.startsWith(pre))
    const label = hit ? hit[1] : null
    if (!current || current.label !== label) {
      current = { label, fields: [] }
      groups.push(current)
    }
    current.fields.push(f)
  }
  return groups
}

export default function Settings() {
  const [providers, setProviders] = useState([])
  const [error, setError] = useState('')

  async function load() {
    try { setProviders(await api('/api/settings/providers')) }
    catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Settings — Providers</h1>
          <div className="sub">
            Credentials for this tenant, per orchestration mode. Tenant settings override the
            platform .env; with nothing configured a provider runs in <b>stub mode</b> and calls are simulated.
          </div>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {['managed', 'self_hosted'].map((mode) => (
        <div key={mode}>
          <h2 className="section-title">{MODE_TITLE[mode]}</h2>
          <p className="sub" style={{ marginTop: -6, marginBottom: 14 }}>{MODE_SUB[mode]}</p>
          <div className="grid" style={{ marginBottom: 10 }}>
            {providers.filter((p) => p.mode === mode).map((p) => (
              <ProviderCard key={p.provider} provider={p} onChanged={load} />
            ))}
          </div>
        </div>
      ))}

      <p className="footnote">
        Secrets are stored server-side only and are never shown again — after saving you see a
        masked form (•••• + last 4). “Test connection” makes a real, lightweight check with the
        saved credentials; without credentials it honestly reports stub mode.
      </p>
    </div>
  )
}

function ProviderCard({ provider: p, onChanged }) {
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({})
  const [enabled, setEnabled] = useState(p.enabled)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState(null)

  const hasTenantValues = p.source === 'tenant' || p.fields.some((f) => f.source === 'tenant')

  function startEdit() {
    const init = {}
    for (const f of p.fields) {
      // Pre-fill values this tenant stored AND shipped defaults (the Vapi
      // voice profiles — source "default"), so the pre-filled values are
      // visible and editable. Secrets always start blank; .env values are
      // shown as hints, never copied into the form.
      init[f.name] = !f.secret && (f.source === 'tenant' || f.source === 'default') ? (f.value || '') : ''
    }
    setForm(init)
    setEnabled(p.enabled)
    setError('')
    setEditing(true)
  }

  async function save() {
    setBusy(true); setError('')
    try {
      await api(`/api/settings/providers/${p.provider}`, {
        method: 'PUT', body: { fields: form, enabled },
      })
      setEditing(false)
      await onChanged()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function remove() {
    if (!window.confirm(`Remove this tenant's ${p.label} settings? The platform .env / stub fallback will apply.`)) return
    setBusy(true); setError('')
    try {
      await api(`/api/settings/providers/${p.provider}`, { method: 'DELETE' })
      setEditing(false)
      await onChanged()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function test() {
    setBusy(true); setError(''); setTestResult(null)
    try {
      setTestResult(await api(`/api/settings/providers/${p.provider}/test`, { method: 'POST' }))
      await onChanged()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="card provider-card">
      <div className="provider-head">
        <h3>{p.label}</h3>
        <span className={`pill ${STATUS_PILL[p.status] || 'slate'}`}>{STATUS_LABEL[p.status] || p.status}</span>
      </div>
      <div className="purpose">{p.description}</div>
      <div className="status-detail">{p.status_detail}</div>

      <dl className="kv">
        {p.fields.map((f) => (
          <React.Fragment key={f.name}>
            <dt>{f.label}{f.secret ? ' 🔒' : ''}</dt>
            <dd>
              {f.is_set
                ? <span className="masked">{f.secret ? f.masked : f.value}</span>
                : <span style={{ color: 'var(--slate)' }}>Not set</span>}
              {f.source && <span className="src-tag">{f.source === 'tenant' ? 'tenant' : f.source === 'default' ? 'default' : '.env'}</span>}
            </dd>
          </React.Fragment>
        ))}
      </dl>
      {p.last_test_at && (
        <div className="last-test">
          Last test: <b>{p.last_test_status}</b> · {new Date(p.last_test_at).toLocaleString()}
          {p.last_test_message ? <div>{p.last_test_message}</div> : null}
        </div>
      )}

      {error && <div className="error" style={{ marginTop: 10 }}>{error}</div>}
      {testResult && (
        <div className={testResult.status === 'connected' ? 'ok-note' : 'error'} style={{ marginTop: 10 }}>
          <b>Test connection — {testResult.status}:</b> {testResult.message}
        </div>
      )}

      {!editing ? (
        <div className="provider-actions">
          <button className="btn small" onClick={test} disabled={busy}>Test connection</button>
          <button className="btn small secondary" onClick={startEdit} disabled={busy}>Edit</button>
          {hasTenantValues && (
            <button className="btn small danger" onClick={remove} disabled={busy}>Remove</button>
          )}
        </div>
      ) : (
        <div className="provider-form">
          {groupFields(p.fields).map((g, gi) => (
            <React.Fragment key={gi}>
              {g.label && <div className="field-group-title">{g.label}</div>}
              {g.fields.map((f) => (
                <div className="field" key={f.name}>
                  <label>{f.label}{f.required ? ' *' : ''}</label>
                  <input
                    type={f.secret ? 'password' : 'text'}
                    autoComplete="off"
                    value={form[f.name] ?? ''}
                    onChange={(e) => setForm({ ...form, [f.name]: e.target.value })}
                    placeholder={
                      f.secret
                        ? (f.is_set ? `Leave blank to keep ${f.masked || '••••'}` : 'Not set')
                        : (f.source === 'env' ? `From .env: ${f.value} — type to override` : '')
                    }
                  />
                  {f.secret && <div className="hint">Write-only — the saved value is never shown again.</div>}
                </div>
              ))}
            </React.Fragment>
          ))}
          <label className="check-row">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            Enabled for this tenant
          </label>
          <div className="provider-actions">
            <button className="btn small" onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
            <button className="btn small secondary" onClick={() => setEditing(false)} disabled={busy}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
}
