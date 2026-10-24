import { useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { QueryBoundary } from '../../components/shared/QueryBoundary'
import { RiskAlert } from '../../components/shared/RiskAlert'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { EmptyState } from '../../components/shared/States'
import { MockBadge } from '../../components/shared/StatusBadge'
import { Select } from '../../components/ui/Select'
import { useForecast } from '../../hooks/queries'
import { formatCompact, formatCurrency, formatDate, formatDateTime } from '../../lib/formatters'
import type { ForecastPoint } from '../../types'
import { ForecastChart } from './ForecastChart'
import { sliceWindow } from './window'

export default function ForecastPage() {
  const { t } = useTranslation()
  const query = useForecast()
  const [days, setDays] = useState(30)

  return (
    <PageContainer>
      <PageHeader
        title={t('forecast.title')}
        description={t('forecast.description')}
        actions={
          <div className="w-full sm:w-36">
            <Select aria-label={t('forecast.horizonAria')} value={days} onChange={(v) => setDays(Number(v))} options={[7, 14, 30].map((d) => ({ value: d, label: t('common.daysCount', { count: d }) }))} />
          </div>
        }
      />
      <QueryBoundary query={query} loadingHeight={420}>
        {(f) => {
          if (f.points.length === 0) {
            return (
              <div className="card">
                <EmptyState title={t('forecast.unavailableTitle')} description={t('forecast.unavailableText')} />
              </div>
            )
          }
          const rows = sliceWindow(f.points, f.today, 0, days).filter((p) => p.date > f.today)
          const hasBand = rows.some((p) => p.lower != null)
          const columns: Column<ForecastPoint>[] = [
            { key: 'date', header: t('forecast.col.date'), render: (p) => formatDate(p.date, 'weekdayDayMonth'), sortValue: (p) => p.date },
            { key: 'in', header: t('forecast.col.inflow'), numeric: true, render: (p) => (p.inflow ? formatCurrency(p.inflow) : '—'), sortValue: (p) => p.inflow },
            { key: 'out', header: t('forecast.col.outflow'), numeric: true, render: (p) => (p.outflow ? formatCurrency(p.outflow) : '—'), sortValue: (p) => p.outflow },
            {
              key: 'bal',
              header: t('forecast.col.balance'),
              numeric: true,
              sortValue: (p) => p.forecast ?? 0,
              render: (p) =>
                p.forecast == null ? (
                  <span className="text-muted">{t('common.noDataLower')}</span>
                ) : (
                  <span className={p.forecast < 0 ? 'text-danger font-semibold inline-flex items-center gap-1.5' : ''}>
                    {p.forecast < 0 && <AlertTriangle size={14} aria-label={t('chart.deficit')} />}
                    {formatCurrency(p.forecast)}
                  </span>
                ),
            },
            ...(hasBand
              ? [{
                  key: 'range',
                  header: t('forecast.col.range'),
                  numeric: true,
                  render: (p: ForecastPoint) => (p.lower != null && p.upper != null ? `${formatCompact(p.lower)} … ${formatCompact(p.upper)}` : '—'),
                }]
              : []),
          ]
          return (
            <>
              <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
                <Card title={t('forecast.cardTitle')} subtitle={t('common.updated', { value: formatDateTime(f.generatedAt) })} action={<MockBadge />}>
                  <div className="px-3 pb-4 sm:px-5">
                    <ForecastChart points={sliceWindow(f.points, f.today, 14, days)} today={f.today} shortageDate={f.risk.shortageDate} height={440} />
                    <p className="text-xs text-muted mt-2">
                      {t('forecast.caption')}
                      {hasBand && ` ${t('forecast.captionBand')}`}
                    </p>
                  </div>
                </Card>
                <RiskAlert risk={f.risk} />
              </div>

              <Card title={t('forecast.tableTitle')} subtitle={t('forecast.tableSubtitle')}>
                <DataTable
                  columns={columns}
                  rows={rows}
                  getRowKey={(p) => p.date}
                  defaultSort={{ key: 'date', dir: 'asc' }}
                  rowClassName={(p) => (p.forecast != null && p.forecast < 0 ? 'bg-danger-soft' : undefined)}
                />
              </Card>
            </>
          )
        }}
      </QueryBoundary>
    </PageContainer>
  )
}
