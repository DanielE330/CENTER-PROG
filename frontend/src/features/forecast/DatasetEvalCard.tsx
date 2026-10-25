import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Download, FileUp, Loader2 } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import { formatCurrency } from '../../lib/formatters'
import { evalRows, isCompaniesFile, readCompanies, splitByCompany } from '../../lib/datasetSplit'
import type { Backtest } from '../../types'

interface CompanyEval {
  id: string
  name?: string
  ops: number
  bt: Backtest | null
}

interface HorizonAgg {
  h: number
  mae: number
  mse: number
  rmse: number
  nmae: number
  coverage: number
}

interface Report {
  companies: CompanyEval[]
  origins: number
  tp: number
  fp: number
  fn: number
  tn: number
  brier: number | null
  meanP: number | null
  horizons: HorizonAgg[]
}

const mean = (v: number[]) => (v.length ? v.reduce((a, b) => a + b, 0) / v.length : NaN)

function aggregate(companies: CompanyEval[]): Report {
  const bts = companies.map((c) => c.bt).filter((b): b is Backtest => !!b)
  const sum = (f: (b: Backtest) => number | undefined | null) => bts.reduce((a, b) => a + (f(b) ?? 0), 0)
  const origins = sum((b) => b.origins)
  const horizons = [7, 14, 30].map((h) => {
    const ms = bts.map((b) => b.metrics.find((m) => m.horizon === h)).filter((m) => !!m)
    const n = ms.reduce((a, m) => a + m.n, 0)
    return {
      h,
      mae: mean(ms.map((m) => Number(m.mae_model) / 100)),
      mse: mean(ms.map((m) => m.mse_model_rub2 ?? NaN)),
      rmse: mean(ms.map((m) => Number(m.rmse_model ?? NaN) / 100)),
      nmae: mean(ms.map((m) => m.mae_pct_turnover ?? NaN)),
      coverage: n ? ms.reduce((a, m) => a + (m.band_coverage ?? 0) * m.n, 0) / n : NaN,
    }
  })
  return {
    companies,
    origins,
    tp: sum((b) => b.alarm_tp),
    fp: sum((b) => b.alarm_fp),
    fn: sum((b) => b.alarm_fn),
    tn: sum((b) => b.alarm_tn),
    brier: origins ? sum((b) => (b.brier ?? 0) * b.origins) / origins : null,
    meanP: origins ? sum((b) => (b.mean_probability ?? 0) * b.origins) / origins : null,
    horizons,
  }
}

const ratio = (a: number, b: number) => (b > 0 ? a / b : null)
const pct = (x: number | null) => (x == null || Number.isNaN(x) ? '—' : `${(x * 100).toFixed(1).replace('.', ',')} %`)

function toCsv(r: Report): string {
  const head = 'company_id;company_name;operations;origins;mae7_rub;mae14_rub;mae30_rub;rmse30_rub;nmae30_pct;tp;fp;fn;tn;brier'
  const rows = r.companies.map((c) => {
    const b = c.bt
    const m = (h: number) => b?.metrics.find((x) => x.horizon === h)
    return [c.id, c.name ?? '', c.ops, b?.origins ?? 0, ...[7, 14, 30].map((h) => (m(h) ? Number(m(h)!.mae_model) / 100 : '')),
      m(30)?.rmse_model != null ? Number(m(30)!.rmse_model) / 100 : '', m(30)?.mae_pct_turnover?.toFixed(2) ?? '',
      b?.alarm_tp ?? '', b?.alarm_fp ?? '', b?.alarm_fn ?? '', b?.alarm_tn ?? '', b?.brier?.toFixed(4) ?? ''].join(';')
  })
  return '﻿' + [head, ...rows].join('\n') + '\n'
}

/** Quality on the control dataset: every company of all_operations.csv is backtested on the server
 * (POST /evaluate, rolling-origin, the same as `cargo run --bin eval`), the totals are pooled here. */
