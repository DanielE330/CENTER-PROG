import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowDownLeft, ArrowUpRight, CalendarClock } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { QueryBoundary } from '../../components/shared/QueryBoundary'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState } from '../../components/shared/States'
import { MockBadge, ObligationStatusBadge } from '../../components/shared/StatusBadge'
import { Select } from '../../components/ui/Select'
import { useObligations } from '../../hooks/queries'
import { formatCurrency, formatDate } from '../../lib/formatters'
import type { Obligation } from '../../types'

export default function ObligationsPage() {
  const { t } = useTranslation()
  const query = useObligations()
  const [kind, setKind] = useState('all')
  const [status, setStatus] = useState('all')

  return (
    <PageContainer>
      <PageHeader title={t('obligations.title')} description={t('obligations.description')} actions={<MockBadge />} />
      <QueryBoundary query={query}>
        {(all) => <Body all={all} kind={kind} setKind={setKind} status={status} setStatus={setStatus} />}
      </QueryBoundary>
    </PageContainer>
  )
}

function Body({ all, kind, setKind, status, setStatus }: { all: Obligation[]; kind: string; setKind: (v: string) => void; status: string; setStatus: (v: string) => void }) {
  const { t } = useTranslation()
  const rows = all.filter((o) => (kind === 'all' || o.kind === kind) && (status === 'all' || o.status === status))

  const columns: Column<Obligation>[] = [
    {
      key: 'name',
      header: t('obligations.col.name'),
      sortValue: (o) => t(o.nameKey),
      render: (o) => (
        <span className="inline-flex items-center gap-2 font-medium min-w-[200px]">
          {o.kind === 'payment' ? <ArrowUpRight size={15} className="text-warning shrink-0" aria-label={t('obligations.payment')} /> : <ArrowDownLeft size={15} className="text-primary shrink-0" aria-label={t('obligations.receipt')} />}
          {t(o.nameKey)}
        </span>
      ),
    },
    { key: 'date', header: t('obligations.col.date'), sortValue: (o) => o.date, render: (o) => formatDate(o.date) },
    { key: 'status', header: t('obligations.col.status'), sortValue: (o) => o.status, render: (o) => <ObligationStatusBadge status={o.status} /> },
    {
      key: 'amount',
      header: t('obligations.col.amount'),
      numeric: true,
      sortValue: (o) => o.amount,
      render: (o) => <span className={o.kind === 'receipt' ? 'text-primary font-semibold' : 'font-semibold'}>{o.kind === 'receipt' ? '+' : '−'}{formatCurrency(o.amount)}</span>,
    },
    { key: 'note', header: t('obligations.col.note'), render: (o) => <span className="text-muted">{o.noteKey ? t(o.noteKey) : '—'}</span> },
  ]

  if (all.length === 0) {
    return (
      <div className="card">
        <EmptyState icon={<CalendarClock size={22} />} title={t('obligations.emptyTitle')} description={t('obligations.emptyText')} />
      </div>
    )
  }

  const overdue = all.filter((o) => o.status === 'overdue').length

  return (
    <>
      {overdue > 0 && (
        <p className="rounded-card border border-danger/40 bg-danger-soft p-3 text-sm">
          <b className="text-danger">{t('obligations.overdueCount', { count: overdue })}</b> {t('obligations.overdueNote')}
        </p>
      )}
      <Card>
        <div className="grid gap-3 p-4 sm:grid-cols-[200px_200px]">
          <Select
            aria-label={t('obligations.typeAria')}
            value={kind}
            onChange={setKind}
            options={[{ value: 'all', label: t('obligations.kindAll') }, { value: 'payment', label: t('obligations.kindPayments') }, { value: 'receipt', label: t('obligations.kindReceipts') }]}
          />
          <Select
            aria-label={t('obligations.col.status')}
            value={status}
            onChange={setStatus}
            options={[
              { value: 'all', label: t('obligations.statusAll') },
              { value: 'planned', label: t('status.planned') },
              { value: 'expected', label: t('status.expected') },
              { value: 'completed', label: t('status.completed') },
              { value: 'overdue', label: t('status.overdue') },
            ]}
          />
        </div>
        {rows.length === 0 ? (
          <EmptyState title={t('common.nothingFound')} description={t('common.changeFiltersShort')} />
        ) : (
          <DataTable
            columns={columns}
            rows={rows}
            getRowKey={(o) => o.id}
            defaultSort={{ key: 'date', dir: 'asc' }}
            rowClassName={(o) => (o.status === 'overdue' ? 'bg-danger-soft' : undefined)}
          />
        )}
      </Card>
    </>
  )
}
