import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Catalog authoring — Super Admin only (Epic C, C2). The platform side
   authors catalog agents and publishes immutable versions. ONE agent,
   MULTIPLE versions: the agent is the JOB (same flow / dispositions /
   purpose); each version is one STACK IMPLEMENTATION of that job
   (prompt tuning + model + provider mechanics for that stack). Example:
   Broken-Promise Recovery v1 suitable_for=managed (Vapi), v2
   suitable_for=self_hosted (Sarvam) — no second agent, no separate
   "self-hosted type" agent.
   There is deliberately NO edit UI that changes an existing version:
   a published version never changes, and a draft being viewed never
   changes either. Editing loads a copy into the detail form; Save
   creates a NEW version as a DRAFT (invisible to tenants), and Publish
   makes a draft visible. Tenants adopt versions from their own Agents
   page — adoption is not done here. Adoption reproduces EXACTLY what
   is on the adopted version (prompt + stack + configuration), because
   the version row carries its full content — the agent row is only
   the job identity plus creation defaults. */
const EMPTY_AGENT = {
  slug: '', name: '', stage: '', purpose: '',
  prompt: '', note: '', suitable_for: 'both', model: '', validated_on: '',
  stt_provider: 'saaras', stt_language: 'hi-IN',
  tts_provider: 'bulbul', tts_voice_id: '', voice_gender: 'female',
  max_duration_s: 300, max_turns: 50, silence_prompt_after_s: 8, silence_max_prompts: 1,
  language_rule_json: '', captured_fields_json: '',
}
const EMPTY_DETAIL_FORM = {
  prompt: '', note: '', suitable_for: 'both', model: '', validated_on: '',
  stt_provider: '', stt_language: 'hi-IN',
  tts_provider: '', tts_voice_id: '', voice_gender: 'female',
  max_duration_s: 300, max_turns: 50, silence_prompt_after_s: 8, silence_max_prompts: 1,
  language_rule_json: '', captured_fields_json: '',
}

// Settled stacks (do not invent others):
//   self_hosted = Sarvam: sarvam-105b + Saaras STT + Bulbul v4 Flash TTS.
//   managed = Vapi: Vapi default LLM + Deepgram nova-3 multi STT +
//     ElevenLabs voices (voice comes from the tenant's Vapi profile).
//   both = proven on both stacks; carries one consistent set.
const SARVAM_MODELS = ['sarvam-105b']
const VAPI_MODELS = ['gpt-4o']
const BULBUL_VOICES = [
  { value: 'roopa_bn_conversational', label: 'roopa_bn_conversational (Bengali, female)' },
  { value: 'arnab_bn_conversation', label: 'arnab_bn_conversation (Bengali, male)' },
  { value: 'kavya_hi_conversational', label: 'kavya_hi_conversational (Hindi, female)' },
  { value: 'aditya_hi_conversational', label: 'aditya_hi_conversational (Hindi, male)' },
  { value: 'roopa_en_conversational', label: 'roopa_en_conversational (English, female)' },
  { value: 'dev_en_conversational', label: 'dev_en_conversational (English, male)' },
]
const STT_LANGUAGES = ['hi-IN', 'bn-IN', 'en-IN']

