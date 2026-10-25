import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Plus, Trash2 } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Checkbox } from '../../components/ui/Checkbox'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import type { Department, TgKind, TgRole } from '../../types'

const KEY = ['notifications', 'team'] as const
const ALL_KINDS: TgKind[] = ['risk', 'week', 'month', 'gap', 'missed']
const ROLES: TgRole[] = ['viewer', 'rater', 'admin']
const NONE = ''

/** Departments with group policies, chat assignment and member roles. Shown only when a bot is connected. */
export function TeamPolicies() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const team = useQuery({ queryKey: KEY, queryFn: api.tgTeam })
  const [name, setName] = useState('')

  const done = () => {
    void qc.invalidateQueries({ queryKey: KEY })
    void qc.invalidateQueries({ queryKey: ['notifications'] })
  }
  const add = useMutation({
    mutationFn: () => api.tgAddDepartment({ name: name.trim(), kinds: ALL_KINDS, allow_excel: true, default_role: 'viewer' }),
    onSuccess: () => {
      setName('')
      done()
    },
  })
  const patchDept = useMutation({ mutationFn: ({ id, b }: { id: number; b: Parameters<typeof api.tgPatchDepartment>[1] }) => api.tgPatchDepartment(id, b), onSuccess: done })
  const delDept = useMutation({ mutationFn: (id: number) => api.tgDeleteDepartment(id), onSuccess: done })
  const chatDept = useMutation({ mutationFn: ({ chat, dept }: { chat: number; dept: number | null }) => api.tgChatDepartment(chat, dept), onSuccess: done })
  const member = useMutation({ mutationFn: ({ uid, b }: { uid: number; b: Parameters<typeof api.tgPatchMember>[1] }) => api.tgPatchMember(uid, b), onSuccess: done })
  const delMember = useMutation({ mutationFn: (uid: number) => api.tgDeleteMember(uid), onSuccess: done })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    add.mutate()
  }
  const toggleKind = (d: Department, k: TgKind, on: boolean) => {
    const kinds = on ? [...d.kinds, k] : d.kinds.filter((x) => x !== k)
    patchDept.mutate({ id: d.id, b: { kinds } })
  }
  const roleOptions = ROLES.map((r) => ({ value: r, label: t(`team.roles.${r}`) }))
  const err = add.error ?? patchDept.error ?? delDept.error ?? chatDept.error ?? member.error ?? delMember.error

  if (team.isPending) return <Card title={t('team.title')}><div className="px-5 pb-5"><LoadingState height={80} /></div></Card>
  if (team.isError) return <Card title={t('team.title')}><ErrorState message={errorText(t, team.error)} onRetry={() => void team.refetch()} /></Card>
  const data = team.data
  const deptOptions = [{ value: NONE, label: t('team.noDepartment') }, ...data.departments.map((d) => ({ value: d.id, label: d.name }))]

  return (
    <Card title={t('team.title')} subtitle={t('team.subtitle')}>
      <div className="px-5 pb-5 flex flex-col gap-5">
        <section className="flex flex-col gap-3">
          <p className="text-[13px] font-medium">{t('team.departments')}</p>
          {data.departments.length === 0 && <p className="text-sm text-muted">{t('team.noDepartments')}</p>}
          {data.departments.map((d) => (
            <div key={d.id} className="rounded-control border border-line p-3.5 flex flex-col gap-3">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-medium truncate">{d.name}</p>
                <Button size="sm" disabled={delDept.isPending} onClick={() => delDept.mutate(d.id)}><Trash2 size={14} aria-hidden /> {t('team.delete')}</Button>
              </div>
              <div className="flex flex-wrap gap-x-5 gap-y-2">
                {ALL_KINDS.map((k) => (
                  <Checkbox key={k} label={t(`team.kinds.${k}`)} checked={d.kinds.includes(k)} disabled={patchDept.isPending} onChange={(e) => toggleKind(d, k, e.target.checked)} />
                ))}
                <Checkbox label={t('team.excel')} checked={!!d.allow_excel} disabled={patchDept.isPending} onChange={(e) => patchDept.mutate({ id: d.id, b: { allow_excel: e.target.checked } })} />
              </div>
              <div className="sm:w-64">
                <Select label={t('team.defaultRole')} value={d.default_role} options={roleOptions} onChange={(v) => patchDept.mutate({ id: d.id, b: { default_role: v as TgRole } })} />
              </div>
            </div>
          ))}
          <form className="flex flex-col sm:flex-row sm:items-end gap-3" onSubmit={submit}>
            <div className="flex-1">
              <Input label={t('team.name')} placeholder={t('team.namePlaceholder')} value={name} onChange={(e) => setName(e.target.value)} maxLength={60} required />
            </div>
            <Button type="submit" variant="primary" disabled={add.isPending || !name.trim()}><Plus size={16} aria-hidden /> {t('team.add')}</Button>
          </form>
        </section>

        {data.chats.length > 0 && (
          <section className="flex flex-col gap-3">
            <p className="text-[13px] font-medium">{t('team.chats')}</p>
            {data.chats.map((c) => (
              <div key={c.chat_id} className="grid grid-cols-1 gap-2 sm:grid-cols-2 sm:items-center">
                <span className="text-sm truncate">{c.title || String(c.chat_id)}</span>
                <Select aria-label={t('team.department')} value={c.department_id ?? NONE} options={deptOptions}
                  onChange={(v) => chatDept.mutate({ chat: c.chat_id, dept: v === NONE ? null : Number(v) })} />
              </div>
            ))}
          </section>
        )}

        <section className="flex flex-col gap-3">
          <p className="text-[13px] font-medium">{t('team.members')}</p>
          {data.members.length === 0 && <p className="text-sm text-muted">{t('team.noMembers')}</p>}
          {data.members.map((m) => (
            <div key={m.tg_user_id} className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-center">
              <span className="text-sm truncate">{m.name || m.tg_user_id}</span>
              <Select aria-label={t('team.role')} value={m.role ?? NONE} options={[{ value: NONE, label: t('team.inherit') }, ...roleOptions]}
                onChange={(v) => member.mutate({ uid: m.tg_user_id, b: { role: v as TgRole | '' } })} />
              <Select aria-label={t('team.department')} value={m.department_id ?? NONE} options={deptOptions}
                onChange={(v) => member.mutate({ uid: m.tg_user_id, b: { department_id: v === NONE ? null : Number(v) } })} />
              <Button size="sm" aria-label={t('team.remove')} disabled={delMember.isPending} onClick={() => delMember.mutate(m.tg_user_id)}><Trash2 size={14} aria-hidden /></Button>
            </div>
          ))}
        </section>

        {err != null && <Notice tone="danger">{errorText(t, err)}</Notice>}
      </div>
    </Card>
  )
}
