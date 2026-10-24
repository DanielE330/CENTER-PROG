/**
 * Rule-based assistant: answers are built from the stored forecast only (no numbers are invented, no LLM).
 * An answer is a list of message parts (translation key + language-neutral params), so it
 * re-translates when the language changes.
 */
import { addDays, format, parseISO } from 'date-fns'
import type { AssistantMessage, ForecastResult, ScenarioResponse } from '../../types'
import type { LineRef } from '../../i18n/helpers'

export type Intent = 'gap' | 'biggest' | 'check' | 'reduce' | 'tax' | 'expenses' | 'delay' | 'revenue' | 'help'
type Parts = NonNullable<AssistantMessage['parts']>

/** Maps free text (Russian or English) to an intent; the first matching rule wins. */
export function classify(q: string): { intent: Intent; days?: number } {
  const s = q.toLowerCase()
  const m = s.match(/(\d+)\s*(дн|нед|day|week)/)
  const days = m ? Number(m[1]) * (/^(нед|week)/.test(m[2]) ? 7 : 1) : undefined
  if (/клиент|задерж|позже|отсроч|delay|late|client|customer/.test(s)) return { intent: 'delay', days }
  if (/выручк|падени|revenue|sales/.test(s)) return { intent: 'revenue' }
  if (/налог|енп|взнос|tax/.test(s)) return { intent: 'tax' }
  if (/снизить|уменьш|что делать|как избеж|reduce|lower|mitigat|avoid/.test(s)) return { intent: 'reduce' }
  if (/провер|в первую очередь|приоритет|check|review|priorit/.test(s)) return { intent: 'check' }
  if (/расход|expense|cost/.test(s) && !/крупн|biggest|largest/.test(s)) return { intent: 'expenses' }
  if (/платеж|платёж|крупн|ближайш|списан|payment|biggest|largest|upcoming/.test(s)) return { intent: 'biggest' }
  if (/разрыв|почему|риск|дефицит|хватит|gap|why|risk|deficit|shortfall|enough/.test(s)) return { intent: 'gap' }
  return { intent: 'help' }
}

const lines = (xs: ForecastResult['planned']): LineRef[] => xs.map((x) => ({ date: x.date, title: x.title, amount: x.amount, overdue: x.overdue }))
const byAmount = (a: { amount: string }, b: { amount: string }) => (BigInt(a.amount) < BigInt(b.amount) ? -1 : 1)

