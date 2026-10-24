import { useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import { GitCompareArrows } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { Badge } from '../../components/shared/StatusBadge'
import type { Tone } from '../../components/shared/StatusBadge'
import { Notice } from '../../components/shared/Notice'
import { BarChart } from '../../components/shared/BarChart'
import { Select } from '../../components/ui/Select'
import { useBacktest, usePlanFact } from '../../hooks/queries'
import { formatAxisNumber, formatCurrency, formatDate, formatKop, formatShortDate } from '../../lib/formatters'
import { cssVar, prefersReducedMotion } from '../../lib/utils'
import { kopToRub, subKop, sumKop } from '../../lib/money'
import { isNoData } from '../../lib/errors'
import { errorText } from '../../i18n/helpers'
import { useTheme } from '../../lib/theme'
import type { Backtest, Kop, PlanFact, PlanFactItem } from '../../types'

/** Direction matters: for receipts a shortfall is bad, for payments an overrun is bad. All math in BigInt kopecks. */
function Variance({ planned, actual, kind }: { planned: Kop; actual: Kop; kind: 'in' | 'out' }) {
  const { t } = useTranslation()
  const diff = BigInt(subKop(actual, planned))
  if (diff === 0n) return <Badge>{t('planVsActual.noVariance')}</Badge>
  const p = BigInt(planned)
  const base = p < 0n ? -p : p
  const pct = base === 0n ? 100 : Number((diff < 0n ? -diff : diff) * 1000n / base) / 10
  const bad = kind === 'out' ? diff > 0n : diff < 0n
  const small = pct < 5
  return (
    <span className="inline-flex flex-col items-end">
      <span className={`tnum font-medium ${small ? '' : bad ? 'text-danger' : 'text-primary'}`}>{formatKop(diff.toString(), { sign: true })}</span>
      <span className="text-xs text-muted">
        {small ? t('planVsActual.within') : `${bad ? t('planVsActual.worse') : t('planVsActual.better')} · ${Math.round(pct)}%`}
      </span>
    </span>
  )
}

const statusTone: Record<string, Tone> = { paid: 'success', deviation: 'warning', missed: 'danger', pending: 'neutral' }

export default function PlanVsActualPage() {
  const { t } = useTranslation()
  const [days, setDays] = useState(90)
  const q = usePlanFact(days)
  const bt = useBacktest(true)

  return (
    <PageContainer>
      <PageHeader
        title={t('planVsActual.title')}
        description={t('planVsActual.description')}
        actions={
          <div className="w-full sm:w-40">
            <Select aria-label={t('planVsActual.periodAria')} value={days} onChange={(v) => setDays(Number(v))} options={[30, 60, 90].map((d) => ({ value: d, label: t('common.daysCount', { count: d }) }))} />
          </div>
        }
      />
      {q.isPending && <div className="card"><LoadingState height={360} /></div>}
      {q.isError && !isNoData(q.error) && <div className="card"><ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} /></div>}
      {q.isError && isNoData(q.error) && <NoData />}
      {q.data && <Body d={q.data} bt={bt} />}
    </PageContainer>
  )
}

function NoData() {
  const { t } = useTranslation()
  return (
    <div className="card">
      <EmptyState
        icon={<GitCompareArrows size={22} aria-hidden />}
        title={t('planVsActual.noActualTitle')}
        description={t('planVsActual.noActualText')}
        action={<Link to="/settings" className="text-primary font-medium hover:underline text-sm">{t('forecast.goToImport')}</Link>}
      />
    </div>
  )
}

