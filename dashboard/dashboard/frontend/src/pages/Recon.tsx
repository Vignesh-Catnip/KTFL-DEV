import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, type CompareRow, type InvoiceDetail } from '../api'
import { useAuth } from '../auth'
import { Icon, ICONS, MiroTag, QueueTag, dt, inr, useToast } from '../ui'

const EDITABLE: Record<string, string> = { po_number: 'po_number', grn_number: 'grn_number', hsn_sac: 'hsn_sac', invoice_date: 'invoice_date' }
const RESOLUTIONS = [
  ['Invoice corrected to GRN', false], ['Accept variance - approved deviation', true],
  ['Raise query with vendor', false], ['Debit note requested', false],
] as const

function Stat({ s }: { s: CompareRow['status'] }) {
  if (s === 'OK') return <span className="mstat ok" title="Matches"><Icon d={ICONS.check} size={14} /></span>
  if (s === 'NA') return <span className="mstat na">–</span>
  return <span className="mstat bad" title={s === 'MISSING' ? 'Missing' : 'Does not match'}><Icon d={ICONS.x} size={14} /></span>
}

function InvoicePages({ id, pages }: { id: number; pages: number }) {
  const [p, setP] = useState(1)
  const [src, setSrc] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => {
    let url = ''
    setSrc(''); setErr('')
    api.blobUrl(`/api/invoices/${id}/page/${p}.png`).then(u => { url = u; setSrc(u) }).catch(() => setErr('The original PDF is not available on the server.'))
    return () => { if (url) URL.revokeObjectURL(url) }
  }, [id, p])
  return (
    <div>
      {pages > 1 && (
        <div className="pagebar">
          <button className="btn sm" disabled={p <= 1} onClick={() => setP(p - 1)}>‹</button>
          <span>Page {p} of {pages}</span>
          <button className="btn sm" disabled={p >= pages} onClick={() => setP(p + 1)}>›</button>
        </div>
      )}
      {err ? <div className="empty">{err}</div> : src ? <img className="imgpane" src={src} alt={`Invoice page ${p}`} /> : <div className="empty"><span className="spin" /></div>}
    </div>
  )
}

const s = (v: unknown) => (v == null ? '' : String(v).trim())
function GrnCopy({ d }: { d: InvoiceDetail }) {
  const rows = d.compare.grn_rows
  if (!rows.length) return <div className="empty">No goods receipt found in SAP for this invoice.</div>
  const f = rows[0]
  return (
    <div className="paper">
      <div className="paper-h">
        <div className="co">Goods Receipt<small>FROM SAP · MSEG</small></div>
        <div className="dt"><small>GRN no.</small><span className="mono">{s(f.MBLNR)}</span><br /><small style={{ marginTop: 6 }}>Posted</small>{s(f.BUDAT_MKPF)}</div>
      </div>
      <div className="pmeta">
        <div><div className="k">Against PO</div><div className="val mono">{s(f.EBELN)}</div></div>
        <div><div className="k">Movement · plant</div><div className="val mono">{s(f.BWART)} · {s(f.WERKS)}</div></div>
      </div>
      <table className="ptable">
        <thead><tr><th>Item</th><th>Text</th><th className="r">Qty</th><th className="r">Value</th></tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={i}><td className="mono">{s(r.EBELP)}</td><td>{s(r.SGTXT)}</td><td className="r">{s(r.MENGE)} {s(r.MEINS)}</td><td className="r">{inr(Number(r.DMBTR))}</td></tr>
        ))}</tbody>
      </table>
    </div>
  )
}
function PoCopy({ d }: { d: InvoiceDetail }) {
  const items = d.compare.po_items
  if (!items.length) return <div className="empty">PO not found in SAP.</div>
  return (
    <div className="paper">
      <div className="paper-h">
        <div className="co">Purchase Order<small>FROM SAP · EKKO / EKPO</small></div>
        <div className="dt"><small>PO no.</small><span className="mono">{d.po_number}</span><br /><small style={{ marginTop: 6 }}>PO date</small>{d.po_date || '—'}</div>
      </div>
      <div className="pmeta">
        <div><div className="k">Vendor</div><div className="val">{d.vendor}<br /><span className="mono">V-{d.vendor_code}</span></div></div>
        <div><div className="k">Company code · currency</div><div className="val mono">{d.company_code || '—'} · {d.currency}</div></div>
      </div>
      <table className="ptable">
        <thead><tr><th>Item</th><th>Description</th><th>HSN</th><th className="r">Qty</th><th className="r">Net value</th></tr></thead>
        <tbody>{items.map((r, i) => (
          <tr key={i}><td className="mono">{s(r.EBELP)}</td><td>{s(r.TXZ01)}</td><td className="mono">{s(r.STAWN) || '—'}</td><td className="r">{s(r.MENGE)} {s(r.MEINS)}</td><td className="r">{inr(Number(r.NETWR))}</td></tr>
        ))}</tbody>
      </table>
    </div>
  )
}

