import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Check, Copy, Loader2, Plus, Trash2 } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { useAuth, useMe } from '../../app/auth'
import { usePerms } from '../../app/perms'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { Role } from '../../types'

const INVITES = ['team', 'invites'] as const
type Assignable = Exclude<Role, 'director'>
const iso = (sec: number) => new Date(sec * 1000).toISOString()

/**
 * Приглашения по ссылке: одноразовая ссылка с ролью (можно привязать к email, тогда на него уйдёт письмо).
 * Состав команды, роли и передача владения — в «Сотрудниках» (MembersPanel); администратора приглашает только владелец.
 */
export function TeamInvites() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { manage } = usePerms()
  const invites = useQuery({ queryKey: INVITES, queryFn: api.teamInvites })
  const [role, setRole] = useState<Assignable>('viewer')
  const [email, setEmail] = useState('')
  const [emailed, setEmailed] = useState<boolean | null>(null)
  const [limited, setLimited] = useState(false)
  const [fresh, setFresh] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const assignable: Assignable[] = manage ? ['admin', 'editor', 'viewer'] : ['editor', 'viewer']
  const refresh = () => void qc.invalidateQueries({ queryKey: INVITES })
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

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(fresh!)
      setCopied(true)
    } catch {
      /* clipboard unavailable: the link is selectable in the field */
    }
  }
  const err = create.error ?? revoke.error

  return (
    <Card title={t('team.web.invite')} subtitle={t('team.web.inviteSubtitle')}>
      <div className="px-5 pb-5 flex flex-col gap-4">
        <div className="flex flex-col sm:flex-row sm:items-end gap-3">
          <div className="sm:w-56"><Select label={t('team.web.inviteRole')} value={role} options={assignable.map((r) => ({ value: r, label: t(`team.web.${r}`) }))} onChange={(v) => setRole(v as Assignable)} /></div>
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
        {err != null && <Notice tone="danger">{errorText(t, err)}</Notice>}
      </div>
    </Card>
  )
}

/** Участник чужой компании (не владелец): выйти из команды и вернуться в собственную компанию. */
export function LeaveTeam() {
  const { t } = useTranslation()
  const me = useMe()
  const { setMe } = useAuth()
  const leave = useMutation({ mutationFn: api.leaveTeam, onSuccess: async () => setMe(await api.me()) })
  if (me.role === undefined || me.role === 'director') return null
  return (
    <Card title={t('team.web.leaveTitle')} subtitle={t('team.web.leaveText', { role: t(`team.web.${me.role}`) })}>
      <div className="px-5 pb-5 flex flex-col gap-3">
        {leave.isError && <Notice tone="danger">{errorText(t, leave.error)}</Notice>}
        <div><Button variant="danger" disabled={leave.isPending} onClick={() => leave.mutate()}>{t('team.web.leave')}</Button></div>
      </div>
    </Card>
  )
}