// Shared stack-conditional editor, used by the create form and the
// detail form. `form` holds suitable_for / model / stt_provider /
// stt_language / tts_provider / tts_voice_id / voice_gender; `setForm`
// is the parent's setState. Flipping suitable_for re-points model /
// STT / TTS to that stack's defaults and clears the voice (a voice
// belongs to one stack); flipping model re-points STT / TTS to the
// model's stack. These handlers keep the form in a valid combination
// by construction; the backend still rejects mismatches with a 422.
// validated_on is NEVER touched here — it is history.
function StackFields({ form, setForm }) {
  const suitable = form.suitable_for || 'both'
  const model = form.model || ''
  const isSarvam = suitable === 'self_hosted' || (suitable === 'both' && model.startsWith('sarvam'))
  const isVapi = suitable === 'managed' || (suitable === 'both' && model !== '' && !model.startsWith('sarvam'))
  function onSuitable(next) {
    if (next === 'self_hosted') setForm((f) => ({ ...f, suitable_for: next, model: 'sarvam-105b', stt_provider: 'saaras', tts_provider: 'bulbul', tts_voice_id: '' }))
    else if (next === 'managed') setForm((f) => ({ ...f, suitable_for: next, model: 'gpt-4o', stt_provider: 'deepgram', tts_provider: 'elevenlabs', tts_voice_id: '' }))
    else setForm((f) => ({ ...f, suitable_for: next }))
  }
  function onModel(next) {
    if (next.startsWith('sarvam')) setForm((f) => ({ ...f, model: next, stt_provider: 'saaras', tts_provider: 'bulbul', tts_voice_id: '' }))
    else setForm((f) => ({ ...f, model: next, stt_provider: 'deepgram', tts_provider: 'elevenlabs', tts_voice_id: '' }))
  }
  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))
  const modelOptions = suitable === 'self_hosted' ? SARVAM_MODELS : suitable === 'managed' ? VAPI_MODELS : [...SARVAM_MODELS, ...VAPI_MODELS]
  return (
    <>
      <div className="field"><label>Suitable for</label>
        <select value={suitable} onChange={(e) => onSuitable(e.target.value)}>
          <option value="both">both</option>
          <option value="managed">managed</option>
          <option value="self_hosted">self_hosted</option>
        </select></div>
      <div className="field"><label>Model (LLM)</label>
        <select value={model} onChange={(e) => onModel(e.target.value)}>
          <option value="">— choose a model —</option>
          {modelOptions.map((m) => <option key={m} value={m}>{m}</option>)}
          {model !== '' && !modelOptions.includes(model) && <option value={model}>{model} (current — pick a model above to change)</option>}
        </select></div>
      {isSarvam && (
        <>
          <div className="field"><label>STT — Sarvam Saaras</label>
            <input value={form.stt_provider || 'saaras'} readOnly /></div>
          <div className="field"><label>TTS — Bulbul v4 Flash</label>
            <input value={form.tts_provider || 'bulbul'} readOnly /></div>
          <div className="field"><label>Bulbul voice</label>
            <select value={form.tts_voice_id || ''} onChange={(e) => set('tts_voice_id', e.target.value)}>
              <option value="">— choose a Bulbul voice —</option>
              {BULBUL_VOICES.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
            </select></div>
        </>
      )}
      {isVapi && (
        <div className="field"><label>Vapi-managed stack</label>
          <div style={{ color: 'var(--slate)', fontSize: 13, paddingTop: 6 }}>
            STT Deepgram nova-3 multi · LLM {model || 'Vapi default'} · voice from the tenant's Vapi profile
            (tenant Settings → Providers → Vapi). No Sarvam fields apply here.
          </div></div>
      )}
      {suitable === 'both' && model === '' && (
        <div className="field"><label>Stack note</label>
          <div style={{ color: 'var(--slate)', fontSize: 13, paddingTop: 6 }}>
            Suitable for both stacks — pick a model to set the runtime stack. The version carries one
            consistent set; tenants on either stack adopt it and run that set.
          </div></div>
      )}
      <div className="field"><label>STT language</label>
        <select value={form.stt_language || 'hi-IN'} onChange={(e) => set('stt_language', e.target.value)}>
          {STT_LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
        </select></div>
      <div className="field"><label>Voice gender</label>
        <select value={form.voice_gender || 'female'} onChange={(e) => set('voice_gender', e.target.value)}>
          <option value="female">female</option>
          <option value="male">male</option>
        </select></div>
    </>
  )
}

export default function CatalogAuthoring() {
  const [catalog, setCatalog] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [agentForm, setAgentForm] = useState(EMPTY_AGENT)
  // Detail view: selected agent slug + version. Null = card list.
  const [selectedSlug, setSelectedSlug] = useState(null)
  const [selectedVersion, setSelectedVersion] = useState(null)
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
    setEditingCopyOf(null)
    setDetailNotice('')
    setDetailError('')
  }

  const vStatus = (v) => (v.status || (v.published_at ? 'published' : 'draft'))

  function selectedVersionObj() {
    const ca = catalog.find((x) => x.slug === selectedSlug)
    const vs = ca ? (ca.versions || []) : []
    return vs.find((v) => v.version === selectedVersion) || null
  }

  // Prefill the FULL detail form whenever the selected version changes.
  // The version row carries its complete content (prompt + stack +
  // configuration), so the form loads everything from it — not only
  // the prompt. validated_on is carried through untouched: flipping
  // suitable_for never rewrites it.
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
      stt_provider: v.stt_provider || '',
      stt_language: v.stt_language || 'hi-IN',
      tts_provider: v.tts_provider || '',
      tts_voice_id: v.tts_voice_id || '',
      voice_gender: v.voice_gender || 'female',
      max_duration_s: v.max_duration_s ?? 300,
      max_turns: v.max_turns ?? 50,
      silence_prompt_after_s: v.silence_prompt_after_s ?? 8,
      silence_max_prompts: v.silence_max_prompts ?? 1,
      language_rule_json: v.language_rule && Object.keys(v.language_rule).length ? JSON.stringify(v.language_rule, null, 2) : '',
      captured_fields_json: v.captured_fields && v.captured_fields.length ? JSON.stringify(v.captured_fields, null, 2) : '',
    })
  }, [selectedSlug, selectedVersion, catalog])

  async function saveDraft(e) {
    e.preventDefault()
    setDetailError(''); setDetailNotice(''); setDetailBusy(true)
    try {
      const body = {
        prompt: detailForm.prompt,
        note: detailForm.note.trim(),
        suitable_for: detailForm.suitable_for,
        model: detailForm.model.trim(),
        validated_on: detailForm.validated_on.trim(),
        stt_provider: detailForm.stt_provider,
        stt_language: detailForm.stt_language,
        tts_provider: detailForm.tts_provider,
        tts_voice_id: detailForm.tts_voice_id,
        voice_gender: detailForm.voice_gender,
        max_duration_s: parseInt(detailForm.max_duration_s, 10) || 300,
        max_turns: parseInt(detailForm.max_turns, 10) || 50,
        silence_prompt_after_s: parseInt(detailForm.silence_prompt_after_s, 10) || 8,
        silence_max_prompts: parseInt(detailForm.silence_max_prompts, 10) || 1,
        language_rule: parseJson(detailForm.language_rule_json, 'Language rule') ?? {},
        captured_fields: parseJson(detailForm.captured_fields_json, 'Captured fields') ?? [],
      }
      const created = await api(`/api/catalog/agents/${selectedSlug}/versions/draft`, {
        method: 'POST',
        body,
      })
      setDetailNotice(`Draft v${created.version} saved — it carries this FULL content (prompt + stack + configuration) and is invisible to tenants until published.`)
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
      const lr = parseJson(agentForm.language_rule_json, 'Language rule') ?? {}
      const cf = parseJson(agentForm.captured_fields_json, 'Captured fields') ?? []
      const stack = {
        model: agentForm.model.trim(),
        stt_provider: agentForm.stt_provider,
        stt_language: agentForm.stt_language,
        tts_provider: agentForm.tts_provider,
        tts_voice_id: agentForm.tts_voice_id,
        voice_gender: agentForm.voice_gender,
        max_duration_s: parseInt(agentForm.max_duration_s, 10) || 300,
        max_turns: parseInt(agentForm.max_turns, 10) || 50,
        silence_prompt_after_s: parseInt(agentForm.silence_prompt_after_s, 10) || 8,
        silence_max_prompts: parseInt(agentForm.silence_max_prompts, 10) || 1,
        language_rule: lr,
        captured_fields: cf,
      }
      const body = {
        slug: agentForm.slug.trim().toLowerCase(),
        name: agentForm.name.trim(),
        stage: agentForm.stage.trim(),
        purpose: agentForm.purpose.trim(),
        llm_model: stack.model,
        stt_provider: stack.stt_provider,
        stt_language: stack.stt_language,
        tts_provider: stack.tts_provider,
        tts_voice_id: stack.tts_voice_id,
        voice_gender: stack.voice_gender,
        max_duration_s: stack.max_duration_s,
        max_turns: stack.max_turns,
        silence_prompt_after_s: stack.silence_prompt_after_s,
        silence_max_prompts: stack.silence_max_prompts,
        language_rule: lr,
        captured_fields: cf,
        // First version, created WITH the agent as v1 DRAFT — the agent
        // is never created version-less. The detail view (form + table
        // + Save draft + Publish) is the only place versions are
        // created or published after this.
        initial_version: {
          prompt: agentForm.prompt,
          note: agentForm.note.trim(),
          suitable_for: agentForm.suitable_for,
          validated_on: agentForm.validated_on.trim(),
          ...stack,
        },
      }
      const created = await api('/api/catalog/agents', { method: 'POST', body })
      setAgentForm(EMPTY_AGENT)
      await load()
      setSelectedSlug(created.slug)
      setSelectedVersion(1)
      setDetailNotice(`Catalog agent “${created.name}” created with v1 as DRAFT. Publish v1 below when ready — tenants adopt only published versions.`)
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

        <h3 className="section-title">Edit version — full editor</h3>
        {!version && <p style={{ color: 'var(--slate)' }}>No versions yet — fill in the form and Save to create v1 as a draft.</p>}
        {version && isDraft && (
          <div className="card" style={{ marginBottom: 14 }}>
            Viewing <strong>DRAFT v{version.version}</strong> — it is not visible to tenants.
            Publish it below to make it available.
          </div>
        )}
        {version && !isDraft && isEditingCopy && (
          <div className="card" style={{ marginBottom: 14 }}>
            Editing a <strong>copy of v{version.version}</strong> — Save will create v{nextVersion} as a draft carrying this full content.
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
            <StackFields form={detailForm} setForm={setDetailForm} />
            <div className="field"><label>Validated on (history — never auto-changed by a stack flip)</label>
              <input value={detailForm.validated_on} onChange={(e) => setDetailForm((f) => ({ ...f, validated_on: e.target.value }))} placeholder="e.g. Vapi managed" /></div>
            <div className="field"><label>Note</label>
              <input value={detailForm.note} onChange={(e) => setDetailForm((f) => ({ ...f, note: e.target.value }))} /></div>
          </div>
          <div className="field" style={{ marginTop: 14 }}><label>Prompt *</label>
            <textarea value={detailForm.prompt} onChange={(e) => setDetailForm((f) => ({ ...f, prompt: e.target.value }))} required rows={14} style={{ whiteSpace: 'pre-wrap' }} /></div>
          <div className="form-grid" style={{ marginTop: 14 }}>
            <div className="field"><label>Max duration (seconds)</label>
              <input type="number" value={detailForm.max_duration_s} onChange={(e) => setDetailForm((f) => ({ ...f, max_duration_s: e.target.value }))} /></div>
            <div className="field"><label>Max turns</label>
              <input type="number" value={detailForm.max_turns} onChange={(e) => setDetailForm((f) => ({ ...f, max_turns: e.target.value }))} /></div>
            <div className="field"><label>Silence prompt after (seconds)</label>
              <input type="number" value={detailForm.silence_prompt_after_s} onChange={(e) => setDetailForm((f) => ({ ...f, silence_prompt_after_s: e.target.value }))} /></div>
            <div className="field"><label>Silence max prompts</label>
              <input type="number" value={detailForm.silence_max_prompts} onChange={(e) => setDetailForm((f) => ({ ...f, silence_max_prompts: e.target.value }))} /></div>
            <div className="field"><label>Language rule (JSON)</label>
              <textarea value={detailForm.language_rule_json} onChange={(e) => setDetailForm((f) => ({ ...f, language_rule_json: e.target.value }))} rows={3} placeholder='{"default": "hi-IN"}' /></div>
            <div className="field"><label>Captured fields (JSON list)</label>
              <textarea value={detailForm.captured_fields_json} onChange={(e) => setDetailForm((f) => ({ ...f, captured_fields_json: e.target.value }))} rows={3} placeholder='[{"name": "amount", "type": "string"}]' /></div>
          </div>
          <div style={{ display: 'flex', gap: 10, marginTop: 12, flexWrap: 'wrap' }}>
            <button className="btn" disabled={detailBusy || !detailForm.prompt.trim() || !detailForm.model.trim()}>
              {detailBusy ? 'Saving…' : `Save as draft (v${nextVersion})`}
            </button>
            <button type="button" className="btn secondary" disabled={detailBusy || !version || !isDraft} onClick={publishDraft}>
              {detailBusy ? 'Publishing…' : 'Publish selected draft'}
            </button>
          </div>
          <p style={{ color: 'var(--slate)', fontSize: 13, marginTop: 8 }}>
            Save always creates a NEW version as a draft carrying this FULL content — prompt + stack + configuration.
            It never modifies any existing version and does not publish. Publish is only enabled for drafts.
            Adopting the new version later reproduces exactly what is in this form.
          </p>
        </form>

        <h3 className="section-title">Versions</h3>
        {versions.length === 0 && <p style={{ color: 'var(--slate)' }}>No versions yet.</p>}
        {versions.length > 0 && (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13.5 }}>
            <thead>
              <tr style={{ textAlign: 'left', borderBottom: '2px solid var(--line)' }}>
                <th style={{ padding: '8px 10px' }}>Version</th>
                <th style={{ padding: '8px 10px' }}>Status</th>
                <th style={{ padding: '8px 10px' }}>Stack</th>
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
                  <td style={{ padding: '8px 10px' }}>{v.suitable_for}</td>
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
          that changes them. Editing loads a copy into the form; Save creates a new draft
          carrying the full edited content; Publish makes a draft visible to tenants.
          One agent, multiple versions — each version is one stack implementation of
          the same job.
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
            A published version never changes — a fix is a new version, and each
            version carries its full content (prompt + stack + configuration).
          </div>
        </div>
        <button className="btn small secondary" onClick={load}>Refresh</button>
      </div>

      {error && <div className="error">{error}</div>}
      {notice && <div className="card" style={{ marginBottom: 18 }}>{notice}</div>}

      <h3 className="section-title">Create a catalog agent</h3>
      <p style={{ color: 'var(--slate)', fontSize: 13 }}>
        The agent is the job; the first version below is its first stack implementation.
        Creating here always creates v1 as a DRAFT — open the agent afterwards and publish
        v1 from its detail view. The detail view (form + version table + Save draft +
        Publish) is the only place versions are created or published.
      </p>
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
          <StackFields form={agentForm} setForm={setAgentForm} />
          <div className="field"><label>Validated on (stacks live-call proven on)</label>
            <input value={agentForm.validated_on} onChange={(e) => setA('validated_on', e.target.value)} placeholder="e.g. Vapi managed" /></div>
          <div className="field"><label>Note</label>
            <input value={agentForm.note} onChange={(e) => setA('note', e.target.value)} /></div>
        </div>
        <div className="field" style={{ marginTop: 14 }}><label>First version prompt * (the agent's single prompt)</label>
          <textarea value={agentForm.prompt} onChange={(e) => setA('prompt', e.target.value)} required rows={10} /></div>
        <div className="form-grid" style={{ marginTop: 14 }}>
          <div className="field"><label>Max duration (seconds)</label>
            <input type="number" value={agentForm.max_duration_s} onChange={(e) => setA('max_duration_s', e.target.value)} /></div>
          <div className="field"><label>Max turns</label>
            <input type="number" value={agentForm.max_turns} onChange={(e) => setA('max_turns', e.target.value)} /></div>
          <div className="field"><label>Silence prompt after (seconds)</label>
            <input type="number" value={agentForm.silence_prompt_after_s} onChange={(e) => setA('silence_prompt_after_s', e.target.value)} /></div>
          <div className="field"><label>Silence max prompts</label>
            <input type="number" value={agentForm.silence_max_prompts} onChange={(e) => setA('silence_max_prompts', e.target.value)} /></div>
          <div className="field"><label>Language rule (optional JSON)</label>
            <textarea value={agentForm.language_rule_json} onChange={(e) => setA('language_rule_json', e.target.value)} rows={3} placeholder='{"default": "hi-IN"}' /></div>
          <div className="field"><label>Captured fields (optional JSON list)</label>
            <textarea value={agentForm.captured_fields_json} onChange={(e) => setA('captured_fields_json', e.target.value)} rows={3} placeholder='[{"name": "amount", "type": "string"}]' /></div>
        </div>
        <button className="btn" style={{ marginTop: 14 }} disabled={busy || !agentForm.slug.trim() || !agentForm.name.trim() || !agentForm.prompt.trim() || !agentForm.model.trim()}>
          {busy ? 'Creating…' : 'Create catalog agent + v1 draft'}</button>
      </form>

      <h3 className="section-title">Catalog agents + versions</h3>
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
