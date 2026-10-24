import type { ScenarioParams } from '../../types'

/** Form state: percent / days as numbers, money as the text typed by the user (rubles). */
export interface FormParams {
  delay_days: number
  delay_counterparty: string
  revenue_drop_pct: number
  fixed_cost_growth_pct: number
  extra_expense: string
  extra_expense_date: string
  extra_income: string
  extra_income_date: string
}

export const ZERO: FormParams = {
  delay_days: 0, delay_counterparty: '', revenue_drop_pct: 0, fixed_cost_growth_pct: 0,
  extra_expense: '', extra_expense_date: '', extra_income: '', extra_income_date: '',
}

export interface SavedScenario {
  id: string
  name: string
  params: FormParams
}

const KEY = 'ci.scenarios'

export function loadSaved(): SavedScenario[] {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? '[]')
    return Array.isArray(v) ? v : []
  } catch {
    return []
  }
}

export function storeSaved(v: SavedScenario[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(v))
  } catch {
    /* without storage the page still works, scenarios are just not remembered */
  }
}

export type ParamError = 'badAmount' | 'expenseDate' | 'incomeDate' | 'empty'

/** Converts the form to the API payload. Returns an error code instead of throwing. */
export function toPayload(p: FormParams, parseKop: (s: string) => string | null): { payload: ScenarioParams } | { error: ParamError } {
  const e = p.extra_expense.trim() ? parseKop(p.extra_expense) : null
  const i = p.extra_income.trim() ? parseKop(p.extra_income) : null
  if ((p.extra_expense.trim() && !e) || (p.extra_income.trim() && !i)) return { error: 'badAmount' }
  if (e && !p.extra_expense_date) return { error: 'expenseDate' }
  if (i && !p.extra_income_date) return { error: 'incomeDate' }
  if (!p.delay_days && !p.revenue_drop_pct && !p.fixed_cost_growth_pct && !e && !i) return { error: 'empty' }
  return {
    payload: {
      delay_days: p.delay_days,
      delay_counterparty: p.delay_counterparty || null,
      revenue_drop_pct: p.revenue_drop_pct,
      fixed_cost_growth_pct: p.fixed_cost_growth_pct,
      extra_expense_kop: e ? Number(e) : null,
      extra_expense_date: e ? p.extra_expense_date : null,
      extra_income_kop: i ? Number(i) : null,
      extra_income_date: i ? p.extra_income_date : null,
    },
  }
}
