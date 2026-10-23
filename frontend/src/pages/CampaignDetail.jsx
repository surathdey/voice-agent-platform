import React, { useEffect, useState } from 'react'
import { api } from '../api'
import LeadsUpload from './LeadsUpload'

/* Campaign page — opened from the Campaigns list. Shows the campaign's
   business settings, its schedule/retry configuration, and ITS leads only.
   A5 adds dispatch: an active campaign can run the dispatcher (stub
   transport in this build — simulated calls, real result records) and
   shows attempt counts by state. A10 adds the RESULTS card below the
   dispatch card: counts per disposition (buckets from the agent's own
   prompt), attempts made, connect rate and leads by state — read from
   GET /api/campaigns/{id}/results, the same §8.2 records the Calls
   page and the Dashboard aggregate. The results spreadsheet export
   arrives with the Phase B agent builds. */
const STATUS_PILL = { draft: 'navy', active: 'teal', paused: 'gold', completed: 'slate' }
const STATUS_LABEL = { draft: 'Draft', active: 'Running', paused: 'Paused', completed: 'Completed' }
const LEAD_PILL = { pending: 'gold', attempted: 'teal', exhausted: 'red' }

export default function CampaignDetail({ campaignId, onBack }) {
  const [campaign, setCampaign] = useState(null)
  const [leads, setLeads] = useState(null)
  const [calls, setCalls] = useState(null)
  const [results, setResults] = useState(null)
  const [dispatchMsg, setDispatchMsg] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showUpload, setShowUpload] = useState(false)

  async function load() {
    try {
      const c = await api(`/api/campaigns/${campaignId}`)
      setCampaign(c)
      const l = await api(`/api/campaigns/${campaignId}/leads`)
      setLeads(l)
      const cl = await api(`/api/campaigns/${campaignId}/calls`)
      setCalls(cl)
      const rs = await api(`/api/campaigns/${campaignId}/results`)
      setResults(rs)
    } catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [campaignId])

  async function runDispatch() {
    setBusy(true); setError(''); setDispatchMsg('')
    try {
      const r = await api(`/api/campaigns/${campaignId}/dispatch`, { method: 'POST' })
      setDispatchMsg(r.message || `Dispatched ${r.created} attempt(s).`)
      await load()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

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

      <div className="card" style={{ marginBottom: 20 }}>
        <h3 className="section-title">Dispatch</h3>
        <div style={{ display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap' }}>
          <button className="btn" disabled={busy || campaign.status !== 'active'}
            onClick={runDispatch}>
            Run dispatch
          </button>
          {campaign.status !== 'active' && (
            <span style={{ color: 'var(--slate)', fontSize: 13.5 }}>
              Dispatch runs on an active campaign — launch it first.
            </span>
          )}
          {calls && calls.total > 0 && (
            <span style={{ display: 'inline-flex', gap: 8, flexWrap: 'wrap' }}>
              {Object.entries(calls.counts_by_state || {}).map(([state, n]) => (
                <span key={state} className="pill slate">{state} · {n}</span>
              ))}
            </span>
          )}
          {calls && calls.total === 0 && campaign.status === 'active' && (
            <span style={{ color: 'var(--slate)', fontSize: 13.5 }}>No attempts yet.</span>
          )}
        </div>
        {dispatchMsg && <p style={{ marginTop: 10, marginBottom: 0 }}>{dispatchMsg}</p>}
        <p className="footnote" style={{ marginTop: 10 }}>
          Attempts are admitted per the schedule — window, calling days and daily
          cap, all in the tenant's market time — and leads whose last attempt
          ended no-answer, busy or failed are re-queued for another attempt
          after the retry gap, up to the campaign's attempt limit. The transport is
          decided by the platform mode: in stub mode calls are simulated end-to-end and
          land as §8.2 result records immediately; with a live mode configured
          (TELEPHONY_MODE + the tenant's provider), calls are placed for real and their
          results land as the provider reports them. The dispatch message above always
          says which transport ran — see Calls for the list.
        </p>
      </div>

      {results && results.results_count > 0 && (
        <div className="card" style={{ marginBottom: 20 }}>
          <h3 className="section-title">Results</h3>
          <div className="stat-grid" style={{ marginBottom: 14 }}>
            <div className="stat-card">
              <div className="n">{results.attempts_made}</div>
              <div className="l">Attempts made</div>
            </div>
            <div className="stat-card">
              <div className="n">{results.connected}</div>
              <div className="l">Connected</div>
            </div>
            <div className="stat-card">
              <div className="n">{Math.round(results.connect_rate * 100)}%</div>
              <div className="l">Connect rate</div>
            </div>
            <div className="stat-card">
              <div className="n" style={results.review_required ? { color: 'var(--danger)' } : undefined}>
                {results.review_required}
              </div>
              <div className="l">Needs review</div>
            </div>
          </div>
          {(() => {
            const rows = Object.entries(results.counts_by_disposition || {})
              .sort((a, b) => b[1] - a[1])
            const max = Math.max(1, ...rows.map(([, n]) => n))
            return rows.map(([name, n]) => (
              <div className="bar-row" key={name}>
                <span className="bar-name">{name}</span>
                <span className="bar-track">
                  <span className={`bar-fill${n === 0 ? ' empty' : ''}`}
                    style={{ width: `${(n / max) * 100}%` }} />
                </span>
                <span className="num">{n}</span>
              </div>
            ))
          })()}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 14, alignItems: 'center' }}>
            <span style={{ color: 'var(--slate)', fontSize: 13 }}>Leads:</span>
            {Object.entries(results.leads_by_state || {}).map(([st, n]) => (
              <span key={st} className={`pill ${LEAD_PILL[st] || 'slate'}`}>{st} · {n}</span>
            ))}
            {results.awaiting_retry > 0 && (
              <span style={{ color: 'var(--slate)', fontSize: 13 }}>
                {results.awaiting_retry} awaiting their retry gap
              </span>
            )}
          </div>
          <p className="footnote" style={{ marginTop: 12 }}>
            Disposition buckets come from the agent's own prompt — its
            DISPOSITIONS section is the single source of truth, so editing
            the prompt changes what is counted here.
          </p>
        </div>
      )}

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
              <th>Status</th><th>Next attempt</th><th className="num">Attempts</th></tr>
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
                <td style={{ whiteSpace: 'nowrap', fontSize: 13 }}>
                  {l.next_attempt_at
                    ? new Date(l.next_attempt_at + (l.next_attempt_at.endsWith('Z') ? '' : 'Z')).toLocaleString()
                    : '—'}
                </td>
                <td className="num">{l.attempt_count}</td>
              </tr>
            ))}
            {leads && leads.total === 0 && (
              <tr><td colSpan={9} style={{ color: 'var(--slate)' }}>
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
