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
    { key: 'rmse', header: t('backtest.col.rmse'), numeric: true, render: (m) => (m.rmse_model == null ? '—' : formatKop(m.rmse_model)) },
    { key: 'mse', header: t('backtest.col.mse'), numeric: true, render: (m) => (m.mse_model_rub2 == null ? '—' : m.mse_model_rub2.toExponential(2)) },
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
            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              <Stat label={t('backtest.origins')} value={String(bt.origins)} />
              <Stat label={t('backtest.accuracy')} value={pct(bt.alarm_accuracy)} />
              <Stat label={t('backtest.precision')} value={pct(bt.alarm_precision)} />
              <Stat label={t('backtest.recall', { count: bt.actual_gap_windows })} value={pct(bt.alarm_recall)} />
              <Stat label={t('backtest.f1')} value={pct(bt.alarm_f1)} />
              <Stat label={t('backtest.brier')} value={bt.brier == null ? '—' : bt.brier.toFixed(3)} />
            </dl>
            {bt.alarm_tn != null && (
              <div className="flex flex-col gap-2">
                <p className="text-sm font-semibold">{t('backtest.matrixTitle', { threshold: Math.round((bt.alarm_threshold ?? 0.5) * 100) })}</p>
                <table className="text-sm border border-line rounded-control w-full max-w-lg" aria-label={t('backtest.matrixTitle', { threshold: 50 })}>
                  <thead>
                    <tr className="bg-app">
                      <th className="p-2 text-left font-medium text-muted" />
                      <th className="p-2 text-right font-medium">{t('backtest.factGap')}</th>
                      <th className="p-2 text-right font-medium">{t('backtest.factNoGap')}</th>
                    </tr>
                  </thead>
                  <tbody className="tnum">
                    <tr className="border-t border-line">
                      <th className="p-2 text-left font-medium">{t('backtest.predGap')}</th>
                      <td className="p-2 text-right text-primary font-semibold">{bt.alarm_tp} <span className="text-xs text-muted font-normal">TP</span></td>
                      <td className="p-2 text-right text-danger">{bt.alarm_fp} <span className="text-xs text-muted">FP</span></td>
                    </tr>
                    <tr className="border-t border-line">
                      <th className="p-2 text-left font-medium">{t('backtest.predNoGap')}</th>
                      <td className="p-2 text-right text-danger">{bt.alarm_fn} <span className="text-xs text-muted">FN</span></td>
                      <td className="p-2 text-right text-primary font-semibold">{bt.alarm_tn} <span className="text-xs text-muted font-normal">TN</span></td>
                    </tr>
                  </tbody>
                </table>
                {bt.mean_probability != null && bt.gap_rate != null && (
                  <p className="text-xs text-muted">{t('backtest.calibration', { p: formatPercent(bt.mean_probability), rate: formatPercent(bt.gap_rate) })}</p>
                )}
              </div>
            )}
            <p className="text-xs text-muted">{t('backtest.note')}</p>
          </>
        )}
      </div>
    </Card>
  )
}

const pct = (x: number | null | undefined) => (x == null ? '—' : `${(x * 100).toFixed(1).replace('.', ',')} %`)

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-control bg-app border border-line p-3">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="tnum text-lg font-semibold">{value}</dd>
    </div>
  )
}
