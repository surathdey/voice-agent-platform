import React, { useEffect, useState } from 'react'
import { api, getToken } from '../api'
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
   (GET /api/campaigns/{id}/export) downloads from the page head.
   Clicking a lead row opens a right-hand drawer with that lead's calls:
   the selected call's recording player and full transcript. */
const STATUS_PILL = { draft: 'navy', active: 'teal', paused: 'gold', completed: 'slate' }
const STATUS_LABEL = { draft: 'Draft', active: 'Running', paused: 'Paused', completed: 'Completed' }
const LEAD_PILL = { pending: 'gold', done: 'teal', exhausted: 'red' }

/* Recording player: an <audio> tag cannot send the SPA's Bearer token,
   so a plain src to the JWT-protected recording proxy 401s and the
   player sits at 0:00. Fetch the audio WITH the token, then play the
   blob — same proxy (S3 first, Vapi fallback), no auth weakening. */
function RecordingPlayer({ sessionId }) {
  const [url, setUrl] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => {
    let live = true
    let objectUrl = ''
    setUrl('')
    setErr('')
    fetch(`/api/calls/${sessionId}/recording`, {
      headers: { ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}) },
    }).then(async (res) => {
      if (!res.ok) {
        if (live) setErr(`Recording could not be loaded (${res.status}).`)
        return
      }
      const blob = await res.blob()
      if (!live) return
      objectUrl = URL.createObjectURL(blob)
      setUrl(objectUrl)
    }).catch(() => { if (live) setErr('Recording could not be loaded.') })
    return () => { live = false; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [sessionId])
  if (err) return <p style={{ color: 'var(--red)' }}>{err}</p>
  if (!url) return <p style={{ color: 'var(--slate)' }}>Loading recording…</p>
  return (
    <>
      <audio controls src={url} style={{ width: '100%' }} />
      <div style={{ marginTop: 6 }}>
        <a href={url} download={`${sessionId}.wav`}>Download recording</a>
      </div>
    </>
  )
}

