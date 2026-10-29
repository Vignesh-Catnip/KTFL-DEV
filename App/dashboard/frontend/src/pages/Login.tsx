import { useState, type FormEvent } from 'react'
import { useAuth } from '../auth'
import logoLeft from '../assets/logo-left.png'

export default function Login() {
  const { login } = useAuth()
  const [u, setU] = useState('')
  const [p, setP] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true); setErr('')
    try { await login(u.trim(), p) } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <img src={logoLeft} alt="" style={{ height: 34, marginBottom: 16 }} />
        <h1>ClearMatch</h1>
        <p>SAP MIRO invoice verification</p>
        {err && <div className="err" role="alert">{err}</div>}
        <label htmlFor="u">User name</label>
        <input id="u" value={u} onChange={e => setU(e.target.value)} autoFocus autoComplete="username" required />
        <label htmlFor="p">Password</label>
        <input id="p" type="password" value={p} onChange={e => setP(e.target.value)} autoComplete="current-password" required />
        <button className="btn primary lg" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </div>
  )
}
