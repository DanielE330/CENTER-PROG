import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ArrowDownToLine, ArrowUpFromLine, Clock, FileUp, RefreshCw, ShieldAlert, TrendingDown, Wallet } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { StatCard } from '../../components/shared/StatCard'
import { RiskAlert } from '../../components/shared/RiskAlert'
import { DataQuality } from '../../components/shared/DataQuality'
import { ErrorState, EmptyState, LoadingState } from '../../components/shared/States'
import { ForecastBanners } from '../../components/shared/ForecastBanners'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Modal } from '../../components/ui/Modal'
import { Select } from '../../components/ui/Select'
import { useForecast, useRecalc } from '../../hooks/queries'
import { useEditGuard } from '../../app/perms'
import { formatDate, formatDateTime, formatKop, formatPercent } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import { loadPrefs } from '../../lib/prefs'
import { ForecastChart } from '../forecast/ForecastChart'
import { sliceWindow } from '../forecast/window'
import type { ForecastView } from '../forecast/model'
import { ImportStatement } from '../settings/ImportStatement'
import { PlannedList } from './PlannedList'
import { KeyRateCard } from './KeyRateCard'
import { DemoWeekCard } from './DemoWeekCard'

const WINDOWS = [7, 14, 30, 60, 90]

export default function DashboardPage() {
  const { t } = useTranslation()
  const forecast = useForecast()
  const recalc = useRecalc()
  const guard = useEditGuard()
  const [days, setDays] = useState<number | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const view = forecast.view
  const horizon = view?.r.horizon ?? 90
  const shown = Math.min(days ?? 30, horizon)
  const windows = WINDOWS.filter((d) => d <= horizon)

  return (
    <PageContainer>
      <PageHeader
        title={t('dashboard.title')}
        description={t('dashboard.descDefault')}
        actions={
          <>
            {view && windows.length > 1 && (
              <div className="w-full sm:w-36">
                <Select aria-label={t('dashboard.periodAria')} value={shown} onChange={(v) => setDays(Number(v))} options={windows.map((d) => ({ value: d, label: t('common.daysCount', { count: d }) }))} />
              </div>
            )}
            {view && !view.run.stale && (
              <Button disabled={recalc.isPending} onClick={() => recalc.mutate(horizon)}>
                <RefreshCw size={16} className={recalc.isPending ? 'spin' : undefined} aria-hidden />
                {recalc.isPending ? t('common.calculating') : t('common.recalculate')}
              </Button>
            )}
            {/* without a forecast the empty state below offers the same import button */}
            {(view || forecast.isPending || forecast.isError) && (
              <Button variant="primary" onClick={() => setImportOpen(true)} {...guard.lock}>
                <FileUp size={16} aria-hidden /> {t('dashboard.import')}
              </Button>
            )}
          </>
        }
      />

      <DemoWeekCard />

      {recalc.isError && <Notice tone="danger">{errorText(t, recalc.error)}</Notice>}

      {forecast.isPending && <div className="card"><LoadingState height={420} /></div>}
      {forecast.isError && (
        <div className="card"><ErrorState message={errorText(t, forecast.error)} onRetry={() => void forecast.refetch()} /></div>
      )}
      {!forecast.isPending && !forecast.isError && !view && (
        <div className="card">
          <EmptyState
            title={t('dashboard.emptyTitle')}
            description={t('dashboard.emptyText')}
            action={
              <div className="flex flex-wrap justify-center gap-2">
                <Button variant="primary" onClick={() => setImportOpen(true)} {...guard.lock}><FileUp size={16} aria-hidden /> {t('dashboard.import')}</Button>
                <Button disabled={recalc.isPending} onClick={() => recalc.mutate(loadPrefs().horizon)}>
                  {recalc.isPending ? t('common.calculating') : t('dashboard.buildFromLoaded')}
                </Button>
              </div>
            }
          />
        </div>
      )}
      {view && <DashboardBody v={view} days={shown} />}

      <Modal open={importOpen} onClose={() => setImportOpen(false)} title={t('import.modalTitle')}>
        <ImportStatement />
      </Modal>
    </PageContainer>
  )
}

