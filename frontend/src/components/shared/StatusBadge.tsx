import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, CalendarClock, CheckCircle2, CircleHelp, Clock, OctagonAlert, ShieldCheck, UserCheck } from 'lucide-react'
import type { ObligationStatus, RiskLevel, Transaction } from '../../types'
import { cn } from '../../lib/utils'

export type Tone = 'neutral' | 'success' | 'warning' | 'danger'

const tones: Record<Tone, string> = {
  neutral: 'bg-app text-muted border-line',
  success: 'bg-primary-soft text-primary border-transparent',
  warning: 'bg-warning-soft text-warning border-transparent',
  danger: 'bg-danger-soft text-danger border-transparent',
}

export function Badge({ tone = 'neutral', icon, children }: { tone?: Tone; icon?: ReactNode; children: ReactNode }) {
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap', tones[tone])}>
      {icon}
      {children}
    </span>
  )
}

const obligation: Record<ObligationStatus, { label: string; tone: Tone; icon: ReactNode }> = {
  planned: { label: 'status.planned', tone: 'neutral', icon: <Clock size={12} aria-hidden /> },
  expected: { label: 'status.expected', tone: 'success', icon: <CalendarClock size={12} aria-hidden /> },
  completed: { label: 'status.completed', tone: 'neutral', icon: <CheckCircle2 size={12} aria-hidden /> },
  overdue: { label: 'status.overdue', tone: 'danger', icon: <OctagonAlert size={12} aria-hidden /> },
}

export function ObligationStatusBadge({ status }: { status: ObligationStatus }) {
  const { t } = useTranslation()
  const s = obligation[status]
  return <Badge tone={s.tone} icon={s.icon}>{t(s.label)}</Badge>
}

/** How reliable the category of a transaction is. */
export function CategoryStatusBadge({ tx }: { tx: Pick<Transaction, 'manual' | 'category_confidence'> }) {
  const { t } = useTranslation()
  if (tx.manual) return <Badge tone="success" icon={<UserCheck size={12} aria-hidden />}>{t('status.txConfirmed')}</Badge>
  if (tx.category_confidence < 0.5) return <Badge tone="warning" icon={<CircleHelp size={12} aria-hidden />}>{t('status.txReview')}</Badge>
  return <Badge icon={<CheckCircle2 size={12} aria-hidden />}>{t('status.txAuto')}</Badge>
}

const risk: Record<RiskLevel, { label: string; tone: Tone; icon: ReactNode }> = {
  none: { label: 'risk.badge.none', tone: 'success', icon: <ShieldCheck size={12} aria-hidden /> },
  low: { label: 'risk.badge.low', tone: 'success', icon: <ShieldCheck size={12} aria-hidden /> },
  medium: { label: 'risk.badge.medium', tone: 'warning', icon: <AlertTriangle size={12} aria-hidden /> },
  high: { label: 'risk.badge.high', tone: 'danger', icon: <OctagonAlert size={12} aria-hidden /> },
}

export function RiskBadge({ level }: { level: RiskLevel }) {
  const { t } = useTranslation()
  const s = risk[level]
  return <Badge tone={s.tone} icon={s.icon}>{t(s.label)}</Badge>
}
