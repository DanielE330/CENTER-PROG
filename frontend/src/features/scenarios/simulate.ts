/**
 * CLIENT-SIDE DEMO CALCULATION.
 * Rule (shown to the user on the page): balance(day) = opening balance
 * + Σ receipts dated ≤ day − Σ payments dated ≤ day.
 * Overdue and completed items are not included. Production forecasting belongs to the backend.
 */
import { addDays, format, parseISO } from 'date-fns'
import type { Obligation } from '../../types'

export interface Override {
  date?: string
  amount?: number
}

export interface ExtraItem {
  id: string
  kind: 'receipt' | 'payment'
  name: string
  date: string
  amount: number
}

export interface Scenario {
  id: string
  name: string
  description?: string
  /** Built-in template (inputs are predefined, results are still calculated live). */
  template?: boolean
  readOnly?: boolean
  overrides: Record<string, Override>
  extras: ExtraItem[]
}

export interface SimPoint {
  date: string
  balance: number
  inflow: number
  outflow: number
}

export interface SimResult {
  points: SimPoint[]
  minBalance: number
  minDate: string
  shortageDate: string | null
  deficit: number | null
}

interface Input {
  opening: number
  today: string
  horizonDays: number
  items: Obligation[]
  scenario: Pick<Scenario, 'overrides' | 'extras'>
}

export function simulate({ opening, today, horizonDays, items, scenario }: Input): SimResult {
  const flows: Array<{ date: string; signed: number }> = []
  for (const o of items) {
    const ov = scenario.overrides[o.id]
    const date = ov?.date ?? o.date
    const amount = ov?.amount ?? o.amount
    flows.push({ date, signed: o.kind === 'receipt' ? amount : -amount })
  }
  for (const e of scenario.extras) flows.push({ date: e.date, signed: e.kind === 'receipt' ? e.amount : -e.amount })

  const points: SimPoint[] = []
  let balance = opening
  // Anything dated on/before today is already reflected in the opening balance.
  for (let d = 1; d <= horizonDays; d++) {
    const date = format(addDays(parseISO(today), d), 'yyyy-MM-dd')
    const day = flows.filter((f) => f.date === date)
    const inflow = day.filter((f) => f.signed > 0).reduce((s, f) => s + f.signed, 0)
    const outflow = day.filter((f) => f.signed < 0).reduce((s, f) => s - f.signed, 0)
    balance += inflow - outflow
    points.push({ date, balance, inflow, outflow })
  }

  const min = points.reduce((m, p) => (p.balance < m.balance ? p : m), points[0])
  const first = points.find((p) => p.balance < 0)
  return {
    points,
    minBalance: min.balance,
    minDate: min.date,
    shortageDate: first?.date ?? null,
    deficit: min.balance < 0 ? -min.balance : null,
  }
}
