import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type InvoiceRow } from '../api'
import { useAuth } from '../auth'
import { Icon, ICONS, MiroTag, Stat, dt, inr, useToast } from '../ui'

const FILTERS = [['', 'All'], ['WAITING', 'Waiting'], ['POSTED', 'Posted'], ['PARKED', 'Parked'], ['CHECKED', 'Checked'], ['FAILED', 'Failed'], ['REVIEW', 'Review'], ['UNKNOWN', 'Unknown']] as const

export default function Posting() {
  const nav = useNavigate()
  const toast = useToast()
  const { can } = useAuth()
  const [f, setF] = useState('')
  const [d, setD] = useState<{ counts: Record<string, number>; items: InvoiceRow[] } | null>(null)
  const [err, setErr] = useState('')
  const load = () => api.posting(f || undefined).then(x => { setD(x); setErr('') }).catch(e => setErr(e.message))
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t) }, [f])
  const n = (k: string) => d?.counts[k] ?? 0

  const requeue = async (id: number) => {
    try { await api.requeue(id); toast('Re-queued', 'The robot will try this invoice again'); load() }
    catch (e) { toast('Cannot re-queue', (e as Error).message, true) }
  }
  return (
    <section>
      <div className="crumb">Finance Operations&nbsp; / &nbsp;<b>Auto Flow SAP Logs</b></div>
      <div className="page-head">
        <div><h1>Auto Flow · SAP posting log</h1><p>What the MIRO robot did with every matched invoice.</p></div>
        <button className="btn" onClick={() => api.download('/api/posting/export.csv' + (f ? `?status=${f}` : ''), 'sap_posting_log.csv')}>
          <Icon d={ICONS.dl} size={15} />Export CSV
        </button>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="stats">
        <Stat cls="b" label="Posted" value={n('POSTED')} subCls="up" sub="MIRO document created" />
        <Stat cls="a" label="Parked / checked" value={n('PARKED') + n('CHECKED')} />
        <Stat cls="d" label="Waiting for robot" value={n('WAITING')} />
        <Stat cls="c" label="Needs attention" value={n('FAILED') + n('REVIEW') + n('UNKNOWN')} subCls="warn" sub="failed / review / unknown" />
      </div>
      <div className="panel">
        <div className="tabs">
          {FILTERS.map(([v, l]) => (
            <button key={v} className={'tabbtn' + (f === v ? ' on' : '')} onClick={() => setF(v)}>{l}{v && <span className="n">{n(v)}</span>}</button>
          ))}
        </div>
        <table>
          <thead><tr><th>Invoice</th><th>Vendor</th><th>PO / GRN</th><th style={{ textAlign: 'right' }}>Amount</th><th>SAP status</th><th>SAP document</th><th>Robot message</th><th>Updated</th><th></th></tr></thead>
          <tbody>
            {d?.items.map(i => (
              <tr key={i.id}>
                <td className="mono poref click" style={{ cursor: 'pointer' }} onClick={() => nav(`/invoice/${i.id}`)}>{i.invoice_number}</td>
                <td className="vend"><b>{i.vendor}</b></td>
                <td className="poref">{i.po_number}<span>{i.grn_number}</span></td>
                <td className="amt num" style={{ textAlign: 'right' }}>{inr(i.total)}</td>
                <td><MiroTag s={i.miro_status} />{!!i.miro_attempts && <span className="muted" style={{ marginLeft: 6, fontSize: 12 }}>×{i.miro_attempts}</span>}</td>
                <td className="mono">{i.miro_doc_number || '—'}</td>
                <td className="issue">{i.miro_message || '—'}</td>
                <td className="date">{dt(i.miro_updated_at ?? i.pushed_at)}</td>
                <td>{can('approver') && ['FAILED', 'REVIEW', 'CHECKED'].includes(i.miro_status ?? '') && <button className="btn sm" onClick={() => requeue(i.id)}>Re-queue</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {d && d.items.length === 0 && <div className="empty">Nothing here.</div>}
      </div>
    </section>
  )
}
