import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Campaigns list / history — wireframe w11-campaigns-list.png (Figure C-1):
   every campaign with its agent, channel, status, leads. Clicking a row
   opens the campaign. */
const STATUS_PILL = { draft: 'navy', active: 'teal', paused: 'gold', completed: 'slate' }
const STATUS_LABEL = { draft: 'Draft', active: 'Running', paused: 'Paused', completed: 'Completed' }

export default function Campaigns({ onOpen, onNew }) {
  const [campaigns, setCampaigns] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    api('/api/campaigns').then(setCampaigns).catch((e) => setError(e.message))
  }, [])

  const count = (s) => campaigns.filter((c) => c.status === s).length

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Campaigns</h1>
          <div className="sub">Every campaign, its agent and its list — the operator’s home for campaign history.</div>
        </div>
        <button className="btn" onClick={onNew}>+ New Campaign</button>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="summary-badges">
        <span className="pill teal">Running&nbsp;&nbsp;{count('active')}</span>
        <span className="pill navy">Draft&nbsp;&nbsp;{count('draft')}</span>
        <span className="pill slate">Completed&nbsp;&nbsp;{count('completed')}</span>
        <span className="pill gold">Paused&nbsp;&nbsp;{count('paused')}</span>
      </div>

      <div className="card" style={{ padding: '6px 10px' }}>
        <table className="data">
          <thead>
            <tr>
              <th>Campaign</th><th>Agent</th><th>Channel</th><th>Status</th>
              <th className="num">Leads</th><th className="num">Connected</th><th></th>
            </tr>
          </thead>
          <tbody>
            {campaigns.map((c) => (
              <tr key={c.campaign_id} className="clickable" onClick={() => onOpen(c.campaign_id)}>
                <td><b style={{ color: 'var(--navy)' }}>{c.name}</b>
                  <div style={{ color: 'var(--slate)', fontSize: 12.5 }}>
                    {c.product_label}{c.product_detail ? ` — ${c.product_detail}` : ''}
                  </div></td>
                <td>{c.agent_name}</td>
                <td>{c.channel}</td>
                <td><span className={`pill ${STATUS_PILL[c.status] || 'slate'}`}>{STATUS_LABEL[c.status] || c.status}</span></td>
                <td className="num">{c.lead_count}</td>
                <td className="num">—</td>
                <td className="chev">›</td>
              </tr>
            ))}
            {campaigns.length === 0 && !error && (
              <tr><td colSpan={7} style={{ color: 'var(--slate)' }}>
                No campaigns yet — create the first one with “+ New Campaign”.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="footnote">
        Click a campaign to open it — its leads, its settings, and (when calling starts, in a later chunk)
        its live outcomes and downloadable results spreadsheet. Connected counts arrive with the dispatch engine.
      </p>
    </div>
  )
}
