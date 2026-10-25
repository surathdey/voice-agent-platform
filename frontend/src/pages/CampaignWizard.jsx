import React, { useEffect, useState } from 'react'
import { api } from '../api'
import LeadsUpload from './LeadsUpload'

/* New Campaign wizard — the per-agent journey of design §15:
   Step 1 Agent → Step 2 Details → Step 3 Leads → Step 4 Schedule → Step 5 Review.
   Campaign screens set BUSINESS fields only: nothing agent-level (model,
   voice, language rule, prompt) appears here, and the operator picks a named
   CHANNEL — never a phone number. */
const STEP_LABELS = ['Agent', 'Details', 'Leads', 'Schedule', 'Review']
const PRODUCTS = [
  ['vehicle_loan', 'Vehicle loan'],
  ['credit_card', 'Credit card'],
  ['home_loan', 'Home loan'],
  ['personal_loan', 'Personal loan'],
  ['other', 'Other'],
]
const CHANNELS = ['Service Channel 1', 'Service Channel 2', 'Service Channel 3']
const RPI_COLS = ['callee_name', 'phone_number', 'bank_name', 'state']
const BANK_COLS = ['ACCOUNT_NO', 'CUSTOMER_NAME', 'PRINCIPAL', 'OUTSTANDING', 'MOBILE_NO', 'RegistrationNumber']
const REMINDER_COLS = [...BANK_COLS, 'due_date', 'amount_due']
const PTP_COLS = [...BANK_COLS] // design §15.3: the bank's file format
const SET_COLS = [...BANK_COLS] // design §15.4: the bank's file format
const BRK_COLS = [...BANK_COLS, 'promise_date', 'promised_amount'] // design §15.5: bank's columns plus the original promise

function Steps({ current }) {
  return (
    <div className="steps">
      {STEP_LABELS.map((lbl, i) => (
        <React.Fragment key={lbl}>
          <div className={`step ${i === current ? 'current' : ''} ${i < current ? 'done' : ''}`}>
            <span className="dot">{i + 1}</span><span className="lbl">{lbl}</span>
          </div>
          {i < STEP_LABELS.length - 1 && <div className="step"><span className="bar" /></div>}
        </React.Fragment>
      ))}
    </div>
  )
}

