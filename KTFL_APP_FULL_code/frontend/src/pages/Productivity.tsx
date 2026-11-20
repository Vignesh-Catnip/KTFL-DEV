import { useCallback, useEffect, useState } from 'react'
import { api, type Stats } from '../api'
import { Icon, ICONS } from '../ui'

// ── constants ──────────────────────────────────────────────────────────────
const SYSTEM_MIN_PER_INV = 1.5  // 1 min extraction + 0.5 min SAP posting
const MANUAL_MIN_PER_INV = 4    // manual client process (review + post)
const SAVED_MIN_PER_INV  = MANUAL_MIN_PER_INV - SYSTEM_MIN_PER_INV

function fmtMins(mins: number) {
  if (mins === 0) return '0s'
  const totalSecs = Math.round(mins * 60)
  const h = Math.floor(totalSecs / 3600)
  const m = Math.floor((totalSecs % 3600) / 60)
  const s = totalSecs % 60
  if (h > 0) return s > 0 ? `${h}h ${m}m ${s}s` : m > 0 ? `${h}h ${m}m` : `${h}h`
  if (m > 0) return s > 0 ? `${m}m ${s}s` : `${m}m`
  return `${s}s`
}

function fmtMinsLong(mins: number) {
  if (mins === 0) return '0 seconds'
  const totalSecs = Math.round(mins * 60)
  const h = Math.floor(totalSecs / 3600)
  const m = Math.floor((totalSecs % 3600) / 60)
  const s = totalSecs % 60
  const parts = []
  if (h > 0) parts.push(`${h} hour${h > 1 ? 's' : ''}`)
  if (m > 0) parts.push(`${m} min`)
  if (s > 0) parts.push(`${s} sec`)
  return parts.join(' ')
}