export default function Recon() {
  const { id } = useParams()
  const nav = useNavigate()
  const toast = useToast()
  const { can } = useAuth()
  const [d, setD] = useState<InvoiceDetail | null>(null)
  const [err, setErr] = useState('')
  const [tab, setTab] = useState<'inv' | 'grn' | 'po'>('inv')
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [reso, setReso] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const iid = Number(id)

  const load = useCallback(() => {
    api.invoice(iid).then(x => { setD(x); setErr(''); setReso(x.resolution ?? ''); setNote(x.resolution_note ?? '') }).catch(e => setErr(e.message))
  }, [iid])
  useEffect(() => { setEdits({}); load() }, [load])

  if (err) return <div className="err">{err}</div>
  if (!d) return <div className="empty"><span className="spin" /></div>

  const locked = d.miro_status === 'POSTED' || d.miro_status === 'PARKED'
  const writable = can('approver') && !locked
  const approve = RESOLUTIONS.find(r => r[0] === reso)?.[1] ?? false
  const open = d.issues.filter(i => i.severity === 'BLOCK' || i.severity === 'RECON')
  const confirmed = d.queue === 'MATCHED'

  /** send edits, then the resolution; returns the final queue */
  const save = async (): Promise<string | null> => {
    setBusy(true)
    try {
      let queue = d.queue
      if (Object.keys(edits).length) queue = (await api.edit(iid, edits)).queue as typeof d.queue
      if (reso && queue !== 'BLOCKED') queue = (await api.resolve(iid, { resolution: reso, note, approve_variance: approve })).queue as typeof d.queue
      setEdits({})
      load()
      return queue
    } catch (e) { toast('Could not save', (e as Error).message, true); return null } finally { setBusy(false) }
  }
  const onSave = async () => {
    const q = await save()
    if (q) toast('Changes saved', q === 'MATCHED' ? 'Everything now agrees with SAP' : `Invoice is in the ${q.toLowerCase()} queue`)
  }
  const onPush = async () => {
    const q = await save()
    if (!q) return
    if (q !== 'MATCHED') return toast('Cannot push yet', 'Resolve the open issues first - only matched invoices go to SAP', true)
    try { const r = await api.push(iid); toast('Sent to MIRO robot', r.message); load() }
    catch (e) { toast('Push failed', (e as Error).message, true) }
  }

  return (
    <section>
      <div className="recon-top">
        <button className="back" onClick={() => nav(-1)} aria-label="Back"><Icon d={ICONS.back} size={18} /></button>
        <div className="recon-title">
          <h1>Three-way reconciliation</h1>
          <p><span className="mono">{d.invoice_number || d.filename}</span> · {d.vendor}</p>
        </div>
        <span style={{ marginLeft: 'auto', marginTop: 4, display: 'flex', gap: 8 }}>
          <QueueTag q={d.queue} />{d.queue === 'MATCHED' && <MiroTag s={d.miro_status} />}
        </span>
      </div>

      <div className="refstrip">
        <div className="cell"><div className="k">Vendor</div><div className="v">{d.vendor}</div></div>
        <div className="cell"><div className="k">PO number</div><div className="v mono">{d.po_number || '—'}</div></div>
        <div className="cell"><div className="k">GRN number</div><div className="v mono">{d.grn_number || '—'}</div></div>
        <div className="cell"><div className="k">Invoice amount</div><div className="v num">{inr(d.total)}</div></div>
        <div className="cell"><div className="k">Plant</div><div className="v">{d.plant_code || '—'}{d.company_code ? ` · CC ${d.company_code}` : ''}</div></div>
      </div>

      {d.multi_po_flag && <div className="panel" style={{ padding: 12, marginBottom: 14, borderColor: '#9cc3ee', background: '#e3effc' }}>ⓘ Multi-PO invoice: {d.red_flag_reason}. {d.queue === 'MATCHED' ? 'PO, GRN and every line agree with SAP.' : 'Check the lines below.'}</div>}
      {d.miro_message && <div className={d.miro_status === 'FAILED' ? 'err' : 'panel'} style={{ padding: 12, marginBottom: 14 }}>Robot: {d.miro_message}{d.miro_doc_number ? ` (document ${d.miro_doc_number})` : ''}</div>}

      <div className="recon-grid">
        <div className="docwrap">
          <div className="doctabs">
            {([['inv', 'Digital Invoice'], ['grn', 'GRN Copy'], ['po', 'PO Copy']] as const).map(([k, l]) => (
              <button key={k} className={'doctab' + (tab === k ? ' on' : '')} onClick={() => setTab(k)}>{l}</button>
            ))}
          </div>
          <div className="docview">
            {tab === 'inv' && <InvoicePages id={iid} pages={d.page_count || 1} />}
            {tab === 'grn' && <GrnCopy d={d} />}
            {tab === 'po' && <PoCopy d={d} />}
          </div>
        </div>

        <div className="match">
          <div className="match-h"><b>Match details</b><span className="conf">{Math.round((d.confidence || 0) * 100)}% AI confidence</span></div>
          <div className="msec">
            <div className="lbl"><span className="t">Invoice vs SAP</span></div>
            <div className="mrow hd"><span>Field</span><span>Invoice</span><span>GRN</span><span /></div>
            {d.compare.rows.map(r => (
              <div className="mrow" key={r.field}>
                <span className="field-k">{r.label}</span>
                {EDITABLE[r.field] && writable ? (
                  <input className={'cellbox' + (edits[EDITABLE[r.field]] !== undefined ? ' edited' : '')} aria-label={r.label}
                    value={edits[EDITABLE[r.field]] ?? r.invoice}
                    onChange={e => setEdits({ ...edits, [EDITABLE[r.field]]: e.target.value })} />
                ) : <div className="cellbox mono">{r.invoice || '—'}</div>}
                <div className="cellbox po mono" title={r.po ? `PO: ${r.po}` : ''}>{r.field === 'hsn_sac' ? r.po || '—' : r.grn || '—'}</div>
                <Stat s={r.status} />
              </div>
            ))}
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>HSN is read from the invoice only — the right-hand value is SAP’s HSN, used just to compare.</div>
          </div>

          {open.length > 0 && (
            <div className="msec">
              <div className="lbl"><span className="t">Open issues ({open.length})</span></div>
              <div className="issues" style={{ padding: 0 }}>
                {open.map((i, k) => (
                  <div className="iss" key={k}>
                    <span className={'tag sev ' + (i.severity === 'BLOCK' ? 'blocked' : 'review')}>{i.severity === 'BLOCK' ? 'Blocked' : 'Mismatch'}</span>
                    <span>{i.message}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="msec">
            <div className="lbl"><span className="t">Line items</span><span className="h">Invoice / GRN</span></div>
            <div style={{ overflowX: 'auto' }}>
              <table className="ltable">
                <thead><tr><th>Item</th><th className="r">Qty</th><th className="r">GR qty</th><th className="r">Amount</th><th className="r">GR value</th><th /></tr></thead>
                <tbody>{d.compare.lines.map(l => (
                  <tr key={l.id} className={l.line_status !== 'OK' && l.line_status !== 'NA' ? 'bad' : ''}>
                    <td title={l.item}>{l.item.slice(0, 30)}</td>
                    <td style={{ textAlign: 'right' }}>{l.quantity}</td><td style={{ textAlign: 'right' }}>{l.gr_quantity ?? '—'}</td>
                    <td style={{ textAlign: 'right' }}>{inr(l.amount)}</td><td style={{ textAlign: 'right' }}>{l.gr_amount == null ? '—' : inr(l.gr_amount)}</td>
                    <td>{l.line_status === 'OK' ? <span className="mstat ok"><Icon d={ICONS.check} size={14} /></span> : l.line_status === 'NA' ? '' : <span className="tag review">{l.line_status}</span>}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </div>

          <div className={'variance-bar ' + (d.variance_amount ? 'nonzero' : 'zero')}>
            <span className="lft">{d.variance_amount ? 'Amount variance' : 'No amount variance'}</span>
            <span className="num">{inr(d.variance_amount ?? 0)}</span>
          </div>

          <div className="reso">
            <div className="t">Resolution</div>
            <select value={reso} onChange={e => setReso(e.target.value)} disabled={!writable || d.queue === 'BLOCKED'} aria-label="Resolution">
              <option value="">{d.queue === 'BLOCKED' ? 'Correct the missing data first…' : 'Select a resolution…'}</option>
              {RESOLUTIONS.map(([l]) => <option key={l}>{l}</option>)}
            </select>
            <textarea rows={2} placeholder="Add a note for the audit trail…" value={note} onChange={e => setNote(e.target.value)} disabled={!writable} aria-label="Note" />
            {d.resolved_by && <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>Last resolved by {d.resolved_by} · {dt(d.resolved_at)}</div>}
            <div className="acts">
              <button className="btn" onClick={onSave} disabled={!writable || busy || (!Object.keys(edits).length && !reso)}>Save changes</button>
              <button className="btn primary lg" onClick={onPush} disabled={!writable || busy || (!confirmed && !approve && !Object.keys(edits).length)} data-testid="push">
                Push to SAP MIRO<Icon d={ICONS.arrow} size={15} />
              </button>
            </div>
            {locked && <div className="muted" style={{ marginTop: 8 }}>Already {d.miro_status?.toLowerCase()} in SAP — read only.</div>}
            {!can('approver') && <div className="muted" style={{ marginTop: 8 }}>View-only access.</div>}
          </div>

          <div className="msec">
            <div className="lbl"><span className="t">History</span></div>
            <div className="hist">{d.history.slice(0, 8).map((h, i) => <div key={i}><b>{h.action}</b> · {h.actor || 'system'} · {dt(h.at)}</div>)}</div>
          </div>
        </div>
      </div>
    </section>
  )
}
