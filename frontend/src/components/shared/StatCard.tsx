import type { CSSProperties, ReactNode } from 'react'
import { cn } from '../../lib/utils'

interface Props {
  label: string
  value: string
  hint?: ReactNode
  icon?: ReactNode
  tone?: 'default' | 'danger' | 'warning'
  badge?: ReactNode
  /** Position in a row; staggers the entrance animation. */
  index?: number
}

export function StatCard({ label, value, hint, icon, tone = 'default', badge, index = 0 }: Props) {
  return (
    <div className="card enter p-5 flex flex-col gap-2 min-w-0" style={{ '--i': index } as CSSProperties}>
      <div className="flex items-center justify-between gap-2 text-muted text-[13px]">
        <span className="inline-flex items-center gap-2">
          {icon}
          {label}
        </span>
        {badge}
      </div>
      <div className={cn('tnum text-[28px] lg:text-[30px] font-semibold leading-tight', tone === 'danger' && 'text-danger', tone === 'warning' && 'text-warning')}>
        {value}
      </div>
      {hint && <div className="text-[13px] text-muted">{hint}</div>}
    </div>
  )
}
