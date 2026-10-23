import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import type { DataQuality as DQ } from '../../types'
import { cn } from '../../lib/utils'

export function DataQuality({ quality }: { quality: DQ }) {
  const low = quality.completeness < 80
  const Icon = quality.warnings.length ? AlertTriangle : CheckCircle2
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between text-[13px]">
        <span className="inline-flex items-center gap-1.5 font-medium">
          <Icon size={14} className={cn(quality.warnings.length ? 'text-warning' : 'text-primary')} />
          Полнота данных
        </span>
        <span className="tnum font-semibold">{quality.completeness}%</span>
      </div>
      <div className="h-1.5 rounded-full bg-line overflow-hidden" role="progressbar" aria-valuenow={quality.completeness} aria-valuemin={0} aria-valuemax={100} aria-label="Полнота данных">
        <div className={cn('h-full rounded-full', low ? 'bg-warning' : 'bg-primary')} style={{ width: `${quality.completeness}%` }} />
      </div>
      {quality.warnings.length > 0 && (
        <ul className="text-[13px] text-muted flex flex-col gap-1 list-disc pl-4">
          {quality.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
