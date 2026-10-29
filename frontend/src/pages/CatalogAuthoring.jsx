import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Catalog authoring — Super Admin only (Epic C, C2). The platform side
   authors catalog agents and publishes immutable versions. There is
   deliberately NO edit UI for a published version: a published version
   never changes, a fix is a new version. Tenants adopt versions from
   their own Agents page — adoption is not done here. */
const EMPTY_AGENT = {
  slug: '', name: '', stage: '', purpose: '',
  llm_model: 'sarvam-105b', stt_provider: 'saaras', stt_language: 'hi-IN',
  tts_provider: 'bulbul', tts_voice_id: '', voice_gender: 'female',
  max_duration_s: 300, language_rule_json: '', captured_fields_json: '',
}
const EMPTY_VERSION = {
  agent_slug: '', prompt: '', note: '',
  suitable_for: 'both', model: '', validated_on: '',
}

export default function CatalogAuthoring() {
  const [catalog, setCatalog] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [agentForm, setAgentForm] = useState(EMPTY_AGENT)
  const [versionForm, setVersionForm] = useState(EMPTY_VERSION)

  async function load() {
    try { setCatalog(await api('/api/catalog/agents')) }
    catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  const setA = (k, v) => setAgentForm((f) => ({ ...f, [k]: v }))
  const setV = (k, v) => setVersionForm((f) => ({ ...f, [k]: v }))

  function parseJson(text, label) {
    const t = (text || '').trim()
    if (!t) return undefined
    try { return JSON.parse(t) }
    catch { throw new Error(`${label} is not valid JSON`) }
  }

  async function createAgent(e) {
    e.preventDefault()
    setError(''); setNotice(''); setBusy(true)
    try {
      const body = {
        slug: agentForm.slug.trim().toLowerCase(),
        name: agentForm.name.trim(),
        stage: agentForm.stage.trim(),
        purpose: agentForm.purpose.trim(),
        llm_model: agentForm.llm_model.trim() || 'sarvam-105b',
        stt_provider: agentForm.stt_provider.trim() || 'saaras',
        stt_language: agentForm.stt_language.trim() || 'hi-IN',
        tts_provider: agentForm.tts_provider.trim() || 'bulbul',
        tts_voice_id: agentForm.tts_voice_id.trim(),
        voice_gender: agentForm.voice_gender,
        max_duration_s: parseInt(agentForm.max_duration_s, 10) || 300,
      }
      const lr = parseJson(agentForm.language_rule_json, 'Language rule')
      if (lr !== undefined) body.language_rule = lr
      const cf = parseJson(agentForm.captured_fields_json, 'Captured fields')
      if (cf !== undefined) body.captured_fields = cf
      const created = await api('/api/catalog/agents', { method: 'POST', body })
      setNotice(`Catalog agent “${created.name}” created. Publish its first version below — tenants adopt only published versions.`)
      setAgentForm(EMPTY_AGENT)
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function publishVersion(e) {
    e.preventDefault()
    setError(''); setNotice(''); setBusy(true)
    try {
      const body = {
        prompt: versionForm.prompt,
        note: versionForm.note.trim(),
        suitable_for: versionForm.suitable_for,
        model: versionForm.model.trim(),
        validated_on: versionForm.validated_on.trim(),
      }
      const published = await api(`/api/catalog/agents/${versionForm.agent_slug}/versions`, { method: 'POST', body })
      setNotice(`Version ${published.version} published for “${versionForm.agent_slug}”. Tenants see it as available — it never auto-applies.`)
      setVersionForm({ ...EMPTY_VERSION, agent_slug: versionForm.agent_slug })
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Catalog — authoring</h1>
          <div className="sub">
            The platform side authors agents once and publishes immutable versions.
            A published version never changes — a fix is a new version.
          </div>
        </div>
        <button className="btn small secondary" onClick={load}>Refresh</button>
      </div>

      {error && <div className="error">{error}</div>}
      {notice && <div className="card" style={{ marginBottom: 18 }}>{notice}</div>}

      <h3 className="section-title">Create a catalog agent</h3>
      <form onSubmit={createAgent}>
        <div className="form-grid">
          <div className="field"><label>Slug * (lowercase, hyphens)</label>
            <input value={agentForm.slug} onChange={(e) => setA('slug', e.target.value)} required placeholder="e.g. rpi" /></div>
          <div className="field"><label>Name *</label>
            <input value={agentForm.name} onChange={(e) => setA('name', e.target.value)} required /></div>
          <div className="field"><label>Stage</label>
            <input value={agentForm.stage} onChange={(e) => setA('stage', e.target.value)} /></div>
          <div className="field"><label>Purpose</label>
            <input value={agentForm.purpose} onChange={(e) => setA('purpose', e.target.value)} /></div>
          <div className="field"><label>LLM model</label>
            <input value={agentForm.llm_model} onChange={(e) => setA('llm_model', e.target.value)} /></div>
          <div className="field"><label>STT provider</label>
            <input value={agentForm.stt_provider} onChange={(e) => setA('stt_provider', e.target.value)} /></div>
          <div className="field"><label>STT language</label>
            <input value={agentForm.stt_language} onChange={(e) => setA('stt_language', e.target.value)} /></div>
          <div className="field"><label>TTS provider</label>
            <input value={agentForm.tts_provider} onChange={(e) => setA('tts_provider', e.target.value)} /></div>
          <div className="field"><label>TTS voice id</label>
            <input value={agentForm.tts_voice_id} onChange={(e) => setA('tts_voice_id', e.target.value)} /></div>
          <div className="field"><label>Voice gender</label>
            <select value={agentForm.voice_gender} onChange={(e) => setA('voice_gender', e.target.value)}>
              <option value="female">female</option>
              <option value="male">male</option>
            </select></div>
          <div className="field"><label>Max duration (seconds)</label>
            <input type="number" value={agentForm.max_duration_s} onChange={(e) => setA('max_duration_s', e.target.value)} /></div>
          <div className="field"><label>Language rule (optional JSON)</label>
            <textarea value={agentForm.language_rule_json} onChange={(e) => setA('language_rule_json', e.target.value)} rows={3} placeholder='{"default": "hi-IN"}' /></div>
          <div className="field"><label>Captured fields (optional JSON list)</label>
            <textarea value={agentForm.captured_fields_json} onChange={(e) => setA('captured_fields_json', e.target.value)} rows={3} placeholder='[{"name": "amount", "type": "string"}]' /></div>
        </div>
        <button className="btn" style={{ marginTop: 14 }} disabled={busy || !agentForm.slug.trim() || !agentForm.name.trim()}>
          {busy ? 'Creating…' : 'Create catalog agent'}</button>
      </form>

      <h3 className="section-title">Publish a version</h3>
      <form onSubmit={publishVersion}>
        <div className="form-grid">
          <div className="field"><label>Agent *</label>
            <select value={versionForm.agent_slug} onChange={(e) => setV('agent_slug', e.target.value)} required>
              <option value="">— choose an agent —</option>
              {catalog.map((ca) => (
                <option key={ca.catalog_agent_id} value={ca.slug}>
                  {ca.name} ({ca.slug}) — latest v{ca.latest_version || 'none'}
                </option>
              ))}
            </select></div>
          <div className="field"><label>Suitable for</label>
            <select value={versionForm.suitable_for} onChange={(e) => setV('suitable_for', e.target.value)}>
              <option value="both">both</option>
              <option value="managed">managed</option>
              <option value="self_hosted">self_hosted</option>
            </select></div>
          <div className="field"><label>Model</label>
            <input value={versionForm.model} onChange={(e) => setV('model', e.target.value)} placeholder="e.g. sarvam-105b" /></div>
          <div className="field"><label>Validated on (stacks live-call proven on)</label>
            <input value={versionForm.validated_on} onChange={(e) => setV('validated_on', e.target.value)} placeholder="e.g. Vapi managed" /></div>
          <div className="field"><label>Note</label>
            <input value={versionForm.note} onChange={(e) => setV('note', e.target.value)} /></div>
        </div>
        <div className="field" style={{ marginTop: 14 }}><label>Prompt * (the agent's single prompt)</label>
          <textarea value={versionForm.prompt} onChange={(e) => setV('prompt', e.target.value)} required rows={10} /></div>
        <button className="btn" style={{ marginTop: 10 }} disabled={busy || !versionForm.agent_slug || !versionForm.prompt.trim()}>
          {busy ? 'Publishing…' : 'Publish version'}</button>
      </form>

      <h3 className="section-title">Catalog agents + published versions</h3>
      {catalog.length === 0 && !error && <p style={{ color: 'var(--slate)' }}>No catalog agents yet.</p>}
      <div className="grid">
        {catalog.map((ca) => (
          <div key={ca.catalog_agent_id} className="card">
            <h3>{ca.name}</h3>
            <div className="purpose">{ca.purpose || ca.stage}</div>
            <dl className="kv">
              <dt>Slug</dt><dd>{ca.slug}</dd>
              <dt>Latest</dt><dd>{ca.latest_version ? `version ${ca.latest_version}` : 'no versions yet'}</dd>
            </dl>
            {(ca.versions || []).map((v) => (
              <div key={v.version_id} className="kv" style={{ marginTop: 8 }}>
                <div>
                  <strong>Version {v.version}</strong>
                  {' · '}{v.suitable_for}
                  {v.model ? ` · ${v.model}` : ''}
                  {v.validated_on ? ` · validated on ${v.validated_on}` : ''}
                </div>
                <div style={{ color: 'var(--slate)', fontSize: 12.5 }}>
                  {v.note ? `${v.note} · ` : ''}published by {v.published_by || '—'}
                  {v.published_at ? ` · ${v.published_at}` : ''}
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>

      <p className="footnote">
        Published versions are immutable — there is no edit or delete for them.
        A fix is always a new version, and tenants adopt it when ready; running
        campaigns keep their pinned version for life.
      </p>
    </div>
  )
}
