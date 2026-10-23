import type { ReactNode } from 'react'
import { AlertTriangle, CalendarClock, CheckCircle2, Clock, HelpCircle, OctagonAlert, ShieldCheck } from 'lucide-react'
import type { ObligationStatus, RiskLevel, TransactionStatus } from '../../types'
import { cn } from '../../lib/utils'

type Tone = 'neutral' | 'success' | 'warning' | 'danger'

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
  planned: { label: 'Запланирован', tone: 'neutral', icon: <Clock size={12} /> },
  expected: { label: 'Ожидается', tone: 'success', icon: <CalendarClock size={12} /> },
  completed: { label: 'Выполнен', tone: 'neutral', icon: <CheckCircle2 size={12} /> },
  overdue: { label: 'Просрочен', tone: 'danger', icon: <OctagonAlert size={12} /> },
}

export function ObligationStatusBadge({ status }: { status: ObligationStatus }) {
  const s = obligation[status]
  return <Badge tone={s.tone} icon={s.icon}>{s.label}</Badge>
}

export function TransactionStatusBadge({ status }: { status?: TransactionStatus }) {
  if (!status) return <span className="text-muted">—</span>
  return status === 'completed' ? (
    <Badge tone="success" icon={<CheckCircle2 size={12} />}>Проведена</Badge>
  ) : (
    <Badge tone="warning" icon={<Clock size={12} />}>В обработке</Badge>
  )
}

const risk: Record<RiskLevel, { label: string; tone: Tone; icon: ReactNode }> = {
  low: { label: 'Низкий риск', tone: 'success', icon: <ShieldCheck size={12} /> },
  medium: { label: 'Средний риск', tone: 'warning', icon: <AlertTriangle size={12} /> },
  high: { label: 'Высокий риск', tone: 'danger', icon: <OctagonAlert size={12} /> },
  unknown: { label: 'Риск не оценён', tone: 'neutral', icon: <HelpCircle size={12} /> },
}

export function RiskBadge({ level }: { level: RiskLevel }) {
  const s = risk[level]
  return <Badge tone={s.tone} icon={s.icon}>{s.label}</Badge>
}

/** Marks values that come from demo data rather than a real source. */
export function MockBadge({ label = 'Демо-данные' }: { label?: string }) {
  return <Badge tone="warning">{label}</Badge>
}
