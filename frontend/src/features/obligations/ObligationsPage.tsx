import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowDownLeft, ArrowUpRight, CalendarClock, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { addDays, format, parseISO } from 'date-fns'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { Badge, ObligationStatusBadge } from '../../components/shared/StatusBadge'
import { Notice } from '../../components/shared/Notice'
import { StatCard } from '../../components/shared/StatCard'
import { ForecastBanners } from '../../components/shared/ForecastBanners'
import { Button } from '../../components/ui/Button'
import { Select } from '../../components/ui/Select'
import { useEditGuard } from '../../app/perms'
import { useDeleteEvent, useExpectedEvents, useForecastRun, usePlanFact } from '../../hooks/queries'
import { formatDate, formatKop, formatPercent } from '../../lib/formatters'
import { kopToRub } from '../../lib/money'
import { errorText } from '../../i18n/helpers'
import { isNoData } from '../../lib/errors'
import type { Obligation } from '../../types'
import { buildHistory, buildUpcoming } from './model'
import { sourceLabel } from './labels'
import { AddEventModal } from './AddEventModal'
import { SyncModal } from './SyncModal'
import { FnsEnsCard } from '../fns/FnsEnsCard'
import { FnsCounterparties } from '../fns/FnsCounterparties'

export default function ObligationsPage() {
  const { t } = useTranslation()
  const forecast = useForecastRun()
  const pf = usePlanFact(90)
  const events = useExpectedEvents()
  const del = useDeleteEvent()
  const guard = useEditGuard()
  const [kind, setKind] = useState('all')
  const [status, setStatus] = useState('all')
  const [addOpen, setAddOpen] = useState(false)
  const [syncOpen, setSyncOpen] = useState(false)

  const run = forecast.data ?? null
  const upcoming = useMemo(() => buildUpcoming(run), [run])
  const history = useMemo(() => buildHistory(pf.data), [pf.data])
  const asOf = run?.result.as_of ?? pf.data?.as_of ?? null

  const pass = (o: Obligation) => (kind === 'all' || o.kind === kind) && (status === 'all' || o.status === status)
  const up = upcoming.filter(pass)
  const hi = history.filter(pass)

  // Next 7 days after the data date: planned payments / receipts that are not done yet.
  const weekEnd = asOf ? format(addDays(parseISO(asOf), 7), 'yyyy-MM-dd') : ''
  const week = upcoming.filter((o) => o.date <= weekEnd && o.status !== 'overdue')
  const outSum = week.filter((o) => o.kind === 'payment').reduce((s, o) => s + -BigInt(o.amount), 0n)
  const inSum = week.filter((o) => o.kind === 'receipt').reduce((s, o) => s + BigInt(o.amount), 0n)
  const overdue = [...upcoming, ...history].filter((o) => o.status === 'overdue').length
  const soon = upcoming.filter((o) => o.status !== 'overdue').slice(0, 5)

  const loading = forecast.isPending
  const failed = forecast.isError ? forecast.error : events.isError ? events.error : null
  const noData = !loading && !run && (!pf.data) && (events.data?.items.length ?? 0) === 0

  // налоговые события из ФНС хранятся с источником tax, но подписаны «· ФНС» в названии: показываем источник «ФНС»
  const srcLabel = (o: Obligation) => (o.source === 'tax' && o.title.includes('· ФНС') ? t('source.fns') : sourceLabel(t, o.source))

  const columns = (withFact: boolean): Column<Obligation>[] => [
    {
      key: 'name', header: t('obligations.col.name'), sortValue: (o) => o.title,
      render: (o) => (
        <span className="inline-flex items-center gap-2 font-medium min-w-[160px]">
          {o.kind === 'payment' ? <ArrowUpRight size={15} className="text-warning shrink-0" aria-label={t('obligations.payment')} /> : <ArrowDownLeft size={15} className="text-primary shrink-0" aria-label={t('obligations.receipt')} />}
          {o.title}
        </span>
      ),
    },
    { key: 'date', header: t('obligations.col.date'), sortValue: (o) => o.date, render: (o) => <span className="whitespace-nowrap">{formatDate(o.date)}</span> },
    { key: 'source', header: t('obligations.col.source'), sortValue: (o) => o.source, render: (o) => <Badge>{srcLabel(o)}</Badge> },
    { key: 'status', header: t('obligations.col.status'), sortValue: (o) => o.status, render: (o) => <ObligationStatusBadge status={o.status} /> },
    ...(withFact
      ? [{
          key: 'fact', header: t('obligations.col.fact'), numeric: true,
          render: (o: Obligation) => o.fact ? (
            <span className="inline-flex flex-col items-end">
              <span>{formatKop(o.fact, { sign: true })}</span>
              <span className="text-xs text-muted">{o.factDate ? formatDate(o.factDate, 'dayMonth') : ''}{o.diff ? ` · ${t('obligations.deviation')} ${formatKop(o.diff, { sign: true })}` : ''}</span>
            </span>
          ) : '—',
        }]
      : []),
    {
      key: 'amount', header: t('obligations.col.amount'), numeric: true, sortValue: (o) => kopToRub(o.amount),
      render: (o) => <span className={o.kind === 'receipt' ? 'text-primary font-semibold' : 'font-semibold'}>{formatKop(o.amount, { sign: true })}</span>,
    },
  ]

  return (
    <PageContainer>
      <PageHeader
        title={t('obligations.title')}
        description={asOf ? t('obligations.descriptionAsOf', { date: formatDate(asOf) }) : t('obligations.description')}
        actions={
          <>
            <Button onClick={() => setSyncOpen(true)} {...guard.lock}><RefreshCw size={16} aria-hidden /> {t('obligations.sync')}</Button>
            <Button variant="primary" onClick={() => setAddOpen(true)} {...guard.lock}><Plus size={16} aria-hidden /> {t('obligations.add.open')}</Button>
          </>
        }
      />

      {del.isError && <Notice tone="danger">{errorText(t, del.error)}</Notice>}
      {loading && <div className="card"><LoadingState /></div>}
      {failed != null && <div className="card"><ErrorState message={errorText(t, failed)} onRetry={() => { void forecast.refetch(); void events.refetch() }} /></div>}
      {noData && failed == null && (
        <div className="card">
          <EmptyState icon={<CalendarClock size={22} />} title={t('obligations.emptyTitle')} description={t('obligations.emptyText')} />
        </div>
      )}

      {!loading && failed == null && !noData && (
        <>
          {run && <ForecastBanners run={run} />}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard index={0} label={t('obligations.kpi.payments')} value={formatKop((-outSum).toString())} tone={outSum > 0n ? 'warning' : 'default'} hint={t('obligations.kpi.paymentsHint', { count: week.filter((o) => o.kind === 'payment').length })} />
            <StatCard index={1} label={t('obligations.kpi.receipts')} value={formatKop(inSum.toString())} hint={t('obligations.kpi.receiptsHint', { count: week.filter((o) => o.kind === 'receipt').length })} />
            <StatCard index={2} label={t('obligations.kpi.overdue')} value={String(overdue)} tone={overdue > 0 ? 'danger' : 'default'} />
            <StatCard index={3} label={t('obligations.kpi.events')} value={String(events.data?.items.length ?? 0)} hint={t('obligations.kpi.eventsHint')} />
          </div>

          {overdue > 0 && (
            <Notice tone="danger">{t('obligations.overdueNote')}</Notice>
          )}

          <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
            <div className="flex flex-col gap-5 min-w-0">
              <Card>
                <div className="grid gap-3 p-4 sm:grid-cols-[220px_220px]">
                  <Select
                    aria-label={t('obligations.typeAria')} value={kind} onChange={setKind}
                    options={[{ value: 'all', label: t('obligations.kindAll') }, { value: 'payment', label: t('obligations.kindPayments') }, { value: 'receipt', label: t('obligations.kindReceipts') }]}
                  />
                  <Select
                    aria-label={t('obligations.col.status')} value={status} onChange={setStatus}
                    options={[
                      { value: 'all', label: t('obligations.statusAll') },
                      { value: 'planned', label: t('status.planned') },
                      { value: 'expected', label: t('status.expected') },
                      { value: 'completed', label: t('status.completed') },
                      { value: 'overdue', label: t('status.overdue') },
                    ]}
                  />
                </div>
              </Card>

              <Card title={t('obligations.upcoming', { count: up.length })}>
                {up.length === 0 ? (
                  <EmptyState title={t('common.nothingFound')} description={t('obligations.noUpcoming')} />
                ) : (
                  <DataTable columns={columns(false)} rows={up} getRowKey={(o) => o.id} defaultSort={{ key: 'date', dir: 'asc' }} rowClassName={(o) => (o.status === 'overdue' ? 'bg-danger-soft' : undefined)} />
                )}
              </Card>

              <FnsCounterparties />

              <Card title={t('obligations.history', { count: hi.length })} subtitle={t('obligations.historySubtitle')}>
                {pf.isPending ? <LoadingState height={120} /> : pf.isError && !isNoData(pf.error) ? (
                  <ErrorState message={errorText(t, pf.error)} onRetry={() => void pf.refetch()} />
                ) : hi.length === 0 ? (
                  <EmptyState title={t('obligations.historyEmpty')} description={t('common.changeFiltersShort')} />
                ) : (
                  <DataTable columns={columns(true)} rows={hi} getRowKey={(o) => o.id} defaultSort={{ key: 'date', dir: 'desc' }} rowClassName={(o) => (o.status === 'overdue' ? 'bg-danger-soft' : undefined)} />
                )}
              </Card>
            </div>

            <div className="flex flex-col gap-5 min-w-0">
              <Card title={t('obligations.nearest')}>
                {soon.length === 0 ? (
                  <p className="px-5 pb-5 text-sm text-muted">{t('obligations.noNearest')}</p>
                ) : (
                  <ul className="px-5 pb-4 divide-y divide-line">
                    {soon.map((o) => (
                      <li key={o.id} className="flex items-center justify-between gap-3 py-2.5">
                        <div className="min-w-0">
                          <p className="text-sm font-medium truncate">{o.title}</p>
                          <p className="text-xs text-muted">{formatDate(o.date, 'dayMonth')} · {srcLabel(o)}</p>
                        </div>
                        <span className={'tnum text-sm font-semibold shrink-0 ' + (o.kind === 'receipt' ? 'text-primary' : '')}>{formatKop(o.amount, { sign: true })}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <FnsEnsCard />

              <Card title={t('obligations.manual')} subtitle={t('obligations.manualSubtitle')}>
                {events.isPending ? <LoadingState height={100} /> : (events.data?.items.length ?? 0) === 0 ? (
                  <p className="px-5 pb-5 text-sm text-muted">{t('obligations.manualEmpty')}</p>
                ) : (
                  <ul className="px-5 pb-4 divide-y divide-line">
                    {events.data!.items.map((e) => (
                      <li key={e.id} className="flex items-center gap-3 py-2.5">
                        <div className="min-w-0 flex-1">
                          <p className="text-sm font-medium truncate">{e.title}</p>
                          <p className="text-xs text-muted">{formatDate(e.date, 'dayMonth')} · {sourceLabel(t, e.source)} · {formatPercent(e.confidence)}</p>
                        </div>
                        <span className={'tnum text-sm font-semibold shrink-0 ' + (e.amount.startsWith('-') ? '' : 'text-primary')}>{formatKop(e.amount, { sign: true })}</span>
                        <button
                          onClick={() => del.mutate(e.id)}
                          disabled={del.isPending || guard.lock.disabled}
                          title={guard.lock.title}
                          aria-label={t('obligations.deleteEvent', { title: e.title })}
                          className="p-1.5 rounded-md text-muted hover:bg-hover hover:text-danger cursor-pointer"
                        >
                          <Trash2 size={15} aria-hidden />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </div>
          </div>
        </>
      )}

      <AddEventModal open={addOpen} onClose={() => setAddOpen(false)} />
      <SyncModal open={syncOpen} onClose={() => setSyncOpen(false)} />
    </PageContainer>
  )
}
