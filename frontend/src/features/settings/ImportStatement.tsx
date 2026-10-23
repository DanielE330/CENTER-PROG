import { useRef, useState } from 'react'
import { CheckCircle2, FileUp, Loader2, XCircle } from 'lucide-react'
import { api } from '../../lib/api'
import type { ImportResult } from '../../types'
import { Button } from '../../components/ui/Button'
import { formatDate } from '../../lib/formatters'
import { MockBadge } from '../../components/shared/StatusBadge'

type Phase = 'idle' | 'loading' | 'success' | 'error'

/** SIMULATED import: the file never leaves the browser and nothing is parsed. */
export function ImportStatement() {
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [result, setResult] = useState<ImportResult | null>(null)
  const [error, setError] = useState('')
  const [forceError, setForceError] = useState(false)

  const run = async () => {
    if (!file) return
    setPhase('loading')
    try {
      setResult(await api.importStatement(file, forceError))
      setPhase('success')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Неизвестная ошибка')
      setPhase('error')
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 text-[13px] text-muted">
        <MockBadge label="Симуляция" />
        Сервер не подключён: файл не загружается и не обрабатывается.
      </div>

      <div className="rounded-control bg-app border border-line p-3 text-[13px] text-muted">
        <p className="font-medium text-ink mb-1">Какие данные будут использованы</p>
        Дата, сумма, направление (входящий / исходящий), контрагент и назначение платежа — для построения истории остатков и
        сопоставления плановых платежей с фактом.
      </div>

      <input
        ref={inputRef}
        type="file"
        accept=".csv,.txt,.xls,.xlsx"
        className="sr-only"
        aria-label="Файл выписки"
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
        <span className="text-sm font-medium">{file ? 'Выбрать другой файл' : 'Выберите файл выписки'}</span>
        <span className="text-xs text-muted">CSV, TXT, XLS, XLSX · до 10 МБ</span>
      </button>

      {file && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted">Файл</dt>
          <dd className="font-medium break-all">{file.name}</dd>
          <dt className="text-muted">Размер</dt>
          <dd className="tnum">{(file.size / 1024).toFixed(1)} КБ</dd>
        </dl>
      )}

      <label className="flex items-center gap-2 text-[13px] text-muted cursor-pointer">
        <input type="checkbox" checked={forceError} onChange={(e) => setForceError(e.target.checked)} />
        Смоделировать ошибку распознавания
      </label>

      {phase === 'loading' && (
        <p className="flex items-center gap-2 text-sm text-muted" role="status">
          <Loader2 size={16} className="spin" /> Обработка файла (симуляция)…
        </p>
      )}
      {phase === 'success' && result && (
        <div className="flex items-start gap-2 rounded-control bg-primary-soft p-3 text-sm text-primary" role="status">
          <CheckCircle2 size={18} className="shrink-0 mt-0.5" />
          <span>
            Симуляция завершена: «распознано» {result.operations} операций за период {formatDate(result.periodFrom)} —{' '}
            {formatDate(result.periodTo)}. Реальные данные не изменены.
          </span>
        </div>
      )}
      {phase === 'error' && (
        <div className="flex items-start gap-2 rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">
          <XCircle size={18} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      <Button variant="primary" disabled={!file || phase === 'loading'} onClick={run} className="self-start">
        Импортировать (симуляция)
      </Button>
    </div>
  )
}
