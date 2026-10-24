import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Loader2, Plus, Trash2 } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Badge } from '../../components/shared/StatusBadge'
import type { Tone } from '../../components/shared/StatusBadge'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { useMe } from '../../app/auth'
import { canEdit } from '../../lib/roles'
import { api } from '../../lib/api'
import { formatDate, formatKop } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { Goal, GoalStatus, SprintView } from '../../types'

const SPRINT = ['notifications', 'sprint'] as const
const HISTORY = ['notifications', 'history'] as const
const STATUSES: GoalStatus[] = ['open', 'done', 'partial', 'failed', 'skipped']
const TONE: Record<GoalStatus, Tone> = { open: 'neutral', done: 'success', partial: 'warning', failed: 'danger', skipped: 'neutral' }
const MOODS = [5, 4, 3, 2]

function GoalRow({ g, onStatus, onDelete, busy }: { g: Goal; onStatus: (s: GoalStatus) => void; onDelete?: () => void; busy: boolean }) {
  const { t } = useTranslation()
  return (
    <li className="rounded-control border border-line px-3.5 py-2.5 flex flex-col sm:flex-row sm:items-center gap-3 justify-between">
      <div className="min-w-0">
        <p className="text-sm font-medium break-words">{g.title}</p>
        <p className="text-xs text-muted">
          {g.source === 'auto' ? t('goals.auto') : t('goals.mine')}
          {g.target_kop != null && g.target_kop > 0 ? ` · ${formatKop(String(g.target_kop))}` : ''}
          {g.due ? ` · ${t('goals.due', { value: formatDate(g.due, 'dayMonth') })}` : ''}
        </p>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Badge tone={TONE[g.status]}>{t(`goals.status.${g.status}`)}</Badge>
        <div className="w-40">
          <Select aria-label={t('goals.status.open')} value={g.status} disabled={busy} onChange={(v) => onStatus(v as GoalStatus)} options={STATUSES.map((s) => ({ value: s, label: t(`goals.status.${s}`) }))} />
        </div>
        {onDelete && (
          <Button size="sm" aria-label={t('goals.delete')} disabled={busy} onClick={onDelete}><Trash2 size={14} aria-hidden /></Button>
        )}
      </div>
    </li>
  )
}

