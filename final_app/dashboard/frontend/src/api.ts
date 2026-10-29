const BASE = (import.meta as any).env?.VITE_API ?? ''
const TOKEN_KEY = 'clearmatch.token'

export const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) } catch { return null } }
export const setToken = (t: string | null) => {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) } catch { /* private mode */ }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) }
  const tok = getToken()
  if (tok) headers.Authorization = `Bearer ${tok}`
  if (init.body && !(init.body instanceof FormData)) headers['Content-Type'] = 'application/json'
  const res = await fetch(BASE + path, { ...init, headers })
  if (res.status === 401 && !path.includes('/auth/login')) {
    setToken(null)
    window.dispatchEvent(new Event('clearmatch:logout'))
  }
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j)
    } catch { /* not json */ }
    throw new ApiError(res.status, msg)
  }
  const ct = res.headers.get('content-type') ?? ''
  return (ct.includes('json') ? res.json() : (res.text() as unknown)) as Promise<T>
}

const json = (body: unknown) => ({ body: JSON.stringify(body) })

export interface Issue {
  code: string; severity: 'BLOCK' | 'RECON' | 'INFO' | 'WAIVED'; category: string; field: string
  message: string; invoice_value: string; sap_value: string; variance: number | null
}
export interface InvoiceRow {
  id: number; filename: string; vendor: string | null; vendor_code: string | null; invoice_number: string | null
  invoice_date: string | null; po_number: string | null; grn_number: string | null; plant_code: string | null
  subtotal: number; total: number; gst_tax_code: string | null; gst_percent: number | null
  hsn_sac: string | null; sap_hsn: string | null; doc_type: string | null; doc_type_desc: string | null; queue: 'MATCHED' | 'RECONCILE' | 'BLOCKED'; status: string
  exception_type: string; issue_count: number; top_issue: string; variance_amount: number | null
  completeness: number | null; multi_po_flag: boolean; resolution: string | null; approved_variance: boolean
  miro_status: string | null; miro_doc_number: string | null; miro_message: string | null
  miro_attempts: number | null; miro_updated_at: string | null; created_at: string; pushed_at: string | null
  pushed_by: string | null
}
export interface CompareRow { field: string; label: string; invoice: string; grn: string; po: string; status: 'OK' | 'MISMATCH' | 'MISSING' | 'NA'; note: string }
export interface CompareLine {
  id: number; item: string; quantity: number; uom: string | null; rate: number; amount: number
  hsn_sac: string | null; gr_quantity: number | null; gr_amount: number | null; gr_row: string | null; line_status: string
}
export interface DocTypeOption { doc_type: string; number_range: string | null; description: string | null }
export interface InvoiceDetail extends InvoiceRow {
  doc_type_options: DocTypeOption[]
  gstin: string | null; currency: string; cgst: number; sgst: number; igst: number; tax: number
  po_date: string | null; grn_date: string | null; grn_amount: number | null; sap_tax_code: string | null
  hsn_match: boolean | null; sap_checked: boolean; grn_in_sap: boolean; po_list: string | null
  red_flag_reason: string | null; issues: Issue[]; page_count: number | null; resolution_note: string | null
  resolved_by: string | null; resolved_at: string | null; confidence: number; company_code: string | null
  compare: { rows: CompareRow[]; lines: CompareLine[]; grn_rows: Record<string, unknown>[]; po_items: Record<string, unknown>[]; exception_type: string }
  history: { action: string; actor: string | null; at: string; detail: Record<string, unknown> }[]
}
export interface Job { id: number; filename: string; status: string; stage: string | null; source: string; invoice_id: number | null; queue: string | null; error: string | null; by: string | null; created_at: string; finished_at: string | null }
export interface Stats { trend?: Record<string, number[]>; queues: Record<string, number>; value: Record<string, number>; posting: Record<string, number>; plants: { plant: string; count: number }[]; pending_jobs: number; total: number; straight_through_pct: number }

export interface RobotRun { id: number; status: string; trigger: string; by: string | null; mode: string | null; started_at: string | null; finished_at: string | null; exit_code: number | null; summary: Record<string, number>; error: string | null; has_report: boolean }
export interface RobotStatus { enabled: boolean; configured: boolean; problem: string; mode: string; running: boolean; running_id: number | null; auto_run: boolean; waiting: number; next_auto_in: number | null; last: RobotRun | null }
export interface ErrorLine { item: string; quantity: number; gr_quantity: number | null; amount: number; gr_amount: number | null; rate: number; status: string; problem: string; fix: string; diff_quantity: number | null; diff_amount: number | null; gr_row: string | null }

