import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowDownLeft, ArrowUpRight, Receipt } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { QueryBoundary } from '../../components/shared/QueryBoundary'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState } from '../../components/shared/States'
import { MockBadge, TransactionStatusBadge } from '../../components/shared/StatusBadge'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { useTransactions } from '../../hooks/queries'
import { formatCurrency, formatDate } from '../../lib/formatters'
import { tm } from '../../i18n/helpers'
import type { Transaction } from '../../types'

export default function TransactionsPage() {
  const { t } = useTranslation()
  const query = useTransactions()
  const [dir, setDir] = useState('all')
  const [category, setCategory] = useState('all')
  const [search, setSearch] = useState('')

  return (
    <PageContainer>
      <PageHeader title={t('transactions.title')} description={t('transactions.description')} actions={<MockBadge />} />
      <QueryBoundary query={query}>
        {(all) => <TransactionsBody all={all} dir={dir} setDir={setDir} category={category} setCategory={setCategory} search={search} setSearch={setSearch} />}
      </QueryBoundary>
    </PageContainer>
  )
}

interface BodyProps {
  all: Transaction[]
  dir: string
  setDir: (v: string) => void
  category: string
  setCategory: (v: string) => void
  search: string
  setSearch: (v: string) => void
}

function TransactionsBody({ all, dir, setDir, category, setCategory, search, setSearch }: BodyProps) {
  const { t } = useTranslation()
  const counterparty = (x: Transaction) => t(x.counterpartyKey)
  const description = (x: Transaction) => tm(t, x.descriptionKey, x.descriptionParams)

  const categories = useMemo(() => [...new Set(all.map((x) => x.categoryKey).filter(Boolean))] as string[], [all])
  const rows = all.filter(
    (x) =>
      (dir === 'all' || x.direction === dir) &&
      (category === 'all' || x.categoryKey === category) &&
      (!search || `${counterparty(x)} ${description(x)}`.toLowerCase().includes(search.toLowerCase())),
  )

  const columns: Column<Transaction>[] = [
    { key: 'date', header: t('transactions.col.date'), render: (x) => formatDate(x.date), sortValue: (x) => x.date },
    {
      key: 'counterparty',
      header: t('transactions.col.counterparty'),
      sortValue: (x) => counterparty(x),
      render: (x) => (
        <div className="min-w-[200px]">
          <p className="font-medium">{counterparty(x)}</p>
          <p className="text-xs text-muted">{description(x)}</p>
        </div>
      ),
    },
    {
      key: 'direction',
      header: t('transactions.col.direction'),
      sortValue: (x) => x.direction,
      render: (x) =>
        x.direction === 'in' ? (
          <span className="inline-flex items-center gap-1 text-primary"><ArrowDownLeft size={14} /> {t('transactions.incoming')}</span>
        ) : (
          <span className="inline-flex items-center gap-1 text-warning"><ArrowUpRight size={14} /> {t('transactions.outgoing')}</span>
        ),
    },
    { key: 'category', header: t('transactions.col.category'), sortValue: (x) => (x.categoryKey ? t(x.categoryKey) : ''), render: (x) => (x.categoryKey ? t(x.categoryKey) : '—') },
    { key: 'status', header: t('transactions.col.status'), render: (x) => <TransactionStatusBadge status={x.status} /> },
    {
      key: 'amount',
      header: t('transactions.col.amount'),
      numeric: true,
      sortValue: (x) => (x.direction === 'in' ? x.amount : -x.amount),
      render: (x) => (
        <span className={x.direction === 'in' ? 'text-primary font-semibold' : 'font-semibold'}>
          {x.direction === 'in' ? '+' : '−'}
          {formatCurrency(x.amount)}
        </span>
      ),
    },
  ]

  if (all.length === 0) {
    return (
      <div className="card">
        <EmptyState icon={<Receipt size={22} />} title={t('transactions.emptyTitle')} description={t('transactions.emptyText')} />
      </div>
    )
  }

  return (
    <Card>
      <div className="grid gap-3 p-4 sm:grid-cols-[1fr_180px_200px]">
        <Input aria-label={t('common.search')} placeholder={t('transactions.searchPlaceholder')} value={search} onChange={(e) => setSearch(e.target.value)} />
        <Select
          aria-label={t('transactions.col.direction')}
          value={dir}
          onChange={setDir}
          options={[{ value: 'all', label: t('transactions.allDirections') }, { value: 'in', label: t('transactions.incomingPlural') }, { value: 'out', label: t('transactions.outgoingPlural') }]}
        />
        <Select
          aria-label={t('transactions.col.category')}
          value={category}
          onChange={setCategory}
          options={[{ value: 'all', label: t('transactions.allCategories') }, ...categories.map((c) => ({ value: c, label: t(c) }))]}
        />
      </div>
      {rows.length === 0 ? (
        <EmptyState title={t('common.nothingFound')} description={t('common.changeFilters')} />
      ) : (
        <DataTable columns={columns} rows={rows} getRowKey={(x) => x.id} defaultSort={{ key: 'date', dir: 'desc' }} />
      )}
      <p className="px-4 py-3 text-xs text-muted border-t border-line">{t('transactions.shown', { shown: rows.length, total: all.length })}</p>
    </Card>
  )
}
