import { useState } from 'react'
import { Link } from 'react-router-dom'
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
import { Select } from '../../components/ui/Input'
import { useForecast, useObligations } from '../../hooks/queries'
import { formatCurrency, formatDate, formatDateTime } from '../../lib/formatters'
import { ForecastChart } from '../forecast/ForecastChart'
import { sliceWindow } from '../forecast/window'
import { ImportStatement } from '../settings/ImportStatement'
import { ObligationList } from './ObligationList'
import type { ForecastResponse } from '../../types'

const periods = [
  { value: 14, label: '14 дней' },
  { value: 30, label: '30 дней' },
]

export default function DashboardPage() {
  const forecast = useForecast()
  const obligations = useObligations()
  const [days, setDays] = useState(30)
  const [importOpen, setImportOpen] = useState(false)

  return (
    <PageContainer>
      <PageHeader
        title="Обзор"
        description={
          forecast.data?.risk.shortageDate
            ? `Прогноз на ${days} дней. Ожидаемая дата возможной нехватки — ${formatDate(forecast.data.risk.shortageDate)}.`
            : 'Текущая ликвидность и прогноз движения денежных средств.'
        }
        actions={
          <>
            <div className="w-full sm:w-36">
              <Select aria-label="Период прогноза" value={days} onChange={(e) => setDays(Number(e.target.value))} options={periods} />
            </div>
            <Button variant="primary" onClick={() => setImportOpen(true)}>
              <FileUp size={16} /> Импортировать выписку
            </Button>
          </>
        }
      />

      <QueryBoundary query={forecast} loadingHeight={420}>
        {(f) => <DashboardBody f={f} days={days} obligations={obligations} />}
      </QueryBoundary>

      <Modal open={importOpen} onClose={() => setImportOpen(false)} title="Импорт банковской выписки">
        <ImportStatement />
      </Modal>
    </PageContainer>
  )
}

function DashboardBody({ f, days, obligations }: { f: ForecastResponse; days: number; obligations: ReturnType<typeof useObligations> }) {
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
            <p className="font-medium">Данные неполные — показатели могут быть недоступны</p>
            <p className="text-muted">Отсутствующие значения не считаются нулём. Импортируйте выписку, чтобы построить прогноз.</p>
          </div>
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={<Wallet size={16} />}
          label="Текущий остаток"
          value={noBalance ? 'Нет данных' : formatCurrency(summary.currentBalance)}
          hint={noBalance ? 'Остаток неизвестен — не равен нулю' : `На ${formatDate(f.today)}`}
        />
        <StatCard
          icon={<TrendingDown size={16} />}
          label="Минимальный остаток"
          value={summary.minBalance == null ? 'Нет данных' : formatCurrency(summary.minBalance)}
          tone={deficit ? 'danger' : 'default'}
          hint={
            summary.minBalanceDate
              ? `${deficit ? 'Дефицит · ' : 'Прогноз · '}${formatDate(summary.minBalanceDate)}`
              : 'Прогноз не построен'
          }
        />
        <StatCard
          icon={<ArrowDownToLine size={16} />}
          label="Ожидаемые поступления"
          value={noData ? 'Нет данных' : formatCurrency(summary.expectedInflows)}
          hint={noData ? undefined : `За ближайшие ${f.horizonDays} дней · ожидания, не факт`}
        />
        <StatCard
          icon={<ShieldAlert size={16} />}
          label="Риск ликвидности"
          value={risk.level === 'high' ? 'Высокий' : risk.level === 'medium' ? 'Средний' : risk.level === 'low' ? 'Низкий' : 'Не оценён'}
          tone={risk.level === 'high' ? 'danger' : risk.level === 'medium' ? 'warning' : 'default'}
          hint={risk.shortageDate ? `Нехватка ≈ ${formatDate(risk.shortageDate)}` : risk.level === 'unknown' ? 'Недостаточно данных' : 'Нехватки не ожидается'}
        />
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-5 min-w-0">
          <Card
            title="Прогноз остатка денежных средств"
            subtitle={`Факт за 14 дней и прогноз на ${days} дней`}
            action={<MockBadge />}
          >
            <div className="px-3 pb-4 sm:px-5">
              {noData ? (
                <EmptyState title="Нет данных для графика" description="Импортируйте банковскую выписку, чтобы построить прогноз." />
              ) : (
                <>
                  {summary.currentBalance == null && (
                    <p className="text-[13px] text-warning mb-2">Прогноз не построен: неизвестен текущий остаток. Показан только факт.</p>
                  )}
                  <ForecastChart points={chartPoints} today={f.today} shortageDate={risk.shortageDate} height={360} />
                </>
              )}
            </div>
          </Card>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card title="Ближайшие платежи" action={<Link to="/obligations" className="text-[13px] text-primary font-medium hover:underline">Все</Link>}>
              <QueryBoundary query={obligations} loadingHeight={160}>
                {(list) => <ObligationList items={list} kind="payment" icon={<ArrowUpFromLine size={15} />} />}
              </QueryBoundary>
            </Card>
            <Card title="Ожидаемые поступления" action={<Link to="/obligations" className="text-[13px] text-primary font-medium hover:underline">Все</Link>}>
              <QueryBoundary query={obligations} loadingHeight={160}>
                {(list) => <ObligationList items={list} kind="receipt" icon={<ArrowDownToLine size={15} />} />}
              </QueryBoundary>
            </Card>
          </div>
        </div>

        <div className="flex flex-col gap-5 min-w-0">
          <RiskAlert risk={risk} />
          <Card title="Состояние данных">
            <div className="px-5 pb-5 flex flex-col gap-4">
              <DataQuality quality={quality} />
              <p className="flex items-center gap-1.5 text-[13px] text-muted">
                <Clock size={14} />
                Обновлено: {formatDateTime(f.generatedAt)}
              </p>
              <p className="text-xs text-muted">
                Все показатели — демонстрационные (mock). Горизонт прогноза: {f.horizonDays} дн. Суммы формирует имитация модели, а не реальный расчёт.
              </p>
            </div>
          </Card>
        </div>
      </div>
    </>
  )
}
