import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api, type Stats } from './api'
import { useAuth } from './auth'
import { Icon, ICONS } from './ui'
import logoLeft from './assets/logo-left.png'
import logoRight from './assets/logo-right.png'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Recon from './pages/Recon'
import Exceptions from './pages/Exceptions'
import Posting from './pages/Posting'
import Audit from './pages/Audit'
import Jobs from './pages/Jobs'

function Shell() {
  const { user, logout } = useAuth()
  const [stats, setStats] = useState<Stats | null>(null)
  const [health, setHealth] = useState<{ sap: boolean; ollama: boolean } | null>(null)
  const loc = useLocation()

  useEffect(() => {
    let live = true
    const tick = () => {
      api.stats().then(s => live && setStats(s)).catch(() => {})
      api.health().then(h => live && setHealth({ sap: h.sap.connected, ollama: h.ollama.ok })).catch(() => {})
    }
    tick()
    const t = setInterval(tick, 15000)
    return () => { live = false; clearInterval(t) }
  }, [loc.pathname])

  const open = stats ? (stats.queues.RECONCILE ?? 0) + (stats.queues.BLOCKED ?? 0) : 0
  const initials = (user?.name || user?.username || '?').split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase()
  return (
    <>
      <header className="appbar">
        <div className="appbar-inner">
          <div className="brand-left">
            <img src={logoLeft} alt="" />
            <div className="brand-div" />
            <div className="product"><b>SAP MIRO Invoice Verification</b></div>
          </div>
          <div className="appbar-spacer" />
          <span className="sap-pill" style={health && !health.sap ? { color: '#8b1a1a', background: '#fde8e8', borderColor: '#f09090' } : undefined}>
            <span className="dot" style={health && !health.sap ? { background: '#8b1a1a' } : undefined} />
            {health == null ? 'Checking SAP…' : health.sap ? 'SAP connected' : 'SAP not reachable'}
          </span>
          {health && !health.ollama && <span className="tag fail" title="Extraction model is not reachable">AI model offline</span>}
          <div className="brand-right"><span className="by">Powered by<br />Catnip Infotech</span><img src={logoRight} alt="" /></div>
        </div>
        <div className="brand-accent" />
      </header>
      <div className="shell">
        <aside className="side">
          <div className="side-inner">
            <div className="grp">Workspace</div>
            <NavLink className={({ isActive }) => 'nav-i' + (isActive ? ' on' : '')} to="/" end>
              <Icon d={ICONS.grid} />Invoices<span className="cnt">{stats?.total ?? '–'}</span>
            </NavLink>
            <NavLink className={({ isActive }) => 'nav-i' + (isActive ? ' on' : '')} to="/exceptions">
              <Icon d={ICONS.warn} />Error Flow<span className="cnt">{open}</span>
            </NavLink>
            <NavLink className={({ isActive }) => 'nav-i' + (isActive ? ' on' : '')} to="/posting">
              <Icon d={ICONS.chart} />Auto Flow SAP Logs
            </NavLink>
            <NavLink className={({ isActive }) => 'nav-i' + (isActive ? ' on' : '')} to="/jobs">
              <Icon d={ICONS.upload} />Auto Extraction{!!stats?.pending_jobs && <span className="cnt">{stats.pending_jobs}</span>}
            </NavLink>
            <NavLink className={({ isActive }) => 'nav-i' + (isActive ? ' on' : '')} to="/audit">
              <Icon d={ICONS.doc} />Audit Trail
            </NavLink>
            {stats && stats.plants.length > 0 && <div className="grp" style={{ marginTop: 20 }}>Plants</div>}
            {stats?.plants.slice(0, 6).map(p => (
              <NavLink key={p.plant} className="nav-i" to={`/?plant=${encodeURIComponent(p.plant === '-' ? '' : p.plant)}`}>
                <Icon d={['M3 21V7l9-4 9 4v14', 'M9 21v-6h6v6']} />Plant {p.plant}<span className="cnt">{p.count}</span>
              </NavLink>
            ))}
            <div className="side-user" style={{ marginTop: 'auto' }}>
              <div className="av">{initials}</div>
              <div style={{ minWidth: 0, flex: 1 }}><b>{user?.name || user?.username}</b><span>{user?.role}</span></div>
              <button className="btn ghost sm" style={{ color: '#fff', padding: '4px 8px', flexShrink: 0 }} onClick={logout} aria-label="Sign out" title="Sign out">⎋</button>
            </div>
          </div>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/invoice/:id" element={<Recon />} />
            <Route path="/exceptions" element={<Exceptions />} />
            <Route path="/posting" element={<Posting />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </>
  )
}

export default function App() {
  const { user, loading } = useAuth()
  if (loading) return <div className="login-wrap"><span className="spin" /></div>
  return user ? <Shell /> : <Login />
}
