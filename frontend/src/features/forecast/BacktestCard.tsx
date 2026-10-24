import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Card } from '../../components/shared/Page'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { useBacktest } from '../../hooks/queries'
import { errorText } from '../../i18n/helpers'
import { formatKop, formatPercent } from '../../lib/formatters'
import type { BacktestMetric } from '../../types'

/** Forecast quality: rolling-origin backtest against two naive baselines. Runs on demand (it is slow). */
export function BacktestCard() {
  const { t } = useTranslation()
  const [run, setRun] = useState(false)
  const q = useBacktest(run)
  const bt = q.data
  const hasCoverage = bt?.metrics.some((m) => m.band_coverage != null)

  const columns: Column<BacktestMetric>[] = [
    { key: 'h', header: t('backtest.col.horizon'), render: (m) => t('common.daysShort', { count: m.horizon }) },
    { key: 'model', header: t('backtest.col.model'), numeric: true, render: (m) => formatKop(m.mae_model) },
    { key: 'last', header: t('backtest.col.lastValue'), numeric: true, render: (m) => formatKop(m.mae_last_value) },
    { key: 'drift', header: t('backtest.col.drift'), numeric: true, render: (m) => formatKop(m.mae_drift) },
    { key: 'beats', header: t('backtest.col.beats'), numeric: true, render: (m) => formatPercent(m.model_beats_baselines_share) },
    ...(hasCoverage
      ? [{ key: 'cov', header: t('backtest.col.coverage'), numeric: true, render: (m: BacktestMetric) => (m.band_coverage == null ? '—' : formatPercent(m.band_coverage)) }]
      : []),
    { key: 'n', header: t('backtest.col.n'), numeric: true, render: (m) => m.n },
  ]

  return (
    <Card
      title={t('backtest.title')}
      subtitle={t('backtest.subtitle')}
      action={
        <Button size="sm" disabled={q.isFetching} onClick={() => (run ? void q.refetch() : setRun(true))}>
          {q.isFetching ? t('common.calculating') : bt ? t('backtest.rerun') : t('backtest.run')}
        </Button>
      }
    >
      <div className="px-5 pb-5 flex flex-col gap-4">
        {q.isError && <Notice tone="danger">{errorText(t, q.error)}</Notice>}
        {!bt && !q.isError && <p className="text-sm text-muted">{t('backtest.hint')}</p>}
        {bt && (
          <>
            <div className="-mx-5 border-y border-line">
              <DataTable columns={columns} rows={bt.metrics} getRowKey={(m) => String(m.horizon)} />
            </div>
            <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Stat label={t('backtest.origins')} value={String(bt.origins)} />
              <Stat label={t('backtest.precision')} value={bt.alarm_precision == null ? '—' : formatPercent(bt.alarm_precision)} />
              <Stat label={t('backtest.recall', { count: bt.actual_gap_windows })} value={bt.alarm_recall == null ? '—' : formatPercent(bt.alarm_recall)} />
            </dl>
            <p className="text-xs text-muted">{t('backtest.note')}</p>
          </>
        )}
      </div>
    </Card>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-control bg-app border border-line p-3">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="tnum text-lg font-semibold">{value}</dd>
    </div>
  )
}
