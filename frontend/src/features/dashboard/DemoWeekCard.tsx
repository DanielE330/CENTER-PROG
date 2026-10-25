import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { CalendarClock, CalendarDays, FastForward, Loader2, Timer } from 'lucide-react'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { useAdvanceWeek, useDemoStatus, useForecastRun } from '../../hooks/queries'
import { formatDate, formatKop, formatPercent } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import { useEditGuard } from '../../app/perms'

/**
 * Demo «A week has passed»: shown only to the demo user (`demo/status.enabled`). The button feeds the next week of
 * operations through the regular import; the server recomputes the forecast and every query is invalidated.
 */
export function DemoWeekCard() {
  const { t } = useTranslation()
  const status = useDemoStatus()
  const advance = useAdvanceWeek()
  const guard = useEditGuard()
  const run = useForecastRun()
  const [showNext, setShowNext] = useState(false)
  const s = status.data
  if (!s?.enabled) return null

  const last = advance.data
  const vs = last?.forecast_vs_fact
  const finished = s.finished === true
  const unavailable = s.available === false
  // 7 дней прогноза после даты «сегодня» демо
  const nextDays = (run.data?.result.days ?? []).filter((d) => !s.as_of || d.date > s.as_of).slice(0, 7)
  const minBalance = nextDays.length ? nextDays.reduce((m, d) => (BigInt(d.p50) < m ? BigInt(d.p50) : m), BigInt(nextDays[0].p50)) : null
  const maxRisk = nextDays.reduce((m, d) => Math.max(m, d.prob_negative), 0)

  return (
    <section className="card enter p-4 sm:p-5 flex flex-col gap-3" aria-label={t('demo.title')}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h2 className="text-base font-semibold flex items-center gap-2">
            <CalendarClock size={18} className="text-primary" aria-hidden /> {t('demo.title')}
          </h2>
          <p className="text-[13px] text-muted mt-0.5">
            {s.as_of ? t('demo.today', { date: formatDate(s.as_of) }) : t('demo.noData')}
            {!finished && !unavailable && s.weeks_left != null ? ` · ${t('demo.weeksLeft', { count: s.weeks_left })}` : ''}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" title={guard.lock.title} disabled={guard.lock.disabled || advance.isPending || finished || unavailable || !s.as_of} onClick={() => advance.mutate()}>
            {advance.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <FastForward size={16} aria-hidden />}
            {advance.isPending ? t('demo.advancing') : finished ? t('demo.finished') : t('demo.advance')}
          </Button>
          <Button variant="secondary" aria-expanded={showNext} aria-controls="demo-next-week" onClick={() => setShowNext((v) => !v)}>
            <CalendarDays size={16} aria-hidden />
            {showNext ? t('demo.nextWeekHide') : t('demo.nextWeek')}
          </Button>
        </div>
      </div>

      {showNext && (
        <div id="demo-next-week" className="rounded-control border border-line p-3">
          <h3 className="text-sm font-semibold">{t('demo.nextWeekTitle')}</h3>
          {nextDays.length === 0 ? (
            <p className="text-[13px] text-muted mt-1">{t('demo.nextWeekEmpty')}</p>
          ) : (
            <>
              <p className="text-[13px] text-muted mt-1">{t('demo.nextWeekSummary', { min: formatKop(String(minBalance)), risk: formatPercent(maxRisk) })}</p>
              <div className="overflow-x-auto mt-2">
                <table className="w-full text-[13px]">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="py-1 pr-3 font-medium">{t('demo.nextWeekDate')}</th>
                      <th className="py-1 pr-3 font-medium">{t('demo.nextWeekBalance')}</th>
                      <th className="py-1 pr-3 font-medium">{t('demo.nextWeekRange')}</th>
                      <th className="py-1 font-medium">{t('demo.nextWeekRisk')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {nextDays.map((d) => (
                      <tr key={d.date} className="border-t border-line tnum">
                        <td className="py-1 pr-3">{formatDate(d.date)}</td>
                        <td className="py-1 pr-3">{formatKop(d.p50)}</td>
                        <td className="py-1 pr-3">{formatKop(d.p10)} – {formatKop(d.p90)}</td>
                        <td className="py-1">{formatPercent(d.prob_negative)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {unavailable && <Notice tone="warning">{t('demo.unavailable')}</Notice>}
      {advance.isError && <Notice tone="danger">{errorText(t, advance.error)}</Notice>}
      {last && (
        <Notice tone="success" title={t('demo.done', { count: last.added, from: formatDate(last.from), to: formatDate(last.to) })}>
          <p className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <span className="inline-flex items-center gap-1.5 tnum">
              <Timer size={14} aria-hidden /> {t('demo.recalc', { ms: last.recalc_ms })}
            </span>
            {vs && (
              <span>
                {t(vs.within_band ? 'demo.vsFactInBand' : 'demo.vsFactOutBand', {
                  date: formatDate(vs.date),
                  predicted: formatKop(vs.p50),
                  actual: formatKop(vs.actual),
                })}
              </span>
            )}
            <Link to="/plan-vs-actual" className="text-primary font-medium hover:underline">{t('demo.openPlanFact')}</Link>
          </p>
        </Notice>
      )}
    </section>
  )
}
