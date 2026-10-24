import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { CheckCircle2, FileUp, Loader2 } from 'lucide-react'
import type { ImportPreview } from '../../types'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { Badge } from '../../components/shared/StatusBadge'
import { useConfirmImport, useRecalc, useUploadStatement } from '../../hooks/queries'
import { formatDate, formatKop, formatNumber } from '../../lib/formatters'
import { loadPrefs } from '../../lib/prefs'
import { errorText } from '../../i18n/helpers'
import { categoryLabel } from '../../i18n/categories'

/** Upload -> preview -> confirm. After confirming, the forecast is rebuilt. */
export function ImportStatement({ onDone }: { onDone?: () => void }) {
  const { t } = useTranslation()
  const inputRef = useRef<HTMLInputElement>(null)
  const upload = useUploadStatement()
  const confirm = useConfirmImport()
  const recalc = useRecalc()
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [done, setDone] = useState<{ inserted: number; skipped: number } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const busy = upload.isPending || confirm.isPending || recalc.isPending

  async function pick(file?: File) {
    if (!file) return
    setError(null)
    setDone(null)
    try {
      setPreview(await upload.mutateAsync(file))
    } catch (e) {
      setPreview(null)
      setError(e)
    }
  }

  async function accept() {
    if (!preview) return
    setError(null)
    try {
      const r = await confirm.mutateAsync(preview.import_id)
      setDone({ inserted: r.inserted, skipped: r.skipped_duplicates })
      setPreview(null)
      // The forecast is rebuilt with the horizon chosen in the settings (default 90 days).
      try {
        await recalc.mutateAsync(loadPrefs().horizon)
      } catch (e) {
        setError(e)
      }
      onDone?.()
    } catch (e) {
      setError(e)
    }
  }

  const s = preview?.summary
  const st = s?.stats

  return (
    <div className="flex flex-col gap-4">
      <input
        ref={inputRef}
        type="file"
        accept=".csv,.xlsx,.txt,.1c"
        className="sr-only"
        tabIndex={-1}
        aria-label={t('import.fileAria')}
        disabled={busy}
        onChange={(e) => {
          void pick(e.target.files?.[0])
          e.target.value = ''
        }}
      />

      {!preview && (
        <>
          <div className="rounded-control bg-app border border-line p-3 text-[13px] text-muted">
            <p className="font-medium text-ink mb-1">{t('import.dataUsedTitle')}</p>
            {t('import.dataUsedText')}
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={() => inputRef.current?.click()}
            className="flex flex-col items-center gap-2 rounded-card border border-dashed border-line px-4 py-6 text-center hover:bg-hover cursor-pointer disabled:opacity-60"
          >
            {upload.isPending ? <Loader2 size={22} className="spin text-primary" aria-hidden /> : <FileUp size={22} className="text-primary" aria-hidden />}
            <span className="text-sm font-medium">{upload.isPending ? t('import.uploading') : t('import.choose')}</span>
            <span className="text-xs text-muted">{t('import.formats')}</span>
          </button>
        </>
      )}

      {error != null && <Notice tone="danger">{errorText(t, error)}</Notice>}

      {done && (
        <Notice tone="success" title={t('import.doneTitle')}>
          {t('import.doneText', { count: done.inserted })}
          {done.skipped > 0 && ` ${t('import.doneSkipped', { count: done.skipped })}`}
        </Notice>
      )}

      {s && st && (
        <>
          <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <Stat label={t('import.rowsOk')} value={formatNumber(st.rows_ok)} />
            <Stat label={t('import.period')} value={`${formatDate(st.date_from)} — ${formatDate(st.date_to)}`} hint={t('import.days', { count: st.days_span })} />
            <Stat label={t('import.rejected')} value={formatNumber(s.errors_total)} />
          </dl>

          {!st.has_balance && <Notice tone="danger">{t('import.noBalance')}</Notice>}
          {!!st.balance_mismatches && <Notice tone="warning">{t('import.mismatch', { count: st.balance_mismatches })}</Notice>}
          {!!st.missing_period_count && (
            <Notice tone="warning">
              {t('import.gaps', { count: st.missing_period_count })}
              {st.first_gap_dates?.length ? ` ${t('import.gapsExample', { list: st.first_gap_dates.slice(0, 3).map(([a, b]) => `${formatDate(a)} — ${formatDate(b)}`).join('; ') })}` : ''}
            </Notice>
          )}
          {!!st.suspected_duplicates && <Notice tone="warning">{t('import.suspectedDuplicates', { count: st.suspected_duplicates })}</Notice>}
          {s.duplicates_existing > 0 && <Notice>{t('import.duplicatesExisting', { count: s.duplicates_existing })}</Notice>}

          <div>
            <p className="text-[13px] text-muted mb-2">{t('import.categories')}</p>
            <ul className="flex flex-wrap gap-1.5">
              {Object.entries(s.categories).map(([k, v]) => (
                <li key={k}><Badge>{categoryLabel(t, k)}: {v}</Badge></li>
              ))}
            </ul>
          </div>

          {s.errors_total > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer">{t('import.rejectedRows', { count: s.errors_total })}</summary>
              <ul className="mt-1 text-[13px] text-muted">
                {s.errors.slice(0, 20).map((e) => <li key={`${e.row}-${e.code}`}>{t('import.rowError', { row: e.row, code: e.code })}</li>)}
              </ul>
            </details>
          )}

          <div className="overflow-x-auto rounded-control border border-line">
            <table className="data-table">
              <caption className="sr-only">{t('import.sample')}</caption>
              <tbody>
                {s.sample.slice(0, 6).map((x, i) => (
                  <tr key={i}>
                    <td className="whitespace-nowrap">{formatDate(x.date)}</td>
                    <td className="num tnum whitespace-nowrap">{formatKop(x.amount_kop, { sign: true })}</td>
                    <td className="max-w-[200px] truncate">{x.counterparty}</td>
                    <td>{categoryLabel(t, x.category)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button variant="primary" onClick={() => void accept()} disabled={busy || !st.has_balance}>
              {busy && <Loader2 size={16} className="spin" aria-hidden />}
              {busy ? t('import.processing') : t('import.confirm')}
            </Button>
            <Button onClick={() => { setPreview(null); setError(null) }} disabled={busy}>{t('import.another')}</Button>
          </div>
        </>
      )}

      {done && !preview && (
        <Button className="self-start" onClick={() => inputRef.current?.click()}>
          <CheckCircle2 size={16} aria-hidden /> {t('import.loadAnother')}
        </Button>
      )}
    </div>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-control bg-app border border-line p-3">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="tnum font-semibold">{value}</dd>
      {hint && <dd className="text-xs text-muted">{hint}</dd>}
    </div>
  )
}
