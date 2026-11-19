import { useEffect, useState } from 'react'
import { api } from '../api'
import { Icon, ICONS, dt } from '../ui'

export default function Audit() {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof api.audit>>>([])
  const [q, setQ] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => { api.audit(500).then(setRows).catch(e => setErr(e.message)) }, [])
  const f = rows.filter(r => !q || `${r.action} ${r.actor} ${r.invoice_id} ${JSON.stringify(r.detail)}`.toLowerCase().includes(q.toLowerCase()))
  return (
    <section>
      <div className="crumb">Finance Operations&nbsp; / &nbsp;<b>Audit Trail</b></div>
      <div className="page-head">
        <div><h1>Audit trail</h1><p>Every extraction, edit, resolution and push — who and when.</p></div>
        <button className="btn" onClick={() => api.download('/api/audit/export.csv', 'audit_trail.csv')}><Icon d={ICONS.dl} size={15} />Export CSV</button>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="panel">
        <div className="toolbar"><div className="search"><Icon d={ICONS.search} size={16} /><input placeholder="Filter…" value={q} onChange={e => setQ(e.target.value)} aria-label="Filter" /></div></div>
        <table>
          <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Invoice</th><th>Detail</th></tr></thead>
          <tbody>
            {f.map(r => (
              <tr key={r.id}>
                <td className="date">{dt(r.at)}</td><td className="who">{r.actor || 'system'}</td>
                <td><span className="tag grey">{r.action}</span></td><td className="mono">{r.invoice_id ?? '—'}</td>
                <td className="issue mono" style={{ fontSize: 11.5 }}>{JSON.stringify(r.detail).slice(0, 220)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {f.length === 0 && <div className="empty">No entries.</div>}
      </div>
    </section>
  )
}
