import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, getToken, setToken } from './api'

interface User { username: string; name: string; role: string }
interface Ctx { user: User | null; loading: boolean; login: (u: string, p: string) => Promise<void>; logout: () => void; can: (role: 'approver' | 'admin') => boolean }
const AuthCtx = createContext<Ctx>(null as unknown as Ctx)
export const useAuth = () => useContext(AuthCtx)
const RANK: Record<string, number> = { viewer: 1, approver: 2, admin: 3 }

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(!!getToken())

  useEffect(() => {
    if (getToken()) api.me().then(setUser).catch(() => setToken(null)).finally(() => setLoading(false))
    const out = () => setUser(null)
    window.addEventListener('clearmatch:logout', out)
    return () => window.removeEventListener('clearmatch:logout', out)
  }, [])

  const login = async (u: string, p: string) => {
    const r = await api.login(u, p)
    setToken(r.token)
    setUser(r.user)
  }
  const logout = () => { setToken(null); setUser(null) }
  const can = (role: 'approver' | 'admin') => (RANK[user?.role ?? ''] ?? 0) >= RANK[role]
  return <AuthCtx.Provider value={{ user, loading, login, logout, can }}>{children}</AuthCtx.Provider>
}