export default function CampaignDetail({ campaignId, onBack }) {
  const [campaign, setCampaign] = useState(null)
  const [leads, setLeads] = useState(null)
  const [calls, setCalls] = useState(null)
  const [results, setResults] = useState(null)
  const [dispatchMsg, setDispatchMsg] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showUpload, setShowUpload] = useState(false)
  const [selectedLead, setSelectedLead] = useState(null)
  const [selectedCallId, setSelectedCallId] = useState(null)

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
    if (!window.confirm('Delete this campaign, all its leads and its call records?')) return
    setBusy(true)
    try { await api(`/api/campaigns/${campaignId}`, { method: 'DELETE' }); onBack() }
    catch (e) { setError(e.message); setBusy(false) }
  }

  async function exportSheet() {
    setBusy(true); setError('')
    try {
      const res = await fetch(`/api/campaigns/${campaignId}/export`, {
        headers: { ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}) },
      })
      if (!res.ok) throw new Error(`Export failed (${res.status})`)
      const blob = await res.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${(campaign.name || 'campaign').replace(/[^A-Za-z0-9._-]+/g, '_')}_export.xlsx`
      document.body.appendChild(a); a.click(); a.remove()
      window.URL.revokeObjectURL(url)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
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
            {campaign.settlement_authority_pct != null ? ` · Authority cap: ${campaign.settlement_authority_pct}%` : ''}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span className={`pill ${STATUS_PILL[campaign.status]}`}>{STATUS_LABEL[campaign.status] || campaign.status}</span>
          {campaign.status === 'draft' && <button className="btn small" disabled={busy} onClick={() => setStatus('active')}>Launch</button>}
          {campaign.status === 'active' && <button className="btn small secondary" disabled={busy} onClick={() => setStatus('paused')}>Pause</button>}
          {campaign.status === 'paused' && <button className="btn small" disabled={busy} onClick={() => setStatus('active')}>Resume</button>}
          {(campaign.status === 'active' || campaign.status === 'paused') &&
            <button className="btn small secondary" disabled={busy} onClick={() => setStatus('completed')}>Complete</button>}
          <button className="btn small secondary" disabled={busy} onClick={exportSheet}>Export spreadsheet</button>
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
          {(results.costs || []).length > 0 && (
            <div style={{ marginTop: 14 }}>
              {results.costs.map((e) => (
                <div key={e.currency} style={{ fontSize: 13.5, margin: '2px 0' }}>
                  <b>Cost · {e.currency}:</b> total {e.total} over {e.calls} costed call(s)
                  {e.connected_calls > 0 && (
                    <> — <b>{e.cost_per_connected}</b> per connected call</>
                  )}
                </div>
              ))}
              <div style={{ color: 'var(--slate)', fontSize: 12.5, marginTop: 4 }}>
                From each result's own cost breakdown — the provider's reported
                figures, or computed from this tenant's rate card (Settings →
                Providers). Stub runs are excluded: simulated money is not money.
              </div>
            </div>
          )}
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
            <dt>Pinned prompt</dt>
            <dd>
              {campaign.pinned_prompt_version
                ? `version ${campaign.pinned_prompt_version}`
                : 'live prompt (pre-C2 campaign)'}
              {campaign.pinned_catalog_agent
                ? ` · catalog ${campaign.pinned_catalog_agent} v${campaign.pinned_catalog_version}`
                : ''}
              {' — kept for life; a later adoption never changes this campaign'}
            </dd>
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
              <tr key={l.lead_id} onClick={() => { setSelectedLead(l); setSelectedCallId(null) }}
                title="Click to view this lead's calls, recording and transcript"
                style={{ cursor: 'pointer' }}>
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

      {selectedLead && (() => {
        const leadCalls = (calls?.calls || [])
          .filter((c) => c.lead_id === selectedLead.lead_id)
          .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''))
        const selectedCall = leadCalls.find((c) => c.session_id === selectedCallId) || leadCalls[0]
        const cost = selectedCall?.cost_breakdown || {}
        return (
          <>
            <div onClick={() => setSelectedLead(null)}
              style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.3)', zIndex: 40 }} />
            <div style={{ position: 'fixed', top: 0, right: 0, bottom: 0, width: 'min(460px, 94vw)',
              background: '#fff', zIndex: 41, overflowY: 'auto', padding: '18px 20px',
              boxShadow: '-4px 0 24px rgba(0,0,0,0.2)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h2 style={{ fontSize: 19, margin: 0 }}>{selectedLead.name || '—'}</h2>
                <button className="btn small secondary" onClick={() => setSelectedLead(null)}>Close</button>
              </div>
              <div className="sub" style={{ margin: '6px 0 14px' }}>
                {selectedLead.phone_number} · {selectedLead.bank_name || '—'} · {selectedLead.state || '—'}
              </div>
              <dl className="kv" style={{ gridTemplateColumns: '130px 1fr' }}>
                <dt>Identifier</dt><dd>{selectedLead.agent_fields?.product_identifier || '—'}</dd>
                <dt>Status</dt><dd>{selectedLead.status}</dd>
                <dt>Attempts</dt><dd>{selectedLead.attempt_count}</dd>
                <dt>Next attempt</dt><dd>{selectedLead.next_attempt_at
                  ? new Date(selectedLead.next_attempt_at + (selectedLead.next_attempt_at.endsWith('Z') ? '' : 'Z')).toLocaleString()
                  : '—'}</dd>
              </dl>

              <h3 className="section-title">Calls — {leadCalls.length}</h3>
              {leadCalls.length === 0 && <p style={{ color: 'var(--slate)' }}>No calls for this lead yet.</p>}
              {leadCalls.map((c) => (
                <button key={c.session_id} className="btn small secondary"
                  style={{ display: 'block', width: '100%', textAlign: 'left', marginBottom: 6,
                    fontWeight: selectedCall?.session_id === c.session_id ? 'bold' : 'normal' }}
                  onClick={() => setSelectedCallId(c.session_id)}>
                  {c.started_at ? new Date(c.started_at + (c.started_at.endsWith('Z') ? '' : 'Z')).toLocaleString() : '—'}
                  {' · '}{c.state}{c.disposition ? ` · ${c.disposition}` : ''}{c.duration_s ? ` · ${c.duration_s}s` : ''}
                </button>
              ))}

              {selectedCall && (
                <>
                  <h3 className="section-title">Recording</h3>
                  {selectedCall.recording_url ? (
                    <RecordingPlayer sessionId={selectedCall.session_id} />
                  ) : (
                    <p style={{ color: 'var(--slate)' }}>No recording for this call.</p>
                  )}
                  <div style={{ fontSize: 13.5, marginTop: 12 }}>
                    <div><b>State:</b> {selectedCall.state}</div>
                    <div><b>Disposition:</b> {selectedCall.disposition || '—'}</div>
                    <div><b>Duration:</b> {selectedCall.duration_s}s</div>
                    {cost.total != null && (
                      <div>
                        <div><b>Cost (Vapi):</b> {cost.total} {cost.currency || 'USD'}</div>
                        <div style={{ fontSize: 13, color: 'var(--slate)', marginTop: 2 }}>
                          telephony {cost.telephony ?? 0} · STT {cost.stt ?? 0} ·{' '}
                          LLM {cost.llm ?? 0} · TTS {cost.tts ?? 0}
                          {cost.mode === 'stub' && ' (simulated — not real money)'}
                          {cost.cost_source === 'provider' && ' (Vapi-reported)'}
                          {cost.cost_source === 'rate_card' && ' (rate card)'}
                        </div>
                        {cost.twilio && cost.twilio.status === 'completed' && (
                          <div style={{ fontSize: 13.5, marginTop: 4 }}>
                            <div><b>Twilio:</b> {cost.twilio.cost} {cost.twilio.currency || 'USD'}</div>
                            <div><b>Total (all-in):</b> {cost.total_all_in ?? cost.total} {cost.currency || 'USD'}</div>
                          </div>
                        )}
                        {cost.twilio && cost.twilio.status === 'pending' && (
                          <div style={{ fontSize: 13, color: 'var(--slate)', marginTop: 4 }}>
                            Twilio cost pending — it lands shortly after the call ends.
                          </div>
                        )}
                        {cost.twilio && cost.twilio.status === 'unavailable' && (
                          <div style={{ fontSize: 13, color: 'var(--slate)', marginTop: 4 }}>
                            Twilio cost unavailable (credentials not configured).
                          </div>
                        )}
                        {cost.basis && Object.entries(cost.basis).map(([leg, note]) => (
                          <div key={leg} style={{ fontSize: 12.5, color: 'var(--slate)' }}>
                            {leg}: {note}
                          </div>
                        ))}
                      </div>
                    )}
                    {selectedCall.summary && <div style={{ marginTop: 8 }}>{selectedCall.summary}</div>}
                  </div>
                  <h3 className="section-title">Transcript</h3>
                  {(selectedCall.transcript || []).length === 0 && (
                    <p style={{ color: 'var(--slate)' }}>No transcript for this call.</p>
                  )}
                  {(selectedCall.transcript || []).map((t, i) => (
                    <div key={i} style={{ marginBottom: 8, fontSize: 14 }}>
                      <span className={`pill ${t.speaker === 'agent' ? 'teal' : 'slate'}`}>
                        {t.speaker === 'agent' ? 'AGENT' : 'CUSTOMER'}
                      </span>{' '}
                      <span>{t.text}</span>
                    </div>
                  ))}
                </>
              )}
            </div>
          </>
        )
      })()}
    </div>
  )
}
