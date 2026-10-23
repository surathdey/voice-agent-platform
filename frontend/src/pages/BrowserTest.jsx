import React, { useEffect, useRef, useState } from 'react'
import { api, getToken } from '../api'

/* Browser test (A11) — talk to an agent from the console, with no phone
   and no telephony provider, to hear and judge it before any live call.

   The page is a TRANSPORT for the same conversation engine the Plivo
   stream runs: the microphone is captured with Web Audio, downsampled
   to PCM16 @16 kHz and shipped over the harness WebSocket; the server
   converts it to the μ-law frames the STT leg eats (so the agent hears
   what a phone callee's audio becomes), and the agent's replies come
   back as PCM16 @8 kHz binary messages, scheduled into Web Audio here.

   Barge-in rides the engine's own interrupt (an STT partial cuts the
   reply server-side); the server then sends an `interrupt` event and
   this page drops its playback queue and stops the sounding source —
   the browser flavour of Plivo's clearAudio, except exact: the buffer
   lives in this page. Use headphones where possible — through speakers
   the agent can hear itself (the mic asks for echo cancellation, which
   helps but is not perfect).

   A test call is a CallSession with channel "browser-test": it lands in
   Calls and the dashboard counts with that marker, and it NEVER
   consumes a campaign lead's attempts — a picked lead is snapshotted,
   not dialled. */

const WORKLET_CODE = `
class VapCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this._sum = 0; this._count = 0
    this._win = Math.max(1, Math.round(sampleRate / 16000))
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (!ch) return true
    const out = []
    for (let i = 0; i < ch.length; i++) {
      this._sum += ch[i]; this._count += 1
      if (this._count >= this._win) {
        const v = Math.max(-1, Math.min(1, this._sum / this._count))
        out.push(v < 0 ? v * 0x8000 : v * 0x7fff)
        this._sum = 0; this._count = 0
      }
    }
    if (out.length) {
      const buf = new Int16Array(out)
      this.port.postMessage(buf.buffer, [buf.buffer])
    }
    return true
  }
}
registerProcessor('vap-capture', VapCapture)
`

