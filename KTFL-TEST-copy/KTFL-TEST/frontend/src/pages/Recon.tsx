import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, type CompareRow, type CompareLine, type InvoiceDetail, type LensBox, type LensRect, type LensResult } from '../api'
import { useAuth } from '../auth'
import { Icon, ICONS, MiroTag, QueueTag, dt, inr, useToast } from '../ui'

const EDITABLE: Record<string, string> = {
  po_number: 'po_number', grn_number: 'grn_number', hsn_sac: 'hsn_sac',
  invoice_date: 'invoice_date', invoice_number: 'invoice_number', vendor: 'vendor',
  gstin: 'gstin', subtotal: 'subtotal', cgst: 'cgst', sgst: 'sgst', igst: 'igst',
  total: 'total', gst_tax_code: 'gst_tax_code',
}

/** SAC codes starting with 99 = service codes — flag on material invoice */
const isSacServiceCode = (hsn: string | null) => hsn ? /^99/.test(hsn.trim()) : false
const RESOLUTIONS = [
  ['Invoice corrected to GRN', false], ['Accept variance - approved deviation', true],
  ['Raise query with vendor', false], ['Debit note requested', false],
] as const

function Stat({ s }: { s: CompareRow['status'] }) {
  if (s === 'OK') return <span className="mstat ok" title="Matches"><Icon d={ICONS.check} size={14} /></span>
  if (s === 'NA') return <span className="mstat na">–</span>
  return <span className="mstat bad" title={s === 'MISSING' ? 'Missing' : 'Does not match'}><Icon d={ICONS.x} size={14} /></span>
}

interface LensProps { on: boolean; boxes: LensBox[]; busy: boolean; onToggle: () => void; onSelect: (r: LensRect) => void; onBox: (b: LensBox) => void }

/** Pan / zoom / rotate image. Toolbar: − + (wheel), Fit page, Fit width, ⟲ ⟳ (also R / Shift+R), 180°. Drag to move,
 *  double-click = 2.5x. Rotation re-fits the page, so a sideways scan is readable after one click.
 *  With `lens` there is a Scan button: drag a box around a number (PO / GRN, even handwritten) or click one of the
 *  outlined printed numbers. Boxes are kept as fractions of the image, so they stay put when the page is rotated or zoomed. */
