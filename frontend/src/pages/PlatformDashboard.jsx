import React, { useEffect, useState } from 'react'
import { api } from '../api'

/* Platform Dashboard — Super Admin only (Epic C). The platform home:
   tenants, catalog agents + latest versions, and the adoption log.
   No tenant customer data ever appears here — tenant records and
   catalog metadata only. */
export default function PlatformDashboard() {
  const [tenants, setTenants] = useState(null)
  const [catalog, setCatalog] = useState(null)
  const [log, setLog] = useState(null)
  const [error, setError] = useState('')

  async function load() {
    setError('')
    try {
      const [t, c, l] = await Promise.all([
        api('/api/tenants'),
        api('/api/catalog/agents'),
        api('/api/catalog/adoption-log'),
      ])
      setTenants(t); setCatalog(c); setLog(l)
    } catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  const versionCount = (catalog || []).reduce((n, ca) => n + (ca.versions?.length || 0), 0)

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Platform Dashboard</h1>
          <div className="sub">
            The platform at a glance — tenants, catalog agents, and who adopted what.
            No tenant customer data lives on this page by design.
          </div>
        </div>
        <button className="btn small secondary" onClick={load}>Refresh</button>
      </div>

      {error && <div className="error">{error}</div>}
      {(!tenants || !catalog || !log) && !error && <div className="sub">Loading…</div>}

      {tenants && catalog && log && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="n">{tenants.length}</div>
              <div className="l">Tenants</div>
            </div>
            <div className="stat-card">
              <div className="n">{catalog.length}</div>
              <div className="l">Catalog agents</div>
            </div>
            <div className="stat-card">
              <div className="n">{versionCount}</div>
              <div className="l">Published versions</div>
            </div>
            <div className="stat-card">
              <div className="n">{log.length}</div>
              <div className="l">Adoptions recorded</div>
            </div>
          </div>

          <h3 className="section-title">Tenants</h3>
          {tenants.length === 0 && <p style={{ color: 'var(--slate)' }}>No tenants yet — create the first one on the Tenants page.</p>}
          <div className="grid">
            {tenants.map((t) => (
              <div key={t.tenant_id} className="card">
                <h3>{t.name}</h3>
                <dl className="kv">
                  <dt>Market</dt><dd>{t.market}</dd>
                  <dt>Timezone</dt><dd>{t.timezone}</dd>
                  <dt>Mode</dt><dd>{t.provider_mode_default}</dd>
                  <dt>Concurrency</dt><dd>{t.concurrency_limit}</dd>
                </dl>
              </div>
            ))}
          </div>

          <h3 className="section-title">Catalog</h3>
          {catalog.length === 0 && <p style={{ color: 'var(--slate)' }}>No catalog agents yet — author the first one on the Catalog page.</p>}
          <div className="grid">
            {catalog.map((ca) => (
              <div key={ca.catalog_agent_id} className="card">
                <h3>{ca.name}</h3>
                <div className="purpose">{ca.purpose || ca.stage}</div>
                <dl className="kv">
                  <dt>Slug</dt><dd>{ca.slug}</dd>
                  <dt>Latest</dt><dd>{ca.latest_version ? `version ${ca.latest_version}` : 'no versions yet'}</dd>
                  <dt>Versions</dt><dd>{ca.versions?.length || 0}</dd>
                </dl>
              </div>
            ))}
          </div>

          <h3 className="section-title">Adoption log</h3>
          {log.length === 0 && <p style={{ color: 'var(--slate)' }}>No adoptions yet — tenants adopt catalog versions from their Agents page.</p>}
          {log.slice(0, 25).map((e, i) => (
            <div key={`${e.tenant_id}-${e.catalog_agent_id}-${e.catalog_version}-${i}`} className="card" style={{ marginBottom: 10 }}>
              <dl className="kv" style={{ marginTop: 0 }}>
                <dt>Tenant</dt><dd>{e.tenant_slug}</dd>
                <dt>Agent</dt><dd>{e.catalog_agent_id} · version {e.catalog_version}</dd>
                <dt>Adopted by</dt><dd>{e.adopted_by}</dd>
                <dt>When</dt><dd>{e.adopted_at || '—'}</dd>
              </dl>
            </div>
          ))}
        </>
      )}
    </div>
  )
}
