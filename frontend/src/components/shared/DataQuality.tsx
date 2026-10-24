import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ForecastResult } from '../../types'
import { cn } from '../../lib/utils'
import { Badge } from './StatusBadge'

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
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
