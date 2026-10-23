import React, { useRef, useState } from 'react'
import { apiUpload } from '../api'

/* Leads CSV upload with column mapping — wireframe leads-* (Step 3).
   Two server steps: preview (headers + sample + suggested mapping) and
   upload (file + confirmed mapping). Only THIS campaign's leads are stored;
   duplicates (same phone already in the campaign) are skipped. */
const TARGETS = [
  ['ignore', 'Ignore column'],
  ['name', 'Name'],
  ['phone_number', 'Phone number *'],
  ['bank_name', 'Bank name'],
  ['state', 'State'],
  ['product_identifier', 'Product identifier'],
  ['agent_field', 'Agent field (keep as data)'],
]

export default function LeadsUpload({ campaignId, identifierLabel, expectedColumns, onDone }) {
  const [file, setFile] = useState(null)
  const [preview, setPreview] = useState(null)
  const [mapping, setMapping] = useState({})
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inputRef = useRef(null)

  async function pick(f) {
    if (!f) return
    setFile(f); setError(''); setResult(null); setBusy(true)
    try {
      const fd = new FormData()
      fd.append('file', f)
      const p = await apiUpload(`/api/campaigns/${campaignId}/leads/preview`, fd)
      setPreview(p)
      setMapping(p.suggested_mapping || {})
    } catch (e) { setError(e.message); setPreview(null) }
    finally { setBusy(false) }
  }

  async function upload() {
    setError(''); setBusy(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('mapping', JSON.stringify(mapping))
      const r = await apiUpload(`/api/campaigns/${campaignId}/leads/upload`, fd)
      setResult(r)
      if (onDone) onDone(r)
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const phoneMapped = Object.values(mapping).includes('phone_number')

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {expectedColumns?.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 8 }}>
            The file for this campaign must have these columns (one list per campaign):
          </div>
          <div>{expectedColumns.map((c) => <span key={c} className="chip">{c}</span>)}</div>
        </div>
      )}

      <div className="dropzone" onClick={() => inputRef.current?.click()}>
        <div className="big">{file ? file.name : 'Drop the leads CSV here, or click to browse'}</div>
        <div className="sub">
          {busy ? 'Reading file…'
            : preview ? `${preview.filename} — ${preview.total_rows} rows · check the mapping below, then upload`
            : 'The identifier column follows the campaign’s product' +
              (identifierLabel ? ` — here: ${identifierLabel.toLowerCase()}` : '')}
        </div>
        <input ref={inputRef} type="file" accept=".csv,text/csv" style={{ display: 'none' }}
          onChange={(e) => pick(e.target.files?.[0])} />
      </div>

      {preview && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3 style={{ fontSize: 15, marginBottom: 4 }}>Column mapping (checked automatically — adjust if needed)</h3>
          <div className="sub" style={{ color: 'var(--slate)', fontSize: 13, marginBottom: 14 }}>
            Sample: {preview.sample_rows?.[0]
              ? Object.values(preview.sample_rows[0]).filter(Boolean).slice(0, 4).join(' · ')
              : '—'}
          </div>
          {preview.headers.map((h) => (
            <div className="map-row" key={h}>
              <span className="col">{h}</span>
              <span className="arrow">→</span>
              <select className="map-target" value={mapping[h] || 'ignore'}
                onChange={(e) => setMapping({ ...mapping, [h]: e.target.value })}>
                {TARGETS.map(([v, lbl]) => (
                  <option key={v} value={v}>{v === 'product_identifier' ? `Product identifier (${identifierLabel || 'per product'})` : lbl}</option>
                ))}
              </select>
            </div>
          ))}
          <div style={{ marginTop: 14, display: 'flex', gap: 12, alignItems: 'center' }}>
            <button className="btn" onClick={upload} disabled={busy || !phoneMapped}>
              {busy ? 'Uploading…' : `Upload ${preview.total_rows} rows`}
            </button>
            {!phoneMapped && <span style={{ color: 'var(--danger)', fontSize: 13 }}>Map one column to Phone number first.</span>}
          </div>
          {result && (
            <div className="ok-note" style={{ marginTop: 14, marginBottom: 0 }}>
              {result.created} leads added · {result.duplicates_skipped} duplicates skipped · {result.invalid_skipped} invalid rows skipped
            </div>
          )}
        </div>
      )}
    </div>
  )
}
