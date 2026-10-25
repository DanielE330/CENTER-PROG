import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { CalendarClock, FastForward, Loader2, Rewind, Timer } from 'lucide-react'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { useAdvanceWeek, useDemoStatus, useRewindWeek } from '../../hooks/queries'
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
  const rewind = useRewindWeek()
  const s = status.data
  if (!s?.enabled) return null

  const last = advance.data
  const vs = last?.forecast_vs_fact
  const finished = s.finished === true
  const unavailable = s.available === false
  // назад можно, пока «сегодня» демо позже даты отсечки (начальная выписка не откатывается)
  const atStart = !s.as_of || (s.cutoff != null && s.as_of <= s.cutoff)

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
          <Button variant="secondary" title={guard.lock.title} disabled={guard.lock.disabled || rewind.isPending || advance.isPending || atStart || unavailable} onClick={() => rewind.mutate()}>
            {rewind.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <Rewind size={16} aria-hidden />}
            {rewind.isPending ? t('demo.rewinding') : atStart ? t('demo.atStart') : t('demo.back')}
          </Button>
        </div>
      </div>


      {unavailable && <Notice tone="warning">{t('demo.unavailable')}</Notice>}
      {rewind.isError && <Notice tone="danger">{errorText(t, rewind.error)}</Notice>}
      {rewind.data && !advance.isPending && (
        <Notice tone="success" title={t('demo.undone', { count: rewind.data.removed, from: rewind.data.from ? formatDate(rewind.data.from) : '', to: rewind.data.to ? formatDate(rewind.data.to) : '' })}>
          <p className="inline-flex items-center gap-1.5 tnum">
            <Timer size={14} aria-hidden /> {t('demo.recalc', { ms: rewind.data.recalc_ms })}
          </p>
        </Notice>
      )}
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
