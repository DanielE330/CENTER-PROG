import { addDays, format, parseISO } from 'date-fns'
import type { ForecastRun, Obligation, ObligationStatus, PlanFact } from '../../types'

const kindOf = (amount: string): Obligation['kind'] => (amount.startsWith('-') ? 'payment' : 'receipt')

/** Upcoming items = the forecast's planned flows. Overdue ones are flagged by the model. */
export function buildUpcoming(run: ForecastRun | null | undefined): Obligation[] {
  if (!run) return []
  const soon = format(addDays(parseISO(run.result.as_of), 3), 'yyyy-MM-dd')
  return run.result.planned
    .map((p, i): Obligation => {
      const status: ObligationStatus = p.overdue ? 'overdue' : p.date <= soon ? 'expected' : 'planned'
      return { id: `u${i}`, kind: kindOf(p.amount), title: p.title, date: p.date, amount: p.amount, source: p.source, status }
    })
    .sort((a, b) => a.date.localeCompare(b.date))
}

/** History = plan/fact items: paid and deviations are completed, missed ones are overdue. */
export function buildHistory(pf: PlanFact | null | undefined): Obligation[] {
  if (!pf) return []
  return pf.items
    .map((p, i): Obligation => {
      const status: ObligationStatus = p.status === 'paid' || p.status === 'deviation' ? 'completed' : p.status === 'missed' ? 'overdue' : 'expected'
      return {
        id: `h${i}`, kind: kindOf(p.plan), title: p.title, date: p.date, amount: p.plan, source: p.source, status,
        fact: p.fact, factDate: p.fact_date, diff: p.status === 'deviation' ? p.diff : null,
      }
    })
    .sort((a, b) => b.date.localeCompare(a.date))
}