export default function CampaignWizard({ onLaunched, onExit }) {
  const [step, setStep] = useState(0)
  const [agents, setAgents] = useState([])
  const [agentId, setAgentId] = useState('')
  const [campaign, setCampaign] = useState(null)   // created at the end of Step 2
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [details, setDetails] = useState({
    name: '', product: 'vehicle_loan', product_detail: '', purpose: '',
    audience: '', channel: 'Service Channel 1', provider_mode: 'managed',
    settlement_authority_pct: '',
  })
  const [schedule, setSchedule] = useState({
    start_date: '', calling_days: 'Mon – Sat', window_start: '09:00', window_end: '19:00', daily_cap: 2000,
  })
  const [retry, setRetry] = useState({ max_attempts: 3, retry_gap_days: 1, retry_different_time: true })

  useEffect(() => {
    api('/api/agents').then((a) => { setAgents(a); if (a.length) setAgentId(a[0].agent_id) })
      .catch((e) => setError(e.message))
  }, [])

  const agent = agents.find((a) => a.agent_id === agentId)
  const setD = (k) => (e) => setDetails({ ...details, [k]: e.target.value })
  const setS = (k) => (e) => setSchedule({ ...schedule, [k]: e.target.value })
  const setR = (k) => (e) => setRetry({ ...retry, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  async function createCampaign() {
    setBusy(true); setError('')
    try {
      const c = await api('/api/campaigns', {
        method: 'POST',
        body: { agent_id: agentId, ...details,
          settlement_authority_pct: details.settlement_authority_pct === '' ? null : Number(details.settlement_authority_pct),
          schedule, retry_rules: retry, routing_rules: [] },
      })
      setCampaign(c); setStep(2)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function saveSchedule() {
    setBusy(true); setError('')
    try {
      const c = await api(`/api/campaigns/${campaign.campaign_id}`, {
        method: 'PUT',
        body: {
          agent_id: campaign.agent_id, name: campaign.name, product: campaign.product,
          product_detail: campaign.product_detail, purpose: campaign.purpose,
          audience: campaign.audience, channel: campaign.channel,
          provider_mode: campaign.provider_mode,
          settlement_authority_pct: campaign.settlement_authority_pct ?? null,
          schedule: { ...schedule, daily_cap: Number(schedule.daily_cap) || 0 },
          retry_rules: {
            max_attempts: Number(retry.max_attempts) || 1,
            retry_gap_days: Number(retry.retry_gap_days) || 0,
            retry_different_time: !!retry.retry_different_time,
          },
          routing_rules: campaign.routing_rules || [],
        },
      })
      setCampaign(c); setStep(4)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function refreshCampaign() {
    if (!campaign) return
    try { setCampaign(await api(`/api/campaigns/${campaign.campaign_id}`)) } catch {}
  }

  async function launch() {
    setBusy(true); setError('')
    try {
      await api(`/api/campaigns/${campaign.campaign_id}/status`, { method: 'POST', body: { status: 'active' } })
      onLaunched(campaign.campaign_id)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const isRpi = agent && /rpi|verification/i.test(`${agent.name} ${agent.stage}`)
  const isReminder = agent && /reminder/i.test(`${agent.name} ${agent.stage}`)
  const isPtp = agent && /promise-to-pay/i.test(`${agent.name} ${agent.stage}`)
  const isSettlement = agent && /settlement/i.test(`${agent.name} ${agent.stage}`)
  const isBroken = agent && /broken/i.test(`${agent.name} ${agent.stage}`)
  const expectedColumns = agent ? (isRpi ? RPI_COLS : isBroken ? BRK_COLS : isSettlement ? SET_COLS : isPtp ? PTP_COLS : isReminder ? REMINDER_COLS : BANK_COLS) : []

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>{agent && step > 0 ? `New Campaign — ${agent.name}` : 'New Campaign'}</h1>
          <div className="sub">One campaign, one agent, one list. Business settings only — the agent’s own configuration is never edited here.</div>
        </div>
      </div>
      <Steps current={step} />
      {error && <div className="error">{error}</div>}

      {/* ── Step 1: Agent ─────────────────────────────────────────── */}
      {step === 0 && (
        <div className="two-col">
          <div>
            <div style={{ fontWeight: 600, marginBottom: 10 }}>Which agent runs this campaign?</div>
            <div className="pick-list">
              {agents.map((a) => (
                <div key={a.agent_id} className={`pick-item ${a.agent_id === agentId ? 'sel' : ''}`}
                  onClick={() => setAgentId(a.agent_id)}>
                  <div className="nm">{a.name}{a.agent_id === agentId ? '  ✓ selected' : ''}</div>
                  <div className="tg">{a.purpose || a.stage}</div>
                </div>
              ))}
              {agents.length === 0 && <div className="pick-item">No agents configured yet — create one in Agents first.</div>}
            </div>
          </div>
          {agent && (
            <div className="notes-panel">
              <h4>What this agent needs</h4>
              <p>· A separate lead list for this campaign, in this agent’s file format (Step 3).</p>
              <p>· Captures on every call: {agent.captured_fields?.length
                ? agent.captured_fields.map((f) => f.name).join(' · ') : '—'}</p>
              <h4 style={{ marginTop: 16 }}>From the agent’s configuration</h4>
              <p>{agent.llm_model} · {agent.stt_provider} STT · {agent.tts_provider} voice · prompt v{agent.prompt_version}<br />
                <span style={{ color: 'var(--slate)' }}>Model, voice, language rule and the prompt itself are set in Agents — never on a campaign screen.</span></p>
            </div>
          )}
        </div>
      )}

      {/* ── Step 2: Details ───────────────────────────────────────── */}
      {step === 1 && (
        <div className="two-col">
          <div className="card">
            <div className="field"><label><span className="marker" style={{ marginRight: 8 }}>1</span>Campaign name</label>
              <input value={details.name} onChange={setD('name')} placeholder="October — RPI Verification Batch 1" /></div>
            <div className="field" style={{ marginTop: 14 }}><label><span className="marker" style={{ marginRight: 8 }}>2</span>Product / loan type</label>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <select value={details.product} onChange={setD('product')}>
                  {PRODUCTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
                <input value={details.product_detail} onChange={setD('product_detail')} placeholder="Detail — e.g. two-wheeler (stale records)" />
              </div></div>
            <div className="field" style={{ marginTop: 14 }}><label><span className="marker" style={{ marginRight: 8 }}>3</span>Purpose</label>
              <input value={details.purpose} onChange={setD('purpose')} placeholder="Why this campaign is running" /></div>
            <div className="field" style={{ marginTop: 14 }}><label>Who it is for (audience)</label>
              <input value={details.audience} onChange={setD('audience')} placeholder="e.g. Customers on the stale-records list" /></div>
            <div className="field" style={{ marginTop: 14 }}><label><span className="marker" style={{ marginRight: 8 }}>4</span>Channel</label>
              <select value={details.channel} onChange={setD('channel')}>
                {CHANNELS.map((c) => <option key={c}>{c}</option>)}
              </select></div>
            <div className="field" style={{ marginTop: 14 }}><label><span className="marker" style={{ marginRight: 8 }}>5</span>Calling runs via</label>
              <select value={details.provider_mode} onChange={setD('provider_mode')}>
                <option value="managed">Managed platform (Vapi)</option>
                <option value="self_hosted">Self-hosted stack (our orchestrator)</option>
              </select>
              <div className="hint">Chosen per campaign — the same campaign data and results either way.</div></div>
            {isSettlement && (
              <div className="field" style={{ marginTop: 14 }}><label><span className="marker" style={{ marginRight: 8 }}>6</span>Settlement authority cap (%)</label>
                <input type="number" min="0" max="100" value={details.settlement_authority_pct} onChange={setD('settlement_authority_pct')} placeholder="e.g. 30 — max discount the agent may agree on its own" />
                <div className="hint">Above this cap the case goes to the Human Queue for approval — the agent never refuses outright and never agrees beyond it. Leave blank for zero authority (everything to approval).</div></div>
            )}
          </div>
          <div className="notes-panel">
            <h4>What this means</h4>
            <p><span className="n">2</span>The campaign states what the loan is, why it is running, and who it is for. The product decides the identifier the agent uses on the call — a vehicle loan by its registration number, a credit card by its last 4 digits, a home loan by its account number.</p>
            <p><span className="n">4</span>The operator picks a named channel. Which number in that channel’s pool dials out is the platform’s job — the operator never sees or manages numbers.</p>
            <p><span className="n">5</span>Managed mode hands the call to the managed platform; self-hosted runs our own orchestrator (Plivo + Sarvam). Switching later is a setting, not a rebuild.</p>
            {isSettlement && <p><span className="n">6</span>The authority cap is the maximum discount this Settlement agent may agree on its own. Above it, the case goes to the Human Queue — the agent never refuses outright.</p>}
            <p>Not set here: the institution name comes from the lead list itself (the bank_name column), and the call language follows the agent’s own rule — neither changes per campaign.</p>
          </div>
        </div>
      )}

      {/* ── Step 3: Leads ─────────────────────────────────────────── */}
      {step === 2 && campaign && (
        <LeadsUpload campaignId={campaign.campaign_id}
          identifierLabel={campaign.identifier_label}
          expectedColumns={expectedColumns}
          onDone={refreshCampaign} />
      )}

      {/* ── Step 4: Schedule ──────────────────────────────────────── */}
      {step === 3 && (
        <div>
          <div className="card">
            <div className="form-grid">
              <div className="field"><label>Start date</label>
                <input type="date" value={schedule.start_date} onChange={setS('start_date')} /></div>
              <div className="field"><label>Calling days</label>
                <select value={schedule.calling_days} onChange={setS('calling_days')}>
                  <option>Mon – Sat</option><option>Mon – Fri</option><option>Every day</option>
                </select></div>
              <div className="field"><label>Calling window — from</label>
                <input type="time" value={schedule.window_start} onChange={setS('window_start')} /></div>
              <div className="field"><label>Calling window — to</label>
                <input type="time" value={schedule.window_end} onChange={setS('window_end')} /></div>
              <div className="field"><label>Max attempts per lead</label>
                <input type="number" min="1" max="10" value={retry.max_attempts} onChange={setR('max_attempts')} /></div>
              <div className="field"><label>Gap between attempts (days)</label>
                <input type="number" min="0" max="30" value={retry.retry_gap_days} onChange={setR('retry_gap_days')} /></div>
              <div className="field"><label>Daily call cap (whole campaign)</label>
                <input type="number" min="0" value={schedule.daily_cap} onChange={setS('daily_cap')} /></div>
              <div className="field"><label>Retry at a different time of day</label>
                <select value={retry.retry_different_time ? 'yes' : 'no'}
                  onChange={(e) => setRetry({ ...retry, retry_different_time: e.target.value === 'yes' })}>
                  <option value="yes">Yes — vary the time of day</option>
                  <option value="no">No — same time</option>
                </select></div>
            </div>
          </div>
          <div className="notes-panel" style={{ marginTop: 16 }}>
            <p>The scheduling step is the same for every agent: outside the window, unanswered leads wait — nobody is called at night. Do-not-call numbers are excluded automatically, and a lead who asks not to be called is never retried. (These rules take effect when the dispatch engine lands — A5/A10; here they are stored as the campaign’s configuration.)</p>
          </div>
        </div>
      )}

      {/* ── Step 5: Review ────────────────────────────────────────── */}
      {step === 4 && campaign && (
        <div>
          {[
            ['Agent', `${agent?.name || campaign.agent_name} — from the agent’s configuration (model, voice, language rule, its single prompt)`],
            ['Details', `Product: ${campaign.product_label}${campaign.product_detail ? ` — ${campaign.product_detail}` : ''} · Channel: ${campaign.channel} · Via: ${campaign.provider_mode === 'managed' ? 'Managed platform' : 'Self-hosted stack'}${campaign.purpose ? ` · Purpose: ${campaign.purpose}` : ''}${campaign.audience ? ` · For: ${campaign.audience}` : ''}${campaign.settlement_authority_pct != null ? ` · Authority cap: ${campaign.settlement_authority_pct}%` : ''}`],
            ['Leads', `${campaign.lead_count} leads loaded for this campaign`],
            ['Schedule', `${schedule.start_date ? `Starts ${schedule.start_date} · ` : ''}${schedule.calling_days}, ${schedule.window_start}–${schedule.window_end} · up to ${retry.max_attempts} attempts per lead · daily cap ${schedule.daily_cap}`],
          ].map(([t, b], i) => (
            <div className="review-block" key={t}>
              <div className="t">{t}</div><div className="b">{b}</div>
              <button className="edit" onClick={() => setStep([0, 1, 2, 3][i])}>Edit</button>
            </div>
          ))}
          <p style={{ color: 'var(--slate)', fontSize: 13.5 }}>
            Launching marks the campaign active; calls begin at the scheduled window once the dispatch engine
            is connected (A10). You can pause the campaign at any time from its page.
          </p>
        </div>
      )}

      {/* ── Footer ────────────────────────────────────────────────── */}
      <div className="wizard-foot">
        {step === 0 && <button className="btn secondary" onClick={onExit}>Cancel</button>}
        {step > 0 && <button className="btn secondary" onClick={() => setStep(step - 1)}>Back</button>}
        {step === 0 && <button className="btn" disabled={!agentId} onClick={() => setStep(1)}>Continue →</button>}
        {step === 1 && <button className="btn" disabled={busy || !details.name.trim()} onClick={createCampaign}>
          {busy ? 'Creating…' : 'Continue →'}</button>}
        {step === 2 && <button className="btn" onClick={() => { refreshCampaign(); setStep(3) }}>Continue →</button>}
        {step === 3 && <button className="btn" disabled={busy} onClick={saveSchedule}>{busy ? 'Saving…' : 'Continue →'}</button>}
        {step === 4 && <>
          <button className="btn secondary" disabled={busy} onClick={onExit}>Save as draft</button>
          <button className="btn" disabled={busy} onClick={launch}>{busy ? 'Launching…' : 'Launch campaign'}</button>
        </>}
      </div>
    </div>
  )
}
