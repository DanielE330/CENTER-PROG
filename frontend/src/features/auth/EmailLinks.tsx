import { useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Landmark, Loader2 } from 'lucide-react'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import { ApiError } from '../../lib/errors'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { PasswordInput } from '../../components/ui/PasswordInput'
import { Notice } from '../../components/shared/Notice'
import { ThemeToggle } from '../../components/layout/ThemeToggle'
import { LanguageSwitch } from '../../components/layout/LanguageSwitch'

/** Карточка в стиле экрана входа: ссылки из писем открываются без входа в приложение. */
function Shell({ title, children }: { title: string; children: ReactNode }) {
  const { t } = useTranslation()
  return (
    <div className="min-h-full flex flex-col">
      <div className="flex justify-end items-center gap-2 p-4 sm:p-6">
        <LanguageSwitch compact />
        <ThemeToggle />
      </div>
      <main className="flex-1 grid place-items-center px-4 pb-10">
        <div className="card enter w-full max-w-[420px] p-6 sm:p-8 flex flex-col gap-4">
          <div className="flex items-center gap-2.5">
            <span className="grid place-items-center size-9 rounded-[10px] bg-primary text-on-primary shrink-0" aria-hidden>
              <Landmark size={18} />
            </span>
            <p className="font-semibold">{t('app.brand')}</p>
          </div>
          <h1 className="text-2xl font-semibold">{title}</h1>
          {children}
        </div>
      </main>
    </div>
  )
}

/** Полная перезагрузка: экран входа и приложение заново читают сессию. */
const goHome = (path = '/') => window.location.assign(path)

function HomeButton({ label, path }: { label: string; path?: string }) {
  return (
    <Button variant="primary" className="w-full" onClick={() => goHome(path)}>
      {label}
    </Button>
  )
}

/** «Забыли пароль?»: ответ всегда одинаковый, чтобы по нему нельзя было узнать, зарегистрирован ли адрес. */
export function ForgotPassword({ initialEmail = '', onBack }: { initialEmail?: string; onBack: () => void }) {
  const { t } = useTranslation()
  const [email, setEmail] = useState(initialEmail)
  const [state, setState] = useState<'idle' | 'busy' | 'sent'>('idle')
  const [error, setError] = useState('')

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setState('busy')
    try {
      await api.forgotPassword(email.trim())
      setState('sent')
    } catch (err) {
      setError(errorText(t, err))
      setState('idle')
    }
  }

  return (
    <Shell title={t('mail.forgot.title')}>
      {state === 'sent' ? (
        <>
          <Notice tone="success" title={t('mail.forgot.sentTitle')}>{t('mail.forgot.sentText', { email: email.trim() })}</Notice>
          <p className="text-[13px] text-muted">{t('mail.forgot.noLetter')}</p>
          <Button className="w-full" onClick={onBack}>{t('mail.backToLogin')}</Button>
        </>
      ) : (
        <form className="flex flex-col gap-4" onSubmit={submit}>
          <p className="text-muted">{t('mail.forgot.text')}</p>
          <Input label={t('auth.email')} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" autoFocus />
          {error && <Notice tone="danger">{error}</Notice>}
          <Button type="submit" variant="primary" className="w-full" disabled={state === 'busy'}>
            {state === 'busy' && <Loader2 size={16} className="spin" aria-hidden />} {t('mail.forgot.submit')}
          </Button>
          <button type="button" className="text-sm text-primary font-medium hover:underline cursor-pointer self-center" onClick={onBack}>
            {t('mail.backToLogin')}
          </button>
        </form>
      )}
    </Shell>
  )
}

