import { useTranslation } from 'react-i18next'
import { ShieldBan, ShieldCheck } from 'lucide-react'
import { Badge, RiskBadge } from '../../components/shared/StatusBadge'
import { formatPercent } from '../../lib/formatters'
import type { OperatorCompany } from './operatorApi'

export function BanBadge({ banned }: { banned: boolean }) {
  const { t } = useTranslation()
  return banned ? (
    <Badge tone="danger" icon={<ShieldBan size={12} aria-hidden />}>
      {t('operator.status.banned')}
    </Badge>
  ) : (
    <Badge tone="success" icon={<ShieldCheck size={12} aria-hidden />}>
      {t('operator.status.active')}
    </Badge>
  )
}

/** Risk level and gap probability from the company's forecast; «no forecast» when there is none. */
export function RiskCell({ company }: { company: Pick<OperatorCompany, 'risk_level' | 'shortfall_probability'> }) {
  const { t } = useTranslation()
  if (!company.risk_level) return <span className="text-muted">{t('operator.noForecast')}</span>
  return (
    <span className="inline-flex items-center gap-2">
      <RiskBadge level={company.risk_level} />
      {company.shortfall_probability != null && <span className="tnum text-muted text-[13px]">{formatPercent(company.shortfall_probability)}</span>}
    </span>
  )
}
