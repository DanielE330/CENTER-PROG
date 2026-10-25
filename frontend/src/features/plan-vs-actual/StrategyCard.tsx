import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ClipboardList, MessageSquare, Sparkles } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { Badge } from '../../components/shared/StatusBadge'
import type { Tone } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { useActivePlan, useDeletePlan } from '../../hooks/queries'
import { useEditGuard } from '../../app/perms'
import { formatCurrency, formatDate, formatDateTime } from '../../lib/formatters'
import { ApiError } from '../../lib/errors'
import type { StrategyStep } from '../assistant/advisorTypes'

const kindTone: Record<StrategyStep['kind'], Tone> = {
  income: 'success', reserve: 'success', expense: 'warning', tax: 'warning', negotiation: 'neutral', financing: 'neutral', control: 'neutral',
}

/** The strategy the AI advisor saved in plan mode (advisor/chats.py): goal, dated steps, risks, what to watch here. */
export function StrategyCard() {
  const { t } = useTranslation()
  const q = useActivePlan()
  const del = useDeletePlan()
  const guard = useEditGuard()
  const [all, setAll] = useState(false)
  if (q.isPending || (q.error instanceof ApiError && q.error.code === 'advisor_disabled')) return null
  const plan = q.data?.plan
  const today = new Date().toISOString().slice(0, 10)

  if (!plan) {
    return (
      <div id="strategy" className="card px-5 py-4 flex flex-wrap items-center gap-3 scroll-mt-20">
        <span className="grid place-items-center size-9 rounded-full bg-primary-soft text-primary shrink-0" aria-hidden><Sparkles size={18} /></span>
        <div className="flex-1 min-w-[200px]">
          <p className="font-semibold text-[15px]">{t('planVsActual.strategy.emptyTitle')}</p>
          <p className="text-sm text-muted">{t('planVsActual.strategy.emptyText')}</p>
        </div>
        <Link to="/assistant?new=plan" className="inline-flex items-center gap-2 rounded-lg bg-primary text-on-primary px-4 py-2 text-sm font-medium hover:bg-primary-hover">
          <ClipboardList size={16} aria-hidden /> {t('planVsActual.strategy.make')}
        </Link>
      </div>
    )
  }

  return (
    <div id="strategy" className="scroll-mt-20">
      <Card
        title={t('planVsActual.strategy.title', { title: plan.title })}
        subtitle={t('planVsActual.strategy.subtitle', { date: formatDateTime(plan.created_at) })}
        action={<Badge tone="success">{t('planVsActual.strategy.badge')}</Badge>}
      >
        <div className="px-5 pb-5 flex flex-col gap-4">
          {plan.summary && <p className={`text-sm whitespace-pre-line ${all ? '' : 'line-clamp-3'}`}>{plan.summary}</p>}
          {plan.goal && (
            <p className="text-sm rounded-lg bg-primary-soft px-3 py-2"><span className="font-semibold">{t('planVsActual.strategy.goal')}</span> {plan.goal}</p>
          )}
          <ol className="flex flex-col gap-2">
            {(all ? plan.steps : plan.steps.slice(0, 3)).map((s, i) => {
              const overdue = !!s.due_date && s.due_date < today
              return (
                <li key={i} className="flex gap-3 rounded-lg border border-line px-3 py-2.5">
                  <span className="grid place-items-center size-6 rounded-full bg-app border border-line text-xs font-semibold shrink-0">{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-sm">{s.title}</span>
                      <Badge tone={kindTone[s.kind] ?? 'neutral'}>{t(`planVsActual.strategy.kind.${s.kind}`)}</Badge>
                    </div>
                    {s.detail && <p className="text-[13px] text-muted mt-0.5">{s.detail}</p>}
                  </div>
                  <div className="text-right shrink-0 text-[13px]">
                    {s.amount_rub != null && <div className={`tnum font-medium ${s.amount_rub < 0 ? 'text-danger' : 'text-primary'}`}>{s.amount_rub > 0 ? '+' : ''}{formatCurrency(s.amount_rub)}</div>}
                    {s.due_date && <div className={overdue ? 'text-danger' : 'text-muted'}>{overdue ? t('planVsActual.strategy.overdue', { date: formatDate(s.due_date, 'dayMonth') }) : t('planVsActual.strategy.due', { date: formatDate(s.due_date, 'dayMonth') })}</div>}
                  </div>
                </li>
              )
            })}
          </ol>
          {!all && (
            <Button variant="ghost" className="self-start" onClick={() => setAll(true)}>
              {t('planVsActual.strategy.showAll', { count: plan.steps.length })}
            </Button>
          )}
          {all && (plan.risks.length > 0 || plan.checkpoints.length > 0) && (
            <div className="grid gap-4 sm:grid-cols-2">
              {plan.checkpoints.length > 0 && (
                <div>
                  <p className="text-sm font-semibold mb-1">{t('planVsActual.strategy.checkpoints')}</p>
                  <ul className="list-disc pl-5 text-[13px] flex flex-col gap-0.5">{plan.checkpoints.map((c) => <li key={c}>{c}</li>)}</ul>
                </div>
              )}
              {plan.risks.length > 0 && (
                <div>
                  <p className="text-sm font-semibold mb-1">{t('planVsActual.strategy.risks')}</p>
                  <ul className="list-disc pl-5 text-[13px] flex flex-col gap-0.5">{plan.risks.map((r) => <li key={r}>{r}</li>)}</ul>
                </div>
              )}
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {plan.chat_id && (
              <Link to={`/assistant?chat=${plan.chat_id}`} className="inline-flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-sm font-medium hover:bg-hover">
                <MessageSquare size={16} aria-hidden /> {t('planVsActual.strategy.openChat')}
              </Link>
            )}
            <Link to="/assistant?new=plan" className="inline-flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-sm font-medium hover:bg-hover">
              <ClipboardList size={16} aria-hidden /> {t('planVsActual.strategy.remake')}
            </Link>
            <Button variant="ghost" {...guard.lock} disabled={guard.lock.disabled || del.isPending} onClick={() => del.mutate(plan.id)}>
              {t('planVsActual.strategy.remove')}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  )
}
