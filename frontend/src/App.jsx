import React, { useState } from 'react'
import Agents from './pages/Agents'
import AgentEditor from './pages/AgentEditor'
import Campaigns from './pages/Campaigns'
import CampaignWizard from './pages/CampaignWizard'
import CampaignDetail from './pages/CampaignDetail'
import Settings from './pages/Settings'
import Calls from './pages/Calls'
import Dashboard from './pages/Dashboard'
import BrowserTest from './pages/BrowserTest'
import { api, clearSession, getUser, setSession } from './api'

const NAV = ['Dashboard', 'Campaigns', 'Agents', 'Calls', 'Browser Test', 'Journey', 'Human Queue', 'Settings']
const ENABLED = ['Dashboard', 'Campaigns', 'Agents', 'Calls', 'Browser Test', 'Settings'] // later chunks enable the rest
const NAV_VIEW = { Dashboard: 'dashboard', Campaigns: 'campaigns', Agents: 'agents', Calls: 'calls', 'Browser Test': 'browsertest', Settings: 'settings' }

export default function App() {
  const [user, setUser] = useState(getUser())
  const [view, setView] = useState({ name: 'dashboard' })

  if (!user) return <Login onDone={(u) => setUser(u)} />

  const activeNav = view.name === 'editor' ? 'Agents'
    : ['campaigns', 'wizard', 'campaign'].includes(view.name) ? 'Campaigns'
    : view.name === 'settings' ? 'Settings'
    : view.name === 'calls' ? 'Calls'
    : view.name === 'browsertest' ? 'Browser Test'
    : view.name === 'dashboard' ? 'Dashboard' : 'Agents'

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">VOICE AGENT PLATFORM</div>
        <nav>
          {NAV.map((item) => (
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
          <button className="logout" onClick={() => { clearSession(); setUser(null) }}>Log out</button>
        </div>
      </aside>
      <main className="main">
        {view.name === 'dashboard' && (
          <Dashboard
            onOpenCalls={() => setView({ name: 'calls' })}
            onOpenCampaigns={() => setView({ name: 'campaigns' })}
          />
        )}
        {view.name === 'agents' && (
          <Agents
            onOpen={(id) => setView({ name: 'editor', id })}
            onNew={() => setView({ name: 'editor', id: null })}
          />
        )}
        {view.name === 'editor' && (
          <AgentEditor agentId={view.id} onBack={() => setView({ name: 'agents' })} />
        )}
        {view.name === 'campaigns' && (
          <Campaigns
            onOpen={(id) => setView({ name: 'campaign', id })}
            onNew={() => setView({ name: 'wizard' })}
          />
        )}
        {view.name === 'wizard' && (
          <CampaignWizard
            onLaunched={(id) => setView({ name: 'campaign', id })}
            onExit={() => setView({ name: 'campaigns' })}
          />
        )}
        {view.name === 'campaign' && (
          <CampaignDetail campaignId={view.id} onBack={() => setView({ name: 'campaigns' })} />
        )}
        {view.name === 'settings' && <Settings />}
        {view.name === 'calls' && <Calls />}
        {view.name === 'browsertest' && <BrowserTest />}
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
