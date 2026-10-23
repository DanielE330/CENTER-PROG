/**
 * MOCK DATA — stands in for the Django API. Nothing here is a real financial figure.
 * The "model" logic below (cumulative balance, risk level) imitates what the backend
 * would return; it exists only so the demo screens are internally consistent.
 */
import { addDays, format, parseISO, startOfDay } from 'date-fns'
import type {
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
import { formatCompact, formatShortDate } from '../lib/formatters'

const OPENING_BALANCE = 38_000_000
const HORIZON = 30
const HISTORY = 30

const iso = (d: Date) => format(d, 'yyyy-MM-dd')
const today = () => startOfDay(new Date())
const at = (offset: number) => iso(addDays(today(), offset))

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

type Seed = Omit<Obligation, 'date' | 'id'> & { offset: number }

const obligationSeeds: Seed[] = [
  // outgoing, upcoming
  { offset: 3, kind: 'payment', name: 'Аренда офиса и склада', amount: 2_400_000, status: 'planned', note: 'Договор аренды №14' },
  { offset: 4, kind: 'payment', name: 'Выплата заработной платы', amount: 18_500_000, status: 'planned', note: 'Расчётный календарь' },
  { offset: 7, kind: 'payment', name: 'НДС за квартал', amount: 6_200_000, status: 'planned', note: 'Налоговый календарь' },
  { offset: 8, kind: 'payment', name: 'Коммунальные услуги', amount: 900_000, status: 'planned' },
  { offset: 9, kind: 'payment', name: 'Поставщик «Агро-Юг»', amount: 9_800_000, status: 'planned', note: 'Счёт №АЮ-2217' },
  { offset: 12, kind: 'payment', name: 'Погашение кредита', amount: 14_000_000, status: 'planned', note: 'График погашения' },
  { offset: 14, kind: 'payment', name: 'Поставщик «ТехСнаб»', amount: 8_900_000, status: 'planned', note: 'Счёт №ТС-0931' },
  { offset: 16, kind: 'payment', name: 'Налог на прибыль', amount: 7_400_000, status: 'planned', note: 'Налоговый календарь' },
  { offset: 20, kind: 'payment', name: 'Аванс сотрудникам', amount: 15_000_000, status: 'planned', note: 'Расчётный календарь' },
  { offset: 22, kind: 'payment', name: 'Лизинговый платёж', amount: 3_100_000, status: 'planned' },
  { offset: 25, kind: 'payment', name: 'Страховые взносы', amount: 5_600_000, status: 'planned', note: 'Налоговый календарь' },
  { offset: 27, kind: 'payment', name: 'Оплата подрядчику', amount: 4_000_000, status: 'planned', note: 'Акт №58' },
  // incoming, upcoming
  { offset: 2, kind: 'receipt', name: 'ООО «Донторг»', amount: 9_200_000, status: 'expected', note: 'Оплата по счёту №1204' },
  { offset: 6, kind: 'receipt', name: 'АО «ЮгАгро»', amount: 12_500_000, status: 'expected', note: 'Оплата по договору поставки' },
  { offset: 10, kind: 'receipt', name: 'ИП Васильев', amount: 3_800_000, status: 'expected' },
  { offset: 17, kind: 'receipt', name: 'ООО «Ростов-Строй»', amount: 14_700_000, status: 'expected', note: 'Срок оплаты уже переносился' },
  { offset: 18, kind: 'receipt', name: 'ООО «Кубань-Фуд»', amount: 8_300_000, status: 'expected' },
  { offset: 24, kind: 'receipt', name: 'ООО «Донторг»', amount: 11_000_000, status: 'expected', note: 'Оплата по счёту №1231' },
  { offset: 29, kind: 'receipt', name: 'ООО «Ростов-Строй»', amount: 6_000_000, status: 'expected' },
  // past
  { offset: -3, kind: 'receipt', name: 'АО «Агро-Партнёр»', amount: 5_000_000, status: 'overdue', note: 'Оплата не поступила, в прогноз не включена' },
  { offset: -1, kind: 'payment', name: 'ООО «ЮгЭнерго»', amount: 1_250_000, status: 'overdue', note: 'Не оплачено, в прогноз не включено' },
  { offset: -5, kind: 'payment', name: 'Поставщик «Донпак»', amount: 4_300_000, status: 'completed' },
  { offset: -8, kind: 'receipt', name: 'ООО «Кубань-Фуд»', amount: 7_100_000, status: 'completed' },
]

export function buildObligations(): Obligation[] {
  return obligationSeeds
    .map(({ offset, ...s }, i) => ({ ...s, id: `ob-${i + 1}`, date: at(offset) }))
    .sort((a, b) => a.date.localeCompare(b.date))
}

/** Items that feed the forecast: not completed, not overdue, dated today or later. */
export function isForecastItem(o: Obligation): boolean {
  return (o.status === 'planned' || o.status === 'expected') && o.date > at(0)
}

/* --------------------------- Transactions ---------------------------- */

const parties: Array<{ name: string; category: string; dir: 'in' | 'out' }> = [
  { name: 'ООО «Донторг»', category: 'Выручка', dir: 'in' },
  { name: 'АО «ЮгАгро»', category: 'Выручка', dir: 'in' },
  { name: 'ООО «Кубань-Фуд»', category: 'Выручка', dir: 'in' },
  { name: 'ИП Васильев', category: 'Выручка', dir: 'in' },
  { name: 'ООО «Ростов-Строй»', category: 'Выручка', dir: 'in' },
  { name: 'Поставщик «Донпак»', category: 'Закупки', dir: 'out' },
  { name: 'Поставщик «Агро-Юг»', category: 'Закупки', dir: 'out' },
  { name: 'Поставщик «ТехСнаб»', category: 'Закупки', dir: 'out' },
  { name: 'ООО «ЮгЭнерго»', category: 'Коммунальные', dir: 'out' },
  { name: 'ФНС России', category: 'Налоги', dir: 'out' },
  { name: 'Лизинг-Юг', category: 'Лизинг', dir: 'out' },
  { name: 'ООО «ЛогистикПро»', category: 'Логистика', dir: 'out' },
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
        counterparty: p.name,
        description: p.dir === 'in' ? `Оплата по счёту №${1000 + n}` : `Оплата по договору №${200 + n}`,
        amount,
        direction: p.dir,
        category: p.category,
        status: 'completed',
      })
    }
  }
  // Pending operations for today: not yet part of the confirmed balance.
  list.push(
    { id: 'tx-p1', date: at(0), counterparty: 'ООО «Донторг»', description: 'Входящий платёж в обработке', amount: 3_200_000, direction: 'in', category: 'Выручка', status: 'pending' },
    { id: 'tx-p2', date: at(0), counterparty: 'ООО «ЛогистикПро»', description: 'Исходящий платёж в обработке', amount: 850_000, direction: 'out', category: 'Логистика', status: 'pending' },
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
      id: 'f-payments',
      label: 'Концентрация платежей',
      description: `До ${formatShortDate(first.date)} запланированы крупные платежи: ${bigOut.map((o) => o.name).join(', ')}.`,
      amount: bigOut.reduce((s, o) => s + o.amount, 0),
    },
  ]
  if (late) {
    factors.push({
      id: 'f-receipt',
      label: 'Поступление после даты дефицита',
      description: `${late.name} ожидается ${formatShortDate(late.date)}, уже после первой даты нехватки. Задержка усугубит дефицит.`,
      amount: late.amount,
    })
  }
  const overdue = obligations.filter((o) => o.status === 'overdue' && o.kind === 'receipt')
  if (overdue.length) {
    factors.push({
      id: 'f-overdue',
      label: 'Просроченные поступления',
      description: `${overdue.map((o) => o.name).join(', ')}: оплата просрочена и не включена в прогноз.`,
      amount: overdue.reduce((s, o) => s + o.amount, 0),
    })
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
    quality: {
      completeness: 92,
      warnings: ['Не загружена выписка по резервному счёту за последние 2 дня (демо).'],
    },
  }

  if (demo === 'empty') {
    return {
      ...base,
      points: [],
      summary: { currentBalance: null, minBalance: null, minBalanceDate: null, expectedInflows: 0, expectedOutflows: 0 },
      risk: { level: 'unknown', shortageDate: null, deficitAmount: null, factors: [] },
      quality: { completeness: 0, warnings: ['Данные ещё не загружены. Импортируйте выписку.'] },
    }
  }
  if (demo === 'incomplete') {
    return {
      ...base,
      points: points.map((p) => ({ ...p, forecast: null, lower: null, upper: null })),
      summary: { currentBalance: null, minBalance: null, minBalanceDate: null, expectedInflows, expectedOutflows },
      risk: { level: 'unknown', shortageDate: null, deficitAmount: null, factors: [] },
      quality: {
        completeness: 58,
        warnings: [
          'Нет данных об остатке на основном расчётном счёте — прогноз не построен.',
          'Выписка загружена не полностью: отсутствуют последние 5 дней.',
          'Часть ожидаемых поступлений не подтверждена контрагентами.',
        ],
      },
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

/** Canned, keyword-routed replies that only quote figures present in the supplied data. */
export function buildAssistantReply(
  question: string,
  f: ForecastResponse,
  obligations: Obligation[],
): { text: string; basedOn: string[] } {
  const q = question.toLowerCase()
  const { risk, summary } = f
  if (f.points.length === 0 || summary.currentBalance == null) {
    return {
      text: 'Для ответа недостаточно данных: прогноз не построен или не хватает остатков. Импортируйте выписку в разделе «Настройки» — после этого я смогу объяснить прогноз.',
      basedOn: ['Качество данных'],
    }
  }
  if (/сценар|что если|перенест|сдвин/.test(q)) {
    const big = obligations.filter((o) => o.kind === 'payment' && isForecastItem(o)).sort((a, b) => b.amount - a.amount).slice(0, 2)
    return {
      text: `Чтобы смоделировать сценарий, откройте раздел «Сценарии». Для примера можно сдвинуть: ${big.map((o) => `«${o.name}» (${formatCompact(o.amount)}, ${formatShortDate(o.date)})`).join(' или ')}. Результат пересчитывается в браузере по простым прозрачным правилам и является демонстрацией.`,
      basedOn: ['Платежи и обязательства'],
    }
  }
  if (/провер|предлож|что делать|как улучш|совет/.test(q)) {
    const ov = obligations.filter((o) => o.status === 'overdue')
    const late = risk.factors.find((x) => x.id === 'f-receipt')
    return {
      text: `Стоит проверить: ${ov.map((o) => `${o.name} — ${formatCompact(o.amount)} (просрочено)`).join('; ') || 'просроченных позиций нет'}. ${late?.description ?? ''} Это подсказки по данным приложения, а не рекомендации по финансовым решениям.`,
      basedOn: ['Платежи и обязательства', 'Факторы риска'],
    }
  }
  if (/остаток|баланс|сейчас/.test(q)) {
    return {
      text: `Текущий остаток по данным приложения — ${formatCompact(summary.currentBalance)}. Минимальный прогнозный остаток: ${formatCompact(summary.minBalance)} на ${summary.minBalanceDate ? formatShortDate(summary.minBalanceDate) : '—'}.`,
      basedOn: ['Прогноз остатка'],
    }
  }
  if (!risk.shortageDate) {
    return { text: `В горизонте прогноза нехватки средств не обнаружено. Минимальный остаток — ${formatCompact(summary.minBalance)}.`, basedOn: ['Прогноз остатка'] }
  }
  return {
    text: `Прогноз показывает возможную нехватку ${formatShortDate(risk.shortageDate)} — ориентировочный дефицит ${formatCompact(risk.deficitAmount)}${risk.probability != null ? `; оценка вероятности модели ${Math.round(risk.probability * 100)}% (оценка, не гарантия)` : ''}. Основные факторы: ${risk.factors.map((x) => x.label.toLowerCase()).join('; ')}. Подробности — на странице «Прогноз».`,
    basedOn: ['Прогноз остатка', 'Факторы риска'],
  }
}
