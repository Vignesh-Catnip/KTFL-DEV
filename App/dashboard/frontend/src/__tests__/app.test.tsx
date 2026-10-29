import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { HashRouter } from 'react-router-dom'
import { vi } from 'vitest'
import App from '../App'
import { AuthProvider } from '../auth'
import { ToastProvider, inr } from '../ui'

const row = {
  id: 7, filename: 'a.pdf', vendor: 'DSK ENGINEERS', vendor_code: '70042', invoice_number: '428/26-27', invoice_date: '04.08.2026',
  po_number: '8100007531', grn_number: '5000343863', plant_code: '2002', subtotal: 64240, total: 75803.2, gst_tax_code: 'I3',
  gst_percent: 18, hsn_sac: '82073000', sap_hsn: '82073000', queue: 'RECONCILE', status: 'REVIEW', exception_type: 'Quantity mismatch',
  issue_count: 1, top_issue: "Line 'X': invoice quantity 20 vs goods receipt 18", variance_amount: 620, multi_po_flag: false,
  resolution: null, approved_variance: false, miro_status: null, miro_doc_number: null, miro_message: null, miro_attempts: 0,
  miro_updated_at: null, created_at: '2026-10-24T10:00:00Z', pushed_at: null, pushed_by: null,
}

function mockFetch(handlers: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).split('?')[0]
    const key = `${init?.method ?? 'GET'} ${path}`
    if (!(key in handlers)) return new Response(JSON.stringify({ detail: 'nf ' + key }), { status: 404, headers: { 'content-type': 'application/json' } })
    const v = handlers[key]
    return new Response(JSON.stringify(v), { status: 200, headers: { 'content-type': 'application/json' } })
  }))
}
const mount = () => render(<HashRouter><AuthProvider><ToastProvider><App /></ToastProvider></AuthProvider></HashRouter>)

beforeEach(() => { localStorage.clear(); window.location.hash = '#/' })

test('formats rupees the Indian way', () => { expect(inr(75803.2)).toBe('₹75,803.20'); expect(inr(null)).toBe('—') })

test('login shows an error for a wrong password', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Wrong user name or password' }), { status: 401, headers: { 'content-type': 'application/json' } })))
  mount()
  fireEvent.change(screen.getByLabelText('User name'), { target: { value: 'admin' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'x' } })
  fireEvent.click(screen.getByRole('button', { name: /sign in/i }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Wrong user name or password')
})

test('dashboard lists invoices with queue tag and reason', async () => {
  localStorage.setItem('clearmatch.token', 't')
  mockFetch({
    'GET /api/auth/me': { username: 'admin', name: 'Administrator', role: 'admin' },
    'GET /api/stats': { queues: { MATCHED: 3, RECONCILE: 1, BLOCKED: 0 }, value: { MATCHED: 1, RECONCILE: 2, BLOCKED: 0 }, posting: {}, plants: [{ plant: '2002', count: 4 }], pending_jobs: 0, total: 4, straight_through_pct: 75 },
    'GET /api/health': { sap: { connected: true, detail: '' }, ollama: { ok: true, detail: '' } },
    'GET /api/invoices': { total: 1, page: 1, size: 25, items: [row] },
  })
  mount()
  expect(await screen.findByText('DSK ENGINEERS')).toBeInTheDocument()
  expect(screen.getAllByText('Reconcile').length).toBeGreaterThan(0)
  expect(screen.getByText(/invoice quantity 20 vs goods receipt 18/)).toBeInTheDocument()
  expect(screen.getByText('75% straight-through')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByText('SQL Server connected')).toBeInTheDocument())
})

test('posting log shows robot message and status', async () => {
  localStorage.setItem('clearmatch.token', 't')
  window.location.hash = '#/posting'
  mockFetch({
    'GET /api/auth/me': { username: 'admin', name: 'A', role: 'admin' },
    'GET /api/stats': { queues: {}, value: {}, posting: {}, plants: [], pending_jobs: 0, total: 0, straight_through_pct: 0 },
    'GET /api/health': { sap: { connected: true, detail: '' }, ollama: { ok: true, detail: '' } },
    'GET /api/posting': { counts: { FAILED: 1 }, items: [{ ...row, queue: 'MATCHED', miro_status: 'FAILED', miro_message: 'Balance not zero', miro_attempts: 2 }] },
  })
  mount()
  expect(await screen.findByText('Balance not zero')).toBeInTheDocument()
  expect(screen.getAllByText('Failed').length).toBeGreaterThan(0)
  expect(screen.getByRole('button', { name: 'Re-queue' })).toBeInTheDocument()
})
