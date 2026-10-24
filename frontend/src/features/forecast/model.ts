import type { ForecastPoint, ForecastResult, ForecastRun, Kop, RiskItem } from '../../types'
import { kopToRub } from '../../lib/money'

export interface ForecastView {
  run: ForecastRun
  r: ForecastResult
  /** Opening point (as_of) followed by one point per forecast day. */
  points: ForecastPoint[]
  /** Day with the lowest expected (p50) balance. */
  minDay: { date: string; p50: Kop }
  /** Sum of planned receipts / payments (signed kopecks, payments negative) over the horizon. */
  plannedIn: Kop
  plannedOut: Kop
  /** Most likely date of the first cash gap, if any. */
  gapDate: string | null
}

export function buildView(run: ForecastRun): ForecastView {
  const r = run.result
  const flows = new Map<string, { in: bigint; out: bigint }>()
  let plannedIn = 0n
  let plannedOut = 0n
  for (const p of r.planned) {
    const a = BigInt(p.amount)
    const f = flows.get(p.date) ?? { in: 0n, out: 0n }
    if (a > 0n) {
      f.in += a
      plannedIn += a
    } else {
      f.out -= a
      plannedOut += a
    }
    flows.set(p.date, f)
  }
  const open = kopToRub(r.opening_balance)
  const points: ForecastPoint[] = [{ date: r.as_of, actual: open, forecast: open, lower: open, upper: open, inflow: 0, outflow: 0 }]
  let minDay = { date: r.as_of, p50: r.opening_balance }
  for (const d of r.days) {
    const f = flows.get(d.date)
    points.push({
      date: d.date,
      actual: null,
      forecast: kopToRub(d.p50),
      lower: kopToRub(d.p10),
      upper: kopToRub(d.p90),
      inflow: f ? kopToRub(f.in.toString()) : 0,
      outflow: f ? kopToRub(f.out.toString()) : 0,
      probNegative: d.prob_negative,
    })
    if (BigInt(d.p50) < BigInt(minDay.p50) || minDay.date === r.as_of) minDay = { date: d.date, p50: d.p50 }
  }
  return {
    run,
    r,
    points,
    minDay,
    plannedIn: plannedIn.toString(),
    plannedOut: plannedOut.toString(),
    gapDate: r.risk.possible_break_date ?? r.risk.first_break_date,
  }
}

/** Planned items grouped by ISO week start (Monday), receipts and payments in kopecks. */
export function weeklyFlows(items: RiskItem[]): Array<{ week: string; in: Kop; out: Kop }> {
  const m = new Map<string, { in: bigint; out: bigint }>()
  for (const p of items) {
    const d = new Date(`${p.date}T00:00:00Z`)
    d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7))
    const k = d.toISOString().slice(0, 10)
    const v = m.get(k) ?? { in: 0n, out: 0n }
    const a = BigInt(p.amount)
    if (a > 0n) v.in += a
    else v.out += -a
    m.set(k, v)
  }
  return [...m.keys()].sort().map((week) => ({ week, in: m.get(week)!.in.toString(), out: m.get(week)!.out.toString() }))
}
