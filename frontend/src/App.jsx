import React, { useEffect, useState } from 'react'
import Agents from './pages/Agents'
import AgentEditor from './pages/AgentEditor'
import Campaigns from './pages/Campaigns'
import CampaignWizard from './pages/CampaignWizard'
import CampaignDetail from './pages/CampaignDetail'
import Settings from './pages/Settings'
import Calls from './pages/Calls'
import Dashboard from './pages/Dashboard'
import BrowserTest from './pages/BrowserTest'
import PlatformDashboard from './pages/PlatformDashboard'
import Tenants from './pages/Tenants'
import CatalogAuthoring from './pages/CatalogAuthoring'
import Setup from './pages/Setup'
import { api, clearSession, getUser, setSession } from './api'

const NAV = ['Dashboard', 'Campaigns', 'Agents', 'Calls', 'Browser Test', 'Journey', 'Human Queue', 'Settings']
const ENABLED = ['Dashboard', 'Campaigns', 'Agents', 'Calls', 'Browser Test', 'Settings'] // later chunks enable the rest
const NAV_VIEW = { Dashboard: 'dashboard', Campaigns: 'campaigns', Agents: 'agents', Calls: 'calls', 'Browser Test': 'browsertest', Settings: 'settings' }
// Super Admin (Epic C): platform tabs ONLY — tenant tabs (Dashboard,
// Campaigns, Calls, Settings, …) are never rendered for this role, so
// the user is never shown a page the backend will then forbid.
const SUPER_NAV = ['Platform Dashboard', 'Catalog', 'Tenants']
const SUPER_VIEW = { 'Platform Dashboard': 'platform', Catalog: 'catalogadmin', Tenants: 'tenants' }
const SUPER_VIEWS = ['platform', 'catalogadmin', 'tenants']
const ROLE_LABEL = { super_admin: 'Super Admin', tenant_admin: 'Tenant Admin', team_leader: 'Team Leader', caller: 'Caller' }

export default function App() {
  const [user, setUser] = useState(getUser())
  const [view, setView] = useState({ name: 'dashboard' })

  // Older stored sessions predate the role field on login: refresh it
  // once from /api/users/me so the sidebar gates on a real role.
  useEffect(() => {
    if (user && !user.role) {
      api('/api/users/me').then((me) => setUser({ ...user, ...me })).catch(() => {})
    }
  }, [user])

  // Public invite setup takes precedence over the login gate (and
  // over any existing session): a setup link must complete even for
  // a logged-in operator, and success replaces the session with the
  // new account. nginx falls back to index.html for /setup, so a
  // hard-loaded invite link lands here with its query params.
  const setupParams = new URLSearchParams(window.location.search)
  const setupInvite = setupParams.get('invite')
  const setupToken = setupParams.get('token')
  const clearSetupParams = () => { window.history.replaceState({}, '', '/') }
  if (setupInvite && setupToken) {
    return <Setup
      inviteId={setupInvite}
      token={setupToken}
      onDone={(u) => { clearSetupParams(); setUser(u) }}
      onBack={() => { clearSetupParams(); window.location.reload() }}
    />
  }

  if (!user) return <Login onDone={(u) => setUser(u)} />

  const isSuper = user.role === 'super_admin'
  // A Super Admin never lands on a tenant view (e.g. a stale default):
  // tenant views resolve to the Platform Dashboard for this role.
  const effectiveView = isSuper && !SUPER_VIEWS.includes(view.name) ? 'platform' : view.name

  const activeNav = isSuper
    ? (effectiveView === 'platform' ? 'Platform Dashboard'
      : effectiveView === 'catalogadmin' ? 'Catalog'
      : 'Tenants')
    : effectiveView === 'editor' ? 'Agents'
    : ['campaigns', 'wizard', 'campaign'].includes(effectiveView) ? 'Campaigns'
    : effectiveView === 'settings' ? 'Settings'
    : effectiveView === 'calls' ? 'Calls'
    : effectiveView === 'browsertest' ? 'Browser Test'
    : effectiveView === 'dashboard' ? 'Dashboard' : 'Agents'

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">VOICE AGENT PLATFORM</div>
        <nav>
          {isSuper
            ? SUPER_NAV.map((item) => (
              <button
                key={item}
                className={item === activeNav ? 'active' : ''}
                onClick={() => setView({ name: SUPER_VIEW[item] })}
              >{item}</button>
            ))
            : NAV.map((item) => (
              <button
                key={item}
                className={item === activeNav ? 'active' : ''}
                disabled={!ENABLED.includes(item)}
                title={ENABLED.includes(item) ? '' : 'Arrives in a later build chunk'}
                onClick={() => ENABLED.includes(item) && setView({ name: NAV_VIEW[item] })}
              >{item}</button>
            ))}
        </nav>
        <div className="tenant">
          Tenant<b>{user.tenant_name || '—'}</b>
          <span style={{ fontSize: 12.5 }}>{user.email}</span>
          {user.role && <span style={{ fontSize: 12.5 }}>{ROLE_LABEL[user.role] || user.role}</span>}
          <button className="logout" onClick={() => { clearSession(); setUser(null) }}>Log out</button>
        </div>
      </aside>
      <main className="main">
        {effectiveView === 'platform' && <PlatformDashboard />}
        {effectiveView === 'catalogadmin' && <CatalogAuthoring />}
        {effectiveView === 'tenants' && <Tenants />}
        {effectiveView === 'dashboard' && (
          <Dashboard
            onOpenCalls={() => setView({ name: 'calls' })}
            onOpenCampaigns={() => setView({ name: 'campaigns' })}
          />
        )}
        {effectiveView === 'agents' && (
          <Agents
            onOpen={(id) => setView({ name: 'editor', id })}
            onNew={() => setView({ name: 'editor', id: null })}
          />
        )}
        {effectiveView === 'editor' && (
          <AgentEditor agentId={view.id} onBack={() => setView({ name: 'agents' })} />
        )}
        {effectiveView === 'campaigns' && (
          <Campaigns
            onOpen={(id) => setView({ name: 'campaign', id })}
            onNew={() => setView({ name: 'wizard' })}
          />
        )}
        {effectiveView === 'wizard' && (
          <CampaignWizard
            onLaunched={(id) => setView({ name: 'campaign', id })}
            onExit={() => setView({ name: 'campaigns' })}
          />
        )}
        {effectiveView === 'campaign' && (
          <CampaignDetail campaignId={view.id} onBack={() => setView({ name: 'campaigns' })} />
        )}
        {effectiveView === 'settings' && <Settings />}
        {effectiveView === 'calls' && <Calls />}
        {effectiveView === 'browsertest' && <BrowserTest />}
      </main>
    </div>
  )
}

function Login({ onDone }) {
  const [email, setEmail] = useState('demo@qgbs.example')
  const [password, setPassword] = useState('demo-password')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      const data = await api('/api/auth/login', { method: 'POST', body: { email, password } })
      setSession(data)
      onDone({ ...data.user, tenant_name: data.tenant_name })
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <h1>Voice Agent Platform</h1>
        <div className="sub">Sign in to the operator console</div>
        {error && <div className="error">{error}</div>}
        <form onSubmit={submit}>
          <div className="field"><label>Email</label>
            <input value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" /></div>
          <div className="field" style={{ marginTop: 12 }}><label>Password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></div>
          <button className="btn" style={{ width: '100%', marginTop: 18 }} disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}</button>
        </form>
        <p className="footnote" style={{ marginTop: 16 }}>
          Seeded demo login: demo@qgbs.example / demo-password (tenant: QGBS).
        </p>
      </div>
    </div>
  )
}
