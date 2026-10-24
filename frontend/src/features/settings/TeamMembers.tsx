import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Check, Copy, Loader2, Plus, Trash2 } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { useAuth, useMe } from '../../app/auth'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { WebRole } from '../../types'

const MEMBERS = ['team', 'members'] as const
const INVITES = ['team', 'invites'] as const
type Assignable = Exclude<WebRole, 'owner'>
const iso = (sec: number) => new Date(sec * 1000).toISOString()

/** Web team: invite links with a role, member roles, removal. An admin manages analysts and observers, the owner manages everyone. */
export function TeamMembers() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const me = useMe()
  const { setMe } = useAuth()
  const members = useQuery({ queryKey: MEMBERS, queryFn: api.teamMembers })
  const invites = useQuery({ queryKey: INVITES, queryFn: api.teamInvites })
  const [role, setRole] = useState<Assignable>('observer')
  const [email, setEmail] = useState('')
  const [emailed, setEmailed] = useState<boolean | null>(null)
  const [limited, setLimited] = useState(false)
  const [confirmTransfer, setConfirmTransfer] = useState<string | null>(null)
  const [fresh, setFresh] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const assignable: Assignable[] = me.role === 'owner' ? ['admin', 'analyst', 'observer'] : ['analyst', 'observer']
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: MEMBERS })
    void qc.invalidateQueries({ queryKey: INVITES })
  }
  const create = useMutation({
    mutationFn: () => api.createInvite(role, email.trim().toLowerCase()),
    onSuccess: (r) => {
      setFresh(`${window.location.origin}/?invite=${r.code}`)
      setEmailed(r.email ? r.emailed : null)
      setLimited(!!r.email_limited)
      setEmail('')
      setCopied(false)
      refresh()
    },
  })
  const revoke = useMutation({ mutationFn: (id: string) => api.deleteInvite(id), onSuccess: refresh })
  const setRoleM = useMutation({ mutationFn: ({ uid, r }: { uid: string; r: Assignable }) => api.setMemberRole(uid, r), onSuccess: refresh })
  const transfer = useMutation({
    mutationFn: (uid: string) => api.transferOwnership(uid),
    onSuccess: async () => {
      setConfirmTransfer(null)
      setMe(await api.me())
    },
  })
  const remove = useMutation({ mutationFn: (uid: string) => api.removeMember(uid), onSuccess: refresh })

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(fresh!)
      setCopied(true)
    } catch {
      /* clipboard unavailable: the link is selectable in the field */
    }
  }
  const rank = (r: WebRole) => ({ observer: 1, analyst: 2, admin: 3, owner: 4 })[r]
  const roleOptions = (list: Assignable[]) => list.map((r) => ({ value: r, label: t(`team.web.${r}`) }))
  const err = create.error ?? revoke.error ?? setRoleM.error ?? remove.error ?? transfer.error

  return (
    <Card title={t('team.web.title')} subtitle={t('team.web.subtitle')}>
      <div className="px-5 pb-5 flex flex-col gap-5">
        <section className="flex flex-col gap-3">
          <p className="text-[13px] font-medium">{t('team.web.invite')}</p>
          <div className="flex flex-col sm:flex-row sm:items-end gap-3">
            <div className="sm:w-56"><Select label={t('team.web.inviteRole')} value={role} options={roleOptions(assignable)} onChange={(v) => setRole(v as Assignable)} /></div>
            <div className="sm:flex-1"><Input label={t('team.web.inviteEmail')} type="email" placeholder="name@company.ru" hint={t('team.web.inviteEmailHint')} value={email} onChange={(e) => setEmail(e.target.value)} maxLength={254} /></div>
            <Button variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
              {create.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <Plus size={16} aria-hidden />} {t('team.web.createInvite')}
            </Button>
          </div>
          {fresh && (
            <Notice tone="success" title={t('team.web.inviteCreated')}>
              <p className="mb-2">{emailed === true ? t('team.web.emailSent') : emailed === false ? (limited ? t('team.web.emailLimited') : t('team.web.emailNotSent')) : t('team.web.inviteHint')}</p>
              <div className="flex items-center gap-2">
                <input readOnly value={fresh} aria-label={t('team.web.inviteLink')} onFocus={(e) => e.currentTarget.select()} className="tnum min-w-0 flex-1 rounded-control border border-line bg-surface px-3 py-2 text-xs" />
                <Button size="sm" onClick={() => void copy()}>{copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />} {copied ? t('apiTokens.copied') : t('apiTokens.copy')}</Button>
              </div>
            </Notice>
          )}
          {invites.data && invites.data.items.length > 0 && (
            <ul className="flex flex-col gap-2">
              {invites.data.items.map((i) => (
                <li key={i.id} className="rounded-control border border-line px-3.5 py-2 flex items-center justify-between gap-3 text-sm">
                  <span>{t(`team.web.${i.role}`)}{i.email ? ` · ${i.email}` : ''} · {t('team.web.until', { value: formatDateTime(iso(i.expires_at)) })}</span>
                  <Button size="sm" disabled={revoke.isPending} onClick={() => revoke.mutate(i.id)}><Trash2 size={14} aria-hidden /> {t('team.web.revoke')}</Button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="flex flex-col gap-3">
          <p className="text-[13px] font-medium">{t('team.web.members')}</p>
          {members.isPending && <LoadingState height={80} />}
          {members.isError && <ErrorState message={errorText(t, members.error)} onRetry={() => void members.refetch()} />}
          {members.data?.items.map((m) => {
            const locked = m.role === 'owner' || m.user_id === me.user_id || rank(m.role) >= rank(me.role)
            return (
              <div key={m.user_id} className="grid gap-2 sm:grid-cols-[1fr_200px_auto] sm:items-center">
                <span className="text-sm truncate">{m.email ?? (m.label || m.user_id.slice(0, 8))}{m.user_id === me.user_id ? ` (${t('team.web.you')})` : ''}</span>
                {locked ? (
                  <span className="text-sm text-muted">{t(`team.web.${m.role}`)}</span>
                ) : (
                  <Select aria-label={t('team.role')} value={m.role} options={roleOptions(assignable)} onChange={(v) => setRoleM.mutate({ uid: m.user_id, r: v as Assignable })} />
                )}
                <div className="flex items-center gap-2">
                  {me.role === 'owner' && m.role !== 'owner' && (
                    confirmTransfer === m.user_id
                      ? <Button size="sm" variant="danger" disabled={transfer.isPending} onClick={() => transfer.mutate(m.user_id)}>{t('team.web.transferConfirm')}</Button>
                      : <Button size="sm" onClick={() => setConfirmTransfer(m.user_id)}>{t('team.web.transfer')}</Button>
                  )}
                  {!locked && <Button size="sm" aria-label={t('team.remove')} disabled={remove.isPending} onClick={() => remove.mutate(m.user_id)}><Trash2 size={14} aria-hidden /></Button>}
                </div>
              </div>
            )
          })}
        </section>
        {err != null && <Notice tone="danger">{errorText(t, err)}</Notice>}
      </div>
    </Card>
  )
}

/** Shown to a member of someone else's company (not the owner): leave and return to the own workspace. */
export function LeaveTeam() {
  const { t } = useTranslation()
  const me = useMe()
  const { setMe } = useAuth()
  const leave = useMutation({ mutationFn: api.leaveTeam, onSuccess: async () => setMe(await api.me()) })
  if (me.role === 'owner') return null
  return (
    <Card title={t('team.web.leaveTitle')} subtitle={t('team.web.leaveText', { role: t(`team.web.${me.role}`) })}>
      <div className="px-5 pb-5 flex flex-col gap-3">
        {leave.isError && <Notice tone="danger">{errorText(t, leave.error)}</Notice>}
        <div><Button variant="danger" disabled={leave.isPending} onClick={() => leave.mutate()}>{t('team.web.leave')}</Button></div>
      </div>
    </Card>
  )
}
