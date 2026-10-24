import type { ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, Info, OctagonAlert } from 'lucide-react'
import { cn } from '../../lib/utils'

type Tone = 'info' | 'success' | 'warning' | 'danger'

const styles: Record<Tone, { box: string; icon: string; Icon: typeof Info }> = {
  info: { box: 'bg-app border-line', icon: 'text-muted', Icon: Info },
  success: { box: 'bg-primary-soft border-primary/30', icon: 'text-primary', Icon: CheckCircle2 },
  warning: { box: 'bg-warning-soft border-warning/40', icon: 'text-warning', Icon: AlertTriangle },
  danger: { box: 'bg-danger-soft border-danger/40', icon: 'text-danger', Icon: OctagonAlert },
}

/** Inline message. Danger is announced assertively (role=alert), the rest politely (role=status). */
export function Notice({ tone = 'info', title, children, action, className }: { tone?: Tone; title?: ReactNode; children?: ReactNode; action?: ReactNode; className?: string }) {
  const s = styles[tone]
  return (
    <div className={cn('enter flex items-start gap-3 rounded-card border p-3.5 text-sm', s.box, className)} role={tone === 'danger' ? 'alert' : 'status'}>
      <s.Icon size={18} className={cn('shrink-0 mt-0.5', s.icon)} aria-hidden />
      <div className="min-w-0 flex-1">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={cn(title ? 'text-muted mt-0.5' : null)}>{children}</div>}
      </div>
      {action}
    </div>
  )
}
