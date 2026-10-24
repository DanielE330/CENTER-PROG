/**
 * Offline demo backend (`npm run dev:demo`). Implements the same surface as the real `api` client with an
 * in-memory company, so the whole app works without registration and without a server. Data resets on reload.
 */
import type {
  Backtest,
  ExpectedEvent,
  ForecastDay,
  ForecastResult,
  ForecastRun,
  ImportPreview,
  ImportRow,
  KeyRate,
  Me,
  NewExpectedEvent,
  PlanFact,
  PlanFactItem,
  RiskItem,
  RiskLevel,
  Rule,
  ScenarioParams,
  ScenarioResponse,
  SyncResult,
  TaxCalendarResult,
  Transaction,
  TransactionPage,
} from '../types'
import type { TransactionQuery } from './api'

const DAY = 86_400_000
const OPENING_KOP = 150_000_000 // 1 500 000 ₽

const me: Me = { email: 'demo@example.com', company_id: 'demo', company_name: 'ООО «Демо-Компания»' }
const sleep = (ms = 150) => new Promise((r) => setTimeout(r, ms))

// ---------- dates ----------
const startOfToday = () => {
  const d = new Date()
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}
const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n)
const dayIndex = (d: Date) => Math.round(d.getTime() / DAY)

// ---------- recurring flows (the "company") ----------
interface Flow {
  date: Date
  amount: number // signed kopecks
  counterparty: string
  title: string
  category: string
  recurring: boolean
}

const R = (rub: number) => rub * 100

function recurringOn(d: Date): Omit<Flow, 'date' | 'recurring'>[] {
  const out: Omit<Flow, 'date' | 'recurring'>[] = []
  const dom = d.getDate()
  const idx = dayIndex(d)
  if (dom === 5 || dom === 20) out.push({ amount: -R(1_400_000), counterparty: 'Сотрудники', title: 'Выплата заработной платы', category: 'payroll' })
  if (dom === 10) out.push({ amount: -R(350_000), counterparty: 'ООО «Бизнес-Центр»', title: 'Аренда офиса', category: 'rent' })
  if (dom === 28) out.push({ amount: -R(620_000), counterparty: 'ФНС России', title: 'Налоги и взносы', category: 'taxes' })
  if (d.getDay() === 1) out.push({ amount: -R(280_000), counterparty: 'ООО «Снабжение»', title: 'Оплата поставщику', category: 'suppliers' })
  if (dom === 15) out.push({ amount: -R(45_000), counterparty: 'ПАО «Связь»', title: 'Связь и интернет', category: 'utilities' })
  if (idx % 14 === 3) out.push({ amount: R(1_100_000), counterparty: 'ООО «Вектор»', title: 'Оплата по договору', category: 'revenue' })
  if (dom === 12 || dom === 26) out.push({ amount: R(900_000), counterparty: 'АО «Прогресс»', title: 'Оплата по счёту', category: 'revenue' })
  return out
}

