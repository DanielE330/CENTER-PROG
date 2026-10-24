import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { Button } from '../../components/ui/Button'
import { Download } from 'lucide-react'
import { Select } from '../../components/ui/Select'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { AuditItem } from '../../types'

const PAGE = 50
const FILTERS = ['', 'team', 'member', 'goal', 'notifications', 'telegram', 'import', 'event', 'forecast', 'account'] as const

/** Who did what: team changes, goals, Telegram settings, imports. Admins and the owner only (the API enforces it). */
export default function ActivityPage() {
  const { t, i18n } = useTranslation()
  const [offset, setOffset] = useState(0)
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>('')
  const q = useQuery({ queryKey: ['activity', filter, offset], queryFn: () => api.activity({ limit: PAGE, offset, q: filter }), placeholderData: keepPreviousData })

  const action = (a: string) => (i18n.exists(`activity.actions.${a}`) ? t(`activity.actions.${a}`) : a)
  const actor = (r: AuditItem) => {
    if (r.actor_name) return r.actor_name
    if (r.actor.startsWith('tg:')) return t('activity.telegram', { id: r.actor.slice(3) })
    return r.actor === 'notifier' ? t('activity.system') : r.actor ? `${r.actor.slice(0, 8)}…` : t('activity.system')
  }
  const columns: Column<AuditItem>[] = [
    { key: 'at', header: t('activity.when'), render: (r) => <span className="whitespace-nowrap">{formatDateTime(r.at)}</span> },
    { key: 'actor', header: t('activity.who'), render: (r) => <span className="break-all">{actor(r)}</span> },
    { key: 'action', header: t('activity.what'), render: (r) => <span className="font-medium">{action(r.action)}</span> },
    { key: 'object', header: t('activity.object'), render: (r) => <span className="tnum text-muted break-all">{r.object_id ? r.object_id.slice(0, 24) : '—'}</span> },
  ]
  const total = q.data?.total ?? 0

  return (
    <PageContainer>
      <PageHeader
        title={t('activity.title')}
        description={t('activity.description')}
        actions={
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center">
          <a href={`/api/v1/team/activity/export?q=${encodeURIComponent(filter)}`} download className="inline-flex h-10 items-center justify-center gap-2 rounded-control border border-line bg-surface px-4 text-sm font-medium hover:bg-hover">
            <Download size={16} aria-hidden /> {t('activity.export')}
          </a>
          <div className="w-full sm:w-56">
            <Select
              aria-label={t('activity.filter')}
              value={filter}
              onChange={(v) => { setFilter(v as (typeof FILTERS)[number]); setOffset(0) }}
              options={FILTERS.map((f) => ({ value: f, label: t(`activity.filters.${f || 'all'}`) }))}
            />
          </div>
          </div>
        }
      />
      <Card>
        {q.isPending && <LoadingState height={160} />}
        {q.isError && <ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} />}
        {q.data && (q.data.items.length === 0
          ? <EmptyState title={t('activity.empty')} />
          : <DataTable columns={columns} rows={q.data.items} getRowKey={(r) => String(r.id)} />)}
        {q.data && total > PAGE && (
          <div className="flex items-center justify-between px-5 py-3 text-sm text-muted">
            <span>{t('activity.range', { from: offset + 1, to: Math.min(offset + PAGE, total), total })}</span>
            <div className="flex gap-2">
              <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>{t('common.back')}</Button>
              <Button size="sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>{t('common.next')}</Button>
            </div>
          </div>
        )}
      </Card>
    </PageContainer>
  )
}
