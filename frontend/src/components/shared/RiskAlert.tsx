import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, OctagonAlert, ShieldCheck } from 'lucide-react'
import type { Risk, RiskLevel } from '../../types'
import { formatKop, formatDate, formatPercent, formatShortDate } from '../../lib/formatters'
import { Button } from '../ui/Button'
import { Badge, RiskBadge } from './StatusBadge'
import { cn } from '../../lib/utils'

const styles = {
  high: { box: 'border-danger/40 bg-danger-soft', icon: 'text-danger', Icon: OctagonAlert },
  medium: { box: 'border-warning/40 bg-warning-soft', icon: 'text-warning', Icon: AlertTriangle },
  low: { box: 'border-primary/30 bg-primary-soft', icon: 'text-primary', Icon: ShieldCheck },
  none: { box: 'border-primary/30 bg-primary-soft', icon: 'text-primary', Icon: ShieldCheck },
} as const satisfies Record<RiskLevel, unknown>

const sevTone = { high: 'danger', medium: 'warning', low: 'neutral' } as const

export function RiskAlert({ risk, showLinks = true }: { risk: Risk; showLinks?: boolean }) {
  const { t } = useTranslation()
  const s = styles[risk.level]
  const Icon = s.Icon
  const gap = risk.possible_break_date ?? risk.first_break_date
  const hasGap = risk.level !== 'none' && gap != null
  const titleKey = { none: 'risk.titleNone', low: 'risk.titleLow', medium: 'risk.title', high: 'risk.title' }[risk.level]
  const raw = risk.raw_shortfall_probability
  const showRaw = raw != null && Math.abs(raw - risk.shortfall_probability) >= 0.005

  return (
    <section className={cn('enter rounded-card border p-5 flex flex-col gap-4', s.box)} aria-label={t('risk.title')}>
      <div className="flex items-start gap-3">
        <Icon size={22} className={cn('shrink-0 mt-0.5', s.icon)} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold">{t(titleKey)}</h2>
            <RiskBadge level={risk.level} />
          </div>
        </div>
      </div>

      <dl className="flex flex-col gap-2">
        <Fact
          label={t('risk.probability')}
          value={risk.shortfall_probability < 0.005 ? formatPercent(risk.shortfall_probability) : `≈ ${formatPercent(risk.shortfall_probability)}`}
          hint={showRaw ? t('risk.rawNote', { value: formatPercent(raw!) }) : t('risk.estimateNote')}
        />
        {hasGap && (
          <>
            <Fact label={t('risk.shortageDate')} value={formatDate(gap)} hint={t('risk.shortageDateHint')} />
            {risk.first_break_date && risk.first_break_date !== gap && <Fact label={t('risk.medianDate')} value={formatDate(risk.first_break_date)} />}
            <Fact label={t('risk.deficit')} value={formatKop(risk.deficit_given_break_median)} danger />
            <Fact label={t('risk.deficitP90')} value={formatKop(risk.max_deficit_p90)} danger />
          </>
        )}
      </dl>

      {risk.warnings.length > 0 && (
        <div>
          <h3 className="text-[13px] font-semibold mb-1.5">{t('risk.factorsTitle')}</h3>
          <ul className="flex flex-col gap-3">
            {risk.warnings.map((w) => (
              <li key={`${w.code}-${w.date ?? ''}`} className="text-sm">
                <p className="flex flex-wrap items-center gap-2 font-medium">
                  <Badge tone={sevTone[w.severity]}>{t(`risk.severity.${w.severity}`)}</Badge>
                  {w.date && <span className="text-xs text-muted font-normal">{formatShortDate(w.date)}</span>}
                </p>
                <p className="mt-1">{w.message}</p>
                {w.reasons.length > 0 && (
                  <ul className="mt-1 flex flex-col gap-0.5 text-muted text-[13px]">
                    {w.reasons.map((x, i) => (
                      <li key={i} className="flex justify-between gap-3">
                        <span className="min-w-0 truncate">
                          {formatShortDate(x.date)} — {x.title}
                          {x.overdue && <span className="text-danger"> · {t('status.overdueLower')}</span>}
                        </span>
                        <span className="tnum shrink-0">{formatKop(x.amount)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {showLinks && hasGap && (
        <div className="flex flex-col sm:flex-row gap-2">
          <Link to="/scenarios">
            <Button variant="primary" className="w-full sm:w-auto">{t('risk.createScenario')}</Button>
          </Link>
          <Link to="/forecast">
            <Button className="w-full sm:w-auto">{t('risk.detailedForecast')}</Button>
          </Link>
        </div>
      )}
    </section>
  )
}

function Fact({ label, value, hint, danger }: { label: string; value: string; hint?: string; danger?: boolean }) {
  return (
    <div className="rounded-control bg-surface/70 border border-line px-3.5 py-2 flex items-baseline justify-between gap-3">
      <dt className="text-[13px] text-muted">
        {label}
        {hint && <span className="block text-xs">{hint}</span>}
      </dt>
      <dd className={cn('tnum text-base font-semibold text-right', danger && 'text-danger')}>{value}</dd>
    </div>
  )
}
