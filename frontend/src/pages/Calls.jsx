import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Calls — every call session of this tenant, across campaigns (read-only
   in A5). Each row is the §8.2 result view: state, disposition, duration,
   the mode the campaign ran in and the transport that actually carried
   the call (the stub in this build — labelled, never disguised). The
   per-call journey view (timeline + transcript) arrives with A10. */
const STATE_PILL = {
  QUEUED: 'navy', DIALING: 'gold', IN_CALL: 'gold',
  COMPLETED: 'teal', NO_ANSWER: 'slate', BUSY: 'slate',
  FAILED: 'red', VOICEMAIL: 'slate',
}

function fmtDuration(s) {
  if (!s) return '—'
  const m = Math.floor(s / 60)
  const r = s % 60
  return m ? `${m}m ${String(r).padStart(2, '0')}s` : `${r}s`
}

function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).toLocaleString()
}

export default function Calls() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  async function load() {
    try { setData(await api('/api/calls')) }
    catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  const counts = data?.counts_by_state || {}

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Calls</h1>
          <div className="sub">
            Every dispatched attempt, in the one result shape both modes produce (§8.2).
          </div>
        </div>
        <button className="btn small secondary" onClick={load}>Refresh</button>
      </div>

      {error && <div className="error">{error}</div>}

      {Object.keys(counts).length > 0 && (
        <div className="summary-badges">
          {Object.entries(counts).map(([state, n]) => (
            <span key={state} className={`pill ${STATE_PILL[state] || 'slate'}`}>
              {state} · {n}
            </span>
          ))}
        </div>
      )}

      <div className="card" style={{ padding: '6px 10px' }}>
        <table className="data">
          <thead>
            <tr>
              <th>Started</th><th>Campaign</th><th>Lead</th><th>Number</th>
              <th>State</th><th>Disposition</th><th>Duration</th>
              <th>Mode</th><th>Transport</th><th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {(data?.calls || []).map((c) => (
              <tr key={c.session_id}>
                <td style={{ whiteSpace: 'nowrap', fontSize: 13 }}>{fmtTime(c.started_at)}</td>
                <td>{c.campaign_name || '—'}</td>
                <td>{c.lead_name || '—'}</td>
                <td style={{ fontFamily: 'var(--mono)', fontSize: 13 }}>{c.to_number || '—'}</td>
                <td><span className={`pill ${STATE_PILL[c.state] || 'slate'}`}>{c.final_state || c.state}</span></td>
                <td style={{ fontFamily: 'var(--mono)', fontSize: 13 }}>{c.disposition || '—'}</td>
                <td>{fmtDuration(c.duration_s)}</td>
                <td>{c.provider_mode === 'managed' ? 'Managed' : 'Self-hosted'}</td>
                <td>{c.transport === 'stub'
                  ? <span className="pill gold">stub</span>
                  : c.transport}</td>
                <td className="num">
                  {c.cost_breakdown && c.cost_breakdown.total !== undefined
                    ? `${c.cost_breakdown.total} ${c.cost_breakdown.currency || ''}`.trim()
                    : '—'}
                </td>
              </tr>
            ))}
            {data && data.total === 0 && (
              <tr><td colSpan={10} style={{ color: 'var(--slate)' }}>
                No calls yet — open an active campaign and choose “Run dispatch”.
              </td></tr>
            )}
            {!data && !error && (
              <tr><td colSpan={10} style={{ color: 'var(--slate)' }}>Loading…</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="footnote">
        The Transport column shows what actually carried each call — the stub
        (simulated), Vapi (managed) or Plivo (self-hosted) — read from the
        result record, never assumed. Live calls appear as DIALING / IN_CALL
        when placed and land here as terminal §8.2 records when the provider
        reports the end of the call.
      </p>
    </div>
  )
}
