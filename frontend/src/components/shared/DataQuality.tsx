import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ForecastResult } from '../../types'
import { cn } from '../../lib/utils'
import { Badge } from './StatusBadge'
import { formatDate } from '../../lib/formatters'

type TFn = (key: string, opts?: Record<string, unknown>) => string

/** The forecast service sends notes as Russian sentences: map the known ones to the UI language, show unknown ones as is. */
const NOTE_PATTERNS: Array<[RegExp, string, (m: RegExpExecArray) => Record<string, unknown>]> = [
  [/^История короче 60 дней/, 'short60', () => ({})],
  [/^История короче 6 месяцев/, 'short180', () => ({})],
  [/^В выписке найдено пропусков: (\d+) \(всего (\d+) дн\.\)/, 'gaps', (m) => ({ count: m[1], days: m[2] })],
  [/^В истории есть отрицательный остаток/, 'overdraft', () => ({})],
  [/^Регулярные платежи не найдены/, 'noRules', () => ({})],
  [/^Мало данных для остаточного потока/, 'fewData', () => ({})],
  [/^Выписка заканчивается (\d{4}-\d{2}-\d{2}): (\d+) дн\./, 'bridge', (m) => ({ date: formatDate(m[1]), days: m[2] })],
  [/^Данные выписки актуальны на (\d{4}-\d{2}-\d{2}), это более (\d+) дн\./, 'stale', (m) => ({ date: formatDate(m[1]), days: m[2] })],
]

export function qualityNote(t: TFn, note: string): string {
  for (const [re, key, params] of NOTE_PATTERNS) {
    const m = re.exec(note)
    if (m) return t(`quality.notes.${key}`, params(m))
  }
  return note
}

const tone = { good: 'success', medium: 'warning', low: 'danger' } as const

/** Data quality as reported by the forecast: level, length of history and notes. */
/** `showTitle={false}` when the surrounding card is already titled "Data quality". */
export function DataQuality({ quality, showTitle = true }: { quality: ForecastResult['quality']; showTitle?: boolean }) {
  const { t } = useTranslation()
  const level = (quality.level in tone ? quality.level : 'medium') as keyof typeof tone
  const Icon = quality.notes.length || level !== 'good' ? AlertTriangle : CheckCircle2
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2 text-[13px]">
        <span className="inline-flex items-center gap-1.5 font-medium">
          <Icon size={14} className={cn(quality.notes.length || level !== 'good' ? 'text-warning' : 'text-primary')} aria-hidden />
          {showTitle && t('quality.title')}
        </span>
        <Badge tone={tone[level]}>{t(`quality.level.${level}`)}</Badge>
      </div>
      <p className="text-[13px] text-muted">{t('quality.history', { count: quality.history_days })}</p>
      {quality.notes.length > 0 && (
        <ul className="text-[13px] text-muted flex flex-col gap-1 list-disc pl-4">
          {quality.notes.map((n) => (
            <li key={n}>{qualityNote(t, n)}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
