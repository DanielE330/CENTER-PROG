import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, HelpCircle, OctagonAlert, ShieldCheck } from 'lucide-react'
import type { RiskAssessment } from '../../types'
import { formatCurrency, formatDate, formatPercent } from '../../lib/formatters'
import { Button } from '../ui/Button'
import { RiskBadge } from './StatusBadge'
import { cn } from '../../lib/utils'
import { tm } from '../../i18n/helpers'

const styles = {
  high: { box: 'border-danger/40 bg-danger-soft', icon: 'text-danger', Icon: OctagonAlert },
  medium: { box: 'border-warning/40 bg-warning-soft', icon: 'text-warning', Icon: AlertTriangle },
  low: { box: 'border-primary/30 bg-primary-soft', icon: 'text-primary', Icon: ShieldCheck },
  unknown: { box: 'border-line bg-app', icon: 'text-muted', Icon: HelpCircle },
} as const

export function RiskAlert({ risk, showLinks = true }: { risk: RiskAssessment; showLinks?: boolean }) {
  const { t } = useTranslation()
  const s = styles[risk.level]
  const Icon = s.Icon

  let title = t('risk.title')
  if (risk.level === 'unknown') title = t('risk.titleUnknown')
  if (risk.level === 'low') title = t('risk.titleLow')

  return (
    <section className={cn('enter rounded-card border p-5 flex flex-col gap-4', s.box)} aria-label={t('risk.title')}>
      <div className="flex items-start gap-3">
        <Icon size={22} className={cn('shrink-0 mt-0.5', s.icon)} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold">{title}</h2>
            <RiskBadge level={risk.level} />
          </div>
          {risk.level === 'unknown' && (
            <p className="text-sm text-muted mt-1">
              {t('risk.unknownText')}
            </p>
          )}
        </div>
      </div>

      {(risk.level === 'high' || risk.level === 'medium') && (
        <>
          <dl className="flex flex-col gap-2">
            <Fact label={t('risk.shortageDate')} value={formatDate(risk.shortageDate)} />
            <Fact label={t('risk.deficit')} value={formatCurrency(risk.deficitAmount)} danger />
            <Fact
              label={t('risk.probability')}
              value={risk.probability != null ? `≈ ${formatPercent(risk.probability)}` : t('risk.probabilityNone')}
              hint={risk.probability != null ? t('risk.estimateNote') : undefined}
            />
          </dl>

          {risk.factors.length > 0 && (
            <div>
              <h3 className="text-[13px] font-semibold mb-1.5">{t('risk.factorsTitle')}</h3>
              <ul className="flex flex-col gap-2.5">
                {risk.factors.map((f) => (
                  <li key={f.id} className="text-sm">
                    <p className="font-medium">
                      {t(`risk.factors.${f.id}.label`)}
                      {f.amount != null && <span className="tnum text-muted font-normal"> · {formatCurrency(f.amount)}</span>}
                    </p>
                    <p className="text-muted">{tm(t, `risk.factors.${f.id}.description`, f.params)}</p>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {showLinks && (
            <div className="flex flex-col sm:flex-row gap-2">
              <Link to="/scenarios">
                <Button variant="primary" className="w-full sm:w-auto">{t('risk.createScenario')}</Button>
              </Link>
              <Link to="/forecast">
                <Button className="w-full sm:w-auto">{t('risk.detailedForecast')}</Button>
              </Link>
            </div>
          )}
        </>
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
