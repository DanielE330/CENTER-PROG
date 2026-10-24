import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import type { RiskItem } from '../../types'
import { formatKop, formatShortDate } from '../../lib/formatters'
import { EmptyState } from '../../components/shared/States'
import { Badge } from '../../components/shared/StatusBadge'
import { sourceLabel } from '../obligations/labels'

interface Props {
  items: RiskItem[]
  kind: 'payment' | 'receipt'
  icon: ReactNode
  limit?: number
}

/** Compact list of the nearest planned payments or receipts from the forecast. */
export function PlannedList({ items, kind, icon, limit = 5 }: Props) {
  const { t } = useTranslation()
  const rows = items
    .filter((o) => (kind === 'receipt' ? BigInt(o.amount) > 0n : BigInt(o.amount) < 0n))
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(0, limit)

  if (rows.length === 0) return <EmptyState title={t('dashboard.noUpcoming')} />

  return (
    <ul className="px-5 pb-4 divide-y divide-line">
      {rows.map((o, i) => (
        <li key={`${o.date}-${o.title}-${i}`} className="flex items-center gap-3 py-2.5">
          <span className={kind === 'payment' ? 'text-warning' : 'text-primary'} aria-hidden>{icon}</span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium truncate">{o.title}</p>
            <p className="text-xs text-muted">{formatShortDate(o.date)} · {sourceLabel(t, o.source)}</p>
          </div>
          <div className="text-right">
            <p className="tnum text-sm font-semibold">{formatKop(o.amount, { sign: true })}</p>
            {o.overdue && <Badge tone="danger">{t('status.overdue')}</Badge>}
          </div>
        </li>
      ))}
    </ul>
  )
}
