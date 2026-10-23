import { useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import { GitCompareArrows } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { QueryBoundary } from '../../components/shared/QueryBoundary'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState } from '../../components/shared/States'
import { Badge, MockBadge } from '../../components/shared/StatusBadge'
import { Select } from '../../components/ui/Select'
import { usePlanActual } from '../../hooks/queries'
import { formatAxisNumber, formatCurrency, formatDate, formatShortDate, formatSignedCurrency } from '../../lib/formatters'
import { cssVar } from '../../lib/utils'
import { useTheme } from '../../lib/theme'
import type { PlanActualRow } from '../../types'

/** Direction matters: for receipts a shortfall is bad, for payments an overrun is bad. */
function Variance({ planned, actual, kind }: { planned: number; actual: number; kind: 'in' | 'out' | 'balance' }) {
  const { t } = useTranslation()
  const diff = actual - planned
  if (diff === 0) return <Badge>{t('planVsActual.noVariance')}</Badge>
  const pct = planned !== 0 ? Math.abs(diff / planned) : 1
  const bad = kind === 'out' ? diff > 0 : diff < 0
  const small = pct < 0.05
  return (
    <span className="inline-flex flex-col items-end">
      <span className={`tnum font-medium ${small ? '' : bad ? 'text-danger' : 'text-primary'}`}>{formatSignedCurrency(diff)}</span>
      <span className="text-xs text-muted">
        {small ? t('planVsActual.within') : bad ? t('planVsActual.worse') : t('planVsActual.better')} · {Math.round(pct * 100)}%
      </span>
    </span>
  )
}

export default function PlanVsActualPage() {
  const { t } = useTranslation()
  const [days, setDays] = useState(14)
  const query = usePlanActual(days)

  return (
    <PageContainer>
      <PageHeader
        title={t('planVsActual.title')}
        description={t('planVsActual.description')}
        actions={
          <>
            <MockBadge />
            <div className="w-full sm:w-36">
              <Select aria-label={t('planVsActual.periodAria')} value={days} onChange={(v) => setDays(Number(v))} options={[7, 14, 30].map((d) => ({ value: d, label: t('common.daysCount', { count: d }) }))} />
            </div>
          </>
        }
      />
      <QueryBoundary query={query} loadingHeight={360}>
        {(data) => {
          if (!data.available || data.rows.length === 0) {
            return (
              <div className="card">
                <EmptyState
                  icon={<GitCompareArrows size={22} />}
                  title={t('planVsActual.noActualTitle')}
                  description={t('planVsActual.noActualText')}
                />
              </div>
            )
          }
          return <Body rows={data.rows} />
        }}
      </QueryBoundary>
    </PageContainer>
  )
}

function Body({ rows }: { rows: PlanActualRow[] }) {
  const { t } = useTranslation()
  const sum = (k: keyof PlanActualRow) => rows.reduce((s, r) => s + (r[k] as number), 0)
  const last = rows[rows.length - 1]

  const columns: Column<PlanActualRow>[] = [
    { key: 'date', header: t('planVsActual.col.date'), sortValue: (r) => r.date, render: (r) => formatDate(r.date, 'dayMonth') },
    { key: 'pin', header: t('planVsActual.col.plannedIn'), numeric: true, render: (r) => formatCurrency(r.plannedIn) },
    { key: 'ain', header: t('planVsActual.actual'), numeric: true, render: (r) => formatCurrency(r.actualIn) },
    { key: 'vin', header: t('planVsActual.col.variance'), numeric: true, render: (r) => <Variance planned={r.plannedIn} actual={r.actualIn} kind="in" /> },
    { key: 'pout', header: t('planVsActual.col.plannedOut'), numeric: true, render: (r) => formatCurrency(r.plannedOut) },
    { key: 'aout', header: t('planVsActual.actual'), numeric: true, render: (r) => formatCurrency(r.actualOut) },
    { key: 'vout', header: t('planVsActual.col.variance'), numeric: true, render: (r) => <Variance planned={r.plannedOut} actual={r.actualOut} kind="out" /> },
  ]

  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <Summary title={t('planVsActual.receipts')} planned={sum('plannedIn')} actual={sum('actualIn')} kind="in" />
        <Summary title={t('planVsActual.payments')} planned={sum('plannedOut')} actual={sum('actualOut')} kind="out" />
        <Summary title={t('planVsActual.endBalance')} planned={last.plannedBalance} actual={last.actualBalance} kind="balance" />
      </div>

      <Card title={t('planVsActual.balanceTitle')}>
        <div className="px-3 pb-4 sm:px-5">
          <BalanceChart rows={rows} />
        </div>
      </Card>

      <Card title={t('planVsActual.detailTitle')} subtitle={t('planVsActual.detailSubtitle')}>
        <DataTable columns={columns} rows={rows} getRowKey={(r) => r.date} defaultSort={{ key: 'date', dir: 'desc' }} />
      </Card>
    </>
  )
}

function Summary({ title, planned, actual, kind }: { title: string; planned: number; actual: number; kind: 'in' | 'out' | 'balance' }) {
  const { t } = useTranslation()
  return (
    <div className="card p-5 flex flex-col gap-2 min-w-0">
      <p className="text-[13px] text-muted">{title}</p>
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <p className="text-xs text-muted">{t('planVsActual.plan')}</p>
          <p className="tnum text-lg font-semibold">{formatCurrency(planned)}</p>
        </div>
        <div className="text-right">
          <p className="text-xs text-muted">{t('planVsActual.actual')}</p>
          <p className="tnum text-lg font-semibold">{formatCurrency(actual)}</p>
        </div>
      </div>
      <div className="flex justify-end border-t border-line pt-2">
        <Variance planned={planned} actual={actual} kind={kind} />
      </div>
    </div>
  )
}

function BalanceChart({ rows }: { rows: PlanActualRow[] }) {
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
      animation: false,
      grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
      legend: { top: 0, left: 0, icon: 'roundRect', itemWidth: 14, itemHeight: 3, textStyle: { color: muted, fontSize: 12 } },
      xAxis: { type: 'category', data: rows.map((r) => r.date), boundaryGap: false, axisTick: { show: false }, axisLine: { lineStyle: { color: line } }, axisLabel: { color: muted, fontSize: 12, hideOverlap: true, formatter: (v: string) => formatShortDate(v) } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: muted, fontSize: 12, formatter: (v: number) => formatAxisNumber(v) }, splitLine: { lineStyle: { color: line } } },
      tooltip: { trigger: 'axis', confine: true, backgroundColor: surface, borderColor: line, textStyle: { color: ink }, valueFormatter: (v) => formatCurrency(v as number) },
      series: [
        { name: t('planVsActual.forecastPlan'), type: 'line', symbol: 'none', lineStyle: { color: primary, width: 2.5, type: 'dashed' }, itemStyle: { color: primary }, data: rows.map((r) => r.plannedBalance) },
        { name: t('planVsActual.actual'), type: 'line', symbol: 'none', lineStyle: { color: neutral, width: 2.5 }, itemStyle: { color: neutral }, data: rows.map((r) => r.actualBalance) },
      ],
    }
  }, [rows, theme, lang, t])
  return (
    <div role="img" aria-label={t('planVsActual.chartAria')} style={{ height: 320 }}>
      <ReactECharts option={option} notMerge style={{ height: '100%', width: '100%' }} />
    </div>
  )
}
