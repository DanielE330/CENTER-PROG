import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowDownLeft, ArrowUpRight, ChevronLeft, ChevronRight, FileUp, Receipt } from 'lucide-react'
import { Link } from 'react-router-dom'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { CategoryStatusBadge } from '../../components/shared/StatusBadge'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { useSetCategory, useTransactions } from '../../hooks/queries'
import { formatDate, formatKop, formatNumber } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import { CATEGORIES, categoryLabel } from '../../i18n/categories'
import type { Transaction } from '../../types'

const LIMIT = 50
type SortKey = 'date' | 'amount' | 'counterparty' | 'category'
const sortable = () => 0

export default function TransactionsPage() {
  const { t } = useTranslation()
  const [dir, setDir] = useState<'' | 'in' | 'out'>('')
  const [category, setCategory] = useState('')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'date', dir: 'desc' })
  const [offset, setOffset] = useState(0)

  // Search is debounced: one request after the user stops typing.
  useEffect(() => {
    const id = window.setTimeout(() => {
      setOffset(0)
      setQ(search.trim())
    }, 300)
    return () => window.clearTimeout(id)
  }, [search])

  const query = useTransactions({ limit: LIMIT, offset, category, direction: dir, q, sort: sort.key, order: sort.dir })
  const setCat = useSetCategory()
  const data = query.data
  const filtered = category !== '' || dir !== '' || q !== ''

  const toggleSort = (key: string) => {
    setOffset(0)
    setSort((s) => (s.key === key ? { key: s.key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key: key as SortKey, dir: key === 'counterparty' || key === 'category' ? 'asc' : 'desc' }))
  }

  const columns: Column<Transaction>[] = [
    { key: 'date', header: t('transactions.col.date'), sortValue: sortable, render: (x) => <span className="whitespace-nowrap">{formatDate(x.date)}</span> },
    {
      key: 'counterparty', header: t('transactions.col.counterparty'), sortValue: sortable,
      render: (x) => (
        <div className="min-w-[220px] max-w-[460px]">
          <p className="font-medium">{x.counterparty || '—'}</p>
          <p className="text-xs text-muted">{x.purpose}</p>
        </div>
      ),
    },
    {
      key: 'direction', header: t('transactions.col.direction'),
      render: (x) => x.amount_kop.startsWith('-')
        ? <span className="inline-flex items-center gap-1 text-warning"><ArrowUpRight size={14} aria-hidden /> {t('transactions.outgoing')}</span>
        : <span className="inline-flex items-center gap-1 text-primary"><ArrowDownLeft size={14} aria-hidden /> {t('transactions.incoming')}</span>,
    },
    {
      key: 'category', header: t('transactions.col.category'), sortValue: sortable,
      render: (x) => (
        <div className="w-[190px]">
          <Select
            aria-label={t('transactions.categoryOf', { name: x.counterparty || x.purpose.slice(0, 40) })}
            value={x.category}
            disabled={setCat.isPending && setCat.variables?.id === x.id}
            onChange={(c) => setCat.mutate({ id: x.id, category: c })}
            options={[...(CATEGORIES as readonly string[]).includes(x.category) ? [] : [{ value: x.category, label: x.category }], ...CATEGORIES.map((c) => ({ value: c, label: categoryLabel(t, c) }))]}
          />
        </div>
      ),
    },
    { key: 'status', header: t('transactions.col.status'), render: (x) => <CategoryStatusBadge tx={x} /> },
    {
      key: 'amount', header: t('transactions.col.amount'), numeric: true, sortValue: sortable,
      render: (x) => <span className={x.amount_kop.startsWith('-') ? 'font-semibold' : 'text-primary font-semibold'}>{formatKop(x.amount_kop, { sign: true })}</span>,
    },
  ]

  return (
    <PageContainer>
      <PageHeader
        title={t('transactions.title')}
        description={data ? t('transactions.descriptionCount', { count: data.total, formatted: formatNumber(data.total) }) : t('transactions.description')}
      />
      {setCat.isError && <Notice tone="danger">{errorText(t, setCat.error)}</Notice>}
      <Card>
        <div className="grid gap-3 p-4 sm:grid-cols-[1fr_180px_220px_auto] items-end">
          <Input aria-label={t('common.search')} type="search" placeholder={t('transactions.searchPlaceholder')} value={search} onChange={(e) => setSearch(e.target.value)} />
          <Select
            aria-label={t('transactions.col.direction')}
            value={dir}
            onChange={(v) => { setOffset(0); setDir(v as '' | 'in' | 'out') }}
            options={[{ value: '', label: t('transactions.allDirections') }, { value: 'in', label: t('transactions.incomingPlural') }, { value: 'out', label: t('transactions.outgoingPlural') }]}
          />
          <Select
            aria-label={t('transactions.col.category')}
            value={category}
            onChange={(v) => { setOffset(0); setCategory(v) }}
            options={[{ value: '', label: t('transactions.allCategories') }, ...CATEGORIES.map((c) => ({ value: c, label: categoryLabel(t, c) }))]}
          />
          {filtered && (
            <Button variant="ghost" onClick={() => { setSearch(''); setQ(''); setCategory(''); setDir(''); setOffset(0) }}>{t('common.reset')}</Button>
          )}
        </div>

        {query.isPending && <LoadingState />}
        {query.isError && <ErrorState message={errorText(t, query.error)} onRetry={() => void query.refetch()} />}
        {data && data.items.length === 0 && (
          <EmptyState
            icon={<Receipt size={22} aria-hidden />}
            title={filtered ? t('common.nothingFound') : t('transactions.emptyTitle')}
            description={filtered ? t('common.changeFilters') : t('transactions.emptyText')}
            action={!filtered && <Link to="/settings"><Button variant="primary"><FileUp size={16} aria-hidden /> {t('dashboard.import')}</Button></Link>}
          />
        )}
        {data && data.items.length > 0 && (
          <div className={query.isFetching ? 'opacity-60 transition-opacity' : 'transition-opacity'} aria-busy={query.isFetching}>
            <DataTable columns={columns} rows={data.items} getRowKey={(x) => String(x.id)} sort={sort} onSortChange={toggleSort} />
          </div>
        )}
        {data && data.total > LIMIT && (
          <nav className="flex items-center justify-between gap-3 px-4 py-3 border-t border-line" aria-label={t('transactions.pagination')}>
            <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}>
              <ChevronLeft size={14} aria-hidden /> {t('common.back')}
            </Button>
            <span className="text-xs text-muted tnum" aria-live="polite">
              {t('transactions.range', { from: offset + 1, to: Math.min(offset + LIMIT, data.total), total: formatNumber(data.total) })}
            </span>
            <Button size="sm" disabled={offset + LIMIT >= data.total} onClick={() => setOffset(offset + LIMIT)}>
              {t('common.next')} <ChevronRight size={14} aria-hidden />
            </Button>
          </nav>
        )}
      </Card>
    </PageContainer>
  )
}
