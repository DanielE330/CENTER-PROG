import { useMemo, useState } from 'react'
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
import type { Transaction } from '../../types'

const columns: Column<Transaction>[] = [
  { key: 'date', header: 'Дата', render: (t) => formatDate(t.date), sortValue: (t) => t.date },
  {
    key: 'counterparty',
    header: 'Контрагент / назначение',
    sortValue: (t) => t.counterparty,
    render: (t) => (
      <div className="min-w-[200px]">
        <p className="font-medium">{t.counterparty}</p>
        <p className="text-xs text-muted">{t.description}</p>
      </div>
    ),
  },
  {
    key: 'direction',
    header: 'Направление',
    sortValue: (t) => t.direction,
    render: (t) =>
      t.direction === 'in' ? (
        <span className="inline-flex items-center gap-1 text-primary"><ArrowDownLeft size={14} /> Входящий</span>
      ) : (
        <span className="inline-flex items-center gap-1 text-warning"><ArrowUpRight size={14} /> Исходящий</span>
      ),
  },
  { key: 'category', header: 'Категория', sortValue: (t) => t.category ?? '', render: (t) => t.category ?? '—' },
  { key: 'status', header: 'Статус', render: (t) => <TransactionStatusBadge status={t.status} /> },
  {
    key: 'amount',
    header: 'Сумма',
    numeric: true,
    sortValue: (t) => (t.direction === 'in' ? t.amount : -t.amount),
    render: (t) => (
      <span className={t.direction === 'in' ? 'text-primary font-semibold' : 'font-semibold'}>
        {t.direction === 'in' ? '+' : '−'}
        {formatCurrency(t.amount)}
      </span>
    ),
  },
]

export default function TransactionsPage() {
  const query = useTransactions()
  const [dir, setDir] = useState('all')
  const [category, setCategory] = useState('all')
  const [search, setSearch] = useState('')

  return (
    <PageContainer>
      <PageHeader title="Операции" description="История поступлений и списаний по счетам." actions={<MockBadge />} />
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
  const categories = useMemo(() => [...new Set(all.map((t) => t.category).filter(Boolean))] as string[], [all])
  const rows = useMemo(
    () =>
      all.filter(
        (t) =>
          (dir === 'all' || t.direction === dir) &&
          (category === 'all' || t.category === category) &&
          (!search || `${t.counterparty} ${t.description}`.toLowerCase().includes(search.toLowerCase())),
      ),
    [all, dir, category, search],
  )

  if (all.length === 0) {
    return (
      <div className="card">
        <EmptyState icon={<Receipt size={22} />} title="Операций пока нет" description="Импортируйте банковскую выписку, чтобы увидеть историю операций." />
      </div>
    )
  }

  return (
    <Card>
      <div className="grid gap-3 p-4 sm:grid-cols-[1fr_180px_200px]">
        <Input aria-label="Поиск" placeholder="Поиск по контрагенту или назначению" value={search} onChange={(e) => setSearch(e.target.value)} />
        <Select
          aria-label="Направление"
          value={dir}
          onChange={(v) => setDir(v)}
          options={[{ value: 'all', label: 'Все направления' }, { value: 'in', label: 'Входящие' }, { value: 'out', label: 'Исходящие' }]}
        />
        <Select
          aria-label="Категория"
          value={category}
          onChange={(v) => setCategory(v)}
          options={[{ value: 'all', label: 'Все категории' }, ...categories.map((c) => ({ value: c, label: c }))]}
        />
      </div>
      {rows.length === 0 ? (
        <EmptyState title="Ничего не найдено" description="Измените фильтры или строку поиска." />
      ) : (
        <DataTable columns={columns} rows={rows} getRowKey={(t) => t.id} defaultSort={{ key: 'date', dir: 'desc' }} />
      )}
      <p className="px-4 py-3 text-xs text-muted border-t border-line">Показано {rows.length} из {all.length}. Все суммы в рублях.</p>
    </Card>
  )
}