export default function BrowserTest() {
  const [agents, setAgents] = useState([])
  const [campaigns, setCampaigns] = useState([])
  const [leads, setLeads] = useState([])
  const [agentId, setAgentId] = useState('')
  const [campaignId, setCampaignId] = useState('')
  const [leadId, setLeadId] = useState('')          // '' = type test values
  const [manual, setManual] = useState({ name: 'Test Customer', phone_number: '9000000099', bank_name: 'Tata Capital', state: 'West Bengal', product_identifier: '' })
  const [phase, setPhase] = useState('idle')        // idle | connecting | live | ended | error
  const [error, setError] = useState('')
  const [lines, setLines] = useState([])
  const [partial, setPartial] = useState('')
  const [endInfo, setEndInfo] = useState(null)
  const [language, setLanguage] = useState('')

  const wsRef = useRef(null)
  const audioRef = useRef(null)
  const scrollRef = useRef(null)

  useEffect(() => {
    (async () => {
      try {
        const [a, c] = await Promise.all([api('/api/agents'), api('/api/campaigns')])
        setAgents(a || []); setCampaigns(c || [])
      } catch (e) { setError(e.message) }
    })()
    return () => teardown()
  }, [])

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [lines, partial])

  async function pickCampaign(id) {
    setCampaignId(id); setLeadId(''); setLeads([])
    if (!id) return
    try {
      const data = await api(`/api/campaigns/${id}/leads`)
      setLeads(data?.leads || [])
    } catch { /* the picker is a convenience — typing values always works */ }
  }

  function teardown() {
    const a = audioRef.current
    if (a) {
      try { a.stream?.getTracks().forEach((t) => t.stop()) } catch {}
      try { a.node?.disconnect() } catch {}
      try { a.src?.disconnect() } catch {}
      a.sources.forEach((s) => { try { s.stop() } catch {} })
      a.sources.clear()
      audioRef.current = null
    }
    if (wsRef.current) {
      try { wsRef.current.close() } catch {}
      wsRef.current = null
    }
  }

  function playPcm(buf) {
    const a = audioRef.current
    if (!a?.ctx) return
    const pcm = new Int16Array(buf)
    if (!pcm.length) return
    const audio = a.ctx.createBuffer(1, pcm.length, 8000)
    const ch = audio.getChannelData(0)
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 0x8000
    const src = a.ctx.createBufferSource()
    src.buffer = audio
    src.connect(a.ctx.destination)
    const startAt = Math.max(a.ctx.currentTime + 0.03, a.nextTime || 0)
    src.start(startAt)
    a.nextTime = startAt + audio.duration
    a.sources.add(src)
    src.onended = () => a.sources.delete(src)
  }

  function flushPlayback() {
    const a = audioRef.current
    if (!a) return
    a.sources.forEach((s) => { try { s.stop() } catch {} })
    a.sources.clear()
    a.nextTime = 0
  }

  async function startMic(ws) {
    const ctx = new AudioContext()
    await ctx.resume()
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
    const a = { ctx, stream, sources: new Set(), nextTime: 0, node: null, src: null }
    audioRef.current = a
    const src = ctx.createMediaStreamSource(stream)
    a.src = src
    const send = (buf) => { if (ws.readyState === WebSocket.OPEN) ws.send(buf) }
    if (ctx.audioWorklet) {
      const url = URL.createObjectURL(new Blob([WORKLET_CODE], { type: 'application/javascript' }))
      await ctx.audioWorklet.addModule(url)
      URL.revokeObjectURL(url)
      const node = new AudioWorkletNode(ctx, 'vap-capture')
      node.port.onmessage = (e) => send(e.data)
      src.connect(node)
      a.node = node
    } else {
      // Fallback for browsers without AudioWorklet: ScriptProcessor
      // (deprecated, but functional) with the same averaging downsample.
      const node = ctx.createScriptProcessor(4096, 1, 1)
      const ratio = Math.max(1, Math.round(ctx.sampleRate / 16000))
      let sum = 0, count = 0
      node.onaudioprocess = (e) => {
        const ch = e.inputBuffer.getChannelData(0)
        const out = []
        for (let i = 0; i < ch.length; i++) {
          sum += ch[i]; count += 1
          if (count >= ratio) {
            const v = Math.max(-1, Math.min(1, sum / count))
            out.push(v < 0 ? v * 0x8000 : v * 0x7fff)
            sum = 0; count = 0
          }
        }
        if (out.length) send(new Int16Array(out).buffer)
      }
      const mute = ctx.createGain(); mute.gain.value = 0
      src.connect(node); node.connect(mute); mute.connect(ctx.destination)
      a.node = node
    }
  }

  async function start() {
    setError(''); setLines([]); setPartial(''); setEndInfo(null); setPhase('connecting')
    let created
    try {
      const body = { agent_id: agentId }
      if (campaignId) body.campaign_id = campaignId
      if (campaignId && leadId) body.lead_id = leadId
      else body.lead = { ...manual }
      created = await api('/api/browser-tests', { method: 'POST', body })
    } catch (e) { setError(e.message); setPhase('error'); return }

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(
      `${proto}//${window.location.host}${created.stream_path}?token=${encodeURIComponent(getToken() || '')}`)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') { playPcm(ev.data); return }
      let msg
      try { msg = JSON.parse(ev.data) } catch { return }
      if (msg.event === 'ready') {
        setLanguage(msg.language || '')
        setPhase('live')
        startMic(ws).catch((e) => {
          setError(`Microphone unavailable: ${e.message}. The test session was created but no audio could be captured — allow microphone access and run a new test.`)
          try { ws.close() } catch {}
        })
      } else if (msg.event === 'transcript') {
        setPartial('')
        setLines((prev) => [...prev, msg.turn])
      } else if (msg.event === 'partial') {
        setPartial(msg.text || '')
      } else if (msg.event === 'interrupt') {
        flushPlayback()
      } else if (msg.event === 'ended') {
        setEndInfo(msg)
        setPhase('ended')
        const a = audioRef.current
        if (a) { try { a.stream?.getTracks().forEach((t) => t.stop()) } catch {} }
      } else if (msg.event === 'error') {
        setError(msg.message || `The test could not run (${msg.reason || 'error'}).`)
        setPhase('error')
      }
    }
    ws.onclose = () => {
      setPhase((p) => (p === 'live' || p === 'connecting' ? 'ended' : p))
    }
    ws.onerror = () => {
      setError('The test stream connection failed.')
      setPhase((p) => (p === 'ended' ? p : 'error'))
    }
  }

  function end() {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'end' })) } catch {}
    }
    const a = audioRef.current
    if (a) { try { a.stream?.getTracks().forEach((t) => t.stop()) } catch {} }
    // The server finalizes on the close and sends `ended`; give it a
    // moment, then close our side regardless.
    setTimeout(() => { try { ws?.close() } catch {} }, 600)
  }

  const agentCampaigns = campaigns.filter((c) => c.agent_id === agentId)
  const pickedLead = leads.find((l) => l.lead_id === leadId)
  const live = phase === 'live' || phase === 'connecting'

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Browser test</h1>
          <div className="sub">
            Talk to an agent from here — no phone, no telephony provider — and judge it
            before any live call. The same STT → LLM → TTS loop as a real self-hosted call.
          </div>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="form-grid">
          <div className="field">
            <label>Agent</label>
            <select value={agentId} disabled={live}
              onChange={(e) => { setAgentId(e.target.value); setCampaignId(''); setLeadId(''); setLeads([]) }}>
              <option value="">Pick an agent…</option>
              {agents.map((a) => <option key={a.agent_id} value={a.agent_id}>{a.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Campaign (optional — context only)</label>
            <select value={campaignId} disabled={live || !agentId}
              onChange={(e) => pickCampaign(e.target.value)}>
              <option value="">No campaign</option>
              {agentCampaigns.map((c) => <option key={c.campaign_id} value={c.campaign_id}>{c.name}</option>)}
            </select>
            <div className="hint">Supplies the product and a lead list to pick from. A test is never attached to the campaign's attempts.</div>
          </div>
          {campaignId && (
            <div className="field">
              <label>Saved lead</label>
              <select value={leadId} disabled={live} onChange={(e) => setLeadId(e.target.value)}>
                <option value="">Type test values instead…</option>
                {leads.map((l) => (
                  <option key={l.lead_id} value={l.lead_id}>
                    {l.name || l.phone_number} · {l.phone_number}
                  </option>
                ))}
              </select>
              {pickedLead && (
                <div className="hint">
                  The agent will address this lead's snapshot ({pickedLead.name}, {pickedLead.state}).
                  Their attempt count ({pickedLead.attempt_count}) is not consumed and no retry is created.
                </div>
              )}
            </div>
          )}
        </div>

        {(!campaignId || !leadId) && (
          <div className="form-grid" style={{ marginTop: 10 }}>
            <div className="field">
              <label>Test name</label>
              <input value={manual.name} disabled={live}
                onChange={(e) => setManual({ ...manual, name: e.target.value })} />
            </div>
            <div className="field">
              <label>Test phone (display only — nothing is dialled)</label>
              <input value={manual.phone_number} disabled={live}
                onChange={(e) => setManual({ ...manual, phone_number: e.target.value })} />
            </div>
            <div className="field">
              <label>Bank name</label>
              <input value={manual.bank_name} disabled={live}
                onChange={(e) => setManual({ ...manual, bank_name: e.target.value })} />
            </div>
            <div className="field">
              <label>State (drives the agent's language rule)</label>
              <input value={manual.state} disabled={live}
                onChange={(e) => setManual({ ...manual, state: e.target.value })} />
            </div>
            <div className="field">
              <label>Product identifier (optional)</label>
              <input value={manual.product_identifier} disabled={live}
                onChange={(e) => setManual({ ...manual, product_identifier: e.target.value })} />
            </div>
          </div>
        )}

        <div style={{ marginTop: 14, display: 'flex', gap: 10, alignItems: 'center' }}>
          {!live ? (
            <button className="btn" disabled={!agentId || phase === 'connecting'} onClick={start}>
              {phase === 'connecting' ? 'Connecting…' : 'Start test call'}
            </button>
          ) : (
            <button className="btn" style={{ background: 'var(--danger)' }} onClick={end}>
              End test call
            </button>
          )}
          {phase === 'live' && (
            <span className="pill teal">Live{language ? ` · ${language}` : ''} — the agent speaks first; just answer</span>
          )}
          {phase === 'ended' && endInfo && (
            <span className="pill slate">
              Ended · {endInfo.end_reason || 'closed'}{endInfo.disposition ? ` · ${endInfo.disposition}` : ''}
            </span>
          )}
          {phase === 'ended' && !endInfo && <span className="pill slate">Ended</span>}
        </div>
      </div>

      {(lines.length > 0 || partial || live) && (
        <div className="card">
          <h3 style={{ marginBottom: 10 }}>Live transcript</h3>
          <div ref={scrollRef} style={{ maxHeight: 380, overflowY: 'auto' }}>
            {lines.map((t, i) => (
              <div key={i} style={{ fontSize: 14, margin: '4px 0' }}>
                <span className={`pill ${t.speaker === 'agent' ? 'navy' : 'slate'}`}>{t.speaker}</span>{' '}
                {t.text}
                {t.interrupted && <em style={{ color: 'var(--gold)' }}> (interrupted — only the words actually spoken)</em>}
              </div>
            ))}
            {partial && (
              <div style={{ fontSize: 14, margin: '4px 0' }}>
                <span className="pill slate">customer</span> {partial}
                <em style={{ color: 'var(--slate)' }}> (live…)</em>
              </div>
            )}
            {!lines.length && !partial && (
              <span style={{ color: 'var(--slate)' }}>Waiting for the agent's opening line…</span>
            )}
          </div>
        </div>
      )}

      <p className="footnote" style={{ marginTop: 16 }}>
        Use headphones where possible: through speakers the agent may hear its own voice
        (echo cancellation is requested, but imperfect). Your microphone audio is converted
        to the same μ-law frames a phone call produces, so what you hear is what a callee
        hears. The finished test appears in <b>Calls</b> and the dashboard counts, marked
        <b> Browser test</b> — it is never a campaign attempt. A session whose stream never
        connects simply stays QUEUED.
      </p>
    </div>
  )
}
