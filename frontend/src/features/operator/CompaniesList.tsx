import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronLeft, ChevronRight, Search } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { Button } from '../../components/ui/Button'
import { Checkbox } from '../../components/ui/Checkbox'
import { errorText } from '../../i18n/helpers'
import { formatNumber } from '../../lib/formatters'
import { BanBadge, RiskCell } from './CompanyStatus'
import { safeDate } from './format'
import { useOperatorCompanies } from './operatorApi'
import type { OperatorCompany } from './operatorApi'

const PAGE = 25

/** Companies registered on the platform: search, «blocked only» filter, pages. */
export function CompaniesList({ onOpen }: { onOpen: (id: string) => void }) {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const [banned, setBanned] = useState(false)
  const [offset, setOffset] = useState(0)

  // debounce typing, and always return to the first page when the filter changes
  useEffect(() => {
    const id = setTimeout(() => {
      setQuery(text)
      setOffset(0)
    }, 300)
    return () => clearTimeout(id)
  }, [text])

  const q = useOperatorCompanies({ query, banned, limit: PAGE, offset })
  const total = q.data?.total ?? 0
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + PAGE, total)

  const columns: Column<OperatorCompany>[] = [
    {
      key: 'name',
      header: t('operator.col.company'),
      render: (c) => (
        <div className="min-w-0">
          <button type="button" onClick={() => onOpen(c.id)} className="text-left font-medium text-primary hover:underline cursor-pointer">
            {c.name || t('operator.noName')}
          </button>
          {c.director_email && <p className="text-xs text-muted break-all">{c.director_email}</p>}
        </div>
      ),
    },
    { key: 'inn', header: t('operator.col.inn'), render: (c) => <span className="tnum">{c.inn || '—'}</span> },
    { key: 'status', header: t('operator.col.status'), render: (c) => <BanBadge banned={c.banned} /> },
    { key: 'risk', header: t('operator.col.risk'), render: (c) => <RiskCell company={c} /> },
    { key: 'members', header: t('operator.col.members'), numeric: true, render: (c) => (c.members_count == null ? '—' : formatNumber(c.members_count)) },
    { key: 'created', header: t('operator.col.registered'), render: (c) => <span className="nowrap">{safeDate(c.created_at)}</span> },
    { key: 'import', header: t('operator.col.lastImport'), render: (c) => <span className="nowrap">{safeDate(c.last_import_at)}</span> },
  ]

  return (
    <Card title={t('operator.companies.title')} subtitle={t('operator.companies.subtitle')}>
      <div className="flex flex-col gap-3 px-5 pb-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="relative w-full sm:max-w-sm">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted pointer-events-none" aria-hidden />
          <input
            type="search"
            className="control pl-9"
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={100}
            placeholder={t('operator.companies.search')}
            aria-label={t('operator.companies.search')}
          />
        </div>
        <Checkbox
          label={t('operator.companies.onlyBanned')}
          checked={banned}
          onChange={(e) => {
            setBanned(e.target.checked)
            setOffset(0)
          }}
        />
      </div>

      <div aria-live="polite" aria-busy={q.isFetching}>
        {q.isError ? (
          <ErrorState message={errorText(t, q.error)} onRetry={() => q.refetch()} />
        ) : q.isLoading ? (
          <LoadingState height={160} />
        ) : q.data && q.data.items.length === 0 ? (
          <EmptyState title={t('operator.companies.empty')} description={query || banned ? t('common.changeFilters') : undefined} />
        ) : (
          <div className={q.isFetching ? 'opacity-70' : undefined}>
            <DataTable columns={columns} rows={q.data?.items ?? []} getRowKey={(c) => c.id} />
          </div>
        )}
      </div>

      {total > 0 && (
        <nav aria-label={t('operator.companies.pages')} className="flex items-center justify-between gap-3 px-5 py-3 border-t border-line">
          <p className="text-[13px] text-muted tnum">{t('operator.companies.range', { from, to, total })}</p>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => setOffset(Math.max(0, offset - PAGE))} disabled={offset === 0} aria-label={t('common.back')}>
              <ChevronLeft size={16} aria-hidden />
            </Button>
            <Button size="sm" onClick={() => setOffset(offset + PAGE)} disabled={offset + PAGE >= total} aria-label={t('common.next')}>
              <ChevronRight size={16} aria-hidden />
            </Button>
          </div>
        </nav>
      )}
    </Card>
  )
}
