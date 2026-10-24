import { addDays, format, parseISO } from 'date-fns'
import type { ForecastPoint } from '../../types'

/** Cuts the supplied series to a view window. Display-only; no values are changed. */
export function sliceWindow(points: ForecastPoint[], today: string, historyDays: number, forwardDays: number): ForecastPoint[] {
  const from = format(addDays(parseISO(today), -historyDays), 'yyyy-MM-dd')
  const to = format(addDays(parseISO(today), forwardDays), 'yyyy-MM-dd')
  return points.filter((p) => p.date >= from && p.date <= to)
}
