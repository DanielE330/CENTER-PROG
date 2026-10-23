import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { CheckCircle2, FileUp, Loader2, XCircle } from 'lucide-react'
import { api } from '../../lib/api'
import type { ImportResult } from '../../types'
import { Button } from '../../components/ui/Button'
import { formatDate, formatNumber } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import { MockBadge } from '../../components/shared/StatusBadge'

type Phase = 'idle' | 'loading' | 'success' | 'error'

/** SIMULATED import: the file never leaves the browser and nothing is parsed. */
export function ImportStatement() {
  const { t } = useTranslation()
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [result, setResult] = useState<ImportResult | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [forceError, setForceError] = useState(false)

  const run = async () => {
    if (!file) return
    setPhase('loading')
    try {
      setResult(await api.importStatement(file, forceError))
      setPhase('success')
    } catch (e) {
      setError(e)
      setPhase('error')
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 text-[13px] text-muted">
        <MockBadge label={t('import.simulation')} />
        {t('import.noServer')}
      </div>

      <div className="rounded-control bg-app border border-line p-3 text-[13px] text-muted">
        <p className="font-medium text-ink mb-1">{t('import.dataUsedTitle')}</p>
        {t('import.dataUsedText')}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept=".csv,.txt,.xls,.xlsx"
        className="sr-only"
        aria-label={t('import.fileAria')}
        onChange={(e) => {
          setFile(e.target.files?.[0] ?? null)
          setPhase('idle')
        }}
      />
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        className="flex flex-col items-center gap-2 rounded-card border border-dashed border-line px-4 py-6 text-center hover:bg-hover cursor-pointer"
      >
        <FileUp size={22} className="text-primary" />
        <span className="text-sm font-medium">{file ? t('import.chooseAnother') : t('import.choose')}</span>
        <span className="text-xs text-muted">{t('import.formats')}</span>
      </button>

      {file && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted">{t('import.file')}</dt>
          <dd className="font-medium break-all">{file.name}</dd>
          <dt className="text-muted">{t('import.size')}</dt>
          <dd className="tnum">{t('import.sizeKb', { value: formatNumber(file.size / 1024) })}</dd>
        </dl>
      )}

      <label className="flex items-center gap-2 text-[13px] text-muted cursor-pointer">
        <input type="checkbox" checked={forceError} onChange={(e) => setForceError(e.target.checked)} />
        {t('import.simulateError')}
      </label>

      {phase === 'loading' && (
        <p className="flex items-center gap-2 text-sm text-muted" role="status">
          <Loader2 size={16} className="spin" /> {t('import.processing')}
        </p>
      )}
      {phase === 'success' && result && (
        <div className="flex items-start gap-2 rounded-control bg-primary-soft p-3 text-sm text-primary" role="status">
          <CheckCircle2 size={18} className="shrink-0 mt-0.5" />
          <span>
            {t('import.success', { count: result.operations, from: formatDate(result.periodFrom), to: formatDate(result.periodTo) })}
          </span>
        </div>
      )}
      {phase === 'error' && (
        <div className="flex items-start gap-2 rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">
          <XCircle size={18} className="shrink-0 mt-0.5" />
          <span>{errorText(t, error)}</span>
        </div>
      )}

      <Button variant="primary" disabled={!file || phase === 'loading'} onClick={run} className="self-start">
        {t('import.run')}
      </Button>
    </div>
  )
}