export function DatasetEvalCard() {
  const { t } = useTranslation()
  const input = useRef<HTMLInputElement>(null)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<unknown>(null)
  const busy = progress != null && progress.done < progress.total

  async function run(files: FileList | null) {
    const list = [...(files ?? [])]
    const ops = list.find((f) => !isCompaniesFile(f))
    const names = list.find(isCompaniesFile)
    if (!ops) return
    setError(null)
    setReport(null)
    try {
      const d = await splitByCompany(ops, names ? await readCompanies(names) : undefined)
      if (!d) throw new Error(t('datasetEval.notDataset'))
      const total = d.companies.length
      setProgress({ done: 0, total })
      const out: CompanyEval[] = new Array(total)
      let next = 0
      let done = 0
      const worker = async () => {
        while (next < total) {
          const i = next++
          const c = d.companies[i]
          let bt: Backtest | null = null
          try {
            bt = (await api.evaluate(c.id, evalRows(d, c.id))).backtest
          } catch {
            bt = null // a company too short for a backtest (less than ~5 months) or a failed request
          }
          out[i] = { id: c.id, name: c.name, ops: c.rows, bt }
          setProgress({ done: ++done, total })
        }
      }
      await Promise.all(Array.from({ length: 6 }, worker))
      setReport(aggregate(out))
    } catch (e) {
      setError(e)
      setProgress(null)
    }
  }

  const r = report
  const n = r ? r.tp + r.fp + r.fn + r.tn : 0
  const evaluated = r ? r.companies.filter((c) => c.bt).length : 0

  return (
    <Card
      title={t('datasetEval.title')}
      subtitle={t('datasetEval.subtitle')}
      action={
        <Button size="sm" variant="primary" disabled={busy} onClick={() => input.current?.click()}>
          {busy ? <Loader2 size={15} className="spin" aria-hidden /> : <FileUp size={15} aria-hidden />}
          {busy ? t('datasetEval.running', { done: progress?.done ?? 0, total: progress?.total ?? 0 }) : t('datasetEval.choose')}
        </Button>
      }
    >
      <input ref={input} type="file" accept=".csv" multiple className="sr-only" tabIndex={-1} aria-label={t('datasetEval.choose')}
        onChange={(e) => { void run(e.target.files); e.target.value = '' }} />
      <div className="px-5 pb-5 flex flex-col gap-4">
        {!r && !busy && <p className="text-sm text-muted">{t('datasetEval.hint')}</p>}
        {busy && progress && (
          <div className="h-2 rounded-full bg-app border border-line overflow-hidden" role="progressbar" aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.done}>
            <div className="h-full bg-primary transition-all" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
          </div>
        )}
        {error != null && <Notice tone="danger">{errorText(t, error)}</Notice>}
        {r && (
          <>
            <p className="text-sm">{t('datasetEval.summary', { companies: r.companies.length, evaluated, origins: r.origins })}</p>
            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              <Stat label="Accuracy" value={pct(ratio(r.tp + r.tn, n))} />
              <Stat label="Precision" value={pct(ratio(r.tp, r.tp + r.fp))} />
              <Stat label="Recall" value={pct(ratio(r.tp, r.tp + r.fn))} />
              <Stat label="F1" value={pct(ratio(2 * r.tp, 2 * r.tp + r.fp + r.fn))} />
              <Stat label={t('datasetEval.brier')} value={r.brier == null ? '—' : r.brier.toFixed(4)} />
            </dl>
            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <p className="text-sm font-semibold mb-2">{t('datasetEval.matrix')}</p>
                <table className="text-sm border border-line w-full tnum">
                  <thead><tr className="bg-app"><th className="p-2" /><th className="p-2 text-right">{t('backtest.factGap')}</th><th className="p-2 text-right">{t('backtest.factNoGap')}</th></tr></thead>
                  <tbody>
                    <tr className="border-t border-line"><th className="p-2 text-left font-medium">{t('backtest.predGap')}</th><td className="p-2 text-right font-semibold text-primary">{r.tp} TP</td><td className="p-2 text-right text-danger">{r.fp} FP</td></tr>
                    <tr className="border-t border-line"><th className="p-2 text-left font-medium">{t('backtest.predNoGap')}</th><td className="p-2 text-right text-danger">{r.fn} FN</td><td className="p-2 text-right font-semibold text-primary">{r.tn} TN</td></tr>
                  </tbody>
                </table>
                <p className="text-xs text-muted mt-2">{t('datasetEval.calibration', { p: pct(r.meanP), rate: pct(ratio(r.tp + r.fn, n)) })}</p>
              </div>
              <div>
                <p className="text-sm font-semibold mb-2">{t('datasetEval.balance')}</p>
                <table className="text-sm border border-line w-full tnum">
                  <thead><tr className="bg-app"><th className="p-2 text-left">{t('backtest.col.horizon')}</th><th className="p-2 text-right">MAE</th><th className="p-2 text-right">MSE, ₽²</th><th className="p-2 text-right">RMSE</th><th className="p-2 text-right">NMAE</th><th className="p-2 text-right">P10–P90</th></tr></thead>
                  <tbody>
                    {r.horizons.map((h) => (
                      <tr key={h.h} className="border-t border-line">
                        <td className="p-2">{t('common.daysShort', { count: h.h })}</td>
                        <td className="p-2 text-right">{formatCurrency(h.mae)}</td>
                        <td className="p-2 text-right">{h.mse.toExponential(2)}</td>
                        <td className="p-2 text-right">{formatCurrency(h.rmse)}</td>
                        <td className="p-2 text-right">{h.nmae.toFixed(1)}%</td>
                        <td className="p-2 text-right">{pct(h.coverage)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="text-xs text-muted mt-2">{t('datasetEval.nmae')}</p>
              </div>
            </div>
            <Button
              size="sm"
              className="self-start"
              onClick={() => {
                const url = URL.createObjectURL(new Blob([toCsv(r)], { type: 'text/csv' }))
                const a = document.createElement('a')
                a.href = url
                a.download = 'control-dataset-metrics.csv'
                a.click()
                URL.revokeObjectURL(url)
              }}
            >
              <Download size={15} aria-hidden /> {t('datasetEval.csv')}
            </Button>
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