function Body({ d, bt }: { d: PlanFact; bt: ReturnType<typeof useBacktest> }) {
  const { t } = useTranslation()
  const pIn = sumKop(d.weeks.map((w) => w.plan_in))
  const fIn = sumKop(d.weeks.map((w) => w.fact_in))
  const pOut = sumKop(d.weeks.map((w) => w.plan_out))
  const fOut = sumKop(d.weeks.map((w) => w.fact_out))

  const columns: Column<PlanFactItem>[] = [
    { key: 'date', header: t('planVsActual.col.date'), sortValue: (i) => i.date, render: (i) => <span className="whitespace-nowrap">{formatDate(i.date, 'dayMonth')}</span> },
    { key: 'title', header: t('planVsActual.col.payment'), sortValue: (i) => i.title, render: (i) => <span className="font-medium min-w-[180px] inline-block">{i.title}</span> },
    { key: 'plan', header: t('planVsActual.plan'), numeric: true, sortValue: (i) => kopToRub(i.plan), render: (i) => formatKop(i.plan, { sign: true }) },
    {
      key: 'fact', header: t('planVsActual.actual'), numeric: true,
      render: (i) => i.fact ? (
        <span className="inline-flex flex-col items-end">
          {formatKop(i.fact, { sign: true })}
          {i.fact_date && <span className="text-xs text-muted">{formatShortDate(i.fact_date)}</span>}
        </span>
      ) : '—',
    },
    {
      key: 'diff', header: t('planVsActual.col.variance'), numeric: true,
      render: (i) => {
        if (i.diff == null) return '—'
        const bad = i.diff !== '0' && statusTone[i.status] !== 'success'
        return <span className={bad ? 'text-danger' : ''}>{formatKop(i.diff, { sign: true })}</span>
      },
    },
    { key: 'status', header: t('planVsActual.col.status'), sortValue: (i) => i.status, render: (i) => <Badge tone={statusTone[i.status] ?? 'neutral'}>{t(`planVsActual.status.${i.status}`, { defaultValue: i.status })}</Badge> },
  ]

  return (
    <>
      {d.missed > 0 && (
        <Notice tone="warning" title={t('planVsActual.missedTitle', { count: d.missed })}>{t('planVsActual.missedText')}</Notice>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Summary title={t('planVsActual.receipts')} planned={pIn} actual={fIn} kind="in" />
        <Summary title={t('planVsActual.payments')} planned={pOut} actual={fOut} kind="out" />
      </div>

      <Card title={t('planVsActual.weeklyTitle')} subtitle={t('planVsActual.weeklySubtitle', { date: formatDate(d.as_of), days: d.days ?? 90 })}>
        <div className="px-3 pb-4 sm:px-5">
          {d.weeks.length === 0 ? (
            <EmptyState title={t('planVsActual.noWeeks')} />
          ) : (
            <BarChart
              weekPrefix
              ariaLabel={t('planVsActual.weeklyAria')}
              categories={d.weeks.map((w) => w.week_start)}
              height={320}
              series={[
                { name: t('planVsActual.planIn'), token: 'primary', soft: true, data: d.weeks.map((w) => kopToRub(w.plan_in)) },
                { name: t('planVsActual.factIn'), token: 'primary', data: d.weeks.map((w) => kopToRub(w.fact_in)) },
                { name: t('planVsActual.planOut'), token: 'warning', soft: true, data: d.weeks.map((w) => -kopToRub(w.plan_out)) },
                { name: t('planVsActual.factOut'), token: 'warning', data: d.weeks.map((w) => -kopToRub(w.fact_out)) },
              ]}
            />
          )}
          <p className="text-xs text-muted mt-2">{t('planVsActual.planNote')}</p>
        </div>
      </Card>

      <Card title={t('planVsActual.balanceTitle')} subtitle={t('planVsActual.balanceSubtitle')}>
        <div className="px-3 pb-4 sm:px-5">
          {bt.isPending ? (
            <LoadingState height={200} label={t('planVsActual.balanceLoading')} />
          ) : bt.data?.series_14?.length ? (
            <>
              <BalanceChart series={bt.data.series_14} />
            </>
          ) : (
            <EmptyState title={t('planVsActual.balanceNA')} description={bt.isError && isNoData(bt.error) ? t('planVsActual.balanceNoHistory') : bt.isError ? errorText(t, bt.error) : t('planVsActual.balanceNoSeries')} />
          )}
        </div>
      </Card>

      <Card title={t('planVsActual.detailTitle')}>
        <DataTable columns={columns} rows={[...d.items].reverse()} getRowKey={(i) => `${i.date}-${i.title}-${i.plan}`} rowClassName={(i) => (i.status === 'missed' ? 'bg-danger-soft' : undefined)} />
      </Card>
    </>
  )
}

function Summary({ title, planned, actual, kind }: { title: string; planned: Kop; actual: Kop; kind: 'in' | 'out' }) {
  const { t } = useTranslation()
  return (
    <div className="card enter p-5 flex flex-col gap-2 min-w-0">
      <p className="text-[13px] text-muted">{title}</p>
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <p className="text-xs text-muted">{t('planVsActual.plan')}</p>
          <p className="tnum text-lg font-semibold">{formatKop(planned)}</p>
        </div>
        <div className="text-right">
          <p className="text-xs text-muted">{t('planVsActual.actual')}</p>
          <p className="tnum text-lg font-semibold">{formatKop(actual)}</p>
        </div>
      </div>
      <div className="flex justify-end border-t border-line pt-2">
        <Variance planned={planned} actual={actual} kind={kind} />
      </div>
    </div>
  )
}

function BalanceChart({ series }: { series: NonNullable<Backtest['series_14']> }) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const theme = useTheme()
  const option = useMemo<EChartsOption>(() => {
    const primary = cssVar('--c-primary', '#176B45')
    const neutral = cssVar('--c-neutral-series', '#9AA59E')
    const muted = cssVar('--c-muted', '#66716A')
    const line = cssVar('--c-line', '#E2E8E3')
    const surface = cssVar('--c-surface', '#FFFFFF')
    const ink = cssVar('--c-ink', '#202923')
    return {
      animation: !prefersReducedMotion(),
      animationDuration: 600,
      animationDurationUpdate: 450,
      animationEasing: 'cubicOut',
      grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
      legend: { top: 0, left: 0, icon: 'roundRect', itemWidth: 14, itemHeight: 3, textStyle: { color: muted, fontSize: 12 } },
      xAxis: { type: 'category', data: series.map((r) => r.date), boundaryGap: false, axisTick: { show: false }, axisLine: { lineStyle: { color: line } }, axisLabel: { color: muted, fontSize: 12, hideOverlap: true, formatter: (v: string) => formatShortDate(v) } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: muted, fontSize: 12, formatter: (v: number) => formatAxisNumber(v) }, splitLine: { lineStyle: { color: line } } },
      tooltip: { trigger: 'axis', confine: true, backgroundColor: surface, borderColor: line, textStyle: { color: ink }, valueFormatter: (v) => formatCurrency(v as number) },
      series: [
        { id: 'plan', name: t('planVsActual.forecastPlan'), type: 'line', symbol: 'none', lineStyle: { color: primary, width: 2.5, type: 'dashed' }, itemStyle: { color: primary }, data: series.map((r) => kopToRub(r.predicted)) },
        { id: 'actual', name: t('planVsActual.actual'), type: 'line', symbol: 'none', lineStyle: { color: neutral, width: 2.5 }, itemStyle: { color: neutral }, data: series.map((r) => kopToRub(r.actual)) },
      ],
    }
  }, [series, theme, lang, t])
  return (
    <div role="img" aria-label={t('planVsActual.chartAria')} style={{ height: 320 }}>
      <ReactECharts option={option} style={{ height: '100%', width: '100%' }} />
    </div>
  )
}
