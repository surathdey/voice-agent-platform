import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Agents library — wireframe w6-agents.png:
   card grid; each card = name, purpose, (REFERENCE AGENT badge), LLM,
   STT/TTS, Captures; footer link into the studio. */
export default function Agents({ onOpen, onNew }) {
  const [agents, setAgents] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    api('/api/agents')
      .then(setAgents)
      .catch((e) => setError(e.message))
  }, [])

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
        Editing an agent edits its single prompt in one place; every campaign using that agent picks up the change.
        Prompt edits are versioned — history stays reviewable and any version can be restored.
      </p>
    </div>
  )
}

function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s }
