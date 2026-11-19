import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, type InvoiceRow, type Stats } from '../api'
import { Icon, ICONS, QueueTag, MiroTag, Stat, inr, inrShort } from '../ui'

const TABS = [['', 'All'], ['MATCHED', 'Matched'], ['RECONCILE', 'Reconcile'], ['BLOCKED', 'Blocked']] as const

export default function Dashboard() {
  const nav = useNavigate()
  const [sp, setSp] = useSearchParams()
  const queue = sp.get('queue') ?? ''
  const plant = sp.get('plant') ?? ''
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState<{ total: number; items: InvoiceRow[] } | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [err, setErr] = useState('')
  const size = 25

  const load = useCallback(() => {
    api.invoices({ queue, plant, q, page, size }).then(d => { setData(d); setErr('') }).catch(e => setErr(e.message))
    api.stats().then(setStats).catch(() => {})
  }, [queue, plant, q, page])
  useEffect(() => { const t = setTimeout(load, q ? 250 : 0); return () => clearTimeout(t) }, [load, q])
  useEffect(() => setPage(1), [queue, plant, q])

  const s = stats
  const totalValue = s ? Object.values(s.value).reduce((a, b) => a + b, 0) : 0
  const setQueue = (v: string) => { const n = new URLSearchParams(sp); v ? n.set('queue', v) : n.delete('queue'); setSp(n) }
  const pages = data ? Math.max(1, Math.ceil(data.total / size)) : 1

  return (
    <section>
      <div className="crumb">Finance Operations&nbsp; / &nbsp;<b>Invoices</b></div>
      <div className="page-head">
        <div><h1>Invoices</h1><p>Extracted invoices checked against SAP goods receipts and purchase orders.</p></div>
        <button className="btn" onClick={load}><Icon d={ICONS.refresh} size={15} />Refresh</button>
      </div>
      {err && <div className="err" role="alert">{err}</div>}
      <div className="stats">
        <Stat cls="a" icon={ICONS.doc} bars={s?.trend?.TOTAL} label="Total invoices" value={s?.total ?? '–'} sub={s ? `${s.pending_jobs} extracting now` : ''} />
        <Stat cls="b" icon={ICONS.check} bars={s?.trend?.MATCHED} label="Matched" value={s?.queues.MATCHED ?? '–'} subCls="up" sub={s ? `${s.straight_through_pct}% straight-through` : ''} />
        <Stat cls="c" icon={ICONS.warn} bars={s?.trend?.RECONCILE} label="Reconcile" value={s?.queues.RECONCILE ?? '–'} subCls="warn" sub="data differs from SAP" />
        <Stat cls="d" icon={ICONS.x} bars={s?.trend?.BLOCKED} label="Blocked" value={s?.queues.BLOCKED ?? '–'} sub={s ? `${inrShort(totalValue)} total value` : ''} />
      </div>
      <div className="panel">
        <div className="tabs" role="tablist">
          {TABS.map(([v, l]) => (
            <button key={v} role="tab" aria-selected={queue === v} className={'tabbtn' + (queue === v ? ' on' : '')} onClick={() => setQueue(v)}>
              {l}<span className="n">{v ? s?.queues[v] ?? 0 : s?.total ?? 0}</span>
            </button>
          ))}
        </div>
        <div className="toolbar">
          <div className="search">
            <Icon d={ICONS.search} size={16} />
            <input placeholder="Search invoice, vendor, PO or GRN…" value={q} onChange={e => setQ(e.target.value)} aria-label="Search" />
          </div>
          {plant && <button className="btn sm" onClick={() => { const n = new URLSearchParams(sp); n.delete('plant'); setSp(n) }}>Plant {plant} ✕</button>}
        </div>
        <table>
          <thead><tr><th>Vendor</th><th>Invoice</th><th>PO / GRN</th><th>Date</th><th style={{ textAlign: 'right' }}>Amount</th><th>Read</th><th>Status</th><th>Why</th><th>SAP</th></tr></thead>
          <tbody>
            {data?.items.map(i => (
              <tr key={i.id} className="click" onClick={() => nav(`/invoice/${i.id}`)}>
                <td className="vend"><b>{i.vendor || '—'}</b><span>{i.vendor_code ? `V-${i.vendor_code}` : 'no vendor code'}{i.plant_code ? ` · ${i.plant_code}` : ''}</span></td>
                <td className="mono poref">{i.invoice_number || '—'}</td>
                <td className="poref">{i.po_number || '—'}<span>{i.grn_number || 'no GRN'}</span></td>
                <td className="date">{i.invoice_date || '—'}</td>
                <td className="amt num" style={{ textAlign: 'right' }}>{inr(i.total)}</td>
                <td className="num" title="Share of mandatory fields found on the invoice" style={i.completeness != null && i.completeness < 1 ? { color: '#7a4800', fontWeight: 700 } : undefined}>{i.completeness == null ? '—' : `${Math.round(i.completeness * 100)}%`}</td>
                <td><QueueTag q={i.queue} />{i.multi_po_flag && <span className="tag info" style={{ marginLeft: 6 }} title="Invoice is booked against several POs in SAP">Multi-PO</span>}</td>
                <td className="issue">{i.top_issue || (i.queue === 'MATCHED' ? 'All checks passed' : '')}</td>
                <td>{i.queue === 'MATCHED' ? <MiroTag s={i.miro_status} /> : <span className="muted">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.items.length === 0 && <div className="empty">No invoices yet. Drop invoice PDFs into the watch folder — they appear here automatically (see “Auto Extraction”).</div>}
        {!data && !err && <div className="empty"><span className="spin" /></div>}
        <div className="pager">
          <span>{data?.total ?? 0} invoices</span>
          <button className="btn sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page} / {pages}</span>
          <button className="btn sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      </div>
    </section>
  )
}
