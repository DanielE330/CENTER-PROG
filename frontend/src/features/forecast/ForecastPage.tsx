import { useMemo } from 'react'
import { AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { RiskAlert } from '../../components/shared/RiskAlert'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { ForecastBanners } from '../../components/shared/ForecastBanners'
import { Notice } from '../../components/shared/Notice'
import { BarChart } from '../../components/shared/BarChart'
import { Badge } from '../../components/shared/StatusBadge'
import { Select } from '../../components/ui/Select'
import { Link } from 'react-router-dom'
import { useForecast, useRecalc } from '../../hooks/queries'
import { formatCompact, formatDate, formatDateTime, formatKop, formatPercent } from '../../lib/formatters'
import { kopToRub } from '../../lib/money'
import { HORIZONS } from '../../lib/prefs'
import { errorText } from '../../i18n/helpers'
import type { ForecastDay, Rule } from '../../types'
import { ForecastChart } from './ForecastChart'
import { BacktestCard } from './BacktestCard'
import { weeklyFlows } from './model'
import { categoryLabel } from '../../i18n/categories'

export default function ForecastPage() {
  const { t } = useTranslation()
  const q = useForecast()
  const recalc = useRecalc()
  const v = q.view

  const weeks = useMemo(() => (v ? weeklyFlows(v.r.planned) : []), [v])
  const plannedByDate = useMemo(() => {
    const m = new Map<string, Array<{ title: string; amount: string }>>()
    v?.r.planned.forEach((p) => m.set(p.date, [...(m.get(p.date) ?? []), p]))
    return m
  }, [v])

  return (
    <PageContainer>
      <PageHeader
        title={t('forecast.title')}
        description={t('forecast.description')}
        actions={
          v && (
            <div className="w-full sm:w-40">
              <Select
                aria-label={t('forecast.horizonAria')}
                value={v.r.horizon}
                disabled={recalc.isPending}
                onChange={(h) => recalc.mutate(Number(h))}
                options={HORIZONS.map((d) => ({ value: d, label: t('common.daysCount', { count: d }) }))}
              />
            </div>
          )
        }
      />
      {recalc.isError && <Notice tone="danger">{errorText(t, recalc.error)}</Notice>}
      {recalc.isPending && <Notice>{t('forecast.recalculating')}</Notice>}

      {q.isPending && <div className="card"><LoadingState height={420} /></div>}
      {q.isError && <div className="card"><ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} /></div>}
      {!q.isPending && !q.isError && !v && (
        <div className="card">
          <EmptyState
            title={t('forecast.unavailableTitle')}
            description={t('forecast.unavailableText')}
            action={<Link to="/settings" className="text-primary font-medium hover:underline text-sm">{t('forecast.goToImport')}</Link>}
          />
        </div>
      )}

      {v && (() => {
        const { r, run } = v
        const columns: Column<ForecastDay>[] = [
          { key: 'date', header: t('forecast.col.date'), render: (d) => <span className="whitespace-nowrap">{formatDate(d.date, 'weekdayDayMonth')}</span>, sortValue: (d) => d.date },
          {
            key: 'bal', header: t('forecast.col.balance'), numeric: true, sortValue: (d) => kopToRub(d.p50),
            render: (d) => (
              <span className={BigInt(d.p50) < 0n ? 'text-danger font-semibold inline-flex items-center gap-1.5' : ''}>
                {BigInt(d.p50) < 0n && <AlertTriangle size={14} aria-label={t('chart.deficit')} />}
                {formatKop(d.p50)}
              </span>
            ),
          },
          { key: 'range', header: t('forecast.col.range'), numeric: true, render: (d) => `${formatCompact(kopToRub(d.p10))} … ${formatCompact(kopToRub(d.p90))}` },
          {
            key: 'prob', header: t('forecast.col.probNegative'), numeric: true, sortValue: (d) => d.prob_negative,
            render: (d) => (
              <span className="inline-flex items-center gap-2 justify-end">
                {d.prob_negative >= 0.25 && <Badge tone="danger">{t('forecast.riskBadge')}</Badge>}
                {formatPercent(d.prob_negative)}
              </span>
            ),
          },
          {
            key: 'items', header: t('forecast.col.payments'),
            render: (d) => {
              const items = plannedByDate.get(d.date) ?? []
              return items.length === 0 ? (
                <span className="text-muted">—</span>
              ) : (
                <ul className="flex flex-col gap-0.5 min-w-[220px]">
                  {items.map((p, i) => (
                    <li key={i} className="flex justify-between gap-3 text-[13px]">
                      <span className="truncate">{p.title}</span>
                      <span className={'tnum shrink-0 ' + (BigInt(p.amount) < 0n ? '' : 'text-primary')}>{formatKop(p.amount, { sign: true })}</span>
                    </li>
                  ))}
                </ul>
              )
            },
          },
        ]
        const ruleCols: Column<Rule>[] = [
          { key: 'cp', header: t('forecast.rules.counterparty'), sortValue: (x) => x.counterparty || x.key, render: (x) => <span className="font-medium">{x.counterparty || x.key}</span> },
          { key: 'cat', header: t('forecast.rules.category'), render: (x) => categoryLabel(t, x.category) },
          {
            key: 'per', header: t('forecast.rules.period'),
            render: (x) => (x.kind === 'monthly' ? t('forecast.rules.monthly', { days: x.anchors.join(', ') }) : t('forecast.rules.weekly')),
          },
          { key: 'amt', header: t('forecast.rules.amount'), numeric: true, sortValue: (x) => kopToRub(x.amount), render: (x) => <span className={BigInt(x.amount) < 0n ? '' : 'text-primary font-semibold'}>{formatKop(x.amount, { sign: true })}</span> },
          { key: 'occ', header: t('forecast.rules.occurrences'), numeric: true, sortValue: (x) => x.occurrences, render: (x) => x.occurrences },
        ]
        return (
          <>
            <ForecastBanners run={run} />
            <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
              <Card title={t('forecast.cardTitle')} subtitle={t('common.updated', { value: formatDateTime(run.created_at) })}>
                <div className="px-3 pb-4 sm:px-5">
                  <ForecastChart points={v.points} today={r.as_of} shortageDate={r.risk.level === 'none' ? null : v.gapDate} height={440} />
                  <p className="text-xs text-muted mt-2">{t('forecast.caption')} {t('forecast.captionBand')}</p>
                </div>
              </Card>
              <RiskAlert risk={r.risk} />
            </div>

            <Card title={t('forecast.flowsTitle')} subtitle={t('forecast.flowsSubtitle')}>
              <div className="px-3 pb-4 sm:px-5">
                {weeks.length === 0 ? (
                  <p className="text-sm text-muted px-2">{t('forecast.flowsEmpty')}</p>
                ) : (
                  <BarChart
                    weekPrefix
                    ariaLabel={t('forecast.flowsAria')}
                    categories={weeks.map((w) => w.week)}
                    height={280}
                    series={[
                      { name: t('forecast.flowsIn'), token: 'primary', data: weeks.map((w) => kopToRub(w.in)) },
                      { name: t('forecast.flowsOut'), token: 'danger', data: weeks.map((w) => -kopToRub(w.out)) },
                    ]}
                  />
                )}
              </div>
            </Card>

            <Card title={t('forecast.tableTitle')} subtitle={t('forecast.tableSubtitle')}>
              <div className="max-h-[520px] overflow-y-auto">
                <DataTable
                  columns={columns}
                  rows={r.days}
                  getRowKey={(d) => d.date}
                  defaultSort={{ key: 'date', dir: 'asc' }}
                  rowClassName={(d) => (BigInt(d.p50) < 0n || d.prob_negative >= 0.25 ? 'bg-danger-soft' : undefined)}
                />
              </div>
            </Card>

            <BacktestCard />

            <Card title={t('forecast.rules.title', { count: r.rules.length })} subtitle={t('forecast.rules.subtitle')}>
              {r.rules.length === 0 ? (
                <p className="px-5 pb-5 text-sm text-muted">{t('forecast.rules.empty')}</p>
              ) : (
                <DataTable columns={ruleCols} rows={r.rules} getRowKey={(x) => x.key} />
              )}
              <p className="px-5 py-3 text-xs text-muted border-t border-line">
                {t('forecast.modelLine', { version: r.model_version, paths: r.n_paths, seed: r.seed, level: t(`quality.level.${r.quality.level}`, { defaultValue: r.quality.level }), days: r.quality.history_days })}
              </p>
            </Card>
          </>
        )
      })()}
    </PageContainer>
  )
}
