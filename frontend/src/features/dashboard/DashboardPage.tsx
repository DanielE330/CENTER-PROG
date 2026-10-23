import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, ArrowDownToLine, ArrowUpFromLine, Clock, FileUp, ShieldAlert, TrendingDown, Wallet } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { StatCard } from '../../components/shared/StatCard'
import { RiskAlert } from '../../components/shared/RiskAlert'
import { MockBadge } from '../../components/shared/StatusBadge'
import { DataQuality } from '../../components/shared/DataQuality'
import { QueryBoundary } from '../../components/shared/QueryBoundary'
import { EmptyState } from '../../components/shared/States'
import { Button } from '../../components/ui/Button'
import { Modal } from '../../components/ui/Modal'
import { Select } from '../../components/ui/Select'
import { useForecast, useObligations } from '../../hooks/queries'
import { formatCurrency, formatDate, formatDateTime } from '../../lib/formatters'
import { ForecastChart } from '../forecast/ForecastChart'
import { sliceWindow } from '../forecast/window'
import { ImportStatement } from '../settings/ImportStatement'
import { ObligationList } from './ObligationList'
import type { ForecastResponse } from '../../types'

export default function DashboardPage() {
  const { t } = useTranslation()
  const forecast = useForecast()
  const obligations = useObligations()
  const [days, setDays] = useState(30)
  const [importOpen, setImportOpen] = useState(false)

  return (
    <PageContainer>
      <PageHeader
        title={t('dashboard.title')}
        description={
          forecast.data?.risk.shortageDate
            ? t('dashboard.descShortage', { days, date: formatDate(forecast.data.risk.shortageDate) })
            : t('dashboard.descDefault')
        }
        actions={
          <>
            <div className="w-full sm:w-36">
              <Select aria-label={t('dashboard.periodAria')} value={days} onChange={(v) => setDays(Number(v))} options={[14, 30].map((d) => ({ value: d, label: t('common.daysCount', { count: d }) }))} />
            </div>
            <Button variant="primary" onClick={() => setImportOpen(true)}>
              <FileUp size={16} /> {t('dashboard.import')}
            </Button>
          </>
        }
      />

      <QueryBoundary query={forecast} loadingHeight={420}>
        {(f) => <DashboardBody f={f} days={days} obligations={obligations} />}
      </QueryBoundary>

      <Modal open={importOpen} onClose={() => setImportOpen(false)} title={t('import.modalTitle')}>
        <ImportStatement />
      </Modal>
    </PageContainer>
  )
}

function DashboardBody({ f, days, obligations }: { f: ForecastResponse; days: number; obligations: ReturnType<typeof useObligations> }) {
  const { t } = useTranslation()
  const { summary, risk, quality } = f
  const noData = f.points.length === 0
  const noBalance = summary.currentBalance == null
  const deficit = summary.minBalance != null && summary.minBalance < 0

  const chartPoints = sliceWindow(f.points, f.today, 14, days)

  return (
    <>
      {quality.completeness < 80 && !noData && (
        <div className="flex items-start gap-2 rounded-card border border-warning/40 bg-warning-soft p-4 text-sm" role="alert">
          <AlertTriangle size={18} className="text-warning shrink-0 mt-0.5" />
          <div>
            <p className="font-medium">{t('dashboard.incompleteTitle')}</p>
            <p className="text-muted">{t('dashboard.incompleteText')}</p>
          </div>
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={<Wallet size={16} />}
          label={t('dashboard.kpi.balance')}
          value={noBalance ? t('common.noData') : formatCurrency(summary.currentBalance)}
          hint={noBalance ? t('dashboard.kpi.balanceUnknown') : t('dashboard.kpi.asOf', { date: formatDate(f.today) })}
        />
        <StatCard
          icon={<TrendingDown size={16} />}
          label={t('dashboard.kpi.minBalance')}
          value={summary.minBalance == null ? t('common.noData') : formatCurrency(summary.minBalance)}
          tone={deficit ? 'danger' : 'default'}
          hint={
            summary.minBalanceDate
              ? t(deficit ? 'dashboard.kpi.minDeficit' : 'dashboard.kpi.minForecast', { date: formatDate(summary.minBalanceDate) })
              : t('dashboard.kpi.forecastNotBuilt')
          }
        />
        <StatCard
          icon={<ArrowDownToLine size={16} />}
          label={t('dashboard.kpi.inflows')}
          value={noData ? t('common.noData') : formatCurrency(summary.expectedInflows)}
          hint={noData ? undefined : t('dashboard.kpi.inflowsHint', { days: f.horizonDays })}
        />
        <StatCard
          icon={<ShieldAlert size={16} />}
          label={t('dashboard.kpi.risk')}
          value={t(`dashboard.kpi.risk_${risk.level}`)}
          tone={risk.level === 'high' ? 'danger' : risk.level === 'medium' ? 'warning' : 'default'}
          hint={risk.shortageDate ? t('dashboard.kpi.riskShortage', { date: formatDate(risk.shortageDate) }) : risk.level === 'unknown' ? t('dashboard.kpi.riskUnknown') : t('dashboard.kpi.riskNone')}
        />
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-5 min-w-0">
          <Card
            title={t('dashboard.chartTitle')}
            subtitle={t('dashboard.chartSubtitle', { days })}
            action={<MockBadge />}
          >
            <div className="px-3 pb-4 sm:px-5">
              {noData ? (
                <EmptyState title={t('dashboard.noChartTitle')} description={t('dashboard.noChartText')} />
              ) : (
                <>
                  {summary.currentBalance == null && (
                    <p className="text-[13px] text-warning mb-2">{t('dashboard.noBalanceChart')}</p>
                  )}
                  <ForecastChart points={chartPoints} today={f.today} shortageDate={risk.shortageDate} height={360} />
                </>
              )}
            </div>
          </Card>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card title={t('dashboard.upcomingPayments')} action={<Link to="/obligations" className="text-[13px] text-primary font-medium hover:underline">{t('common.all')}</Link>}>
              <QueryBoundary query={obligations} loadingHeight={160}>
                {(list) => <ObligationList items={list} kind="payment" icon={<ArrowUpFromLine size={15} />} />}
              </QueryBoundary>
            </Card>
            <Card title={t('dashboard.expectedReceipts')} action={<Link to="/obligations" className="text-[13px] text-primary font-medium hover:underline">{t('common.all')}</Link>}>
              <QueryBoundary query={obligations} loadingHeight={160}>
                {(list) => <ObligationList items={list} kind="receipt" icon={<ArrowDownToLine size={15} />} />}
              </QueryBoundary>
            </Card>
          </div>
        </div>

        <div className="flex flex-col gap-5 min-w-0">
          <RiskAlert risk={risk} />
          <Card title={t('dashboard.dataState')}>
            <div className="px-5 pb-5 flex flex-col gap-4">
              <DataQuality quality={quality} />
              <p className="flex items-center gap-1.5 text-[13px] text-muted">
                <Clock size={14} />
                {t('common.updated', { value: formatDateTime(f.generatedAt) })}
              </p>
              <p className="text-xs text-muted">
                {t('dashboard.mockNote', { days: f.horizonDays })}
              </p>
            </div>
          </Card>
        </div>
      </div>
    </>
  )
}