/** Weekly goals: rate last week, plan the new one, retro and completion trend. The bot offers the same actions with buttons. */
export default function GoalsPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const editable = canEdit(useMe())
  const sprint = useQuery({ queryKey: SPRINT, queryFn: api.tgSprint })
  const history = useQuery({ queryKey: HISTORY, queryFn: api.tgHistory })
  const status = useQuery({ queryKey: ['notifications'], queryFn: api.tgStatus })
  const [title, setTitle] = useState('')
  const [amount, setAmount] = useState('')
  const [blockers, setBlockers] = useState<string | null>(null)
  const [retroSaved, setRetroSaved] = useState(false)

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: SPRINT })
    void qc.invalidateQueries({ queryKey: HISTORY })
  }
  const patch = useMutation({ mutationFn: ({ id, s }: { id: number; s: GoalStatus }) => api.tgPatchGoal(id, { status: s }), onSuccess: refresh })
  const remove = useMutation({ mutationFn: (id: number) => api.tgDeleteGoal(id), onSuccess: refresh })
  const add = useMutation({
    mutationFn: () => {
      const rub = Number(amount.replace(',', '.').replace(/\s/g, ''))
      return api.tgAddGoal({ title: title.trim(), ...(rub > 0 ? { target_kop: Math.round(rub * 100) } : {}) })
    },
    onSuccess: () => {
      setTitle('')
      setAmount('')
      refresh()
    },
  })
  const retro = useMutation({
    mutationFn: (b: { mood?: number; blockers?: string }) => api.tgRetro({ week_start: sprint.data!.prev_sprint!, ...b }),
    onSuccess: () => {
      setRetroSaved(true)
      setTimeout(() => setRetroSaved(false), 2000)
      refresh()
    },
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    add.mutate()
  }
  const busy = patch.isPending || remove.isPending || !editable
  const s: SprintView | undefined = sprint.data
  const unrated = s?.prev_goals.some((g) => g.status === 'open') ?? false

  return (
    <PageContainer>
      <PageHeader title={t('goals.title')} description={t('goals.description')} />
      {status.data && <Notice tone={status.data.advisor ? 'success' : 'info'}>{status.data.advisor ? t('goals.advisorOn') : t('goals.advisorOff')}</Notice>}
      {sprint.isPending && <LoadingState height={160} />}
      {sprint.isError && <ErrorState message={errorText(t, sprint.error)} onRetry={() => void sprint.refetch()} />}

      {s && (
        <div className="grid gap-5 lg:grid-cols-2">
          <div className="flex flex-col gap-5">
            <Card title={t('goals.current')} subtitle={s.sprint ? t('goals.since', { value: formatDate(s.sprint, 'dayMonth') }) : undefined}>
              <div className="px-5 pb-5 flex flex-col gap-4">
                {s.goals.length === 0 ? (
                  <p className="text-sm text-muted">{t('goals.empty')}</p>
                ) : (
                  <ul className="flex flex-col gap-2">
                    {s.goals.map((g) => (
                      <GoalRow key={g.id} g={g} busy={busy} onStatus={(st) => patch.mutate({ id: g.id, s: st })} onDelete={editable ? () => remove.mutate(g.id) : undefined} />
                    ))}
                  </ul>
                )}
                {editable && <form className="flex flex-col gap-3 border-t border-line pt-4" onSubmit={submit}>
                  <Input label={t('goals.newTitle')} placeholder={t('goals.newPlaceholder')} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} required />
                  <Input label={t('goals.newAmount')} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
                  <div>
                    <Button type="submit" variant="primary" disabled={add.isPending || !title.trim()}>
                      {add.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <Plus size={16} aria-hidden />} {t('goals.add')}
                    </Button>
                  </div>
                </form>}
                {(patch.isError || remove.isError || add.isError) && <Notice tone="danger">{errorText(t, patch.error ?? remove.error ?? add.error)}</Notice>}
              </div>
            </Card>
          </div>

          <div className="flex flex-col gap-5">
            <Card title={t('goals.previous')} subtitle={s.velocity != null ? t('goals.velocity', { value: `${Math.round(s.velocity * 100)}%` }) : undefined}>
              <div className="px-5 pb-5 flex flex-col gap-4">
                {s.prev_goals.length === 0 ? (
                  <p className="text-sm text-muted">{t('goals.noPrevious')}</p>
                ) : (
                  <>
                    {unrated && <Notice tone="warning">{t('goals.rateHint')}</Notice>}
                    <ul className="flex flex-col gap-2">
                      {s.prev_goals.map((g) => (
                        <GoalRow key={g.id} g={g} busy={busy} onStatus={(st) => patch.mutate({ id: g.id, s: st })} />
                      ))}
                    </ul>
                  </>
                )}
              </div>
            </Card>

            {s.prev_sprint && (
              <Card title={t('goals.retro')}>
                <div className="px-5 pb-5 flex flex-col gap-4">
                  <Select
                    label={t('goals.mood')}
                    value={s.retro?.mood ?? ''}
                    placeholder={t('common.select.placeholder')}
                    onChange={(v) => retro.mutate({ mood: Number(v) })}
                    options={MOODS.map((m) => ({ value: m, label: t(`goals.moods.${m}`) }))}
                  />
                  <Input
                    label={t('goals.blockers')}
                    placeholder={t('goals.blockersPlaceholder')}
                    value={blockers ?? s.retro?.blockers ?? ''}
                    onChange={(e) => setBlockers(e.target.value)}
                    maxLength={500}
                  />
                  <div className="flex items-center gap-3">
                    <Button variant="primary" disabled={retro.isPending || blockers === null} onClick={() => retro.mutate({ blockers: blockers ?? '' })}>{t('goals.saveRetro')}</Button>
                    {retroSaved && <span className="text-sm text-primary" role="status">{t('goals.retroSaved')}</span>}
                  </div>
                  {retro.isError && <Notice tone="danger">{errorText(t, retro.error)}</Notice>}
                </div>
              </Card>
            )}

            <Card title={t('goals.history')}>
              <div className="px-5 pb-5">
                {history.isPending && <LoadingState height={80} />}
                {history.data && (history.data.items.filter((i) => i.velocity != null).length === 0 ? (
                  <p className="text-sm text-muted">{t('goals.historyEmpty')}</p>
                ) : (
                  <ul className="flex flex-col gap-2.5">
                    {history.data.items.filter((i) => i.velocity != null).map((i) => (
                      <li key={i.week_start} className="flex items-center gap-3 text-sm">
                        <span className="w-24 shrink-0 text-muted">{formatDate(i.week_start, 'dayMonth')}</span>
                        <div className="h-2 flex-1 rounded-full bg-hover" role="img" aria-label={`${Math.round((i.velocity ?? 0) * 100)}%`}>
                          <div className="h-2 rounded-full bg-primary" style={{ width: `${Math.round((i.velocity ?? 0) * 100)}%` }} />
                        </div>
                        <span className="tnum w-24 shrink-0 text-right">{t('goals.doneOf', { done: i.done, total: i.total })}</span>
                      </li>
                    ))}
                  </ul>
                ))}
              </div>
            </Card>
          </div>
        </div>
      )}
    </PageContainer>
  )
}
