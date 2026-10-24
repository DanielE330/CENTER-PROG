import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { CalendarClock, FastForward, Loader2, Timer } from 'lucide-react'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { useAdvanceWeek, useDemoStatus } from '../../hooks/queries'
import { formatDate, formatKop } from '../../lib/formatters'
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
  const s = status.data
  if (!s?.enabled) return null

  const last = advance.data
  const vs = last?.forecast_vs_fact
  const finished = s.finished === true
  const unavailable = s.available === false

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
        <Button variant="primary" title={guard.lock.title} disabled={guard.lock.disabled || advance.isPending || finished || unavailable || !s.as_of} onClick={() => advance.mutate()}>
          {advance.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <FastForward size={16} aria-hidden />}
          {advance.isPending ? t('demo.advancing') : finished ? t('demo.finished') : t('demo.advance')}
        </Button>
      </div>

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
