import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type InvoiceRow, type RobotRun, type RobotStatus } from '../api'
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
  const [rb, setRb] = useState<RobotStatus | null>(null)
  const [runs, setRuns] = useState<RobotRun[]>([])
  const [log, setLog] = useState<{ id: number; text: string } | null>(null)
  const loadRb = () => { api.robotStatus().then(setRb).catch(() => {}); api.robotRuns().then(setRuns).catch(() => {}) }
  useEffect(() => { loadRb(); const t = setInterval(loadRb, 5000); return () => clearInterval(t) }, [])
  const runNow = async () => {
    try { const r = await api.robotRun(); toast(r.started ? 'Robot started' : 'Robot busy', r.started ? 'It is posting the matched invoices now' : r.reason); loadRb() }
    catch (e) { toast('Robot not started', (e as Error).message, true) }
  }
  const showLog = async (id: number) => setLog({ id, text: await api.robotLog(id) })

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
      <div className="panel" style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap' }}>
          <b>MIRO robot</b>
          {rb && !rb.configured && <span className="tag fail">Not set up</span>}
          {rb?.configured && <span className={'tag ' + (rb.running ? 'amber' : 'ok')}>{rb.running ? 'Running' : 'Ready'}</span>}
          {rb?.configured && <span className="muted">Mode <b>{rb.mode}</b>{rb.mode === 'CHECK' ? ' (dry run - nothing is posted)' : rb.mode === 'PARK' ? ' (parks the document)' : ' (posts the document)'} · {rb.waiting} waiting · auto-start {rb.auto_run ? 'on' : 'off'}{rb.next_auto_in != null ? ` (in ${rb.next_auto_in}s)` : ''}</span>}
          {rb && !rb.configured && <span className="muted">{rb.problem} - set ROBOT_ENABLED=true and ROBOT_DIR in backend/.env</span>}
          {can('approver') && <button className="btn primary sm" style={{ marginLeft: 'auto' }} disabled={!rb?.configured} onClick={runNow}>{rb?.running ? 'Run again after this' : 'Run robot now'}</button>}
        </div>
        {rb?.last?.error && <div className="err" style={{ marginTop: 10 }}>{rb.last.error}</div>}
        {runs.length > 0 && (
          <table className="ltable" style={{ marginTop: 10, width: '100%' }}>
            <thead><tr><th>Run</th><th>Started</th><th>By</th><th>Mode</th><th>Result</th><th>What happened</th><th></th></tr></thead>
            <tbody>{runs.slice(0, 5).map(r => (
              <tr key={r.id}>
                <td>#{r.id}</td><td>{dt(r.started_at)}</td><td>{r.trigger === 'AUTO' ? 'auto' : r.by}</td><td>{r.mode}</td>
                <td><span className={'tag ' + (r.status === 'DONE' ? 'ok' : r.status === 'RUNNING' ? 'amber' : 'fail')}>{r.status}</span></td>
                <td className="muted">{Object.entries(r.summary || {}).map(([k, v]) => `${k} ${v}`).join(' · ') || r.error || '—'}</td>
                <td><button className="btn sm" onClick={() => showLog(r.id)}>Log</button></td>
              </tr>))}</tbody>
          </table>
        )}
        {log && <pre style={{ marginTop: 10, maxHeight: 260, overflow: 'auto', background: '#0f1b2d', color: '#d6e2f2', padding: 12, fontSize: 12, borderRadius: 6 }}>{log.text}</pre>}
      </div>
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
                <td><MiroTag s={i.miro_status} />{(i as InvoiceRow & { dry_run?: boolean }).dry_run && <span className="muted" style={{ marginLeft: 6, fontSize: 12 }}>dry run</span>}{!!i.miro_attempts && <span className="muted" style={{ marginLeft: 6, fontSize: 12 }}>×{i.miro_attempts}</span>}</td>
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
      <div className="panel" style={{ padding: 16, marginTop: 16 }}>
        <b>What each status means</b>
        <table className="ltable" style={{ width: '100%', marginTop: 8 }}>
          <thead><tr><th>Status</th><th>Meaning</th><th>What to do</th></tr></thead>
          <tbody>
            <tr><td><MiroTag s="WAITING" /></td><td>Matched invoice, robot has not touched it yet.</td><td>Nothing - the robot picks it up (or press Run robot now).</td></tr>
            <tr><td><MiroTag s="POSTED" /></td><td>The robot <b>posted</b> the MIRO invoice in SAP. The SAP document number is shown.</td><td>Done.</td></tr>
            <tr><td><MiroTag s="PARKED" /></td><td>The robot <b>parked</b> the document in SAP (saved, not posted). Used in PARK mode.</td><td>AP team checks it in MIR4 and posts it.</td></tr>
            <tr><td><MiroTag s="CHECKED" /></td><td><b>Dry run</b> (CHECK mode): robot filled MIRO, selected the GRN rows and checked the balance, then left <b>without saving</b>. Nothing exists in SAP.</td><td>Looks good? Switch the robot to PARK / POST - CHECKED invoices are taken again automatically.</td></tr>
            <tr><td><MiroTag s="REVIEW" /></td><td>Data problem found by the robot: GRN not in MIRO, amount gap, duplicate-invoice warning, balance not zero.</td><td>Fix the data, then Re-queue.</td></tr>
            <tr><td><MiroTag s="FAILED" /></td><td>Technical / SAP error <b>before</b> saving (PO not found, screen error). Nothing was saved.</td><td>Robot retries up to 3 times; then check the message and Re-queue.</td></tr>
            <tr><td><MiroTag s="UNKNOWN" /></td><td>Error <b>while saving</b>, or robot stopped in the middle. SAP may or may not have the document.</td><td>Check MIR4 / MIR6 in SAP first. Do not re-queue before that.</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  )
}
