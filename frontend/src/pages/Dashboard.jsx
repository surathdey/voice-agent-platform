import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Dashboard — the tenant home (A10). One read model (GET /api/dashboard)
   assembled from the same §8.2 result records both orchestration modes
   land through the finalize path, so the numbers here never disagree
   with the Calls page or a campaign's Results card. "Today" is the
   tenant's market-local day — the timezone is shown on the page, since
   every schedule on the platform runs on that clock. */
const STATUS_PILL = { draft: 'navy', active: 'teal', paused: 'gold', completed: 'slate' }

export default function Dashboard({ onOpenCalls, onOpenCampaigns }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  async function load() {
    try { setData(await api('/api/dashboard')) }
    catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  const disp = Object.entries(data?.dispositions || {}).sort((a, b) => b[1] - a[1])
  const maxDisp = Math.max(1, ...disp.map(([, n]) => n))

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <div className="sub">
            The day at a glance — every figure below comes from the one §8.2
            result record each call produces, in both modes.
            {data?.timezone && <> Times run on the market clock: <b>{data.timezone}</b>.</>}
          </div>
        </div>
        <button className="btn small secondary" onClick={load}>Refresh</button>
      </div>

      {error && <div className="error">{error}</div>}
      {!data && !error && <div className="sub">Loading…</div>}

      {data && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="n">{data.active_campaigns}</div>
              <div className="l">Active campaigns</div>
            </div>
            <div className="stat-card">
              <div className="n">{data.calls_today}</div>
              <div className="l">Calls today</div>
            </div>
            <div className="stat-card">
              <div className="n">{data.results_total}</div>
              <div className="l">Results recorded</div>
            </div>
            <div className="stat-card">
              <div className="n" style={data.review_queue_count ? { color: 'var(--danger)' } : undefined}>
                {data.review_queue_count}
              </div>
              <div className="l">Awaiting human review</div>
            </div>
          </div>

          <div className="two-col">
            <div className="card">
              <h3 className="section-title">Dispositions — all campaigns</h3>
              {disp.length === 0 && (
                <p style={{ color: 'var(--slate)', margin: 0 }}>
                  No results yet — dispositions appear here as calls complete.
                </p>
              )}
              {disp.map(([name, n]) => (
                <div className="bar-row" key={name}>
                  <span className="bar-name">{name}</span>
                  <span className="bar-track">
                    <span className="bar-fill" style={{ width: `${(n / maxDisp) * 100}%` }} />
                  </span>
                  <span className="num">{n}</span>
                </div>
              ))}
              <p className="footnote" style={{ marginTop: 14 }}>
                Buckets are defined by each agent's own prompt — the totals
                above simply add up what the campaigns recorded.
              </p>
            </div>

            <div>
              <div className="card" style={{ marginBottom: 20 }}>
                <h3 className="section-title">Campaigns by status</h3>
                <div className="summary-badges" style={{ marginBottom: 0 }}>
                  {Object.entries(data.campaigns_by_status || {}).map(([st, n]) => (
                    <span key={st} className={`pill ${STATUS_PILL[st] || 'slate'}`}>{st} · {n}</span>
                  ))}
                  {Object.keys(data.campaigns_by_status || {}).length === 0 && (
                    <span style={{ color: 'var(--slate)' }}>No campaigns yet.</span>
                  )}
                </div>
              </div>
              <div className="card">
                <h3 className="section-title">Human review queue</h3>
                {data.review_queue_count > 0 ? (
                  <p style={{ margin: 0 }}>
                    <span className="pill red">Needs review · {data.review_queue_count}</span>{' '}
                    result(s) could not be classified with confidence — the
                    disposition was left empty rather than guessed.
                  </p>
                ) : (
                  <p style={{ margin: 0, color: 'var(--slate)' }}>
                    Nothing is waiting for review — every result so far
                    carries a disposition from its agent's own definitions.
                  </p>
                )}
                <div style={{ marginTop: 14, display: 'flex', gap: 10 }}>
                  <button className="btn small secondary" onClick={onOpenCalls}>
                    Review flagged calls
                  </button>
                  <button className="btn small secondary" onClick={onOpenCampaigns}>
                    Open campaigns
                  </button>
                </div>
              </div>
              <div className="card" style={{ marginTop: 20 }}>
                <h3 className="section-title">Cost — all campaigns</h3>
                {(data.costs || []).length === 0 && (
                  <p style={{ margin: 0, color: 'var(--slate)' }}>
                    No costed calls yet. Real calls are priced from each
                    result's cost breakdown — the provider's reported
                    figures, or the tenant's rate card in Settings →
                    Providers. Stub runs never count as money.
                  </p>
                )}
                {(data.costs || []).map((e) => (
                  <div key={e.currency} style={{ margin: '4px 0', fontSize: 13.5 }}>
                    <b>{e.currency}:</b> total {e.total} over {e.calls} costed call(s)
                    {e.connected_calls > 0 && (
                      <> — <b>{e.cost_per_connected}</b> per connected call</>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
