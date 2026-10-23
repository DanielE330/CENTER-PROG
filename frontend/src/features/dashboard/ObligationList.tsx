import type { ReactNode } from 'react'
import { format } from 'date-fns'
import { useTranslation } from 'react-i18next'
import type { Obligation, ObligationKind } from '../../types'
import { formatCurrency, formatShortDate } from '../../lib/formatters'
import { EmptyState } from '../../components/shared/States'
import { ObligationStatusBadge } from '../../components/shared/StatusBadge'

interface Props {
  items: Obligation[]
  kind: ObligationKind
  icon: ReactNode
  limit?: number
}

/** Compact list of upcoming (not yet completed) obligations of one kind. */
export function ObligationList({ items, kind, icon, limit = 5 }: Props) {
  const { t } = useTranslation()
  const today = format(new Date(), 'yyyy-MM-dd')
  const rows = items
    .filter((o) => o.kind === kind && o.status !== 'completed' && o.date >= today)
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(0, limit)

  if (rows.length === 0) return <EmptyState title={t('common.noRecords')} description={t('dashboard.noUpcoming')} />

  return (
    <ul className="px-5 pb-4 divide-y divide-line">
      {rows.map((o) => (
        <li key={o.id} className="flex items-center gap-3 py-2.5">
          <span className={kind === 'payment' ? 'text-warning' : 'text-primary'}>{icon}</span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium truncate">{t(o.nameKey)}</p>
            <p className="text-xs text-muted">{formatShortDate(o.date)}</p>
          </div>
          <div className="text-right">
            <p className="tnum text-sm font-semibold">{formatCurrency(o.amount)}</p>
            <ObligationStatusBadge status={o.status} />
          </div>
        </li>
      ))}
    </ul>
  )
}
