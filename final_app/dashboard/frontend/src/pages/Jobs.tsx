import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type Job } from '../api'
import { useAuth } from '../auth'
import { Icon, ICONS, QueueTag, dt, useToast } from '../ui'

const JT: Record<string, string> = { QUEUED: 'grey', RUNNING: 'info', DONE: 'match', DUPLICATE: 'review', FAILED: 'fail' }
type Cfg = Awaited<ReturnType<typeof api.config>>

export default function Jobs() {
  const { can } = useAuth()
  const nav = useNavigate()
  const toast = useToast()
  const [jobs, setJobs] = useState<Job[]>([])
  const [cfg, setCfg] = useState<Cfg | null>(null)
  const [busy, setBusy] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const load = () => api.jobs().then(setJobs).catch(() => {})
  useEffect(() => { api.config().then(setCfg).catch(() => {}); load(); const t = setInterval(load, 3000); return () => clearInterval(t) }, [])

  const send = async (files: FileList | null) => {
    const list = Array.from(files ?? []).filter(f => f.name.toLowerCase().endsWith('.pdf'))
    if (!list.length) return
    setBusy(true)
    try { await api.upload(list); toast(`${list.length} file(s) queued`); load() }
    catch (e) { toast('Upload failed', (e as Error).message, true) } finally { setBusy(false) }
  }
  const running = jobs.filter(j => j.status === 'QUEUED' || j.status === 'RUNNING').length
  return (
    <section>
      <div className="crumb">Finance Operations&nbsp; / &nbsp;<b>Auto Extraction</b></div>
      <div className="page-head"><div><h1>Auto extraction</h1><p>PDFs dropped in the watch folder are read by the AI model and checked against SAP automatically.</p></div></div>
      {cfg?.folders?.input && (
        <div className="panel" style={{ padding: 16, marginBottom: 18 }}>
          <b>Where the PDF is</b>
          <div style={{ display: 'flex', gap: 10, marginTop: 10, flexWrap: 'wrap', alignItems: 'stretch' }}>
            {([['1  Input', cfg.folders.input, 'drop PDFs here'], ['2  Processing', cfg.folders.processing, 'agent is reading it'],
               ['3  Output', cfg.folders.output, 'matched / reconcile / blocked'], ['4  Finish', cfg.folders.finish, 'posted / parked / duplicate']] as const).map(([t, p, d], i) => (
              <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                {i > 0 && <span className="muted">→</span>}
                <div style={{ border: '1px solid var(--line)', borderRadius: 8, padding: '8px 12px', minWidth: 190 }}>
                  <div style={{ fontWeight: 700 }}>{t}</div>
                  <div className="muted" style={{ fontSize: 12 }}>{d}</div>
                  <div className="mono" style={{ fontSize: 11, wordBreak: 'break-all' }}>{p}</div>
                </div>
              </div>
            ))}
          </div>
          <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>PDF could not be read → <span className="mono">{cfg.folders.error}</span></div>
        </div>
      )}
      <div className="panel" style={{ padding: 16, marginBottom: 18, display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'center' }}>
        <div><div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.06em', fontWeight: 700 }}>Watch folder</div>
          <div className="mono" style={{ fontWeight: 600 }}>{cfg ? (cfg.watch_folder ?? 'NOT SET - set WATCH_FOLDER in .env') : '…'}</div></div>
        <div><div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.06em', fontWeight: 700 }}>Checks every</div>
          <div style={{ fontWeight: 600 }}>{cfg?.poll_seconds ?? '…'} sec</div></div>
        <div><div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.06em', fontWeight: 700 }}>Agent</div>
          <span className={'tag ' + (cfg?.worker_enabled && cfg.watch_folder ? 'match' : 'fail')}>{cfg?.worker_enabled && cfg.watch_folder ? (running ? `Working · ${running} in queue` : 'Running · waiting for PDFs') : 'Not running'}</span></div>
        {cfg?.allow_upload && can('approver') && (
          <div style={{ marginLeft: 'auto' }}>
            <button className="btn" disabled={busy} onClick={() => input.current?.click()}><Icon d={ICONS.upload} size={15} />Upload PDF</button>
            <input ref={input} type="file" accept="application/pdf" multiple hidden onChange={e => { send(e.target.files); e.target.value = '' }} data-testid="file" />
          </div>
        )}
      </div>
      <div className="panel">
        <table>
          <thead><tr><th>File</th><th>Source</th><th>Status</th><th>Result</th><th>Started</th><th></th></tr></thead>
          <tbody>
            {jobs.map(j => (
              <tr key={j.id}>
                <td className="poref">{j.filename}{j.error && <span style={{ color: '#8b1a1a' }}>{j.error}</span>}</td>
                <td className="who">{j.source}</td>
                <td><span className={`tag ${JT[j.status] ?? 'grey'}`}>{j.status}</span>{j.status === 'RUNNING' && <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>{j.stage}</span>}</td>
                <td>{j.queue && <QueueTag q={j.queue} />}{j.status === 'DUPLICATE' && <span className="muted"> already in system</span>}</td>
                <td className="date">{dt(j.created_at)}</td>
                <td className="row-actions">
                  {j.invoice_id && <button className="btn sm" onClick={() => nav(`/invoice/${j.invoice_id}`)}>Open</button>}
                  {j.status === 'FAILED' && can('approver') && <button className="btn sm" onClick={() => api.retryJob(j.id).then(load)}>Retry</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {jobs.length === 0 && <div className="empty">Nothing picked up yet. Copy invoice PDFs into the watch folder.</div>}
      </div>
    </section>
  )
}
