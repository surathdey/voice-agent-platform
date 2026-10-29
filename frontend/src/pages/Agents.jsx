import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Agents library — wireframe w6-agents.png:
   card grid; each card = name, purpose, (REFERENCE AGENT badge), LLM,
   STT/TTS, Captures; footer link into the studio. */
export default function Agents({ onOpen, onNew }) {
  const [agents, setAgents] = useState([])
  const [catalog, setCatalog] = useState([])
  const [catalogError, setCatalogError] = useState('')
  const [adopting, setAdopting] = useState('')
  const [error, setError] = useState('')

  const loadAgents = () => {
    api('/api/agents')
      .then(setAgents)
      .catch((e) => setError(e.message))
  }

  useEffect(() => {
    loadAgents()
    api('/api/catalog/agents')
      .then(setCatalog)
      .catch((e) => setCatalogError(e.message))
  }, [])

  const adopt = (slug, version) => {
    setAdopting(`${slug}:${version}`)
    setCatalogError('')
    api('/api/catalog/adoptions', {
      method: 'POST',
      body: JSON.stringify({ catalog_agent_id: slug, version }),
    })
      .then(() => {
        loadAgents()
        return api('/api/catalog/agents')
      })
      .then(setCatalog)
      .catch((e) => setCatalogError(e.message))
      .finally(() => setAdopting(''))
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Agents — configured once, used by every campaign</h1>
          <div className="sub">An agent is data, not code: one prompt, its voice and model, and the fields it must capture.</div>
        </div>
        <button className="btn" onClick={onNew}>+ New agent</button>
      </div>

      {error && <div className="error">{error}</div>}

      {/* Catalog adoption (Epic C, C2): authorship is shared, adoption is
          per tenant. Adopting copies a catalog version's prompt into this
          tenant's own agent — running campaigns keep their pinned version. */}
      <h2>Catalog — adopt a version</h2>
      <div className="sub">
        Versions are authored once on the platform side and adopted here, one tenant at a time.
        A new version never auto-applies; running campaigns keep their pinned version for life.
      </div>
      {catalogError && <div className="error">{catalogError}</div>}
      <div className="grid">
        {catalog.map((ca) => (
          <div key={ca.catalog_agent_id} className="card agent-card">
            <h3>{ca.name}</h3>
            <div className="purpose">{ca.purpose || ca.stage}</div>
            <dl className="kv">
              <dt>Adopted</dt>
              <dd>{ca.adopted_version ? `version ${ca.adopted_version}` : 'not adopted'}</dd>
              <dt>Latest</dt>
              <dd>{ca.latest_version ? `version ${ca.latest_version}` : 'no versions yet'}</dd>
            </dl>
            {ca.new_version_available && (
              <div><span className="badge">New version available</span></div>
            )}
            {ca.versions?.map((v) => (
              <div key={v.version_id} className="kv" style={{ marginTop: 8 }}>
                <div>
                  <strong>Version {v.version}</strong>
                  {' · '}{v.suitable_for}
                  {v.model ? ` · ${v.model}` : ''}
                  {v.validated_on ? ` · validated on ${v.validated_on}` : ''}
                  {v.fit_label ? ` — ${v.fit_label}` : ''}
                </div>
                {v.unproven_on_stack && (
                  <div className="error">
                    Not validated on this tenant&apos;s stack — adopt only if you mean to test it here.
                  </div>
                )}
                <button
                  className="btn"
                  disabled={adopting === `${ca.slug}:${v.version}`}
                  onClick={() => adopt(ca.slug, v.version)}
                >
                  {adopting === `${ca.slug}:${v.version}` ? 'Adopting…' : `Adopt v${v.version}`}
                </button>
              </div>
            ))}
          </div>
        ))}
        {catalog.length === 0 && !catalogError && (
          <div className="card">No catalog agents yet — the platform side publishes versions here.</div>
        )}
      </div>

      <div className="grid">
        {agents.map((a) => (
          <div key={a.agent_id} className="card agent-card" onClick={() => onOpen(a.agent_id)}>
            <h3>{a.name}</h3>
            <div className="purpose">{a.purpose || a.stage}</div>
            {a.is_reference && <div><span className="badge">Reference agent</span></div>}
            <dl className="kv">
              <dt>LLM</dt><dd>{a.llm_model}</dd>
              <dt>STT / TTS</dt><dd>{cap(a.stt_provider)} · {cap(a.tts_provider)}</dd>
              <dt>Captures</dt>
              <dd>{a.captured_fields?.length
                ? a.captured_fields.map((f) => f.name).join(' · ')
                : '—'}</dd>
              <dt>Prompt</dt><dd>version {a.prompt_version}</dd>
            </dl>
            <div className="card-foot">Prompt · captured fields · version history →</div>
          </div>
        ))}
        {agents.length === 0 && !error && (
          <div className="card">No agents yet — create the first one with “+ New agent”.</div>
        )}
      </div>

      <p className="footnote">
        Editing an agent edits its single prompt in one place. Campaigns pin their prompt at creation and keep it for
        life — a later adoption or edit never changes a running campaign; only new campaigns pick up the new version.
        Prompt edits are versioned — history stays reviewable and any version can be restored.
      </p>
    </div>
  )
}

function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s }