export function localAnswer(intent: Exclude<Intent, 'delay' | 'revenue'>, r: ForecastResult): Parts {
  const risk = r.risk
  const when = risk.possible_break_date ?? risk.first_break_date
  const R = 'assistant.replies.'

  switch (intent) {
    case 'gap': {
      if (risk.level === 'none') {
        return [{ key: R + 'gapNone', params: { probPercent: risk.shortfall_probability, endKop: r.days[r.days.length - 1]?.p50 ?? r.opening_balance } }]
      }
      const reasons = risk.warnings.find((w) => w.code === 'cash_gap')?.reasons ?? []
      return [
        { key: R + 'gapHead', params: { horizon: r.horizon, probPercent: risk.shortfall_probability, levelKey: `risk.badge.${risk.level}` } },
        ...(when ? [{ key: R + 'gapWhen', params: { whenDate: when, typicalKop: risk.deficit_given_break_median, p90Kop: risk.max_deficit_p90 } }] : []),
        ...(reasons.length ? [{ key: R + 'gapReasons', params: { itemsLines: lines(reasons) } }] : []),
        { key: R + 'gapOpening', params: { asOfDate: r.as_of, balanceKop: r.opening_balance } },
      ]
    }
    case 'biggest': {
      const end = format(addDays(parseISO(r.as_of), 14), 'yyyy-MM-dd')
      const out = r.planned.filter((p) => BigInt(p.amount) < 0n && p.date <= end).sort(byAmount).slice(0, 5)
      if (!out.length) return [{ key: R + 'biggestNone', params: { asOfDate: r.as_of } }]
      const total = out.reduce((s, p) => s + BigInt(p.amount), 0n).toString()
      return [{ key: R + 'biggest', params: { endDate: end, itemsLines: lines(out), totalKop: total } }]
    }
    case 'check': {
      const parts: Parts = []
      risk.warnings.filter((w) => w.code.startsWith('overdue')).forEach((w) => parts.push({ key: R + 'checkWarning', params: { message: w.message } }))
      const out = r.planned.find((p) => BigInt(p.amount) < 0n)
      if (out) parts.push({ key: R + 'checkNextOut', params: { dateDate: out.date, title: out.title, amountKop: out.amount } })
      const inc = r.planned.find((p) => BigInt(p.amount) > 0n)
      if (inc) parts.push({ key: R + 'checkNextIn', params: { dateDate: inc.date, title: inc.title, amountKop: inc.amount } })
      r.quality.notes.forEach((n) => parts.push({ key: R + 'checkQuality', params: { note: n } }))
      if (risk.level !== 'none') parts.push({ key: R + 'checkRisk', params: { probPercent: risk.shortfall_probability } })
      return parts.length ? [{ key: R + 'checkHead' }, ...parts] : [{ key: R + 'checkNone' }]
    }
    case 'reduce': {
      if (risk.level === 'none') return [{ key: R + 'reduceNone', params: { probPercent: risk.shortfall_probability } }]
      const upto = when ?? format(addDays(parseISO(r.as_of), r.horizon), 'yyyy-MM-dd')
      const out = r.planned.filter((p) => BigInt(p.amount) < 0n && p.date <= upto).sort(byAmount).slice(0, 3)
      const inc = r.planned.filter((p) => BigInt(p.amount) > 0n && p.date <= upto).slice(0, 3)
      return [
        { key: when ? R + 'reduceHeadDate' : R + 'reduceHead', params: { probPercent: risk.shortfall_probability, whenDate: when ?? undefined } },
        ...(out.length ? [{ key: R + 'reduceMove', params: { itemsLines: lines(out) } }] : []),
        ...(inc.length ? [{ key: R + 'reduceSpeed', params: { itemsLines: lines(inc) } }] : []),
        { key: R + 'reduceCover', params: { p90Kop: risk.max_deficit_p90 } },
      ]
    }
    case 'tax': {
      const tx = r.planned.filter((p) => /фнс|налог|енп|взнос|tax/i.test(p.title) || p.source === 'tax')
      if (tx.length) return [{ key: R + 'taxItems', params: { itemsLines: lines(tx) } }]
      const rule = r.rules.find((x) => x.category === 'taxes')
      return rule ? [{ key: R + 'taxRule', params: { name: rule.counterparty || rule.key, amountKop: rule.amount } }] : [{ key: R + 'taxNone' }]
    }
    case 'expenses': {
      const rules = r.rules.filter((x) => BigInt(x.amount) < 0n).sort(byAmount).slice(0, 5)
      if (!rules.length) return [{ key: R + 'expensesNone' }]
      return [
        { key: R + 'expensesHead' },
        ...rules.map((x) => ({ key: R + 'expenseRow', params: { name: x.counterparty || x.key, amountKop: x.amount, periodKey: x.kind === 'monthly' ? 'assistant.monthly' : 'assistant.weekly' } })),
      ]
    }
    default:
      return [{ key: R + 'help' }]
  }
}

export function scenarioAnswer(kind: 'delay' | 'revenue', days: number, o: ScenarioResponse): Parts {
  const R = 'assistant.replies.'
  const b = o.base.result.risk
  const s = o.scenario.result.risk
  const when = s.possible_break_date ?? s.first_break_date
  return [
    { key: kind === 'delay' ? R + 'scenarioDelay' : R + 'scenarioRevenue', params: { days } },
    { key: R + 'scenarioProb', params: { fromPercent: b.shortfall_probability, toPercent: s.shortfall_probability } },
    when && s.level !== 'none' ? { key: R + 'scenarioWhen', params: { whenDate: when } } : { key: R + 'scenarioNoGap' },
    { key: R + 'scenarioDeficit', params: { fromKop: b.max_deficit_p90, toKop: s.max_deficit_p90 } },
  ]
}
