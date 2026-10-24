/**
 * MOCK DATA — stands in for the Django API. Nothing here is a real financial figure.
 * Records are language-neutral: names, notes, categories and risk explanations are translation
 * keys (see i18n/locales), so the UI renders them in the active language.
 * The "model" logic below (cumulative balance, risk level) imitates what the backend would return.
 */
import { addDays, format, parseISO, startOfDay } from 'date-fns'
import type {
  AssistantReplyPart,
  DemoState,
  ForecastPoint,
  ForecastResponse,
  Obligation,
  PlanActualResponse,
  PlanActualRow,
  RiskAssessment,
  RiskFactor,
  Transaction,
} from '../types'
import type { ItemRef } from '../i18n/helpers'

const OPENING_BALANCE = 38_000_000
const HORIZON = 30
const HISTORY = 30

const iso = (d: Date) => format(d, 'yyyy-MM-dd')
const today = () => startOfDay(new Date())
const at = (offset: number) => iso(addDays(today(), offset))
const name = (id: string) => `mock.names.${id}`
const note = (id: string) => `mock.notes.${id}`

function mulberry32(seed: number) {
  let a = seed
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/* ---------------------------- Obligations ---------------------------- */

type Seed = { offset: number; kind: Obligation['kind']; name: string; amount: number; status: Obligation['status']; note?: string }

const obligationSeeds: Seed[] = [
  // outgoing, upcoming
  { offset: 3, kind: 'payment', name: 'rent', amount: 2_400_000, status: 'planned', note: 'leaseContract' },
  { offset: 4, kind: 'payment', name: 'payroll', amount: 18_500_000, status: 'planned', note: 'payrollCalendar' },
  { offset: 7, kind: 'payment', name: 'vat', amount: 6_200_000, status: 'planned', note: 'taxCalendar' },
  { offset: 8, kind: 'payment', name: 'utilities', amount: 900_000, status: 'planned' },
  { offset: 9, kind: 'payment', name: 'agroYug', amount: 9_800_000, status: 'planned', note: 'invoiceAY' },
  { offset: 12, kind: 'payment', name: 'loan', amount: 14_000_000, status: 'planned', note: 'loanSchedule' },
  { offset: 14, kind: 'payment', name: 'techSnab', amount: 8_900_000, status: 'planned', note: 'invoiceTS' },
  { offset: 16, kind: 'payment', name: 'profitTax', amount: 7_400_000, status: 'planned', note: 'taxCalendar' },
  { offset: 20, kind: 'payment', name: 'advance', amount: 15_000_000, status: 'planned', note: 'payrollCalendar' },
  { offset: 22, kind: 'payment', name: 'leasingPayment', amount: 3_100_000, status: 'planned' },
  { offset: 25, kind: 'payment', name: 'insurance', amount: 5_600_000, status: 'planned', note: 'taxCalendar' },
  { offset: 27, kind: 'payment', name: 'contractor', amount: 4_000_000, status: 'planned', note: 'act58' },
  // incoming, upcoming
  { offset: 2, kind: 'receipt', name: 'dontorg', amount: 9_200_000, status: 'expected', note: 'invoice1204' },
  { offset: 6, kind: 'receipt', name: 'yugAgro', amount: 12_500_000, status: 'expected', note: 'supplyContract' },
  { offset: 10, kind: 'receipt', name: 'vasiliev', amount: 3_800_000, status: 'expected' },
  { offset: 17, kind: 'receipt', name: 'rostovStroy', amount: 14_700_000, status: 'expected', note: 'postponedBefore' },
  { offset: 18, kind: 'receipt', name: 'kubanFood', amount: 8_300_000, status: 'expected' },
  { offset: 24, kind: 'receipt', name: 'dontorg', amount: 11_000_000, status: 'expected', note: 'invoice1231' },
  { offset: 29, kind: 'receipt', name: 'rostovStroy', amount: 6_000_000, status: 'expected' },
  // past
  { offset: -3, kind: 'receipt', name: 'agroPartner', amount: 5_000_000, status: 'overdue', note: 'overdueReceipt' },
  { offset: -1, kind: 'payment', name: 'yugEnergo', amount: 1_250_000, status: 'overdue', note: 'overduePayment' },
  { offset: -5, kind: 'payment', name: 'donpak', amount: 4_300_000, status: 'completed' },
  { offset: -8, kind: 'receipt', name: 'kubanFood', amount: 7_100_000, status: 'completed' },
]

export function buildObligations(): Obligation[] {
  return obligationSeeds
    .map((s, i) => ({
      id: `ob-${i + 1}`,
      kind: s.kind,
      nameKey: name(s.name),
      date: at(s.offset),
      amount: s.amount,
      status: s.status,
      noteKey: s.note ? note(s.note) : undefined,
    }))
    .sort((a, b) => a.date.localeCompare(b.date))
}

/** Items that feed the forecast: not completed, not overdue, dated after today. */
export function isForecastItem(o: Obligation): boolean {
  return (o.status === 'planned' || o.status === 'expected') && o.date > at(0)
}

/* --------------------------- Transactions ---------------------------- */

const parties: Array<{ id: string; category: string; dir: 'in' | 'out' }> = [
  { id: 'dontorg', category: 'revenue', dir: 'in' },
  { id: 'yugAgro', category: 'revenue', dir: 'in' },
  { id: 'kubanFood', category: 'revenue', dir: 'in' },
  { id: 'vasiliev', category: 'revenue', dir: 'in' },
  { id: 'rostovStroy', category: 'revenue', dir: 'in' },
  { id: 'donpak', category: 'purchases', dir: 'out' },
  { id: 'agroYug', category: 'purchases', dir: 'out' },
  { id: 'techSnab', category: 'purchases', dir: 'out' },
  { id: 'yugEnergo', category: 'utilities', dir: 'out' },
  { id: 'taxService', category: 'taxes', dir: 'out' },
  { id: 'leasingYug', category: 'leasing', dir: 'out' },
  { id: 'logisticPro', category: 'logistics', dir: 'out' },
]

export function buildTransactions(): Transaction[] {
  const rnd = mulberry32(2026)
  const list: Transaction[] = []
  let n = 0
  for (let d = -(HISTORY - 1); d <= 0; d++) {
    const count = 1 + Math.floor(rnd() * 3)
    for (let k = 0; k < count; k++) {
      const p = parties[Math.floor(rnd() * parties.length)]
      const base = p.dir === 'in' ? 2_500_000 + rnd() * 8_000_000 : 1_000_000 + rnd() * 6_500_000
      const amount = Math.round(base / 10_000) * 10_000
      n++
      list.push({
        id: `tx-${n}`,
        date: at(d),
        counterpartyKey: name(p.id),
        descriptionKey: p.dir === 'in' ? 'mock.tx.invoice' : 'mock.tx.contract',
        descriptionParams: { n: p.dir === 'in' ? 1000 + n : 200 + n },
        amount,
        direction: p.dir,
        categoryKey: `categories.${p.category}`,
        status: 'completed',
      })
    }
  }
  // Pending operations for today: not yet part of the confirmed balance.
  list.push(
    { id: 'tx-p1', date: at(0), counterpartyKey: name('dontorg'), descriptionKey: 'mock.tx.pendingIn', amount: 3_200_000, direction: 'in', categoryKey: 'categories.revenue', status: 'pending' },
    { id: 'tx-p2', date: at(0), counterpartyKey: name('logisticPro'), descriptionKey: 'mock.tx.pendingOut', amount: 850_000, direction: 'out', categoryKey: 'categories.logistics', status: 'pending' },
  )
  return list
}

const signed = (t: Transaction) => (t.direction === 'in' ? t.amount : -t.amount)

/** End-of-day balance for the last HISTORY days, walking back from today's balance. */
function historyBalances(txs: Transaction[]): Map<string, number> {
  const net = new Map<string, number>()
  for (const t of txs) {
    if (t.status === 'pending') continue
    net.set(t.date, (net.get(t.date) ?? 0) + signed(t))
  }
  const out = new Map<string, number>()
  let bal = OPENING_BALANCE
  for (let d = 0; d >= -(HISTORY - 1); d--) {
    out.set(at(d), bal)
    bal -= net.get(at(d)) ?? 0
  }
  return out
}

/* ----------------------------- Forecast ------------------------------ */

function buildRisk(points: ForecastPoint[], obligations: Obligation[]): RiskAssessment {
  const future = points.filter((p) => p.date > at(0))
  const first = future.find((p) => (p.forecast as number) < 0)
  if (!first) return { level: 'low', shortageDate: null, deficitAmount: null, factors: [] }
  const min = Math.min(...future.map((p) => p.forecast as number))
  const items = obligations.filter(isForecastItem)
  const bigOut = items
    .filter((o) => o.kind === 'payment' && o.date <= first.date)
    .sort((a, b) => b.amount - a.amount)
    .slice(0, 3)
  const late = items.find((o) => o.kind === 'receipt' && o.date > first.date && o.date <= iso(addDays(parseISO(first.date), 3)))
  const factors: RiskFactor[] = [
    {
      id: 'payments',
      params: { namesKeys: bigOut.map((o) => o.nameKey), untilShortDate: first.date },
      amount: bigOut.reduce((s, o) => s + o.amount, 0),
    },
  ]
  if (late) {
    factors.push({ id: 'lateReceipt', params: { nameKey: late.nameKey, onShortDate: late.date }, amount: late.amount })
  }
  const overdue = obligations.filter((o) => o.status === 'overdue' && o.kind === 'receipt')
  if (overdue.length) {
    factors.push({ id: 'overdue', params: { namesKeys: overdue.map((o) => o.nameKey) }, amount: overdue.reduce((s, o) => s + o.amount, 0) })
  }
  return {
    level: -min > 3_000_000 ? 'high' : 'medium',
    shortageDate: first.date,
    deficitAmount: -min,
    probability: 0.62, // supplied by the (mock) model, not calculated in the UI
    factors,
  }
}

export function buildForecast(demo: DemoState): ForecastResponse {
  const obligations = buildObligations()
  const txs = buildTransactions()
  const hist = historyBalances(txs)
  const items = obligations.filter(isForecastItem)

  const points: ForecastPoint[] = []
  for (let d = -(HISTORY - 1); d <= 0; d++) {
    const date = at(d)
    const day = txs.filter((t) => t.date === date && t.status !== 'pending')
    const bal = hist.get(date) ?? null
    points.push({
      date,
      actual: bal,
      forecast: d === 0 ? bal : null,
      inflow: day.filter((t) => t.direction === 'in').reduce((s, t) => s + t.amount, 0),
      outflow: day.filter((t) => t.direction === 'out').reduce((s, t) => s + t.amount, 0),
    })
  }
  let bal = OPENING_BALANCE
  for (let d = 1; d <= HORIZON; d++) {
    const date = at(d)
    const day = items.filter((o) => o.date === date)
    const inflow = day.filter((o) => o.kind === 'receipt').reduce((s, o) => s + o.amount, 0)
    const outflow = day.filter((o) => o.kind === 'payment').reduce((s, o) => s + o.amount, 0)
    bal += inflow - outflow
    const spread = Math.round(250_000 * Math.pow(d, 0.9))
    points.push({ date, actual: null, forecast: bal, lower: bal - spread, upper: bal + Math.round(spread * 0.8), inflow, outflow })
  }

  const fut = points.filter((p) => p.date > at(0))
  const minPoint = fut.reduce((m, p) => ((p.forecast as number) < (m.forecast as number) ? p : m), fut[0])
  const expectedInflows = fut.reduce((s, p) => s + p.inflow, 0)
  const expectedOutflows = fut.reduce((s, p) => s + p.outflow, 0)

  const base: ForecastResponse = {
    source: 'mock',
    generatedAt: format(new Date(), "yyyy-MM-dd'T'HH:mm:ss"),
    today: at(0),
    horizonDays: HORIZON,
    points,
    summary: {
      currentBalance: OPENING_BALANCE,
      minBalance: minPoint.forecast,
      minBalanceDate: minPoint.date,
      expectedInflows,
      expectedOutflows,
    },
    risk: buildRisk(points, obligations),
    quality: { completeness: 92, warnings: ['reserveStatement'] },
  }

  if (demo === 'empty') {
    return {
      ...base,
      points: [],
      summary: { currentBalance: null, minBalance: null, minBalanceDate: null, expectedInflows: 0, expectedOutflows: 0 },
      risk: { level: 'unknown', shortageDate: null, deficitAmount: null, factors: [] },
      quality: { completeness: 0, warnings: ['noData'] },
    }
  }
  if (demo === 'incomplete') {
    return {
      ...base,
      points: points.map((p) => ({ ...p, forecast: null, lower: null, upper: null })),
      summary: { currentBalance: null, minBalance: null, minBalanceDate: null, expectedInflows, expectedOutflows },
      risk: { level: 'unknown', shortageDate: null, deficitAmount: null, factors: [] },
      quality: { completeness: 58, warnings: ['noBalance', 'partialStatement', 'unconfirmedReceipts'] },
    }
  }
  return base
}

/* --------------------------- Plan vs actual -------------------------- */

export function buildPlanActual(periodDays: number, demo: DemoState): PlanActualResponse {
  if (demo === 'empty' || demo === 'incomplete') return { source: 'mock', periodDays, rows: [], available: false }
  const rnd = mulberry32(77)
  const txs = buildTransactions()
  const hist = historyBalances(txs)
  const done = txs.filter((t) => t.status !== 'pending')
  const rows: PlanActualRow[] = []
  for (let d = -(periodDays - 1); d <= 0; d++) {
    const date = at(d)
    const day = done.filter((t) => t.date === date)
    const actualIn = day.filter((t) => t.direction === 'in').reduce((s, t) => s + t.amount, 0)
    const actualOut = day.filter((t) => t.direction === 'out').reduce((s, t) => s + t.amount, 0)
    const k1 = 0.8 + rnd() * 0.45
    const k2 = 0.85 + rnd() * 0.35
    const k3 = 0.93 + rnd() * 0.12
    const actualBalance = hist.get(date) as number
    rows.push({
      date,
      plannedIn: Math.round((actualIn * k1) / 10_000) * 10_000,
      actualIn,
      plannedOut: Math.round((actualOut * k2) / 10_000) * 10_000,
      actualOut,
      plannedBalance: Math.round((actualBalance * k3) / 10_000) * 10_000,
      actualBalance,
    })
  }
  return { source: 'mock', periodDays, rows, available: true }
}

/* ---------------------------- Assistant ------------------------------ */

/** Canned, keyword-routed replies as keys + raw params. Only figures present in the supplied data are used. */
export function buildAssistantReply(
  question: string,
  f: ForecastResponse,
  obligations: Obligation[],
): { parts: AssistantReplyPart[]; basedOn: string[] } {
  const q = question.toLowerCase()
  const { risk, summary } = f
  const src = (id: string) => `assistant.sources.${id}`
  const reply = (key: string) => `assistant.replies.${key}`

  if (f.points.length === 0 || summary.currentBalance == null) {
    return { parts: [{ key: reply('noData') }], basedOn: [src('quality')] }
  }
  // Keywords for both languages, since users may type in either.
  if (/сценар|что если|перенест|сдвин|scenario|what if|shift|postpone/.test(q)) {
    const big: ItemRef[] = obligations
      .filter((o) => o.kind === 'payment' && isForecastItem(o))
      .sort((a, b) => b.amount - a.amount)
      .slice(0, 2)
      .map((o) => ({ nameKey: o.nameKey, amount: o.amount, date: o.date }))
    return { parts: [{ key: reply('scenario'), params: { shiftItems: big } }], basedOn: [src('obligations')] }
  }
  if (/провер|предлож|что делать|как улучш|совет|check|suggest|what should|improve|advice/.test(q)) {
    const ov: ItemRef[] = obligations
      .filter((o) => o.status === 'overdue')
      .map((o) => ({ nameKey: o.nameKey, amount: o.amount, statusKey: 'status.overdueLower' }))
    const late = risk.factors.find((x) => x.id === 'lateReceipt')
    const parts: AssistantReplyPart[] = [
      ov.length ? { key: reply('review'), params: { overdueItems: ov } } : { key: reply('reviewNone') },
    ]
    if (late) parts.push({ key: 'risk.factors.lateReceipt.description', params: late.params })
    parts.push({ key: reply('hintsDisclaimer') })
    return { parts, basedOn: [src('obligations'), src('factors')] }
  }
  if (/остаток|баланс|сейчас|balance|current|now/.test(q)) {
    return {
      parts: [{ key: reply('balance'), params: { currentAmount: summary.currentBalance, minAmount: summary.minBalance ?? undefined, minShortDate: summary.minBalanceDate ?? undefined } }],
      basedOn: [src('forecast')],
    }
  }
  if (!risk.shortageDate) {
    return { parts: [{ key: reply('noShortage'), params: { minAmount: summary.minBalance ?? undefined } }], basedOn: [src('forecast')] }
  }
  return {
    parts: [
      {
        key: reply(risk.probability != null ? 'shortageProb' : 'shortage'),
        params: { shortageShortDate: risk.shortageDate, deficitAmount: risk.deficitAmount ?? undefined, probabilityPercent: risk.probability },
      },
      { key: reply('factorsList'), params: { factorsKeys: risk.factors.map((x) => `risk.factors.${x.id}.label`) } },
    ],
    basedOn: [src('forecast'), src('factors')],
  }
}