export const api = {
  login: (username: string, password: string) =>
    request<{ token: string; user: { username: string; name: string; role: string } }>('/api/auth/login', { method: 'POST', ...json({ username, password }) }),
  me: () => request<{ username: string; name: string; role: string }>('/api/auth/me'),
  stats: () => request<Stats>('/api/stats'),
  health: () => request<{ sap: { connected: boolean; detail: string }; ollama: { ok: boolean; detail: string }; robot?: { enabled: boolean; configured: boolean; running: boolean; mode: string; waiting: number } }>('/api/health'),
  robotStatus: () => request<RobotStatus>('/api/robot/status'),
  robotRuns: () => request<RobotRun[]>('/api/robot/runs'),
  robotRun: () => request<{ started: boolean; queued: boolean; reason: string }>('/api/robot/run', { method: 'POST' }),
  robotStop: () => request<{ stopped: boolean }>('/api/robot/stop', { method: 'POST' }),
  robotLog: (id: number) => request<string>(`/api/robot/runs/${id}/log`),
  invoices: (p: Record<string, string | number | undefined>) => {
    const qs = new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]))
    return request<{ total: number; page: number; size: number; items: InvoiceRow[] }>(`/api/invoices?${qs}`)
  },
  invoice: (id: number) => request<InvoiceDetail>(`/api/invoices/${id}`),
  edit: (id: number, body: Record<string, unknown>) => request<{ queue: string }>(`/api/invoices/${id}`, { method: 'PATCH', ...json(body) }),
  recheck: (id: number) => request<{ queue: string }>(`/api/invoices/${id}/recheck`, { method: 'POST' }),
  resolve: (id: number, body: { resolution: string; note: string; approve_variance: boolean }) =>
    request<{ queue: string }>(`/api/invoices/${id}/resolve`, { method: 'POST', ...json(body) }),
  push: (id: number) => request<{ ok: boolean; message: string; robot?: { started: boolean; queued: boolean } }>(`/api/invoices/${id}/push`, { method: 'POST' }),
  exceptions: (category?: string) => request<{ total: number; by_type: Record<string, number>; items: (InvoiceRow & { issues: (Issue & { fix: string })[]; bad_lines: ErrorLine[] })[] }>(`/api/exceptions${category ? `?category=${encodeURIComponent(category)}` : ''}`),
  posting: (status?: string) => request<{ counts: Record<string, number>; items: InvoiceRow[] }>(`/api/posting${status ? `?status=${status}` : ''}`),
  requeue: (id: number) => request(`/api/posting/${id}/requeue`, { method: 'POST' }),
  audit: (limit = 200) => request<{ id: number; invoice_id: number | null; action: string; actor: string | null; at: string; detail: Record<string, unknown> }[]>(`/api/audit?limit=${limit}`),
  config: () => request<{ allow_upload: boolean; watch_folder: string | null; poll_seconds: number; min_completeness: number; worker_enabled: boolean; folders?: Record<string, string | null> }>('/api/config'),
  jobs: () => request<Job[]>('/api/jobs'),
  retryJob: (id: number) => request(`/api/jobs/${id}/retry`, { method: 'POST' }),
  upload: (files: File[]) => {
    const fd = new FormData()
    files.forEach(f => fd.append('files', f))
    return request<{ jobs: { id: number; filename: string }[] }>('/api/jobs/upload', { method: 'POST', body: fd })
  },
  pageUrl: (id: number, page: number) => `${BASE}/api/invoices/${id}/page/${page}.png`,
  /** page images / CSV need the bearer token, so they are fetched and turned into blob URLs */
  blobUrl: async (path: string) => {
    const res = await fetch(BASE + path, { headers: { Authorization: `Bearer ${getToken()}` } })
    if (!res.ok) throw new ApiError(res.status, 'Not available')
    return URL.createObjectURL(await res.blob())
  },
  download: async (path: string, name: string) => {
    const url = await api.blobUrl(path)
    const a = document.createElement('a')
    a.href = url; a.download = name; a.click()
    setTimeout(() => URL.revokeObjectURL(url), 5000)
  },
}