function mulberry32(seed: number) {
  return () => {
    seed |= 0
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

// ---------- history ----------
const HISTORY_DAYS = 180

function buildHistory(): Transaction[] {
  const rnd = mulberry32(42)
  const today = startOfToday()
  const rows: Omit<Transaction, 'id'>[] = []
  for (let i = HISTORY_DAYS; i >= 1; i--) {
    const d = addDays(today, -i)
    for (const f of recurringOn(d)) {
      const jitter = f.category === 'revenue' || f.category === 'suppliers' ? 1 + (rnd() - 0.5) * 0.2 : 1
      rows.push({
        date: iso(d),
        amount_kop: String(Math.round((f.amount * jitter) / 100) * 100),
        counterparty: f.counterparty,
        purpose: f.title,
        category: f.category,
        category_confidence: 0.9 + rnd() * 0.1,
        manual: false,
      })
    }
    if (rnd() < 0.35) {
      rows.push({
        date: iso(d),
        amount_kop: String(-R(Math.round(2_000 + rnd() * 38_000))),
        counterparty: ['ООО «Реклама+»', 'ИП Соколов', 'ООО «Канцторг»'][Math.floor(rnd() * 3)],
        purpose: 'Оплата услуг',
        category: rnd() < 0.5 ? 'marketing' : 'other_expense',
        category_confidence: 0.55 + rnd() * 0.35,
        manual: false,
      })
    }
    if (rnd() < 0.2) {
      rows.push({
        date: iso(d),
        amount_kop: String(-R(Math.round(100 + rnd() * 900))),
        counterparty: 'Банк',
        purpose: 'Комиссия за обслуживание',
        category: 'bank_fees',
        category_confidence: 0.99,
        manual: false,
      })
    }
  }
  return rows.map((r, i) => ({ ...r, id: i + 1 }))
}

const transactions = buildHistory()

// ---------- expected events ----------
let eventSeq = 0
const mkEvent = (offset: number, amountRub: number, title: string, source: string, counterparty: string, confidence = 0.8): ExpectedEvent => ({
  id: `demo-ev-${++eventSeq}`,
  date: iso(addDays(startOfToday(), offset)),
  amount: String(R(amountRub)),
  title,
  source,
  confidence,
  counterparty,
})

const events: ExpectedEvent[] = [
  mkEvent(4, 650_000, 'Счёт № 114', 'invoice', 'ООО «Горизонт»', 0.85),
  mkEvent(7, -420_000, 'Закупка оборудования', 'contract', 'ООО «ТехноПоставка»', 0.9),
  mkEvent(11, -180_000, 'Страхование', 'manual', 'СК «Надёжность»', 0.95),
  mkEvent(18, 1_300_000, 'Оплата по договору № 27', 'contract', 'АО «Прогресс»', 0.7),
]

// ---------- forecast ----------
const phi = (x: number) => {
  // Abramowitz–Stegun normal CDF
  const t = 1 / (1 + 0.2316419 * Math.abs(x))
  const d = 0.3989423 * Math.exp((-x * x) / 2)
  const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
  return x > 0 ? 1 - p : p
}

const DEFAULT_PARAMS: ScenarioParams = {
  delay_days: 0,
  delay_counterparty: null,
  revenue_drop_pct: 0,
  fixed_cost_growth_pct: 0,
  extra_expense_kop: null,
  extra_expense_date: null,
  extra_income_kop: null,
  extra_income_date: null,
}

let horizon = 60
let runSeq = 0

function compute(h: number, sc: ScenarioParams): ForecastResult {
  const today = startOfToday()
  const flows: Flow[] = []
  for (let i = 1; i <= h + 60; i++) {
    const d = addDays(today, i)
    for (const f of recurringOn(d)) flows.push({ ...f, date: d, recurring: true })
  }
  for (const e of events) {
    const d = new Date(`${e.date}T00:00:00`)
    flows.push({ date: d, amount: Number(e.amount), counterparty: e.counterparty, title: e.title, category: Number(e.amount) > 0 ? 'revenue' : 'other_expense', recurring: false })
  }

  const adjusted = flows.map((f) => {
    let { amount, date } = f
    if (amount > 0) {
      if (sc.delay_days && (!sc.delay_counterparty || f.counterparty === sc.delay_counterparty)) date = addDays(date, sc.delay_days)
      amount = Math.round(amount * (1 - sc.revenue_drop_pct / 100))
    } else if (f.category === 'payroll' || f.category === 'rent') {
      amount = Math.round(amount * (1 + sc.fixed_cost_growth_pct / 100))
    }
    return { ...f, amount, date }
  })
  if (sc.extra_expense_kop && sc.extra_expense_date) adjusted.push({ date: new Date(`${sc.extra_expense_date}T00:00:00`), amount: -Math.abs(sc.extra_expense_kop), counterparty: '', title: 'Доп. расход', category: 'other_expense', recurring: false })
  if (sc.extra_income_kop && sc.extra_income_date) adjusted.push({ date: new Date(`${sc.extra_income_date}T00:00:00`), amount: Math.abs(sc.extra_income_kop), counterparty: '', title: 'Доп. поступление', category: 'revenue', recurring: false })

  const byDate = new Map<string, number>()
  for (const f of adjusted) byDate.set(iso(f.date), (byDate.get(iso(f.date)) ?? 0) + f.amount)

  const days: ForecastDay[] = []
  let bal = OPENING_KOP
  let brokenBy = 0
  for (let i = 1; i <= h; i++) {
    const date = iso(addDays(today, i))
    bal += byDate.get(date) ?? 0
    const sigma = R(55_000) * Math.sqrt(i) + R(20_000)
    const probNeg = phi(-bal / sigma)
    brokenBy = Math.max(brokenBy, probNeg)
    days.push({
      date,
      p10: String(Math.round(bal - 1.2816 * sigma)),
      p50: String(Math.round(bal)),
      p90: String(Math.round(bal + 1.2816 * sigma)),
      prob_negative: probNeg,
      prob_broken_by: Math.min(1, brokenBy + (1 - brokenBy) * 0.15 * brokenBy),
    })
  }

  const p50 = days.map((d) => Number(d.p50))
  const p10 = days.map((d) => Number(d.p10))
  const firstBreak = days.find((d) => Number(d.p50) < 0)
  const possible = days.find((d) => d.prob_negative > 0.25)
  const shortfall = days.length ? days[days.length - 1].prob_broken_by : 0
  const minP50 = Math.min(0, ...p50)
  const minP10 = Math.min(0, ...p10)
  const level: RiskLevel = shortfall > 0.6 ? 'high' : shortfall > 0.3 ? 'medium' : shortfall > 0.08 ? 'low' : 'none'

  const warnDate = firstBreak?.date ?? possible?.date ?? null
  const reasons: RiskItem[] = warnDate
    ? adjusted
        .filter((f) => f.amount < 0 && f.date <= new Date(`${warnDate}T00:00:00`) && f.date >= addDays(new Date(`${warnDate}T00:00:00`), -7))
        .map((f) => ({ date: iso(f.date), amount: String(f.amount), title: f.title, source: f.recurring ? 'recurring' : 'event', overdue: false }))
    : []

  const planned: RiskItem[] = adjusted
    .filter((f) => !f.recurring && f.date <= addDays(today, h))
    .sort((a, b) => a.date.getTime() - b.date.getTime())
    .map((f) => ({ date: iso(f.date), amount: String(f.amount), title: f.title, source: 'event', overdue: false }))

  const rules: Rule[] = [
    { key: 'payroll', counterparty: 'Сотрудники', category: 'payroll', direction: 'out', kind: 'monthly', anchors: [5, 20], amount: String(-R(1_400_000)), occurrences: 12, confidence: 0.97 },
    { key: 'rent', counterparty: 'ООО «Бизнес-Центр»', category: 'rent', direction: 'out', kind: 'monthly', anchors: [10], amount: String(-R(350_000)), occurrences: 6, confidence: 0.98 },
    { key: 'tax', counterparty: 'ФНС России', category: 'taxes', direction: 'out', kind: 'monthly', anchors: [28], amount: String(-R(620_000)), occurrences: 6, confidence: 0.95 },
    { key: 'suppliers', counterparty: 'ООО «Снабжение»', category: 'suppliers', direction: 'out', kind: 'weekly', anchors: [0], amount: String(-R(280_000)), occurrences: 26, confidence: 0.88 },
    { key: 'vector', counterparty: 'ООО «Вектор»', category: 'revenue', direction: 'in', kind: 'weekly', anchors: [3], amount: String(R(1_100_000)), occurrences: 13, confidence: 0.8 },
    { key: 'progress', counterparty: 'АО «Прогресс»', category: 'revenue', direction: 'in', kind: 'monthly', anchors: [12, 26], amount: String(R(900_000)), occurrences: 12, confidence: 0.82 },
  ]

  return {
    as_of: iso(today),
    horizon: h,
    opening_balance: String(OPENING_KOP),
    model_version: 'demo-1',
    seed: 42,
    n_paths: 2000,
    quality: { level: 'good', history_days: HISTORY_DAYS, notes: [] },
    days,
    risk: {
      level,
      shortfall_probability: shortfall,
      raw_shortfall_probability: shortfall,
      first_break_date: firstBreak?.date ?? null,
      possible_break_date: possible?.date ?? null,
      median_path_deficit: String(Math.round(-minP50)),
      max_deficit_p90: String(Math.round(-minP10)),
      deficit_given_break_median: String(Math.round(-Math.min(minP50, minP10 / 2))),
      warnings: warnDate
        ? [{ code: 'cash_gap', severity: level === 'high' ? 'high' : 'medium', date: warnDate, message: 'Возможен кассовый разрыв', reasons }]
        : [],
    },
    rules,
    planned,
  }
}

function makeRun(h: number, sc: ScenarioParams | null): ForecastRun {
  return {
    run_id: `demo-run-${++runSeq}`,
    created_at: new Date().toISOString(),
    stale: false,
    scenario: sc,
    result: compute(h, sc ?? DEFAULT_PARAMS),
  }
}

// ---------- plan / fact ----------
function buildPlanFact(days: number): PlanFact {
  const today = startOfToday()
  const from = addDays(today, -days)
  const rnd = mulberry32(7)
  const big = transactions.filter((t) => new Date(`${t.date}T00:00:00`) >= from && Math.abs(Number(t.amount_kop)) >= R(250_000))
  const items: PlanFactItem[] = big.map((t, i) => {
    const fact = Number(t.amount_kop)
    const missed = i % 17 === 9
    const dev = !missed && i % 5 === 2
    const plan = missed || dev ? Math.round((fact * (dev ? 0.9 : 1.0)) / 100) * 100 : fact
    return {
      date: t.date,
      title: t.purpose,
      source: 'recurring',
      plan: String(missed ? Math.round(fact / 100) * 100 : dev ? Math.round(fact / 0.9 / 100) * 100 : plan),
      fact: missed ? null : String(fact),
      fact_date: missed ? null : t.date,
      diff: missed ? null : String(fact - (dev ? Math.round(fact / 0.9 / 100) * 100 : plan)),
      status: missed ? 'missed' : dev ? 'deviation' : 'paid',
    }
  })
  void rnd
  const weeks = new Map<string, { plan_in: number; plan_out: number; fact_in: number; fact_out: number }>()
  for (const it of items) {
    const d = new Date(`${it.date}T00:00:00`)
    const monday = addDays(d, -((d.getDay() + 6) % 7))
    const k = iso(monday)
    const w = weeks.get(k) ?? { plan_in: 0, plan_out: 0, fact_in: 0, fact_out: 0 }
    const p = Number(it.plan)
    const f = it.fact == null ? 0 : Number(it.fact)
    if (p > 0) w.plan_in += p
    else w.plan_out += -p
    if (f > 0) w.fact_in += f
    else w.fact_out += -f
    weeks.set(k, w)
  }
  return {
    as_of: iso(today),
    days,
    missed: items.filter((i) => i.status === 'missed').length,
    note: 'Демо-данные: план построен по повторяющимся платежам компании.',
    weeks: [...weeks.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([week_start, w]) => ({ week_start, plan_in: String(w.plan_in), plan_out: String(w.plan_out), fact_in: String(w.fact_in), fact_out: String(w.fact_out) })),
    items: items.sort((a, b) => b.date.localeCompare(a.date)),
  }
}

// ---------- backtest / key rate ----------
function buildBacktest(): Backtest {
  const rnd = mulberry32(11)
  const today = startOfToday()
  let actual = OPENING_KOP
  const series = Array.from({ length: 14 }, (_, i) => {
    actual += Math.round((rnd() - 0.45) * R(300_000))
    return { date: iso(addDays(today, i - 14)), predicted: String(actual + Math.round((rnd() - 0.5) * R(120_000))), actual: String(actual) }
  })
  return {
    origins: 24,
    alarm_precision: 0.78,
    alarm_recall: 0.71,
    actual_gap_windows: 5,
    metrics: [7, 14, 30].map((h, i) => ({
      horizon: h,
      mae_model: String(R(90_000 + i * 70_000)),
      mae_last_value: String(R(160_000 + i * 120_000)),
      mae_drift: String(R(140_000 + i * 100_000)),
      model_beats_baselines_share: 0.82 - i * 0.07,
      band_coverage: 0.8,
      n: 24,
    })),
    series_14: series,
    note: 'Демо-данные',
  }
}

const keyRate: KeyRate = {
  rate: 16,
  previous_rate: 17,
  as_of: iso(startOfToday()),
  changed_on: iso(addDays(startOfToday(), -40)),
  source: 'cbr.ru',
  stale: false,
  url: 'https://www.cbr.ru/hd_base/KeyRate/',
  history: [
    { date: iso(addDays(startOfToday(), -200)), rate: 19 },
    { date: iso(addDays(startOfToday(), -120)), rate: 18 },
    { date: iso(addDays(startOfToday(), -80)), rate: 17 },
    { date: iso(addDays(startOfToday(), -40)), rate: 16 },
  ],
}

// ---------- imports ----------
const imports: ImportRow[] = []
let importSeq = 0

// ---------- API ----------
export const demoApi = {
  me: async (): Promise<Me> => me,
  login: async (): Promise<Me> => me,
  register: async (b: { company_name: string }): Promise<Me> => ({ ...me, company_name: b.company_name || me.company_name }),
  ecpChallenge: async () => ({ challenge: 'demo' }),
  ecpLogin: async (): Promise<Me> => ({ ...me, ecp: true }),
  logout: async (): Promise<void> => undefined,

  renameCompany: async (name: string) => {
    me.company_name = name
    return { company_name: name }
  },

  latestForecast: async (): Promise<ForecastRun | null> => {
    await sleep()
    return makeRun(horizon, null)
  },
  createForecast: async (horizonDays: number): Promise<ForecastRun> => {
    await sleep(400)
    horizon = horizonDays
    return makeRun(horizon, null)
  },
  scenario: async (horizonDays: number, scenario: ScenarioParams): Promise<ScenarioResponse> => {
    await sleep(300)
    const base = makeRun(horizonDays, null)
    const sc = makeRun(horizonDays, scenario)
    return {
      base,
      scenario: sc,
      delta: {
        shortfall_probability: sc.result.risk.shortfall_probability - base.result.risk.shortfall_probability,
        max_deficit_p90: String(Number(sc.result.risk.max_deficit_p90) - Number(base.result.risk.max_deficit_p90)),
      },
    }
  },
  backtest: async (): Promise<Backtest> => {
    await sleep()
    return buildBacktest()
  },

  transactions: async (q: TransactionQuery): Promise<TransactionPage> => {
    await sleep(100)
    const needle = (q.q ?? '').toLowerCase()
    let rows = transactions.filter((t) => {
      const amt = Number(t.amount_kop)
      if (q.category && t.category !== q.category) return false
      if (q.direction === 'in' && amt <= 0) return false
      if (q.direction === 'out' && amt >= 0) return false
      if (needle && !`${t.counterparty} ${t.purpose}`.toLowerCase().includes(needle)) return false
      return true
    })
    const dir = q.order === 'asc' ? 1 : -1
    const key = q.sort ?? 'date'
    rows = [...rows].sort((a, b) => {
      const cmp =
        key === 'amount' || key === 'amount_kop'
          ? Number(a.amount_kop) - Number(b.amount_kop)
          : key === 'counterparty'
            ? a.counterparty.localeCompare(b.counterparty)
            : key === 'category'
              ? a.category.localeCompare(b.category)
              : a.date.localeCompare(b.date) || a.id - b.id
      return cmp * dir
    })
    return { items: rows.slice(q.offset, q.offset + q.limit), total: rows.length, limit: q.limit, offset: q.offset }
  },
  setCategory: async (id: number, category: string): Promise<unknown> => {
    const t = transactions.find((x) => x.id === id)
    if (t) {
      t.category = category
      t.manual = true
      t.category_confidence = 1
    }
    return {}
  },
  expectedEvents: async () => {
    await sleep(100)
    return { items: [...events].sort((a, b) => a.date.localeCompare(b.date)) }
  },
  createEvent: async (e: NewExpectedEvent): Promise<unknown> => {
    const abs = Math.abs(Number(e.amount_kop))
    events.push({
      id: `demo-ev-${++eventSeq}`,
      date: e.date,
      amount: String(e.direction === 'out' ? -abs : abs),
      title: e.title,
      source: e.source,
      confidence: e.confidence,
      counterparty: e.counterparty,
    })
    return {}
  },
  deleteEvent: async (id: string): Promise<void> => {
    const i = events.findIndex((e) => e.id === id)
    if (i >= 0) events.splice(i, 1)
  },
  taxCalendar: async (amountKop?: string): Promise<TaxCalendarResult> => {
    const amount = amountKop ?? String(R(620_000))
    const d = startOfToday()
    const date = iso(new Date(d.getFullYear(), d.getMonth() + (d.getDate() > 28 ? 1 : 0), 28))
    events.push({ id: `demo-ev-${++eventSeq}`, date, amount: String(-Math.abs(Number(amount))), title: 'Налоги по календарю', source: 'tax', confidence: 0.95, counterparty: 'ФНС России' })
    return { created: [date], source: 'tax', amount_kop: amount, amount_estimated: !amountKop }
  },
  syncIntegrations: async (): Promise<SyncResult> => {
    await sleep(600)
    events.push(
      mkEvent(5, 380_000, 'Счёт из учётной системы', 'invoice', 'ООО «Альфа»', 0.8),
      mkEvent(9, -150_000, 'Договор аренды склада', 'contract', 'ИП Волков', 0.9),
    )
    return { created: 2, skipped: 0 }
  },

  imports: async () => ({ items: [...imports].reverse() }),
  uploadStatement: async (file: File): Promise<ImportPreview> => {
    await sleep(700)
    const id = `demo-imp-${++importSeq}`
    const today = startOfToday()
    imports.push({ import_id: id, filename: file.name, status: 'preview', created_at: new Date().toISOString(), rows_ok: 0, errors_total: 0, date_from: iso(addDays(today, -HISTORY_DAYS)), date_to: iso(addDays(today, -1)) })
    const sample = transactions.slice(-5).map((t) => ({ date: t.date, amount_kop: t.amount_kop, counterparty: t.counterparty, purpose: t.purpose, category: t.category }))
    return {
      import_id: id,
      status: 'preview',
      summary: {
        stats: { rows_ok: transactions.length, date_from: iso(addDays(today, -HISTORY_DAYS)), date_to: iso(addDays(today, -1)), days_span: HISTORY_DAYS, has_balance: true, balance_mismatches: 0, suspected_duplicates: 0, opening_balance_kop: String(OPENING_KOP) },
        errors_total: 0,
        errors: [],
        duplicates_existing: transactions.length,
        categories: transactions.reduce<Record<string, number>>((m, t) => ((m[t.category] = (m[t.category] ?? 0) + 1), m), {}),
        sample,
      },
    }
  },
  confirmImport: async (id: string) => {
    await sleep(400)
    const r = imports.find((i) => i.import_id === id)
    if (r) r.status = 'confirmed'
    return { inserted: 0, skipped_duplicates: transactions.length, status: 'confirmed' }
  },

  planFact: async (days: number): Promise<PlanFact> => {
    await sleep(150)
    return buildPlanFact(days)
  },
  keyRate: async (): Promise<KeyRate> => keyRate,
}
