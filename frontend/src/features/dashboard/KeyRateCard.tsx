import { useTranslation } from 'react-i18next'
import { Landmark } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { Badge } from '../../components/shared/StatusBadge'
import { useKeyRate } from '../../hooks/queries'
import { errorText } from '../../i18n/helpers'
import { formatDate, formatRate } from '../../lib/formatters'
import { Button } from '../../components/ui/Button'

/** Central Bank key rate — reference information only, it is not used by the forecast. */
export function KeyRateCard() {
  const { t } = useTranslation()
  const q = useKeyRate()
  const d = q.data
  const delta = d && d.previous_rate != null ? d.rate - d.previous_rate : null
  return (
    <Card title={t('keyRate.title')} subtitle={t('keyRate.subtitle')} action={<Landmark size={18} className="text-muted" aria-hidden />}>
      <div className="px-5 pb-5">
        {q.isPending && <div className="skeleton h-8 w-28" role="status" aria-label={t('common.loading')} />}
        {q.isError && (
          <div className="flex flex-col items-start gap-2 text-sm" role="alert">
            <p className="text-muted">{t('keyRate.error')} {errorText(t, q.error)}</p>
            <Button size="sm" onClick={() => void q.refetch()}>{t('common.retry')}</Button>
          </div>
        )}
        {d && (
          <div className="flex flex-col gap-1.5">
            <div className="flex items-baseline gap-3">
              <span className="tnum text-[28px] font-semibold leading-tight">{formatRate(d.rate)}</span>
              {delta != null && delta !== 0 && (
                <Badge tone={delta < 0 ? 'success' : 'warning'}>{delta > 0 ? '+' : '−'}{formatRate(Math.abs(delta)).replace('%', '')} {t('keyRate.pp')}</Badge>
              )}
            </div>
            <p className="text-[13px] text-muted">
              {d.changed_on ? t('keyRate.since', { date: formatDate(d.changed_on) }) : t('keyRate.asOf', { date: formatDate(d.as_of) })}
              {d.previous_rate != null && ` · ${t('keyRate.previous', { value: formatRate(d.previous_rate) })}`}
            </p>
            <p className="text-xs text-muted">
              {t('keyRate.source', { date: formatDate(d.as_of) })}
              {d.stale && ` · ${t('keyRate.stale')}`}
            </p>
          </div>
        )}
      </div>
    </Card>
  )
}