function PanZoom({ src, alt, full, onFull, lens }: { src: string; alt: string; full?: boolean; onFull?: () => void; lens?: LensProps }) {
  const box = useRef<HTMLDivElement>(null)
  const page = useRef<HTMLDivElement>(null)
  const [nat, setNat] = useState({ w: 0, h: 0 })
  const [bw, setBw] = useState({ w: 1, h: 1 })
  const [rot, setRot] = useState(0)
  const [z, setZ] = useState(1)
  const [pos, setPos] = useState({ x: 0, y: 0 })
  const [drag, setDrag] = useState<{ x: number; y: number; px: number; py: number } | null>(null)
  const [selA, setSelA] = useState<{ x: number; y: number } | null>(null)      // lens: start corner (image fractions)
  const [sel, setSel] = useState<LensRect | null>(null)
  const clamp = (v: number) => Math.min(8, Math.max(0.2, v))
  useEffect(() => { setZ(1); setPos({ x: 0, y: 0 }); setSel(null) }, [src])
  useEffect(() => { if (!lens?.on) { setSel(null); setSelA(null) } }, [lens?.on])
  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(() => setBw({ w: el.clientWidth || 1, h: el.clientHeight || 1 }))
    ro.observe(el)
    const wheel = (e: WheelEvent) => { e.preventDefault(); setZ(v => clamp(v * (e.deltaY < 0 ? 1.12 : 1 / 1.12))) }
    el.addEventListener('wheel', wheel, { passive: false })     // non-passive: stop the page from scrolling
    return () => { ro.disconnect(); el.removeEventListener('wheel', wheel) }
  }, [])
  useEffect(() => {
    if (!full) return
    const k = (e: KeyboardEvent) => {
      if (e.key === '+' || e.key === '=') setZ(v => clamp(v * 1.25))
      if (e.key === '-') setZ(v => clamp(v / 1.25))
      if (e.key === 'r') setRot(v => (v + 90) % 360)
      if (e.key === 'R') setRot(v => (v + 270) % 360)
      if (e.key === '0') { setZ(1); setPos({ x: 0, y: 0 }) }
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [full])
  const sideways = rot % 180 !== 0
  const rw = sideways ? nat.h : nat.w, rh = sideways ? nat.w : nat.h        // size of the page after rotation
  const fit = rw && rh ? Math.min(bw.w / rw, bw.h / rh) : 1                  // scale that shows the whole page
  const fitW = rw ? bw.w / rw : 1
  const scale = fit * z
  const turn = (d: number) => { setRot(v => (v + d + 360) % 360); setZ(1); setPos({ x: 0, y: 0 }) }
  const reset = () => { setZ(1); setPos({ x: 0, y: 0 }) }
  const widthFit = () => { setZ(fit ? fitW / fit : 1); setPos({ x: 0, y: 0 }) }
  const stop = (e: React.MouseEvent) => e.stopPropagation()
  /** screen point -> position on the (unrotated) image as fractions 0..1, for any rotation */
  const toImg = (cx: number, cy: number) => {
    const r = page.current!.getBoundingClientRect()
    const fx = (cx - r.left) / r.width, fy = (cy - r.top) / r.height
    const c = (v: number) => Math.min(1, Math.max(0, v))
    if (rot === 90) return { x: c(fy), y: c(1 - fx) }
    if (rot === 180) return { x: c(1 - fx), y: c(1 - fy) }
    if (rot === 270) return { x: c(1 - fy), y: c(fx) }
    return { x: c(fx), y: c(fy) }
  }
  const lensOn = !!lens?.on
  const down = (e: React.MouseEvent) => {
    if (lensOn && !lens?.busy) { setSelA(toImg(e.clientX, e.clientY)); setSel(null); return }
    setDrag({ x: e.clientX, y: e.clientY, px: pos.x, py: pos.y })
  }
  const move = (e: React.MouseEvent) => {
    if (selA) {
      const b = toImg(e.clientX, e.clientY)
      setSel({ x: Math.min(selA.x, b.x), y: Math.min(selA.y, b.y), w: Math.abs(b.x - selA.x), h: Math.abs(b.y - selA.y) })
    } else if (drag) setPos({ x: drag.px + e.clientX - drag.x, y: drag.py + e.clientY - drag.y })
  }
  const up = () => {
    if (selA) {
      setSelA(null)
      if (sel && sel.w > 0.004 && sel.h > 0.003) lens?.onSelect(sel)
      else setSel(null)
    }
    setDrag(null)
  }
  const pct = (v: number) => `${(v * 100).toFixed(3)}%`
  return (
    <div className={`pz${full ? ' pz-full' : ''}`}>
      <div className="pz-bar" onClick={stop} onMouseDown={stop}>
        <button className="btn sm" onClick={() => setZ(v => clamp(v / 1.25))} aria-label="Zoom out" title="Zoom out (-)">−</button>
        <span className="pz-pct">{Math.round(scale * 100)}%</span>
        <button className="btn sm" onClick={() => setZ(v => clamp(v * 1.25))} aria-label="Zoom in" title="Zoom in (+)">+</button>
        <button className="btn sm" onClick={reset} title="Fit whole page (0)">Fit</button>
        <button className="btn sm" onClick={widthFit} title="Fit page width">Width</button>
        <button className="btn sm" onClick={() => turn(-90)} aria-label="Rotate left" title="Rotate left (Shift+R)">⟲ 90°</button>
        <button className="btn sm" onClick={() => turn(90)} aria-label="Rotate right" title="Rotate right (R)">⟳ 90°</button>
        <button className="btn sm" onClick={() => turn(180)} title="Turn upside down">180°</button>
        {lens && <button className={'btn sm' + (lens.on ? ' primary' : '')} onClick={lens.onToggle} aria-pressed={lens.on}
          title="Scan: draw a box around a PO / GRN number (even handwritten) to read it">🔍 Scan</button>}
        {onFull && <button className="btn sm" onClick={onFull} title="Full screen">⤢</button>}
      </div>
      <div className="pz-stage" ref={box}
        onMouseDown={down} onMouseMove={move} onMouseUp={up} onMouseLeave={up}
        onDoubleClick={() => { if (!lensOn) (z === 1 ? setZ(2.5) : reset()) }}
        style={{ cursor: lensOn ? 'crosshair' : drag ? 'grabbing' : 'grab' }}>
        <div className="pz-page" ref={page}
          style={{ width: nat.w || undefined, height: nat.h || undefined,
                   transform: `translate(-50%, -50%) translate(${pos.x}px, ${pos.y}px) rotate(${rot}deg) scale(${scale})`,
                   transition: drag || selA ? 'none' : 'transform 0.15s ease' }}>
          <img src={src} alt={alt} draggable={false}
            onLoad={e => setNat({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })} />
          {lensOn && (
            <div className={'pz-lens' + (lens?.busy ? ' busy' : '')}>
              {lens?.boxes.map((b, i) => (
                <button key={i} type="button" className={'pz-nbox ' + (b.kind === 'GRN' ? 'grn' : b.kind === 'PO' ? 'po' : 'oth')}
                  style={{ left: pct(b.x - 0.004), top: pct(b.y - 0.003), width: pct(b.w + 0.008), height: pct(b.h + 0.006) }}
                  title={`${b.kind === 'GRN' ? 'GRN' : b.kind === 'PO' ? 'PO' : 'Number'} ${b.value} - click to use`}
                  onMouseDown={stop} onClick={e => { e.stopPropagation(); lens.onBox(b) }} />
              ))}
              {sel && <div className="pz-sel" style={{ left: pct(sel.x), top: pct(sel.y), width: pct(sel.w), height: pct(sel.h) }} />}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** full-screen viewer: PanZoom + page arrows, ← → pages, Esc closes */
function Viewer({ id, page, pages, onClose, onPage }: { id: number; page: number; pages: number; onClose: () => void; onPage: (p: number) => void }) {
  const [src, setSrc] = useState('')
  useEffect(() => {
    let url = ''
    setSrc('')
    api.blobUrl(`/api/invoices/${id}/page/${page}.png?zoom=3`).then(u => { url = u; setSrc(u) }).catch(() => setSrc(''))
    return () => { if (url) URL.revokeObjectURL(url) }
  }, [id, page])
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      if (e.key === 'ArrowRight' && page < pages) onPage(page + 1)
      if (e.key === 'ArrowLeft' && page > 1) onPage(page - 1)
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [page, pages, onClose, onPage])
  const openPdf = async () => { const u = await api.blobUrl(`/api/invoices/${id}/file`); window.open(u, '_blank') }
  return (
    <div className="viewer" role="dialog" aria-label="Invoice viewer">
      <div className="viewer-bar">
        {pages > 1 && <><button className="btn sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>‹</button><span>Page {page} / {pages}</span><button className="btn sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>›</button></>}
        <button className="btn sm" onClick={openPdf}>Open PDF</button>
        <button className="btn sm" onClick={onClose}>Close ✕</button>
      </div>
      <div className="viewer-stage2">{src ? <PanZoom src={src} alt={`Invoice page ${page}`} full /> : <span className="spin" />}</div>
    </div>
  )
}

/** HSN display: single code or multiple codes shown cleanly as tags */
function HsnDisplay({ value }: { value: string | null | undefined }) {
  if (!value) return <div className="cellbox mono">—</div>
  const codes = value.split(/[|,;]/).map(s => s.trim()).filter(Boolean)
  if (codes.length <= 1) return <div className="cellbox mono">{value || '—'}</div>
  return (
    <div className="cellbox" style={{ display: 'flex', flexWrap: 'wrap', gap: 3, padding: '3px 6px' }}>
      {codes.map((c, i) => (
        <span key={i} style={{ background: '#e8f4f8', borderRadius: 3, padding: '1px 5px', fontSize: 11.5, fontFamily: 'monospace' }}>{c}</span>
      ))}
    </div>
  )
}

const KIND_LABEL: Record<string, string> = { PO: 'PO number', GRN: 'GRN number', SERVICE_PO: 'Service PO (81/82/83)', OTHER: 'Not a PO / GRN pattern' }

function LensPanel({ res, busy, err, writable, onApply, onClose, canDetect, detecting, onDetect, detectMsg }: {
  res: LensResult | null; busy: boolean; err: string; writable: boolean; onApply: (kind: 'PO' | 'GRN', v: string) => void; onClose: () => void
  canDetect: boolean; detecting: boolean; onDetect: () => void; detectMsg: string
}) {
  return (
    <div className="lens-panel">
      <div className="lens-h"><b>🔍 Scan the digital invoice</b><button className="btn sm ghost" onClick={onClose} aria-label="Close scan">✕</button></div>
      {canDetect && (
        <div style={{ marginBottom: 6, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn sm" onClick={onDetect} disabled={detecting || busy}>{detecting ? <><span className="spin" /> AI is looking…</> : '✨ Detect PO / GRN numbers with AI'}</button>
          <span className="muted" style={{ fontSize: 11.5 }}>{detectMsg || 'Takes a minute. Outlines are approximate - check them. Or just draw a box yourself.'}</span>
        </div>
      )}
      {busy ? <div className="muted"><span className="spin" /> Reading the selected area…</div>
        : err ? <div className="err" style={{ margin: 0 }}>{err}</div>
        : !res ? <div className="muted">Draw a box around the PO or GRN number (handwriting is fine), or click one of the outlined printed numbers.</div>
        : !res.candidates.length ? (
          <div className="muted">{res.crop && <img className="lens-crop" src={`data:image/png;base64,${res.crop}`} alt="Captured area" />}No number found in that box. Draw the box a little bigger around the digits.{res.ai_raw && <div style={{ fontSize: 11.5, marginTop: 4 }}>AI read: <span className="mono">{res.ai_raw}</span></div>}{res.ai === false ? ' (The AI reader did not answer - printed numbers still work.)' : ''}{!res.ocr && res.ai == null ? ' (OCR is not installed on the server.)' : ''}</div>
        ) : (
          <div className="lens-list">
            {res.crop && <img className="lens-crop" src={`data:image/png;base64,${res.crop}`} alt="Captured area" title="The area you captured - compare the digits" />}
            {res.candidates.map(c => (
              <div className="lens-row" key={c.value}>
                <span className="mono lens-val">{c.value}</span>
                <span className={'lens-kind ' + c.kind.toLowerCase()}>{KIND_LABEL[c.kind]}</span>
                <span className="lens-src" title="OCR = printed text read by Tesseract, AI = read by the vision model">{c.sources.join(' + ')}</span>
                {c.sap.checked
                  ? c.sap.in_sap ? <span className="lens-sap ok">✓ in SAP{c.sap.plant_code ? ` · plant ${c.sap.plant_code}` : ''}</span> : <span className="lens-sap bad">✗ not in SAP</span>
                  : <span className="lens-sap">SAP not checked</span>}
                {writable && (
                  <span className="lens-act">
                    <button className={'btn sm' + (c.kind === 'PO' || c.kind === 'SERVICE_PO' ? ' primary' : '')} onClick={() => onApply('PO', c.value)}>Use as PO</button>
                    <button className={'btn sm' + (c.kind === 'GRN' ? ' primary' : '')} onClick={() => onApply('GRN', c.value)}>Use as GRN</button>
                  </span>
                )}
              </div>
            ))}
            {res.ai_raw && <div className="muted" style={{ fontSize: 11.5 }}>AI read: <span className="mono">{res.ai_raw}</span></div>}
            <div className="muted" style={{ fontSize: 11.5 }}>Check the digits against the picture, then press <b>Save</b> on the right. A value you apply is only a suggestion until you save.</div>
          </div>
        )}
    </div>
  )
}

function InvoicePages({ id, pages, writable, onApply }: { id: number; pages: number; writable: boolean; onApply: (kind: 'PO' | 'GRN', v: string) => void }) {
  const [p, setP] = useState(1)
  const [src, setSrc] = useState('')
  const [err, setErr] = useState('')
  const [big, setBig] = useState(false)
  const [lensOn, setLensOn] = useState(false)
  const [boxes, setBoxes] = useState<LensBox[]>([])
  const [res, setRes] = useState<LensResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [lerr, setLerr] = useState('')
  const [ocrAvail, setOcrAvail] = useState(true)       // false when the server has no Tesseract: outlines then come from the AI on request
  const [detecting, setDetecting] = useState(false)
  const [detectMsg, setDetectMsg] = useState('')
  useEffect(() => {
    let url = ''
    setSrc(''); setErr(''); setRes(null); setLerr('')
    api.blobUrl(`/api/invoices/${id}/page/${p}.png?zoom=2.5`).then(u => { url = u; setSrc(u) }).catch(() => setErr('The original PDF is not available on the server.'))
    return () => { if (url) URL.revokeObjectURL(url) }
  }, [id, p])
  useEffect(() => {
    if (!lensOn) { setBoxes([]); return }
    let live = true
    setDetectMsg('')
    api.lensNumbers(id, p).then(r => { if (live) { setBoxes(r.boxes); setOcrAvail(r.available) } }).catch(() => { if (live) setBoxes([]) })
    return () => { live = false }
  }, [lensOn, id, p])
  const detectAi = async () => {
    setDetecting(true); setDetectMsg('')
    try {
      const r = await api.lensNumbers(id, p, true)
      setBoxes(r.boxes)
      setDetectMsg(r.boxes.length ? `${r.boxes.length} number(s) outlined - click one.` : 'The AI found no PO / GRN number on this page. Draw a box around it.')
    } catch (e) { setDetectMsg((e as Error).message || 'AI detection failed') } finally { setDetecting(false) }
  }
  const scan = async (body: Record<string, unknown>) => {
    setBusy(true); setLerr(''); setRes(null)
    try { setRes(await api.lensScan(id, { page: p, ...body })) } catch (e) { setLerr((e as Error).message || 'Scan failed') } finally { setBusy(false) }
  }
  const lens: LensProps = {
    on: lensOn, boxes, busy,
    onToggle: () => { setLensOn(v => !v); setRes(null); setLerr('') },
    onSelect: r => { void scan({ x: r.x, y: r.y, w: r.w, h: r.h }) },
    onBox: b => { void scan({ value: b.value, x: b.x, y: b.y, w: b.w, h: b.h }) },
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {pages > 1 && (
        <div className="pagebar" style={{ flexShrink: 0 }}>
          <button className="btn sm" disabled={p <= 1} onClick={() => setP(p - 1)}>‹</button>
          <span>Page {p} of {pages}</span>
          <button className="btn sm" disabled={p >= pages} onClick={() => setP(p + 1)}>›</button>
        </div>
      )}
      {lensOn && <LensPanel res={res} busy={busy} err={lerr} writable={writable} onApply={onApply} onClose={() => setLensOn(false)}
        canDetect={!ocrAvail} detecting={detecting} onDetect={() => void detectAi()} detectMsg={detectMsg} />}
      {err ? <div className="empty">{err}</div> : src ? (
        <PanZoom src={src} alt={`Invoice page ${p}`} onFull={() => setBig(true)} lens={lens} />
      ) : <div className="empty"><span className="spin" /></div>}
      {big && <Viewer id={id} page={p} pages={pages} onClose={() => setBig(false)} onPage={setP} />}
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
/** Find HSN value from a po_items row — EKPO.J_1BNBM only (new snapshots: HSNP_J_1BNBM) */
function poHsn(r: Record<string, unknown>): string {
  // New snapshots: SAP service adds HSNP_ prefix → HSNP_J_1BNBM
  if (r.HSNP_J_1BNBM) return String(r.HSNP_J_1BNBM)
  // Old snapshots: plain column name
  if (r.J_1BNBM) return String(r.J_1BNBM)
  return '—'
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
          <tr key={i}><td className="mono">{s(r.EBELP)}</td><td>{s(r.TXZ01)}</td><td className="mono">{poHsn(r)}</td><td className="r">{s(r.MENGE)} {s(r.MEINS)}</td><td className="r">{inr(Number(r.NETWR))}</td></tr>
        ))}</tbody>
      </table>
    </div>
  )
}

function LineItemsSection({ lines, invoiceHsn, writable, onSaved, invoiceId }: {
  lines: CompareLine[]; invoiceHsn: string | null; writable: boolean; onSaved: () => void; invoiceId: number
}) {
  const toast = useToast()
  const [editId, setEditId] = useState<number | null>(null)
  const [editVals, setEditVals] = useState<{ item: string; quantity: string; rate: string; amount: string; hsn_sac: string }>({ item: '', quantity: '', rate: '', amount: '', hsn_sac: '' })
  const [saving, setSaving] = useState(false)

  const startEdit = (l: CompareLine) => {
    setEditId(l.id)
    setEditVals({ item: l.item, quantity: String(l.quantity), rate: String(l.rate), amount: String(l.amount), hsn_sac: l.hsn_sac ?? '' })
  }
  const cancelEdit = () => setEditId(null)
  const saveEdit = async () => {
    if (editId == null) return
    setSaving(true)
    try {
      await api.edit(invoiceId, { [`line_${editId}`]: editVals })
      toast('Line updated', 'Changes saved')
      setEditId(null)
      onSaved()
    } catch (e) { toast('Save failed', (e as Error).message, true) } finally { setSaving(false) }
  }

  const hasSacService = lines.some(l => isSacServiceCode(l.hsn_sac)) || isSacServiceCode(invoiceHsn)
  return (
    <div className="msec">
      <div className="lbl"><span className="t">Line items</span><span className="h">Invoice / GRN</span></div>
      {hasSacService && (
        <div className="sac-warn">⚠️ SAC code starting 99 detected — this is a <b>service HSN</b>. Material invoices should have goods HSN (e.g. 7326xxxx). Please verify with vendor.</div>
      )}
      <div style={{ overflowX: 'auto' }}>
        <table className="ltable">
          <thead><tr><th>Item</th><th>Invoice HSN</th><th>SAP HSN</th><th className="r">Qty</th><th className="r">GR qty</th><th className="r">Amount</th><th className="r">GR value</th><th /></tr></thead>
          <tbody>{lines.map(l => editId === l.id ? (
            <tr key={l.id} className="editing">
              <td><input className="line-edit" value={editVals.item} onChange={e => setEditVals(v => ({ ...v, item: e.target.value }))} /></td>
              <td><input className="line-edit" style={{ width: 90 }} value={editVals.hsn_sac} onChange={e => setEditVals(v => ({ ...v, hsn_sac: e.target.value }))} /></td>
              <td className="mono" style={{ fontSize: 11, color: '#666' }}>{l.sap_item_hsn || '—'}</td>
              <td><input className="line-edit" style={{ width: 70, textAlign: 'right' }} value={editVals.quantity} onChange={e => setEditVals(v => ({ ...v, quantity: e.target.value }))} /></td>
              <td style={{ textAlign: 'right' }}>{l.gr_quantity ?? '—'}</td>
              <td><input className="line-edit" style={{ width: 90, textAlign: 'right' }} value={editVals.amount} onChange={e => setEditVals(v => ({ ...v, amount: e.target.value }))} /></td>
              <td style={{ textAlign: 'right' }}>{l.gr_amount == null ? '—' : inr(l.gr_amount)}</td>
              <td style={{ display: 'flex', gap: 4 }}>
                <button className="btn sm primary" onClick={saveEdit} disabled={saving}>✓</button>
                <button className="btn sm" onClick={cancelEdit}>✕</button>
              </td>
            </tr>
          ) : (
            <tr key={l.id} className={l.line_status !== 'OK' && l.line_status !== 'NA' ? 'bad' : ''}>
              <td title={l.item}>{l.item.slice(0, 32)}</td>
              <td className="mono" style={{ fontSize: 11.5 }}>
                {l.hsn_sac || '—'}{isSacServiceCode(l.hsn_sac) && <span className="sac-badge">SVC</span>}
              </td>
              <td className="mono" style={{ fontSize: 11, color: l.sap_item_hsn && l.hsn_sac && l.sap_item_hsn !== l.hsn_sac ? '#8b1a1a' : '#666' }}
                  title={l.sap_item_hsn ? `SAP PO line HSN: ${l.sap_item_hsn}` : 'SAP HSN not available'}>
                {l.sap_item_hsn || '—'}
              </td>
              <td style={{ textAlign: 'right' }}>{l.quantity}</td>
              <td style={{ textAlign: 'right' }}>{l.gr_quantity ?? '—'}</td>
              <td style={{ textAlign: 'right' }}>{inr(l.amount)}</td>
              <td style={{ textAlign: 'right' }}>{l.gr_amount == null ? '—' : inr(l.gr_amount)}</td>
              <td style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                {l.line_status === 'OK' ? <span className="mstat ok"><Icon d={ICONS.check} size={14} /></span> : l.line_status === 'NA' ? '' : <span className="tag review">{l.line_status}</span>}
                {writable && <button className="btn sm ghost" style={{ padding: '2px 7px' }} onClick={() => startEdit(l)} title="Edit line">✎</button>}
              </td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  )
}

function InvoiceDateRow({ editVal, dateRow, invoiceDate, writable, onChange }: {
  editVal: string | undefined; dateRow: CompareRow | undefined; invoiceDate: string | null; writable: boolean; onChange: (v: string) => void
}): JSX.Element {
  return (
    <div className="invoice-date-block" style={{ marginBottom: 10, padding: '8px 10px', background: '#f8f9fa', borderRadius: 6, border: '1px solid #e2e8f0' }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>Invoice Date</div>
      {writable ? (
        <input className={'cellbox' + (editVal !== undefined ? ' edited' : '')} aria-label="Invoice date"
          style={{ width: '100%' }}
          value={editVal ?? (dateRow?.invoice ?? invoiceDate ?? '')}
          onChange={e => onChange(e.target.value)} />
      ) : <div className="cellbox mono" style={{ width: '100%' }}>{(dateRow?.invoice ?? invoiceDate) || '—'}</div>}
    </div>
  )
}

function GstTaxRow({ show, editVal, invoiceVal, sapVal, onChange }: {
  show: boolean; editVal: string | undefined; invoiceVal: string | null; sapVal: string | null; onChange: (v: string) => void
}): JSX.Element {
  if (!show) return <></>
  const stat: CompareRow['status'] = invoiceVal && sapVal ? (invoiceVal === sapVal ? 'OK' : 'MISMATCH') : 'NA'
  return (
    <div className="mrow" style={{ marginTop: 6 }}>
      <span className="field-k">GST tax code</span>
      <input className={'cellbox' + (editVal !== undefined ? ' edited' : '')} aria-label="GST tax code"
        value={editVal ?? invoiceVal ?? ''}
        onChange={e => onChange(e.target.value)}
        placeholder="e.g. I3, I8" />
      <div className="cellbox po mono">{sapVal || '—'}</div>
      <Stat s={stat} />
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
  const [sapLooking, setSapLooking] = useState(false)
  const [sapResult, setSapResult] = useState<Record<string, unknown> | null>(null)
  const iid = Number(id)

  const load = useCallback(() => {
    api.invoice(iid).then(x => { setD(x); setErr(''); setReso(x.resolution ?? ''); setNote(x.resolution_note ?? '') }).catch(e => setErr(e.message))
  }, [iid])
  useEffect(() => { setEdits({}); load() }, [load])

  if (err) return <div className="err">{err}</div>
  if (!d) return <div className="empty"><span className="spin" /></div>

  const locked = d.miro_status === 'POSTED' || d.miro_status === 'PARKED'
  const writable: boolean = !!can('approver') && !locked
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
  const onSapLookup = async () => {
    const po = edits.po_number ?? d.po_number ?? ''
    const grn = edits.grn_number ?? d.grn_number ?? ''
    if (!po && !grn) return toast('SAP lookup', 'Enter a PO or GRN number first', true)
    setSapLooking(true); setSapResult(null)
    try {
      const r = await api.post<Record<string, unknown>>(`/api/invoices/${iid}/sap-lookup`, { po_number: po, grn_number: grn })
      setSapResult(r)
      if (r.grn_in_sap || r.po_in_sap) toast('SAP lookup', `Found in SAP — PO: ${r.po_number || '—'}  GRN: ${r.grn_number || '—'}`)
      else toast('SAP lookup', 'Not found in SAP', true)
    } catch (e) { toast('SAP lookup failed', (e as Error).message, true) } finally { setSapLooking(false) }
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
    <section style={{ maxWidth: '100%', width: '100%' }}>
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
            {tab === 'inv' && <InvoicePages id={iid} pages={d.page_count || 1} writable={writable} onApply={(kind, v) => {
              setEdits(e => ({ ...e, [kind === 'PO' ? 'po_number' : 'grn_number']: v }))
              toast(kind === 'PO' ? 'PO number set' : 'GRN number set', `${v} - check it, then press Save`)
            }} />}
            {tab === 'grn' && <GrnCopy d={d} />}
            {tab === 'po' && <PoCopy d={d} />}
          </div>
        </div>

        <div className="match">
          <div className="match-h"><b>Match details</b><span className="conf">{Math.round((d.confidence || 0) * 100)}% AI confidence</span></div>
          <div className="match-scroll">
          <div className="msec">
            <div className="lbl"><span className="t">Invoice vs SAP</span></div>

            {/* Invoice Date — label above field, separate from compare rows */}
            <InvoiceDateRow
              editVal={edits['invoice_date']}
              dateRow={d.compare.rows.find(r => r.field === 'invoice_date')}
              invoiceDate={d.invoice_date}
              writable={writable}
              onChange={v => setEdits({ ...edits, invoice_date: v })}
            />

            <div className="mrow hd"><span>Field</span><span>Invoice</span><span>GRN / SAP</span><span /></div>
            {d.compare.rows.filter(r => r.field !== 'invoice_date').map(r => (
              <div className="mrow" key={r.field}>
                <span className="field-k">{r.label}</span>
                {EDITABLE[r.field] && writable ? (
                  <input className={'cellbox' + (edits[EDITABLE[r.field]] !== undefined ? ' edited' : '')} aria-label={r.label}
                    value={edits[EDITABLE[r.field]] ?? r.invoice}
                    onChange={e => setEdits({ ...edits, [EDITABLE[r.field]]: e.target.value })} />
                ) : r.field === 'hsn_sac' ? (
                  /* HSN: show single or multiple codes cleanly */
                  <HsnDisplay value={r.invoice} />
                ) : <div className="cellbox mono">{r.invoice || '—'}</div>}
                <div className="cellbox po mono" title={r.po ? `PO: ${r.po}` : ''}>{r.field === 'hsn_sac' ? r.po || '—' : r.grn || '—'}</div>
                <Stat s={r.status} />
              </div>
            ))}
            <div className="mrow">
              <span className="field-k">MIRO document type</span>
              {writable && d.doc_type_options.length > 1 ? (
                <select className={'cellbox' + (edits.doc_type !== undefined ? ' edited' : '')} aria-label="MIRO document type"
                  value={edits.doc_type ?? d.doc_type ?? ''} onChange={e => setEdits({ ...edits, doc_type: e.target.value })}>
                  <option value="">— choose —</option>
                  {d.doc_type_options.map(o => <option key={o.doc_type} value={o.doc_type}>{o.doc_type} · {o.description}</option>)}
                </select>
              ) : <div className="cellbox mono" title="From the document-type master of the plant">{d.doc_type ? `${d.doc_type} · ${d.doc_type_desc ?? ''}` : '—'}</div>}
              <div className="cellbox po mono">plant {d.plant_code || '—'}</div>
              <Stat s={d.doc_type ? 'OK' : 'MISSING'} />
            </div>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>HSN is read from the invoice only — the right-hand value is SAP's HSN, used just to compare.</div>

            {/* GST Tax Code edit row */}
            <GstTaxRow
              show={writable}
              editVal={edits.gst_tax_code}
              invoiceVal={d.gst_tax_code}
              sapVal={d.sap_tax_code}
              onChange={v => setEdits({ ...edits, gst_tax_code: v })}
            />

            {/* SAP re-lookup button when PO or GRN is manually edited */}
            {(writable && (edits.po_number !== undefined || edits.grn_number !== undefined)) ? (
              <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 10 }}>
                <button className="btn sm" onClick={onSapLookup} disabled={sapLooking}>
                  {sapLooking ? '…' : '🔍'} Re-check SAP with entered PO/GRN
                </button>
                {sapResult != null ? (
                  <span style={{ fontSize: 12, color: (sapResult.po_in_sap || sapResult.grn_in_sap) ? '#16a34a' : '#dc2626' }}>
                    {(sapResult.po_in_sap || sapResult.grn_in_sap)
                      ? `✓ Found — PO: ${sapResult.po_number || '—'}  GRN: ${sapResult.grn_number || '—'}  Plant: ${sapResult.plant_code || '—'}`
                      : '✗ Not found in SAP'}
                  </span>
                ) : null}
              </div>
            ) : null}
            {(sapResult != null && (sapResult.po_in_sap || sapResult.grn_in_sap)) ? (
              <div style={{ marginTop: 8, padding: '8px 10px', background: '#f0fdf4', borderRadius: 6, border: '1px solid #bbf7d0', fontSize: 12 }}>
                <b>SAP matched details</b><br />
                PO date: {String(sapResult.po_date || '—')} · GRN date: {String(sapResult.grn_date || '—')} · GRN amount: {sapResult.grn_amount ? `₹${Number(sapResult.grn_amount).toLocaleString('en-IN', { maximumFractionDigits: 2 })}` : '—'}<br />
                Vendor code: {String(sapResult.sap_vendor_code || sapResult.vendor_code || '—')} · Plant: {String(sapResult.plant_code || '—')} · Tax code: {String(sapResult.sap_tax_code || '—')} · Company: {String(sapResult.company_code || '—')}
              </div>
            ) : null}
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

          <LineItemsSection lines={d.compare.lines} invoiceHsn={d.hsn_sac} writable={writable} onSaved={load} invoiceId={iid} />

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
          </div>{/* end match-scroll */}
        </div>
      </div>
    </section>
  )
}
