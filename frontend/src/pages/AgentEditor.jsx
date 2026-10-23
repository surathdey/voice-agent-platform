import React, { useEffect, useState } from 'react'
import { api } from '../api'

const EMPTY = {
  name: '', stage: '', purpose: '', is_reference: false,
  prompt: 'ROLE\n\n\nFLOW\n\n\nDISPOSITIONS\n\n\nCLOSINGS\n',
  llm_model: 'sarvam-105b', stt_provider: 'saaras', stt_language: 'hi-IN',
  tts_provider: 'bulbul', tts_voice_id: '', voice_gender: 'female',
  language_rule: { default: 'hi-IN', by_state: {} },
  captured_fields: [], max_duration_s: 300, silence_prompt_after_s: 8,
}

const FIELD_TYPES = ['string', 'date', 'number', 'boolean']
const LANGUAGES = ['hi-IN', 'bn-IN', 'en-IN', 'en-US', 'te-IN', 'ta-IN', 'mr-IN', 'kn-IN']

/* Agent Config Studio (A2) — single prompt (Role/Flow/Dispositions/Closings),
   structured config beside it, and the append-only prompt version history. */
export default function AgentEditor({ agentId, onBack }) {
  const [form, setForm] = useState(EMPTY)
  const [versions, setVersions] = useState([])
  const [preview, setPreview] = useState(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const isNew = !agentId

  useEffect(() => {
    if (!isNew) {
      api(`/api/agents/${agentId}`).then((a) =>
        setForm({ ...EMPTY, ...a, language_rule: a.language_rule || { default: 'hi-IN', by_state: {} } })
      ).catch((e) => setError(e.message))
      loadVersions()
    }
  }, [agentId])

  function loadVersions() {
    if (isNew) return
    api(`/api/agents/${agentId}/prompt-versions`).then(setVersions).catch(() => {})
  }

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  async function save() {
    setError(''); setNote('')
    try {
      const payload = { ...form, captured_fields: form.captured_fields.filter((f) => f.name.trim()) }
      if (isNew) {
        const created = await api('/api/agents', { method: 'POST', body: payload })
        setNote(`Agent created with prompt version ${created.prompt_version}.`)
        onBack()
      } else {
        const updated = await api(`/api/agents/${agentId}`, { method: 'PUT', body: payload })
        setForm({ ...EMPTY, ...updated })
        setNote(`Saved. Current prompt version: ${updated.prompt_version}.`)
        loadVersions()
      }
    } catch (e) { setError(e.message) }
  }

  async function rollback(version) {
    setError(''); setNote('')
    try {
      const updated = await api(`/api/agents/${agentId}/prompt-versions/${version}/rollback`, { method: 'POST' })
      setForm({ ...EMPTY, ...updated })
      setNote(`Rolled back to v${version} — saved as the new version ${updated.prompt_version}.`)
      setPreview(null)
      loadVersions()
    } catch (e) { setError(e.message) }
  }

  async function remove() {
    if (!window.confirm('Delete this agent and its prompt history?')) return
    try { await api(`/api/agents/${agentId}`, { method: 'DELETE' }); onBack() }
    catch (e) { setError(e.message) }
  }

  const langRows = Object.entries(form.language_rule?.by_state || {})

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>{isNew ? 'New agent' : `Agent — ${form.name || '…'}`}</h1>
          <div className="sub">Agent Config Studio · the agent as data, no code</div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <button className="btn secondary" onClick={onBack}>← All agents</button>
          {!isNew && <button className="btn danger" onClick={remove}>Delete</button>}
          <button className="btn" onClick={save}>{isNew ? 'Create agent' : 'Save agent'}</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      {note && <div className="ok-note">{note}</div>}

      <div className="card">
        <div className="section-title"><span className="marker">1</span> Basics</div>
        <div className="form-grid">
          <div className="field"><label>Agent name</label>
            <input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="e.g. Payment Reminder" /></div>
          <div className="field"><label>Journey stage</label>
            <input value={form.stage} onChange={(e) => set('stage', e.target.value)} placeholder="e.g. Reminder" /></div>
          <div className="field" style={{ gridColumn: '1 / -1' }}><label>Purpose (one line, shown on the card)</label>
            <input value={form.purpose} onChange={(e) => set('purpose', e.target.value)} placeholder="e.g. Amount-due reminder" /></div>
          <div className="field"><label>Reference agent</label>
            <select value={form.is_reference ? 'yes' : 'no'} onChange={(e) => set('is_reference', e.target.value === 'yes')}>
              <option value="no">No</option><option value="yes">Yes — badge on the library card</option>
            </select></div>
        </div>

        <div className="section-title"><span className="marker">2</span> Voice &amp; model (structured — set per agent)</div>
        <p className="info-note">
          These model and voice settings apply in <b>Self-hosted mode</b>. In Managed mode the
          campaign runs on Vapi, which uses its own models and voices — the agent's prompt and
          language rule carry over, and the <b>voice gender</b> below picks the female / male
          voice from the tenant's per-language Vapi voice profile (Settings → Providers → Vapi).
        </p>
        <div className="form-grid">
          <div className="field"><label>LLM model</label>
            <select value={form.llm_model} onChange={(e) => set('llm_model', e.target.value)}>
              <option value="sarvam-105b">sarvam-105b — low-cost tier</option>
              <option value="sarvam-30b">sarvam-30b — low-cost tier</option>
              <option value="frontier-1">frontier-1 — negotiation tier</option>
            </select>
            <div className="hint">Design rule: model belongs to the agent, never to a campaign.</div></div>
          <div className="field"><label>STT provider · language</label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <select value={form.stt_provider} onChange={(e) => set('stt_provider', e.target.value)}>
                <option value="saaras">Saaras</option><option value="deepgram">Deepgram</option>
              </select>
              <select value={form.stt_language} onChange={(e) => set('stt_language', e.target.value)}>
                {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
              </select>
            </div></div>
          <div className="field"><label>TTS provider · voice</label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <select value={form.tts_provider} onChange={(e) => set('tts_provider', e.target.value)}>
                <option value="bulbul">Bulbul</option><option value="elevenlabs">ElevenLabs</option>
              </select>
              <input value={form.tts_voice_id} onChange={(e) => set('tts_voice_id', e.target.value)} placeholder="voice id" />
            </div></div>
          <div className="field"><label>Voice gender (agent persona)</label>
            <select value={form.voice_gender || 'female'} onChange={(e) => set('voice_gender', e.target.value)}>
              <option value="female">Female</option>
              <option value="male">Male</option>
            </select>
            <div className="hint">Managed mode: selects the female / male voice ID in the tenant's Vapi voice profile for the call's language.</div></div>
          <div className="field"><label>Max call duration (seconds) · silence prompt after (s)</label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <input type="number" value={form.max_duration_s} onChange={(e) => set('max_duration_s', Number(e.target.value))} />
              <input type="number" value={form.silence_prompt_after_s} onChange={(e) => set('silence_prompt_after_s', Number(e.target.value))} />
            </div></div>
        </div>

        <div className="section-title"><span className="marker">3</span> Language rule (structured)</div>
        <div className="form-grid">
          <div className="field"><label>Default language</label>
            <select value={form.language_rule?.default || 'hi-IN'}
              onChange={(e) => set('language_rule', { ...form.language_rule, default: e.target.value })}>
              {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
            </select></div>
          <div className="field"><label>Per-state overrides</label>
            <div className="rows">
              {langRows.map(([state, lang], i) => (
                <div className="row2" key={i}>
                  <input value={state} placeholder="State, e.g. West Bengal"
                    onChange={(e) => {
                      const by = { ...(form.language_rule.by_state || {}) }
                      delete by[state]; by[e.target.value] = lang
                      set('language_rule', { ...form.language_rule, by_state: by })
                    }} />
                  <select value={lang}
                    onChange={(e) => set('language_rule', { ...form.language_rule, by_state: { ...form.language_rule.by_state, [state]: e.target.value } })}>
                    {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
                  </select>
                  <button className="icon-btn" onClick={() => {
                    const by = { ...(form.language_rule.by_state || {}) }; delete by[state]
                    set('language_rule', { ...form.language_rule, by_state: by })
                  }}>✕</button>
                </div>
              ))}
              <button className="btn secondary small" style={{ alignSelf: 'flex-start' }}
                onClick={() => set('language_rule', { ...form.language_rule, by_state: { ...(form.language_rule.by_state || {}), '': 'hi-IN' } })}>
                + Add state rule</button>
            </div>
            <div className="hint">Resolved before dispatch (e.g. West Bengal → Bengali, other states → Hindi).</div></div>
        </div>

        <div className="section-title"><span className="marker">4</span> Captured fields (structured — what the call must record)</div>
        <div className="rows">
          {form.captured_fields.map((f, i) => (
            <div className="row3" key={i}>
              <input value={f.name} placeholder="field_name"
                onChange={(e) => set('captured_fields', form.captured_fields.map((x, j) => j === i ? { ...x, name: e.target.value } : x))} />
              <select value={f.type}
                onChange={(e) => set('captured_fields', form.captured_fields.map((x, j) => j === i ? { ...x, type: e.target.value } : x))}>
                {FIELD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <input value={f.description || ''} placeholder="What counts as captured?"
                onChange={(e) => set('captured_fields', form.captured_fields.map((x, j) => j === i ? { ...x, description: e.target.value } : x))} />
              <button className="icon-btn" onClick={() => set('captured_fields', form.captured_fields.filter((_, j) => j !== i))}>✕</button>
            </div>
          ))}
          <button className="btn secondary small" style={{ alignSelf: 'flex-start' }}
            onClick={() => set('captured_fields', [...form.captured_fields, { name: '', type: 'string', description: '' }])}>
            + Add captured field</button>
        </div>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="section-title"><span className="marker">5</span> The prompt — one text, the single source of truth</div>
        <div className="guidance">
          <b>Write the prompt in four sections: ROLE · FLOW · DISPOSITIONS · CLOSINGS.</b>{' '}
          Everything the agent is — its dialogue, its flow and the <b>full disposition logic</b> — lives in this
          one text. There are no separate script templates or disposition-rule stores: the post-call classifier
          reads its disposition definitions from the DISPOSITIONS section here. Every save creates a new version
          (see history below) — edits are reviewable and reversible.
        </div>
        <textarea className="prompt" value={form.prompt} onChange={(e) => set('prompt', e.target.value)} />
        <div style={{ marginTop: 14, display: 'flex', justifyContent: 'flex-end' }}>
          <button className="btn" onClick={save}>{isNew ? 'Create agent' : 'Save agent'}</button>
        </div>
      </div>

      {!isNew && (
        <div className="card" style={{ marginTop: 18 }}>
          <div className="section-title"><span className="marker">6</span> Prompt version history</div>
          <div className="version-list">
            {versions.map((v, idx) => (
              <div key={v.version} className={`version-item ${idx === 0 ? 'current' : ''}`}>
                <div>
                  <span className="v">v{v.version}</span>{idx === 0 && ' · current'}{' '}
                  <span className="meta">{v.note || '—'} · {v.created_by} · {v.created_at ? new Date(v.created_at).toLocaleString() : ''}</span>
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button className="btn secondary small" onClick={() => setPreview(preview?.version === v.version ? null : v)}>View</button>
                  {idx !== 0 && <button className="btn gold small" onClick={() => rollback(v.version)}>Roll back to v{v.version}</button>}
                </div>
              </div>
            ))}
            {versions.length === 0 && <div className="meta">No versions yet.</div>}
          </div>
          {preview && <pre className="preview">{`── v${preview.version} ──\n`}{preview.prompt}</pre>}
        </div>
      )}
    </div>
  )
}
