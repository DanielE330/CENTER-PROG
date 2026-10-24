import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowRightLeft, Check, Copy, KeyRound, Loader2, Plus, RotateCcw, Trash2, TriangleAlert, UserRound } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Badge } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { Checkbox } from '../../components/ui/Checkbox'
import { Input } from '../../components/ui/Input'
import { Modal } from '../../components/ui/Modal'
import { PasswordInput } from '../../components/ui/PasswordInput'
import { Select } from '../../components/ui/Select'
import { useAuth } from '../../app/auth'
import { usePerms } from '../../app/perms'
import { api } from '../../lib/api'
import { formatDate } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import { ChangePasswordForm } from '../auth/AccessGates'
import type { Member, Role } from '../../types'

const KEY = ['account', 'members'] as const
type Assignable = Exclude<Role, 'director'>

/**
 * Сотрудники компании. Список видят все; добавляют, меняют роль, сбрасывают пароль и удаляют владелец и администратор
 * (администратора назначает и убирает только владелец); владение передаёт только владелец.
 */
export function MembersPanel() {
  const { t } = useTranslation()
  const { me } = useAuth()
  const { admin, manage } = usePerms()
  const { setMe } = useAuth()
  const qc = useQueryClient()
  const list = useQuery({ queryKey: KEY, queryFn: api.members })
  const [addOpen, setAddOpen] = useState(false)
  const [pwdOpen, setPwdOpen] = useState(false)
  const [pwdDone, setPwdDone] = useState(false)
  const [reveal, setReveal] = useState<{ email: string; password: string; reset: boolean } | null>(null)
  // письмо со ссылкой ушло сотруднику: приглашение или сброс пароля
  const [mailed, setMailed] = useState<{ email: string; reset: boolean } | null>(null)
  const [toDelete, setToDelete] = useState<Member | null>(null)
  const [toTransfer, setToTransfer] = useState<Member | null>(null)

  const refresh = () => void qc.invalidateQueries({ queryKey: KEY })
  const setRole = useMutation({ mutationFn: (v: { id: string; role: Assignable }) => api.setMemberRole(v.id, v.role), onSuccess: refresh })
  const reset = useMutation({
    mutationFn: (m: Member) => api.resetMemberPassword(m.user_id).then((r) => ({ m, r })),
    onSuccess: ({ m, r }) => {
      setMailed(null)
      if (r.email_sent) setMailed({ email: m.email ?? '', reset: true })
      else if (r.password) setReveal({ email: m.email ?? '', password: r.password, reset: true })
    },
  })
  const remove = useMutation({
    mutationFn: (m: Member) => api.deleteMember(m.user_id),
    onSuccess: () => {
      setToDelete(null)
      refresh()
      // the removed employee's API tokens are revoked on the server
      void qc.invalidateQueries({ queryKey: ['account', 'tokens'] })
    },
  })

  const transfer = useMutation({
    mutationFn: (m: Member) => api.transferOwnership(m.user_id),
    onSuccess: async () => {
      setToTransfer(null)
      refresh()
      setMe(await api.me()) // прежний владелец становится администратором: права в интерфейсе обновляются сразу
    },
  })

  const items = list.data?.items ?? []
  const error = setRole.error ?? reset.error
  const assignable: Assignable[] = manage ? ['admin', 'editor', 'viewer'] : ['editor', 'viewer']

  return (
    <>
    <Card
      title={t('members.title')}
      subtitle={admin ? t('members.subtitleManage') : t('members.subtitleView')}
      action={
        <div className="flex flex-wrap justify-end gap-2">
          <Button size="sm" onClick={() => { setPwdDone(false); setPwdOpen(true) }}>
            <KeyRound size={14} aria-hidden /> {t('password.change')}
          </Button>
          {admin && (
            <Button size="sm" variant="primary" onClick={() => setAddOpen(true)}>
              <Plus size={14} aria-hidden /> {t('members.add')}
            </Button>
          )}
        </div>
      }
    >
      <div className="px-5 pb-5 flex flex-col gap-3">
        {list.isPending && <LoadingState height={100} />}
        {list.isError && <ErrorState message={errorText(t, list.error)} onRetry={() => void list.refetch()} />}
        {list.data && items.length === 0 && <p className="text-sm text-muted">{t('members.empty')}</p>}
        {items.length > 0 && (
          <ul className="flex flex-col gap-2" aria-label={t('members.title')}>
            {items.map((m) => {
              const isDirector = m.role === 'director'
              const mine = m.is_me ?? (!!me?.email && (m.email ?? '').toLowerCase() === me.email.toLowerCase())
              const who = m.email ?? t('members.noEmail')
              // администратор управляет аналитиками и наблюдателями; администраторов и себя — нет
              const manageable = admin && !isDirector && !mine && (m.role !== 'admin' || manage)
              return (
                <li key={m.user_id} className="rounded-control border border-line px-3.5 py-2.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
                  <div className="flex items-center gap-2.5 min-w-[12rem] flex-1">
                    <UserRound size={16} className="text-muted shrink-0" aria-hidden />
                    <div className="min-w-0">
                      <p className="text-sm font-medium truncate">
                        {who} {mine && <span className="text-xs text-muted font-normal">· {t('members.you')}</span>}
                      </p>
                      <p className="text-xs text-muted">{t('members.added', { date: formatDate(m.created_at.slice(0, 10)) })}</p>
                    </div>
                  </div>
                  <div className="flex max-w-full flex-wrap items-center gap-2">
                    {manageable ? (
                      <Select
                        className="w-44"
                        aria-label={t('members.roleOf', { email: who })}
                        value={m.role}
                        disabled={setRole.isPending}
                        onChange={(r) => setRole.mutate({ id: m.user_id, role: r as Assignable })}
                        options={assignable.map((r) => ({ value: r, label: t(`role.${r}`) }))}
                      />
                    ) : (
                      <Badge tone={isDirector ? 'success' : 'neutral'}>{t(`role.${m.role}`)}</Badge>
                    )}
                    {manage && !isDirector && !mine && (
                      <Button size="sm" onClick={() => { transfer.reset(); setToTransfer(m) }} aria-label={t('members.transferAria', { email: who })}>
                        <ArrowRightLeft size={14} aria-hidden /> {t('members.transfer')}
                      </Button>
                    )}
                    {manageable && (
                      <>
                        <Button size="sm" disabled={reset.isPending} onClick={() => reset.mutate(m)} aria-label={t('members.resetAria', { email: who })}>
                          {reset.isPending && reset.variables?.user_id === m.user_id ? <Loader2 size={14} className="spin" aria-hidden /> : <RotateCcw size={14} aria-hidden />} {t('members.reset')}
                        </Button>
                        <Button size="sm" variant="danger" onClick={() => { remove.reset(); setToDelete(m) }} aria-label={t('members.deleteAria', { email: who })}>
                          <Trash2 size={14} aria-hidden /> {t('members.delete')}
                        </Button>
                      </>
                    )}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
        {error != null && <Notice tone="danger">{errorText(t, error)}</Notice>}
        {mailed && (
          <Notice tone="success" title={t(mailed.reset ? 'members.resetMailedTitle' : 'members.inviteMailedTitle')}>
            {t(mailed.reset ? 'members.resetMailedText' : 'members.inviteMailedText', { email: mailed.email })}
          </Notice>
        )}
        <p className="text-xs text-muted">{t('members.rolesNote')}</p>
      </div>
    </Card>

      <AddMemberModal
        assignable={assignable}
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onCreated={(r) => {
          refresh()
          setAddOpen(false)
          setMailed(r.invite_sent ? { email: r.email, reset: false } : null)
          if (r.password) setReveal({ email: r.email, password: r.password, reset: false })
        }}
      />
      <RevealPasswordModal data={reveal} onClose={() => setReveal(null)} />

      <Modal open={toTransfer != null} onClose={() => setToTransfer(null)} title={t('members.transferTitle')}>
        <div className="flex flex-col gap-4">
          <p className="text-sm">{t('members.transferText', { email: toTransfer?.email ?? t('members.noEmail') })}</p>
          {transfer.isError && <Notice tone="danger">{errorText(t, transfer.error)}</Notice>}
          <div className="flex gap-2">
            <Button variant="danger" disabled={transfer.isPending} onClick={() => toTransfer && transfer.mutate(toTransfer)}>
              {transfer.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <ArrowRightLeft size={16} aria-hidden />} {t('members.transferConfirm')}
            </Button>
            <Button variant="ghost" onClick={() => setToTransfer(null)}>{t('common.cancel')}</Button>
          </div>
        </div>
      </Modal>

      <Modal open={toDelete != null} onClose={() => setToDelete(null)} title={t('members.deleteTitle')}>
        <div className="flex flex-col gap-4">
          <p className="text-sm">{t('members.deleteText', { email: toDelete?.email ?? t('members.noEmail') })}</p>
          {remove.isError && <Notice tone="danger">{errorText(t, remove.error)}</Notice>}
          <div className="flex gap-2">
            <Button variant="danger" disabled={remove.isPending} onClick={() => toDelete && remove.mutate(toDelete)}>
              {remove.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <Trash2 size={16} aria-hidden />} {t('members.deleteConfirm')}
            </Button>
            <Button variant="ghost" onClick={() => setToDelete(null)}>{t('common.cancel')}</Button>
          </div>
        </div>
      </Modal>

      <Modal open={pwdOpen} onClose={() => setPwdOpen(false)} title={t('password.change')}>
        {pwdDone ? (
          <div className="flex flex-col gap-4">
            <Notice tone="success">{t('password.changed')}</Notice>
            <div><Button onClick={() => setPwdOpen(false)}>{t('common.close')}</Button></div>
          </div>
        ) : (
          <ChangePasswordForm forced onDone={() => setPwdDone(true)} />
        )}
      </Modal>
    </>
  )
}

function AddMemberModal({ open, onClose, onCreated, assignable }: { open: boolean; onClose: () => void; onCreated: (r: { email: string; password?: string; invite_sent?: boolean }) => void; assignable: Assignable[] }) {
  const { t } = useTranslation()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Assignable>('viewer')
  const [generate, setGenerate] = useState(true)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const add = useMutation({
    mutationFn: () => api.addMember({ email: email.trim(), role, ...(generate ? { generate: true } : { password }) }),
    onSuccess: (r) => {
      onCreated(r)
      setEmail('')
      setPassword('')
      setGenerate(true)
      setRole('viewer')
      setError('')
    },
    onError: (e) => setError(errorText(t, e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (!generate && (password.length < 8 || password.length > 128)) return setError(t('apiErrors.invalid_password'))
    add.mutate()
  }

  return (
    <Modal open={open} onClose={onClose} title={t('members.addTitle')}>
      <form className="flex flex-col gap-4" onSubmit={submit}>
        <Input label={t('auth.email')} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="off" />
        <Select
          label={t('members.role')}
          value={role}
          onChange={(v) => setRole(v as Assignable)}
          hint={t(`role.${role}Hint`)}
          options={assignable.map((r) => ({ value: r, label: t(`role.${r}`) }))}
        />
        <Checkbox label={t('members.generate')} checked={generate} onChange={(e) => setGenerate(e.target.checked)} />
        {!generate && (
          <PasswordInput label={t('auth.password')} value={password} onChange={setPassword} required minLength={8} maxLength={128} autoComplete="new-password" hint={t('members.manualHint')} />
        )}
        {generate && <p className="text-xs text-muted">{t('members.generateHint')}</p>}
        {error && <Notice tone="danger">{error}</Notice>}
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={add.isPending || !email.trim()}>
            {add.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <Plus size={16} aria-hidden />} {t('members.addSubmit')}
          </Button>
          <Button variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>
        </div>
      </form>
    </Modal>
  )
}

/** A generated or reset password is shown exactly once. */
function RevealPasswordModal({ data, onClose }: { data: { email: string; password: string; reset: boolean } | null; onClose: () => void }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(data!.password)
      setCopied(true)
    } catch {
      /* clipboard unavailable: the password is selectable in the field */
    }
  }
  const close = () => { setCopied(false); onClose() }
  return (
    <Modal open={data != null} onClose={close} title={data?.reset ? t('members.resetDoneTitle') : t('members.createdTitle')}>
      {data && (
        <div className="flex flex-col gap-4">
          <p className="text-sm">{t(data.reset ? 'members.resetDoneText' : 'members.createdText', { email: data.email })}</p>
          <div className="flex items-center gap-2">
            <input
              readOnly
              value={data.password}
              aria-label={t('members.passwordAria')}
              onFocus={(e) => e.currentTarget.select()}
              className="tnum min-w-0 flex-1 rounded-control border border-line bg-surface px-3 py-2 text-sm font-mono"
            />
            <Button size="sm" onClick={() => void copy()}>
              {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />} {copied ? t('apiTokens.copied') : t('apiTokens.copy')}
            </Button>
          </div>
          <Notice tone="warning" title={<span className="inline-flex items-center gap-1.5"><TriangleAlert size={14} aria-hidden /> {t('members.onceTitle')}</span>}>
            {t('members.onceText')}
          </Notice>
          <div><Button variant="primary" onClick={close}>{t('members.savedIt')}</Button></div>
        </div>
      )}
    </Modal>
  )
}