// ── small reusable card ─────────────────────────────────────────────────────
function KPI({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: string }) {
  return (
    <div className="stat" style={{ minWidth: 160 }}>
      <div className="top">{label}</div>
      <div className="val num" style={accent ? { color: accent } : undefined}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

// ── breakdown table row ─────────────────────────────────────────────────────
function Row({ label, count, systemMins, manualMins, savedMins }: { label: string; count: number; systemMins: number; manualMins: number; savedMins: number }) {
  if (count === 0) return null
  return (
    <tr>
      <td>{label}</td>
      <td className="num" style={{ textAlign: 'right' }}>{count}</td>
      <td className="num" style={{ textAlign: 'right' }}>{fmtMins(systemMins)}</td>
      <td className="num" style={{ textAlign: 'right' }}>{fmtMins(manualMins)}</td>
      <td className="num" style={{ textAlign: 'right', color: '#3a7d44', fontWeight: 600 }}>{fmtMins(savedMins)}</td>
    </tr>
  )
}

// ── main page ───────────────────────────────────────────────────────────────
export default function Productivity() {
  const [stats, setStats]   = useState<Stats | null>(null)
  const [err,   setErr]     = useState('')

  const load = useCallback(() => {
    api.stats().then(s => { setStats(s); setErr('') }).catch(e => setErr(e.message))
  }, [])
  useEffect(() => { load() }, [load])

  const s = stats
  const total     = s?.total ?? 0
  const matched   = s?.queues.MATCHED   ?? 0
  const reconcile = s?.queues.RECONCILE ?? 0
  const blocked   = s?.queues.BLOCKED   ?? 0

  const systemMinsTotal = total * SYSTEM_MIN_PER_INV
  const manualMinsTotal = total * MANUAL_MIN_PER_INV
  const savedMinsTotal  = total * SAVED_MIN_PER_INV

  const straightThrough = matched

  return (
    <section>
      <div className="crumb">Finance Operations&nbsp; / &nbsp;<b>Time Savings</b></div>
      <div className="page-head">
        <div>
          <h1>Time Savings</h1>
          <p>How much time the system saves compared to fully manual invoice processing.</p>
        </div>
        <button className="btn" onClick={load}><Icon d={ICONS.refresh} size={15} />Refresh</button>
      </div>
      {err && <div className="err" role="alert">{err}</div>}

      {/* ── top KPI row ── */}
      <div className="stats">
        <KPI label="Total invoices processed" value={s ? String(total) : '–'} sub={s ? `${s.pending_jobs} extracting now` : ''} />
        <KPI label="System processing time" value={s ? fmtMins(systemMinsTotal) : '–'} sub={`${SYSTEM_MIN_PER_INV} min per invoice`} />
        <KPI label="Equivalent manual time" value={s ? fmtMins(manualMinsTotal) : '–'} sub={`${MANUAL_MIN_PER_INV} min per invoice`} />
        <KPI label="Time saved" value={s ? fmtMins(savedMinsTotal) : '–'} sub={`${SAVED_MIN_PER_INV} min saved per invoice`} accent="#3a7d44" />
      </div>

      {/* ── breakdown panel ── */}
      <div className="panel" style={{ marginTop: 24 }}>
        <div style={{ padding: '16px 20px 8px', fontWeight: 600, fontSize: 15 }}>Processing time breakdown by queue</div>
        <div style={{ padding: '0 20px 20px' }}>
          <table>
            <thead>
              <tr>
                <th>Queue</th>
                <th style={{ textAlign: 'right' }}>Invoices</th>
                <th style={{ textAlign: 'right' }}>System time</th>
                <th style={{ textAlign: 'right' }}>Manual equivalent</th>
                <th style={{ textAlign: 'right' }}>Time saved</th>
              </tr>
            </thead>
            <tbody>
              <Row label="Matched (straight-through)" count={matched}   systemMins={matched   * SYSTEM_MIN_PER_INV} manualMins={matched   * MANUAL_MIN_PER_INV} savedMins={matched   * SAVED_MIN_PER_INV} />
              <Row label="Reconcile (needs review)"   count={reconcile} systemMins={reconcile * SYSTEM_MIN_PER_INV} manualMins={reconcile * MANUAL_MIN_PER_INV} savedMins={reconcile * SAVED_MIN_PER_INV} />
              <Row label="Blocked"                    count={blocked}   systemMins={blocked   * SYSTEM_MIN_PER_INV} manualMins={blocked   * MANUAL_MIN_PER_INV} savedMins={blocked   * SAVED_MIN_PER_INV} />
              {total > 0 && (
                <tr style={{ fontWeight: 700, borderTop: '2px solid #e0e0e0' }}>
                  <td>Total</td>
                  <td className="num" style={{ textAlign: 'right' }}>{total}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{fmtMins(systemMinsTotal)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{fmtMins(manualMinsTotal)}</td>
                  <td className="num" style={{ textAlign: 'right', color: '#3a7d44' }}>{fmtMins(savedMinsTotal)}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── per-invoice assumptions panel ── */}
      <div className="panel" style={{ marginTop: 24 }}>
        <div style={{ padding: '16px 20px 8px', fontWeight: 600, fontSize: 15 }}>Assumptions</div>
        <div style={{ padding: '0 20px 20px' }}>
          <table>
            <thead>
              <tr><th>Step</th><th style={{ textAlign: 'right' }}>System (per invoice)</th><th style={{ textAlign: 'right' }}>Manual (per invoice)</th></tr>
            </thead>
            <tbody>
              <tr><td>Invoice field extraction (AI / OCR)</td><td className="num" style={{ textAlign: 'right' }}>1 min</td><td className="num" style={{ textAlign: 'right' }}>—</td></tr>
              <tr><td>SAP PO / GRN validation</td><td className="num" style={{ textAlign: 'right' }}>included</td><td className="num" style={{ textAlign: 'right' }}>—</td></tr>
              <tr><td>MIRO SAP posting (robot)</td><td className="num" style={{ textAlign: 'right' }}>0.5 min</td><td className="num" style={{ textAlign: 'right' }}>—</td></tr>
              <tr><td>Manual review &amp; SAP entry (client)</td><td className="num" style={{ textAlign: 'right' }}>—</td><td className="num" style={{ textAlign: 'right' }}>4 min</td></tr>
              <tr style={{ fontWeight: 700 }}><td>Total per invoice</td><td className="num" style={{ textAlign: 'right' }}>{SYSTEM_MIN_PER_INV} min</td><td className="num" style={{ textAlign: 'right' }}>{MANUAL_MIN_PER_INV} min</td></tr>
            </tbody>
          </table>
          <p style={{ marginTop: 12, fontSize: 13, color: '#888' }}>
            Time saved = {SAVED_MIN_PER_INV} min × {total} invoices = <b>{fmtMinsLong(savedMinsTotal)}</b>.
            Straight-through invoices (Matched): <b>{straightThrough}</b> posted automatically without any manual intervention.
          </p>
        </div>
      </div>
    </section>
  )
}
