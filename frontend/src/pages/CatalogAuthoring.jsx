import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Catalog authoring — Super Admin only (Epic C, C2). The platform side
   authors catalog agents and publishes immutable versions. There is
   deliberately NO edit UI that changes a published version: a published
   version never changes. Editing loads a copy into the detail form;
   Save creates a NEW version as a DRAFT (invisible to tenants), and
   Publish makes a draft visible. Tenants adopt versions from
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
const EMPTY_DETAIL_FORM = {
  prompt: '', note: '', suitable_for: 'both', model: '', validated_on: '',
}

export default function CatalogAuthoring() {
  const [catalog, setCatalog] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [agentForm, setAgentForm] = useState(EMPTY_AGENT)
  const [versionForm, setVersionForm] = useState(EMPTY_VERSION)
  // Read-only detail view: selected agent slug + version. Null = card list.
  const [selectedSlug, setSelectedSlug] = useState(null)
  const [selectedVersion, setSelectedVersion] = useState(null)
  const [detailExport, setDetailExport] = useState(null)
  const [detailExportError, setDetailExportError] = useState('')
  const [detailExportLoading, setDetailExportLoading] = useState(false)
  const [detailForm, setDetailForm] = useState(EMPTY_DETAIL_FORM)
  const [editingCopyOf, setEditingCopyOf] = useState(null)
  const [detailNotice, setDetailNotice] = useState('')
  const [detailError, setDetailError] = useState('')
  const [detailBusy, setDetailBusy] = useState(false)

  async function load() {
    try { setCatalog(await api('/api/catalog/agents')) }
    catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  function openDetail(ca) {
    setSelectedSlug(ca.slug)
    const versions = ca.versions || []
    const latest = ca.latest_version || (versions.length ? versions[versions.length - 1].version : null)
    setSelectedVersion(latest)
    setDetailExport(null)
    setDetailExportError('')
    setEditingCopyOf(null)
    setDetailNotice('')
    setDetailError('')
  }

  // Full structured config for the selected version comes from the export
  // payload. The prompt + metadata below come from the list data, so the
  // detail view still renders if the export fetch fails.
  useEffect(() => {
    if (!selectedSlug || selectedVersion === null || selectedVersion === undefined) return
    let cancelled = false
    setDetailExportLoading(true)
    setDetailExportError('')
    api(`/api/catalog/agents/${selectedSlug}/versions/${selectedVersion}/export`)
      .then((data) => { if (!cancelled) setDetailExport(data) })
      .catch((e) => { if (!cancelled) setDetailExportError(e.message) })
      .finally(() => { if (!cancelled) setDetailExportLoading(false) })
    return () => { cancelled = true }
  }, [selectedSlug, selectedVersion])

  const vStatus = (v) => (v.status || (v.published_at ? 'published' : 'draft'))

  function selectedVersionObj() {
    const ca = catalog.find((x) => x.slug === selectedSlug)
    const vs = ca ? (ca.versions || []) : []
    return vs.find((v) => v.version === selectedVersion) || null
  }

  // Prefill the detail form whenever the selected version changes. The
  // form edits version fields only (prompt + metadata); agent config is
  // displayed read-only below from the export payload.
  useEffect(() => {
    if (!selectedSlug) return
    const ca = catalog.find((x) => x.slug === selectedSlug)
    const vs = ca ? (ca.versions || []) : []
    const v = vs.find((x) => x.version === selectedVersion)
    if (!v) {
      setDetailForm(EMPTY_DETAIL_FORM)
      return
    }
    setDetailForm({
      prompt: v.prompt || '',
      note: v.note || '',
      suitable_for: v.suitable_for || 'both',
      model: v.model || '',
      validated_on: v.validated_on || '',
    })
  }, [selectedSlug, selectedVersion, catalog])

  async function saveDraft(e) {
    e.preventDefault()
    setDetailError(''); setDetailNotice(''); setDetailBusy(true)
    try {
      const created = await api(`/api/catalog/agents/${selectedSlug}/versions/draft`, {
        method: 'POST',
        body: {
          prompt: detailForm.prompt,
          note: detailForm.note.trim(),
          suitable_for: detailForm.suitable_for,
          model: detailForm.model.trim(),
          validated_on: detailForm.validated_on.trim(),
        },
      })
      setDetailNotice(`Draft v${created.version} saved — it is DRAFT and invisible to tenants until published.`)
      setEditingCopyOf(null)
      await load()
      setSelectedVersion(created.version)
    } catch (err) { setDetailError(err.message) } finally { setDetailBusy(false) }
  }

  async function publishDraft() {
    const v = selectedVersionObj()
    if (!v) return
    setDetailError(''); setDetailNotice(''); setDetailBusy(true)
    try {
      const published = await api(`/api/catalog/agents/${selectedSlug}/versions/${v.version}/publish`, { method: 'POST' })
      setDetailNotice(`Version ${published.version} published — tenants now see it as available (it never auto-applies).`)
      await load()
      setSelectedVersion(published.version)
    } catch (err) { setDetailError(err.message) } finally { setDetailBusy(false) }
  }

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

  const selectedAgent = selectedSlug ? catalog.find((ca) => ca.slug === selectedSlug) : null
  if (selectedSlug) {
    if (!selectedAgent) {
      return (
        <div>
          <button className="btn small secondary" onClick={() => { setSelectedSlug(null); setEditingCopyOf(null) }}>← Back to catalog agents</button>
          <p style={{ color: 'var(--slate)', marginTop: 14 }}>
            Catalog agent “{selectedSlug}” not found. Go back to the list.
          </p>
        </div>
      )
    }
    const versions = selectedAgent.versions || []
    const version = selectedVersionObj()
    const cfg = detailExport || selectedAgent
    const fields = cfg.captured_fields || []
    const nextVersion = versions.length ? Math.max(...versions.map((v) => v.version)) + 1 : 1
    const isDraft = !!version && vStatus(version) === 'draft'
    const isEditingCopy = editingCopyOf !== null && !!version && editingCopyOf === version.version
    const publishedVersions = versions.filter((v) => vStatus(v) !== 'draft')
    return (
      <div>
        <button className="btn small secondary" onClick={() => { setSelectedSlug(null); setEditingCopyOf(null) }}>← Back to catalog agents</button>
        <div className="page-head" style={{ marginTop: 12 }}>
          <div>
            <h1>{selectedAgent.name}</h1>
            <div className="sub">{selectedAgent.purpose || selectedAgent.stage}</div>
          </div>
        </div>
        {detailError && <div className="error">{detailError}</div>}
        {detailNotice && <div className="card" style={{ marginBottom: 18 }}>{detailNotice}</div>}
        <dl className="kv">
          <dt>Slug</dt><dd>{selectedAgent.slug}</dd>
          <dt>Stage</dt><dd>{selectedAgent.stage || '—'}</dd>
          <dt>Purpose</dt><dd>{selectedAgent.purpose || '—'}</dd>
          <dt>Latest published</dt><dd>{publishedVersions.length ? `version ${publishedVersions[publishedVersions.length - 1].version}` : 'none yet'}</dd>
        </dl>

        <h3 className="section-title">Edit version</h3>
        {!version && <p style={{ color: 'var(--slate)' }}>No versions yet — fill in the form and Save to create v1 as a draft.</p>}
        {version && isDraft && (
          <div className="card" style={{ marginBottom: 14 }}>
            Viewing <strong>DRAFT v{version.version}</strong> — it is not visible to tenants.
            Publish it below to make it available.
          </div>
        )}
        {version && !isDraft && isEditingCopy && (
          <div className="card" style={{ marginBottom: 14 }}>
            Editing a <strong>copy of v{version.version}</strong> — Save will create v{nextVersion} as a draft.
            v{version.version} itself will not change.
          </div>
        )}
        {version && !isDraft && !isEditingCopy && (
          <div className="card" style={{ marginBottom: 14 }}>
            Viewing <strong>PUBLISHED v{version.version}</strong> — click Edit on its row below to edit a copy.
            v{version.version} itself never changes.
          </div>
        )}
        <form onSubmit={saveDraft}>
          <div className="form-grid">
            <div className="field"><label>Suitable for</label>
              <select value={detailForm.suitable_for} onChange={(e) => setDetailForm((f) => ({ ...f, suitable_for: e.target.value }))}>
                <option value="both">both</option>
                <option value="managed">managed</option>
                <option value="self_hosted">self_hosted</option>
              </select></div>
            <div className="field"><label>Model</label>
              <input value={detailForm.model} onChange={(e) => setDetailForm((f) => ({ ...f, model: e.target.value }))} placeholder="e.g. sarvam-105b" /></div>
            <div className="field"><label>Validated on</label>
              <input value={detailForm.validated_on} onChange={(e) => setDetailForm((f) => ({ ...f, validated_on: e.target.value }))} placeholder="e.g. Vapi managed" /></div>
            <div className="field"><label>Note</label>
              <input value={detailForm.note} onChange={(e) => setDetailForm((f) => ({ ...f, note: e.target.value }))} /></div>
          </div>
          <div className="field" style={{ marginTop: 14 }}><label>Prompt *</label>
            <textarea value={detailForm.prompt} onChange={(e) => setDetailForm((f) => ({ ...f, prompt: e.target.value }))} required rows={14} style={{ whiteSpace: 'pre-wrap' }} /></div>
          <div style={{ display: 'flex', gap: 10, marginTop: 12, flexWrap: 'wrap' }}>
            <button className="btn" disabled={detailBusy || !detailForm.prompt.trim()}>
              {detailBusy ? 'Saving…' : `Save as draft (v${nextVersion})`}
            </button>
            <button type="button" className="btn secondary" disabled={detailBusy || !version || !isDraft} onClick={publishDraft}>
              {detailBusy ? 'Publishing…' : 'Publish selected draft'}
            </button>
          </div>
          <p style={{ color: 'var(--slate)', fontSize: 13, marginTop: 8 }}>
            Save always creates a NEW version as a draft — it never modifies any existing version and does not publish.
            Publish is only enabled for drafts.
          </p>
        </form>

        <h3 className="section-title">Configuration (agent defaults, from version export)</h3>
        {detailExportLoading && <p style={{ color: 'var(--slate)' }}>Loading configuration…</p>}
        {detailExportError && <div className="error">Could not load the export payload ({detailExportError}) — showing agent-level defaults instead.</div>}
        <dl className="kv">
          <dt>LLM model</dt><dd>{cfg.llm_model || '—'}</dd>
          <dt>STT provider</dt><dd>{cfg.stt_provider || '—'}</dd>
          <dt>STT language</dt><dd>{cfg.stt_language || '—'}</dd>
          <dt>TTS provider</dt><dd>{cfg.tts_provider || '—'}</dd>
          <dt>TTS voice id</dt><dd>{cfg.tts_voice_id || '—'}</dd>
          <dt>Voice gender</dt><dd>{cfg.voice_gender || '—'}</dd>
          <dt>Max duration (seconds)</dt><dd>{cfg.max_duration_s ?? '—'}</dd>
          <dt>Max turns</dt><dd>{cfg.max_turns ?? '—'}</dd>
          <dt>Silence prompt after (seconds)</dt><dd>{cfg.silence_prompt_after_s ?? '—'}</dd>
          <dt>Silence max prompts</dt><dd>{cfg.silence_max_prompts ?? '—'}</dd>
        </dl>
        <div className="field"><label>Language rule</label>
          <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 260, overflow: 'auto', padding: 14, background: 'var(--bg)', border: '1px solid var(--line)', borderRadius: 8, fontSize: 13 }}>{Object.keys(cfg.language_rule || {}).length ? JSON.stringify(cfg.language_rule, null, 2) : '—'}</pre></div>
        <div className="field"><label>Captured fields</label>
          {fields.length ? (
            <ul style={{ marginTop: 6 }}>
              {fields.map((f, i) => (
                <li key={i}>{typeof f === 'string' ? f : `${f.name || '—'}${f.type ? ` (${f.type})` : ''}${f.description ? ` — ${f.description}` : ''}`}</li>
              ))}
            </ul>
          ) : <p style={{ color: 'var(--slate)' }}>—</p>}
        </div>
        <p style={{ color: 'var(--slate)', fontSize: 13 }}>
          Configuration lives on the agent, not on a version: versions carry the prompt + metadata above.
          At adoption, the tenant copies the version's prompt together with these current agent defaults.
        </p>

        <h3 className="section-title">Versions</h3>
        {versions.length === 0 && <p style={{ color: 'var(--slate)' }}>No versions yet.</p>}
        {versions.length > 0 && (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13.5 }}>
            <thead>
              <tr style={{ textAlign: 'left', borderBottom: '2px solid var(--line)' }}>
                <th style={{ padding: '8px 10px' }}>Version</th>
                <th style={{ padding: '8px 10px' }}>Status</th>
                <th style={{ padding: '8px 10px' }}>Note</th>
                <th style={{ padding: '8px 10px' }}>Model</th>
                <th style={{ padding: '8px 10px' }}>Validated on</th>
                <th style={{ padding: '8px 10px' }}>Published by</th>
                <th style={{ padding: '8px 10px' }}>Published at</th>
                <th style={{ padding: '8px 10px' }}></th>
              </tr>
            </thead>
            <tbody>
              {versions.map((v) => (
                <tr
                  key={v.version_id}
                  onClick={() => { setSelectedVersion(v.version); setEditingCopyOf(null) }}
                  style={{ cursor: 'pointer', borderBottom: '1px solid var(--line)', background: version && v.version === version.version ? 'var(--gold-soft)' : 'transparent' }}
                >
                  <td style={{ padding: '8px 10px' }}>v{v.version}</td>
                  <td style={{ padding: '8px 10px', fontWeight: vStatus(v) === 'draft' ? 700 : 400 }}>{vStatus(v) === 'draft' ? 'DRAFT' : 'PUBLISHED'}</td>
                  <td style={{ padding: '8px 10px' }}>{v.note || '—'}</td>
                  <td style={{ padding: '8px 10px' }}>{v.model || '—'}</td>
                  <td style={{ padding: '8px 10px' }}>{v.validated_on || '—'}</td>
                  <td style={{ padding: '8px 10px' }}>{v.published_by || '—'}</td>
                  <td style={{ padding: '8px 10px' }}>{v.published_at || '—'}</td>
                  <td style={{ padding: '8px 10px' }}>
                    <button type="button" className="btn small secondary" onClick={(e) => { e.stopPropagation(); setSelectedVersion(v.version); setEditingCopyOf(v.version); window.scrollTo(0, 0) }}>Edit</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <p className="footnote">
          Published versions are immutable — there is no edit, save, delete, or rollback
          that changes them. Editing loads a copy into the form; Save creates a new draft;
          Publish makes a draft visible to tenants.
        </p>
      </div>
    )
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
          <div
            key={ca.catalog_agent_id}
            className="card"
            onClick={() => openDetail(ca)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetail(ca) } }}
            tabIndex={0}
            role="button"
            title="View full prompt + configuration"
            style={{ cursor: 'pointer' }}
          >
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
                  {vStatus(v) === 'draft' ? ' · DRAFT' : ''}
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
            <div style={{ marginTop: 10, color: 'var(--teal)', fontWeight: 600 }}>View full prompt + configuration →</div>
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