function ResetPassword({ token, invite }: { token: string; invite: boolean }) {
  const { t } = useTranslation()
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'expired'>('idle')
  const [error, setError] = useState('')
  const mismatch = confirm !== '' && confirm !== password

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (password !== confirm) return setError(t('auth.mismatch'))
    setError('')
    setState('busy')
    try {
      await api.resetPassword({ token, password, password_confirm: confirm })
      setState('done')
    } catch (err) {
      if (err instanceof ApiError && err.code === 'token_invalid') return setState('expired')
      setError(errorText(t, err))
      setState('idle')
    }
  }

  const title = invite ? t('mail.reset.inviteTitle') : t('mail.reset.title')
  if (state === 'done') {
    return (
      <Shell title={title}>
        <Notice tone="success" title={t('mail.reset.doneTitle')}>{t('mail.reset.doneText')}</Notice>
        <HomeButton label={t('auth.login')} />
      </Shell>
    )
  }
  if (state === 'expired' || !token) {
    return (
      <Shell title={title}>
        <Notice tone="warning" title={t('mail.expiredTitle')}>{invite ? t('mail.reset.expiredInvite') : t('mail.reset.expiredText')}</Notice>
        <HomeButton label={t('mail.backToLogin')} />
      </Shell>
    )
  }
  return (
    <Shell title={title}>
      <form className="flex flex-col gap-4" onSubmit={submit}>
        <p className="text-muted">{invite ? t('mail.reset.inviteText') : t('mail.reset.text')}</p>
        <PasswordInput label={t('password.new')} value={password} onChange={setPassword} required minLength={8} maxLength={128} autoComplete="new-password" hint={t('auth.passwordHint')} />
        <PasswordInput label={t('auth.confirm')} value={confirm} onChange={setConfirm} required minLength={8} autoComplete="new-password" error={mismatch ? t('auth.mismatch') : null} />
        {error && <Notice tone="danger">{error}</Notice>}
        <Button type="submit" variant="primary" className="w-full" disabled={state === 'busy' || mismatch}>
          {state === 'busy' && <Loader2 size={16} className="spin" aria-hidden />} {t('mail.reset.submit')}
        </Button>
      </form>
    </Shell>
  )
}

/** Подтверждение почты и отписка: одно действие по токену сразу при открытии страницы. */
function TokenAction({ kind, token }: { kind: 'verify' | 'unsubscribe'; token: string }) {
  const { t } = useTranslation()
  const [state, setState] = useState<'busy' | 'done' | 'expired' | 'error'>(token ? 'busy' : 'expired')
  const [error, setError] = useState('')
  const started = useRef(false)

  useEffect(() => {
    // StrictMode вызывает эффект дважды, а ссылка одноразовая
    if (!token || started.current) return
    started.current = true
    const call = kind === 'verify' ? api.verifyEmail(token) : api.unsubscribe(token)
    call
      .then(() => setState('done'))
      .catch((err) => {
        if (err instanceof ApiError && (err.code === 'token_invalid' || err.status === 404)) return setState('expired')
        setError(errorText(t, err))
        setState('error')
      })
  }, [kind, token, t])

  const v = kind === 'verify'
  return (
    <Shell title={v ? t('mail.verify.title') : t('mail.unsubscribe.title')}>
      {state === 'busy' && <p className="flex items-center gap-2 text-muted"><Loader2 size={16} className="spin" aria-hidden /> {t('mail.checking')}</p>}
      {state === 'done' && (
        <>
          <Notice tone="success" title={v ? t('mail.verify.doneTitle') : t('mail.unsubscribe.doneTitle')}>
            {v ? t('mail.verify.doneText') : t('mail.unsubscribe.doneText')}
          </Notice>
          <HomeButton label={v ? t('mail.toApp') : t('mail.unsubscribe.toSettings')} path={v ? '/' : '/settings#notifications'} />
        </>
      )}
      {state === 'expired' && (
        <>
          <Notice tone="warning" title={t('mail.expiredTitle')}>{v ? t('mail.verify.expiredText') : t('mail.unsubscribe.expiredText')}</Notice>
          <HomeButton label={t('mail.toApp')} />
        </>
      )}
      {state === 'error' && (
        <>
          <Notice tone="danger">{error}</Notice>
          <Button className="w-full" onClick={() => window.location.reload()}>{t('common.retry')}</Button>
        </>
      )}
    </Shell>
  )
}

/** /verify-email, /reset-password и /unsubscribe: страницы для ссылок из писем. */
export default function EmailLinkPage() {
  const q = new URLSearchParams(window.location.search)
  const token = q.get('token') ?? ''
  const path = window.location.pathname.replace(/\/$/, '')
  if (path === '/reset-password') return <ResetPassword token={token} invite={q.get('invite') === '1'} />
  return <TokenAction kind={path === '/unsubscribe' ? 'unsubscribe' : 'verify'} token={token} />
}
