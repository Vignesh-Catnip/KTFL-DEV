import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'

export const inr = (n: number | null | undefined, dec = 2) =>
  n == null ? '—' : '₹' + n.toLocaleString('en-IN', { minimumFractionDigits: dec, maximumFractionDigits: dec })
export const inrShort = (n: number) =>
  n >= 1e7 ? `₹${(n / 1e7).toFixed(2)}Cr` : n >= 1e5 ? `₹${(n / 1e5).toFixed(1)}L` : inr(n, 0)
export const dt = (s?: string | null) => (s ? new Date(s).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—')

export const Icon = ({ d, size = 17 }: { d: string | string[]; size?: number }) => (
  <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    {(Array.isArray(d) ? d : [d]).map((p, i) => <path key={i} d={p} />)}
  </svg>
)
export const ICONS = {
  grid: ['M3 3h7v7H3z', 'M14 3h7v7h-7z', 'M3 14h7v7H3z', 'M14 14h7v7h-7z'],
  warn: ['M12 3 2 20h20L12 3Z', 'M12 10v4', 'M12 17h.01'],
  chart: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  doc: ['M9 12h6M9 16h6M9 8h6', 'M6 3h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z'],
  upload: ['M12 16V4', 'm7 9 5-5 5 5', 'M4 20h16'],
  back: 'm15 18-6-6 6-6',
  search: ['M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z', 'm21 21-4.3-4.3'],
  check: 'M20 6 9 17l-5-5',
  x: ['M18 6 6 18', 'M6 6l12 12'],
  refresh: ['M21 12a9 9 0 1 1-3-6.7L21 8', 'M21 3v5h-5'],
  dl: ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'M7 10l5 5 5-5', 'M12 15V3'],
  arrow: ['M5 12h14', 'M13 6l6 6-6 6'],
}

const QTAG: Record<string, [string, string]> = {
  MATCHED: ['match', 'Matched'], RECONCILE: ['review', 'Reconcile'], BLOCKED: ['blocked', 'Blocked'],
}
export const QueueTag = ({ q }: { q: string }) => {
  const [c, l] = QTAG[q] ?? ['grey', q]
  return <span className={`tag ${c}`}>{l}</span>
}
const MTAG: Record<string, [string, string]> = {
  POSTED: ['match', 'Posted'], PARKED: ['info', 'Parked in SAP'], CHECKED: ['info', 'Checked (not saved)'],
  FAILED: ['fail', 'Failed'], REVIEW: ['review', 'Robot review'], UNKNOWN: ['blocked', 'Unknown - check SAP'],
  IN_PROGRESS: ['info', 'Posting…'], WAITING: ['grey', 'Waiting for robot'],
}
export const MiroTag = ({ s }: { s: string | null }) => {
  const [c, l] = MTAG[s ?? 'WAITING'] ?? ['grey', s ?? '']
  return <span className={`tag ${c}`}>{l}</span>
}

const BAR_COLOR: Record<string, string> = { a: '#3f8fcc', b: '#5aad80', c: '#e0a85c', d: '#8f8f8f', e: '#8f8f8f' }
export const Stat = ({ cls, label, value, sub, subCls, icon, bars }: { cls: string; label: string; value: ReactNode; sub?: ReactNode; subCls?: string; icon?: string | string[]; bars?: number[] }) => {
  const max = Math.max(1, ...(bars ?? [0]))
  return (
    <div className={`stat ${cls}`}>
      <div className="top">{label}{icon && <span className="ic"><Icon d={icon} size={16} /></span>}</div>
      <div className="val num">{value}</div>
      {bars && (
        <div className="chart" aria-hidden="true" data-testid="chart">
          {bars.map((b, i) => (
            <span key={i} className="bar" title={`${b}`} style={{ height: `${Math.max(8, Math.round((b / max) * 100))}%`, background: BAR_COLOR[cls] ?? '#8f8f8f', opacity: 0.35 + 0.65 * (i / Math.max(1, bars.length - 1)) }} />
          ))}
        </div>
      )}
      {sub != null && <div className={`sub ${subCls ?? ''}`}>{sub}</div>}
    </div>
  )
}

type Toast = { id: number; title: string; sub?: string; bad?: boolean }
const ToastCtx = createContext<(title: string, sub?: string, bad?: boolean) => void>(() => {})
export const useToast = () => useContext(ToastCtx)
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const push = useCallback((title: string, sub?: string, bad?: boolean) => {
    const id = Date.now() + Math.random()
    setItems(x => [...x, { id, title, sub, bad }])
    setTimeout(() => setItems(x => x.filter(t => t.id !== id)), 4500)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-wrap" role="status" aria-live="polite">
        {items.map(t => (
          <div className="toast" key={t.id} style={t.bad ? { background: '#8b1a1a' } : undefined}>
            <span className="ic"><Icon d={t.bad ? ICONS.x : ICONS.check} size={15} /></span>
            <div><b>{t.title}</b>{t.sub && <small>{t.sub}</small>}</div>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
