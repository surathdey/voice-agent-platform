import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Calls — every call session of this tenant, across campaigns (read-only
   in A5). Each row is the §8.2 result view: state, disposition, duration,
   the mode the campaign ran in and the transport that actually carried
   the call (the stub in this build — labelled, never disguised). From
   A7, self-hosted rows also carry the pipeline legs' record (STT ·
   LLM · TTS status, from A8) and expanding a row shows the summary and
   the transcript — finals are persisted as they arrive, and a call
   still in progress shows its live partial segment, marked. A result
   the post-call classifier could not trust carries a "Needs review"
   flag (its disposition stays empty rather than guessed). The full
   per-call journey view (timeline) arrives with A10. */
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

function LegsCell({ c }) {
  const legs = [
    ['STT', c.stt_status, c.stt_detail, c.stt_language],
    ['LLM', c.llm_status, c.llm_detail, ''],
    ['TTS', c.tts_status, c.tts_detail, ''],
  ].filter(([, status]) => status)
  if (!legs.length) return '—'
  return (
    <span style={{ display: 'inline-flex', flexDirection: 'column', gap: 2 }}>
      {legs.map(([label, status, detail, lang]) => (
        status === 'active'
          ? <span key={label} className="pill teal">{label}{lang ? ` · ${lang}` : ''}</span>
          : status === 'failed'
            ? <span key={label} className="pill red" title={detail || ''}>{label} failed{lang ? ` · ${lang}` : ''}</span>
            : status === 'connecting'
              ? <span key={label} className="pill gold">{label} connecting</span>
              : <span key={label} className="pill slate">{label} off</span>
      ))}
    </span>
  )
}

export default function Calls() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [openId, setOpenId] = useState('')

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
              <th>Mode</th><th>Transport</th><th>Legs</th><th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {(data?.calls || []).map((c) => (
              <React.Fragment key={c.session_id}>
              <tr onClick={() => setOpenId(openId === c.session_id ? '' : c.session_id)}
                  style={{ cursor: 'pointer' }}>
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
                <td><LegsCell c={c} /></td>
                <td className="num">
                  {c.cost_breakdown && c.cost_breakdown.total !== undefined
                    ? `${c.cost_breakdown.total} ${c.cost_breakdown.currency || ''}`.trim()
                    : '—'}
                </td>
              </tr>
              {openId === c.session_id && (
                <tr>
                  <td colSpan={11} style={{ background: 'var(--bg)', whiteSpace: 'normal' }}>
                    {c.review_required && (
                      <div style={{ marginBottom: 6 }}>
                        <span className="pill red">Needs review</span>{' '}
                        <span style={{ color: 'var(--slate)', fontSize: 13 }}>
                          classification could not produce a trustworthy disposition — see the summary.
                        </span>
                      </div>
                    )}
                    {c.summary && <div style={{ marginBottom: 6 }}>{c.summary}</div>}
                    {(c.transcript || []).length === 0 && !c.summary && (
                      <span style={{ color: 'var(--slate)' }}>No transcript for this call.</span>
                    )}
                    {(c.transcript || []).map((t, i) => (
                      <div key={i} style={{ fontSize: 13, margin: '2px 0' }}>
                        <span className={`pill ${t.speaker === 'agent' ? 'navy' : t.speaker === 'system' ? 'red' : 'slate'}`}>
                          {t.speaker}
                        </span>{' '}
                        {t.text}
                        {t.partial && <em style={{ color: 'var(--slate)' }}> (live…)</em>}
                        {t.interrupted && <em style={{ color: 'var(--gold)' }}> (interrupted — only the words actually spoken)</em>}
                      </div>
                    ))}
                  </td>
                </tr>
              )}
              </React.Fragment>
            ))}
            {data && data.total === 0 && (
              <tr><td colSpan={11} style={{ color: 'var(--slate)' }}>
                No calls yet — open an active campaign and choose “Run dispatch”.
              </td></tr>
            )}
            {!data && !error && (
              <tr><td colSpan={11} style={{ color: 'var(--slate)' }}>Loading…</td></tr>
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