function DashboardBody({ v, days }: { v: ForecastView; days: number }) {
  const { t } = useTranslation()
  const { r, run } = v
  const risk = r.risk
  const deficit = BigInt(v.minDay.p50) < 0n
  const chartPoints = sliceWindow(v.points, r.as_of, 0, days + (r.bridge_days ?? 0))
  const riskTone = risk.level === 'high' ? 'danger' : risk.level === 'medium' ? 'warning' : 'default'

  return (
    <>
      <ForecastBanners run={run} />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          index={0}
          icon={<Wallet size={16} aria-hidden />}
          label={t('dashboard.kpi.balance')}
          value={formatKop(r.opening_balance)}
          hint={t('dashboard.kpi.asOf', { date: formatDate(r.as_of) })}
        />
        <StatCard
          index={1}
          icon={<TrendingDown size={16} aria-hidden />}
          label={t('dashboard.kpi.minBalance')}
          value={formatKop(v.minDay.p50)}
          tone={deficit ? 'danger' : 'default'}
          hint={t(deficit ? 'dashboard.kpi.minDeficit' : 'dashboard.kpi.minForecast', { date: formatDate(v.minDay.date) })}
        />
        <StatCard
          index={2}
          icon={<ArrowDownToLine size={16} aria-hidden />}
          label={t('dashboard.kpi.inflows')}
          value={formatKop(v.plannedIn)}
          hint={t('dashboard.kpi.inflowsHint', { days: r.horizon })}
        />
        <StatCard
          index={3}
          icon={<ShieldAlert size={16} aria-hidden />}
          label={t('dashboard.kpi.risk')}
          value={formatPercent(risk.shortfall_probability)}
          tone={riskTone}
          hint={
            risk.level !== 'none' && v.gapDate
              ? t('dashboard.kpi.riskShortage', { level: t(`risk.badge.${risk.level}`), date: formatDate(v.gapDate) })
              : risk.level === 'none'
                ? t('dashboard.kpi.riskNone', { days: r.horizon })
                : t(`risk.badge.${risk.level}`)
          }
        />
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-5 min-w-0">
          <Card title={t('dashboard.chartTitle')} subtitle={t('dashboard.chartSubtitle', { days })}>
            <div className="px-3 pb-4 sm:px-5">
              <ForecastChart points={chartPoints} today={r.as_of} shortageDate={risk.level === 'none' ? null : v.gapDate} height={360} />
              <p className="text-xs text-muted mt-2">{t('forecast.caption')} {t('forecast.captionBand')}</p>
            </div>
          </Card>

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
            <Card title={t('dashboard.upcomingPayments')} action={<Link to="/obligations" className="text-[13px] text-primary font-medium hover:underline">{t('common.all')}</Link>}>
              <PlannedList items={r.planned} kind="payment" icon={<ArrowUpFromLine size={15} />} />
            </Card>
            <Card title={t('dashboard.expectedReceipts')} action={<Link to="/obligations" className="text-[13px] text-primary font-medium hover:underline">{t('common.all')}</Link>}>
              <PlannedList items={r.planned} kind="receipt" icon={<ArrowDownToLine size={15} />} />
            </Card>
          </div>
        </div>

        <div className="flex flex-col gap-5 min-w-0">
          <RiskAlert risk={risk} />
          <KeyRateCard />
          <Card title={t('quality.title')}>
            <div className="px-5 pb-5 flex flex-col gap-4">
              <DataQuality quality={r.quality} showTitle={false} />
              <p className="flex items-center gap-1.5 text-[13px] text-muted">
                <Clock size={14} aria-hidden />
                {t('common.updated', { value: formatDateTime(run.created_at) })}
              </p>
              <p className="text-xs text-muted">{t('dashboard.modelNote', { days: r.horizon, version: r.model_version, paths: r.n_paths })}</p>
            </div>
          </Card>
        </div>
      </div>
    </>
  )
}
