import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { Icon, ICONS, QueueTag, Stat, inr } from '../ui'

type Data = Awaited<ReturnType<typeof api.exceptions>>

export default function Exceptions() {
  const nav = useNavigate()
  const [d, setD] = useState<Data | null>(null)
  const [cat, setCat] = useState('')
  const [err, setErr] = useState('')
  const load = () => api.exceptions(cat || undefined).then(x => { setD(x); setErr('') }).catch(e => setErr(e.message))
  useEffect(() => { load() }, [cat])
  const c = (k: string) => d?.by_type[k] ?? 0

  return (
    <section>
      <div className="crumb">Finance Operations&nbsp; / &nbsp;<b>Error Flow</b></div>
      <div className="page-head">
        <div><h1>Error Flow</h1><p>{d ? `${d.total} invoices` : '…'} need manual intervention before SAP posting. Line-item mismatches are listed per line.</p></div>
        <button className="btn" onClick={load}><Icon d={ICONS.refresh} size={15} />Refresh</button>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="stats" style={{ gridTemplateColumns: 'repeat(5,1fr)' }}>
        <Stat cls="c" label="Quantity mismatch" value={c('Quantity mismatch')} />
        <Stat cls="a" label="Price variance" value={c('Price variance')} />
        <Stat cls="e" label="Tax / HSN" value={c('Tax / HSN')} />
        <Stat cls="d" label="Data missing (partly read)" value={c('Data missing')} />
        <Stat cls="a" label="Blocked" value={c('Blocked')} />
      </div>
      <div className="panel">
        <div className="toolbar">
          <div className="sel">
            <select value={cat} onChange={e => setCat(e.target.value)} aria-label="Exception type">
              <option value="">All types</option>
              {['Data missing', 'Quantity mismatch', 'Price variance', 'Tax / HSN', 'Other mismatch', 'Blocked'].map(t => <option key={t}>{t}</option>)}
            </select>
          </div>
        </div>
        <table>
          <thead><tr><th>Vendor</th><th>Invoice</th><th>Exception</th><th>What is wrong</th><th style={{ textAlign: 'right' }}>Variance</th><th></th></tr></thead>
          <tbody>
            {d?.items.map(i => (
              <tr key={i.id} className="click" onClick={() => nav(`/invoice/${i.id}`)}>
                <td className="vend"><b>{i.vendor || '—'}</b><span>{i.vendor_code ? `V-${i.vendor_code}` : ''}</span></td>
                <td className="mono poref">{i.invoice_number || '—'}<span>{i.po_number}</span></td>
                <td><QueueTag q={i.queue} /><div className="muted" style={{ marginTop: 4, fontSize: 12 }}>{i.exception_type}</div></td>
                <td className="issue">
                  {i.issues.slice(0, 3).map((x, k) => <div key={k}>{x.message}</div>)}
                  {i.issues.length > 3 && <div className="muted">+{i.issues.length - 3} more</div>}
                  {i.bad_lines.length > 0 && (
                    <table className="ltable" style={{ marginTop: 6 }}><tbody>
                      {i.bad_lines.slice(0, 4).map((l, k) => (
                        <tr key={k}><td>{l.item.slice(0, 32)}</td><td>qty {l.quantity} / GR {l.gr_quantity ?? '—'}</td><td>{inr(l.amount)} / GR {l.gr_amount == null ? '—' : inr(l.gr_amount)}</td></tr>
                      ))}
                    </tbody></table>
                  )}
                </td>
                <td className={'varc ' + (i.variance_amount ? 'p' : 'z')} style={{ textAlign: 'right' }}>{i.variance_amount ? inr(i.variance_amount) : '—'}</td>
                <td><button className="btn sm">Resolve</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {d && d.items.length === 0 && <div className="empty">No open exceptions 🎉</div>}
      </div>
    </section>
  )
}
