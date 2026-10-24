import { useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Loader2, Mail, PenLine, Plus, Trash2 } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { PasswordInput } from '../../components/ui/PasswordInput'
import { Select } from '../../components/ui/Select'
import { useAuth } from '../../app/auth'
import { api } from '../../lib/api'
import { listCertificates, signDetached } from '../../lib/cades'
import type { CertInfo } from '../../lib/cades'
import { formatDate } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { AccountMethods as Methods } from '../../types'

const KEY = ['account', 'methods'] as const

/** Sign-in methods of the account: one email and any number of signatures, but never fewer than one method in total. */
export function AccountMethods() {
  const { t } = useTranslation()
  const { setMe } = useAuth()
  const qc = useQueryClient()
  const methods = useQuery({ queryKey: KEY, queryFn: api.accountMethods })
  const [error, setError] = useState<unknown>(null)

  const apply = async (m: Methods) => {
    qc.setQueryData(KEY, m)
    setError(null)
    // the company requisites may have been completed from the certificate
    setMe(await api.me())
  }
  const run = useMutation({
    mutationFn: (fn: () => Promise<Methods>) => fn(),
    onSuccess: apply,
    onError: setError,
  })

  const m = methods.data
  const total = m ? (m.email ? 1 : 0) + m.signatures.length : 0
  const last = total <= 1

  return (
    <Card title={t('account.title')} subtitle={t('account.subtitle')}>
      <div className="px-5 pb-5 flex flex-col gap-5">
        {methods.isPending && <LoadingState height={120} />}
        {methods.isError && <ErrorState message={errorText(t, methods.error)} onRetry={() => void methods.refetch()} />}
        {m && (
          <>
            <section className="flex flex-col gap-2" aria-label={t('account.emailTitle')}>
              <h3 className="text-[13px] font-semibold">{t('account.emailTitle')}</h3>
              {m.email ? (
                <Row icon={<Mail size={16} aria-hidden />} title={m.email} hint={t('account.emailHint')}>
                  <Button
                    size="sm"
                    disabled={last || run.isPending}
                    title={last ? t('account.lastMethod') : undefined}
                    onClick={() => run.mutate(api.removeEmail)}
                  >
                    <Trash2 size={14} aria-hidden /> {t('account.remove')}
                  </Button>
                </Row>
              ) : (
                <AddEmail busy={run.isPending} onSubmit={(b) => run.mutate(() => api.addEmail(b))} />
              )}
            </section>

            <section className="flex flex-col gap-2" aria-label={t('account.signaturesTitle')}>
              <h3 className="text-[13px] font-semibold">{t('account.signaturesTitle')}</h3>
              {m.signatures.length === 0 && <p className="text-sm text-muted">{t('account.noSignatures')}</p>}
              {m.signatures.map((s) => (
                <Row
                  key={s.id}
                  icon={<PenLine size={16} aria-hidden />}
                  title={s.label || t('account.signatureNoName')}
                  hint={t('account.signatureHint', { fp: s.id.slice(0, 8), date: formatDate(new Date(s.created_at * 1000).toISOString().slice(0, 10)) })}
                >
                  <Button
                    size="sm"
                    disabled={last || run.isPending}
                    title={last ? t('account.lastMethod') : undefined}
                    onClick={() => run.mutate(() => api.removeSignature(s.id))}
                  >
                    <Trash2 size={14} aria-hidden /> {t('account.remove')}
                  </Button>
                </Row>
              ))}
              <AddSignature busy={run.isPending} onDone={apply} onError={setError} />
            </section>
          </>
        )}
        {last && m && <p className="text-xs text-muted">{t('account.lastMethodNote')}</p>}
        {error != null && <Notice tone="danger">{errorText(t, error)}</Notice>}
      </div>
    </Card>
  )
}

function Row({ icon, title, hint, children }: { icon: ReactNode; title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="rounded-control border border-line px-3.5 py-2.5 flex items-center justify-between gap-3">
      <div className="flex items-center gap-2.5 min-w-0">
        <span className="text-muted shrink-0">{icon}</span>
        <div className="min-w-0">
          <p className="text-sm font-medium truncate">{title}</p>
          {hint && <p className="text-xs text-muted truncate">{hint}</p>}
        </div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

function AddEmail({ busy, onSubmit }: { busy: boolean; onSubmit: (b: { email: string; password: string; password_confirm: string }) => void }) {
  const { t } = useTranslation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const submit = (e: FormEvent) => {
    e.preventDefault()
    onSubmit({ email: email.trim(), password, password_confirm: confirm })
  }
  return (
    <form className="flex flex-col gap-3" onSubmit={submit}>
      <p className="text-sm text-muted">{t('account.noEmail')}</p>
      <Input label={t('auth.email')} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
      <PasswordInput label={t('auth.password')} value={password} onChange={setPassword} required minLength={8} maxLength={128} autoComplete="new-password" />
      <PasswordInput label={t('auth.confirm')} value={confirm} onChange={setConfirm} required autoComplete="new-password" />
      <div>
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? <Loader2 size={16} className="spin" aria-hidden /> : <Plus size={16} aria-hidden />} {t('account.addEmail')}
        </Button>
      </div>
    </form>
  )
}

function AddSignature({ busy, onDone, onError }: { busy: boolean; onDone: (m: Methods) => Promise<void>; onError: (e: unknown) => void }) {
  const { t } = useTranslation()
  const [certs, setCerts] = useState<CertInfo[] | null>(null)
  const [sel, setSel] = useState('')
  const [working, setWorking] = useState(false)

  async function load() {
    setWorking(true)
    try {
      const list = await listCertificates()
      setCerts(list)
      setSel(list[0]?.thumbprint ?? '')
      if (!list.length) onError(new Error(t('auth.ecp.noCerts')))
    } catch (e) {
      onError(e)
    } finally {
      setWorking(false)
    }
  }

  async function add() {
    setWorking(true)
    try {
      const { challenge } = await api.ecpChallenge()
      const signature = await signDetached(sel, challenge)
      await onDone(await api.addSignature(challenge, signature))
      setCerts(null)
    } catch (e) {
      onError(e)
    } finally {
      setWorking(false)
    }
  }

  if (!certs?.length) {
    return (
      <div>
        <Button disabled={busy || working} onClick={() => void load()}>
          {working ? <Loader2 size={16} className="spin" aria-hidden /> : <Plus size={16} aria-hidden />} {t('account.addSignature')}
        </Button>
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-3">
      <Select
        label={t('auth.ecp.certificate')}
        value={sel}
        onChange={setSel}
        options={certs.map((c) => ({ value: c.thumbprint, label: t('auth.ecp.certLabel', { name: c.name, date: formatDate(c.validTo), issuer: c.issuer }) }))}
      />
      <div className="flex gap-2">
        <Button variant="primary" disabled={busy || working || !sel} onClick={() => void add()}>
          {working && <Loader2 size={16} className="spin" aria-hidden />} {t('account.signAndAdd')}
        </Button>
        <Button variant="ghost" onClick={() => setCerts(null)}>{t('common.cancel')}</Button>
      </div>
    </div>
  )
}
