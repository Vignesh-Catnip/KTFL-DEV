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
        <div><h1>Error Flow</h1><p>{d ? `${d.total} invoice${d.total === 1 ? "" : "s"}` : "…"} need a person before posting. For every invoice you see what the invoice says, what SQL says, the difference and what to do - line by line.</p></div>
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
        {d?.items.map(i => (
          <div key={i.id} className="errcard" style={{ borderTop: '1px solid var(--line, #e3e8ef)', padding: '14px 16px' }}>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <b className="mono" style={{ cursor: 'pointer', color: '#006CB7' }} onClick={() => nav(`/invoice/${i.id}`)}>{i.invoice_number || '—'}</b>
              <span>{i.vendor || '—'}</span>
              <span className="muted">PO {i.po_number || '—'} · GRN {i.grn_number || '—'}</span>
              <QueueTag q={i.queue} />
              <span className="muted">{i.exception_type}</span>
              <span style={{ marginLeft: 'auto' }}>
                {i.variance_amount ? <span className="varc p">Variance {inr(i.variance_amount)}</span> : null}
                <button className="btn sm" style={{ marginLeft: 10 }} onClick={() => nav(`/invoice/${i.id}`)}>Open &amp; fix</button>
              </span>
            </div>
            {i.issues.length > 0 && (
              <table className="ltable" style={{ marginTop: 10, width: '100%' }}>
                <thead><tr><th>Problem</th><th>Invoice says</th><th>SQL (GRN / PO) says</th><th style={{ textAlign: 'right' }}>Difference</th><th>What to do</th></tr></thead>
                <tbody>
                  {i.issues.map((x, k) => (
                    <tr key={k}>
                      <td>{x.message}</td>
                      <td className="mono">{x.invoice_value || '—'}</td>
                      <td className="mono">{x.sap_value || '—'}</td>
                      <td className="mono" style={{ textAlign: 'right' }}>{x.variance ? inr(x.variance) : '—'}</td>
                      <td className="muted">{x.fix}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {i.bad_lines.length > 0 && (
              <>
                <div style={{ marginTop: 10, fontWeight: 600, fontSize: 13 }}>Line items that do not agree ({i.bad_lines.length})</div>
                <table className="ltable" style={{ marginTop: 4, width: '100%' }}>
                  <thead><tr><th>Item</th><th>Problem</th><th style={{ textAlign: 'right' }}>Invoice qty</th><th style={{ textAlign: 'right' }}>GRN qty</th><th style={{ textAlign: 'right' }}>Invoice amount</th><th style={{ textAlign: 'right' }}>GRN amount</th><th>GRN row</th></tr></thead>
                  <tbody>
                    {i.bad_lines.map((l, k) => (
                      <tr key={k} title={l.fix}>
                        <td>{l.item.slice(0, 48)}</td>
                        <td><b>{l.problem}</b><div className="muted" style={{ fontSize: 12 }}>{l.fix}</div></td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.quantity}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.gr_quantity ?? '—'}{l.diff_quantity ? <span className="muted"> ({l.diff_quantity > 0 ? '+' : ''}{l.diff_quantity})</span> : null}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{inr(l.amount)}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.gr_amount == null ? '—' : inr(l.gr_amount)}{l.diff_amount ? <span className="muted"> ({l.diff_amount > 0 ? '+' : ''}{inr(l.diff_amount)})</span> : null}</td>
                        <td className="mono">{l.gr_row || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        ))}
        {d && d.items.length === 0 && <div className="empty">No open exceptions 🎉</div>}
      </div>
    </section>
  )
}
