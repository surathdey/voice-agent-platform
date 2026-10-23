import React, { useEffect, useState } from 'react'
import { api } from '../api'
import LeadsUpload from './LeadsUpload'

/* Campaign page — opened from the Campaigns list. Shows the campaign's
   business settings, its schedule/retry configuration, and ITS leads only.
   (Live calling outcomes, cost and the results spreadsheet arrive with the
   dispatch/results chunk — A10.) */
const STATUS_PILL = { draft: 'navy', active: 'teal', paused: 'gold', completed: 'slate' }
const STATUS_LABEL = { draft: 'Draft', active: 'Running', paused: 'Paused', completed: 'Completed' }

export default function CampaignDetail({ campaignId, onBack }) {
  const [campaign, setCampaign] = useState(null)
  const [leads, setLeads] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showUpload, setShowUpload] = useState(false)

  async function load() {
    try {
      const c = await api(`/api/campaigns/${campaignId}`)
      setCampaign(c)
      const l = await api(`/api/campaigns/${campaignId}/leads`)
      setLeads(l)
    } catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [campaignId])

  async function setStatus(s) {
    setBusy(true); setError('')
    try {
      setCampaign(await api(`/api/campaigns/${campaignId}/status`, { method: 'POST', body: { status: s } }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function remove() {
    if (!window.confirm('Delete this campaign and all its leads?')) return
    setBusy(true)
    try { await api(`/api/campaigns/${campaignId}`, { method: 'DELETE' }); onBack() }
    catch (e) { setError(e.message); setBusy(false) }
  }

  if (error && !campaign) return <div><div className="error">{error}</div>
    <button className="btn secondary" onClick={onBack}>← Campaigns</button></div>
  if (!campaign) return <div className="sub">Loading…</div>

  const s = campaign.schedule || {}
  const r = campaign.retry_rules || {}
  const otherFields = (lead) => Object.entries(lead.agent_fields || {})
    .filter(([k]) => k !== 'product_identifier' && k !== 'product_identifier_type')

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>{campaign.name}</h1>
          <div className="sub">
            {campaign.agent_name} · {campaign.product_label}
            {campaign.product_detail ? ` — ${campaign.product_detail}` : ''} · {campaign.channel}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span className={`pill ${STATUS_PILL[campaign.status]}`}>{STATUS_LABEL[campaign.status] || campaign.status}</span>
          {campaign.status === 'draft' && <button className="btn small" disabled={busy} onClick={() => setStatus('active')}>Launch</button>}
          {campaign.status === 'active' && <button className="btn small secondary" disabled={busy} onClick={() => setStatus('paused')}>Pause</button>}
          {campaign.status === 'paused' && <button className="btn small" disabled={busy} onClick={() => setStatus('active')}>Resume</button>}
          {(campaign.status === 'active' || campaign.status === 'paused') &&
            <button className="btn small secondary" disabled={busy} onClick={() => setStatus('completed')}>Complete</button>}
          <button className="btn small danger" disabled={busy} onClick={remove}>Delete</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {campaign.provider_flag && (
        <div className="guidance" style={{ marginBottom: 18 }}>
          <b>Stub mode:</b> {campaign.provider_flag}
        </div>
      )}

      <div style={{ display: 'flex', gap: 12, marginBottom: 18 }}>
        <button className="btn secondary" onClick={onBack}>← All campaigns</button>
      </div>

      <div className="two-col" style={{ marginBottom: 20 }}>
        <div className="card">
          <h3 className="section-title">Details</h3>
          <dl className="kv" style={{ gridTemplateColumns: '130px 1fr' }}>
            <dt>Product</dt><dd>{campaign.product_label}{campaign.product_detail ? ` — ${campaign.product_detail}` : ''}</dd>
            <dt>Identifier</dt><dd>{campaign.identifier_label} (from the lead file)</dd>
            <dt>Purpose</dt><dd>{campaign.purpose || '—'}</dd>
            <dt>Audience</dt><dd>{campaign.audience || '—'}</dd>
            <dt>Channel</dt><dd>{campaign.channel}</dd>
            <dt>Calling via</dt><dd>{campaign.provider_mode === 'managed' ? 'Managed platform (Vapi)' : 'Self-hosted stack'}</dd>
            <dt>Agent</dt><dd>{campaign.agent_name} — configured in Agents; nothing agent-level is set here</dd>
          </dl>
        </div>
        <div className="card">
          <h3 className="section-title">Schedule &amp; retries</h3>
          <dl className="kv" style={{ gridTemplateColumns: '130px 1fr' }}>
            <dt>Starts</dt><dd>{s.start_date || '—'}</dd>
            <dt>Days</dt><dd>{s.calling_days || '—'}</dd>
            <dt>Window</dt><dd>{s.window_start || '—'} – {s.window_end || '—'}</dd>
            <dt>Daily cap</dt><dd>{s.daily_cap ?? '—'}</dd>
            <dt>Attempts</dt><dd>up to {r.max_attempts ?? '—'} per lead</dd>
            <dt>Retry gap</dt><dd>{r.retry_gap_days ?? '—'} day(s){r.retry_different_time ? ', different time of day' : ''}</dd>
          </dl>
          {(campaign.routing_rules || []).length > 0 && <>
            <h3 className="section-title">Outcome routing</h3>
            <dl className="kv" style={{ gridTemplateColumns: '130px 1fr' }}>
              {campaign.routing_rules.map((rule, i) => (
                <React.Fragment key={i}><dt>{rule.disposition}</dt><dd>{rule.action}{rule.target ? ` → ${rule.target}` : ''}</dd></React.Fragment>
              ))}
            </dl>
          </>}
        </div>
      </div>

      <div className="page-head" style={{ marginBottom: 12 }}>
        <h2 style={{ fontSize: 19 }}>Leads — {leads ? leads.total : '…'}</h2>
        <button className="btn small" onClick={() => setShowUpload(!showUpload)}>
          {showUpload ? 'Close upload' : '+ Add leads (CSV)'}
        </button>
      </div>

      {showUpload && (
        <div style={{ marginBottom: 18 }}>
          <LeadsUpload campaignId={campaignId} identifierLabel={campaign.identifier_label}
            onDone={() => load()} />
        </div>
      )}

      <div className="card" style={{ padding: '6px 10px' }}>
        <table className="data">
          <thead>
            <tr><th>Name</th><th>Phone</th><th>Bank</th><th>State</th>
              <th>{campaign.identifier_label || 'Identifier'}</th><th>Other fields</th>
              <th>Status</th><th className="num">Attempts</th></tr>
          </thead>
          <tbody>
            {(leads?.leads || []).map((l) => (
              <tr key={l.lead_id}>
                <td>{l.name || '—'}</td>
                <td style={{ fontFamily: 'var(--mono)', fontSize: 13 }}>{l.phone_number}</td>
                <td>{l.bank_name || '—'}</td>
                <td>{l.state || '—'}</td>
                <td>{l.agent_fields?.product_identifier || '—'}</td>
                <td style={{ color: 'var(--slate)', fontSize: 13 }}>
                  {otherFields(l).map(([k, v]) => `${k}: ${v}`).join(' · ') || '—'}</td>
                <td>{l.status}</td>
                <td className="num">{l.attempt_count}</td>
              </tr>
            ))}
            {leads && leads.total === 0 && (
              <tr><td colSpan={8} style={{ color: 'var(--slate)' }}>
                No leads yet — add this campaign’s list with “+ Add leads (CSV)”.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="footnote">Only this campaign’s leads are shown — every campaign works its own separate list from the lender.</p>
    </div>
  )
}
