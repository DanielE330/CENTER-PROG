import { useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Ban, KeyRound, Landmark, Loader2, LogOut, ShieldCheck } from 'lucide-react'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import { useAuth } from '../../app/auth'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { PasswordInput } from '../../components/ui/PasswordInput'
import { ThemeToggle } from '../../components/layout/ThemeToggle'
import { LanguageSwitch } from '../../components/layout/LanguageSwitch'
import type { Me } from '../../types'

/** Screens that stand between a signed-in user and the app: forced password change, director credentials, banned company. */
function Shell({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  const { t } = useTranslation()
  return (
    <div className="min-h-full flex flex-col">
      <div className="flex justify-end items-center gap-2 p-4 sm:p-6">
        <LanguageSwitch compact />
        <ThemeToggle />
      </div>
      <main className="flex-1 grid place-items-center px-4 pb-10">
        <div className="card enter w-full max-w-[440px] p-6 sm:p-8 flex flex-col gap-4">
          <div className="flex items-center gap-2.5">
            <span className="grid place-items-center size-9 rounded-[10px] bg-primary text-on-primary shrink-0" aria-hidden>
              <Landmark size={18} />
            </span>
            <p className="font-semibold">{t('app.brand')}</p>
          </div>
          <div className="flex items-start gap-3">
            <span className="mt-1 text-primary shrink-0" aria-hidden>{icon}</span>
            <h1 className="text-2xl font-semibold" id="gate-title">{title}</h1>
          </div>
          {children}
        </div>
      </main>
    </div>
  )
}

const validPassword = (p: string) => p.length >= 8 && p.length <= 128

/** Current / new / confirm. Also used (voluntarily) from the staff panel. */
export function ChangePasswordForm({ onDone, onSkip, forced = false }: { onDone: (me: Me | null) => void; onSkip?: () => void; forced?: boolean }) {
  const { t } = useTranslation()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const mismatch = confirm !== '' && confirm !== next

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (!validPassword(next)) return setError(t('apiErrors.invalid_password'))
    if (next !== confirm) return setError(t('auth.mismatch'))
    setBusy(true)
    try {
      await api.changePassword({ current, new: next })
      let me: Me | null = null
      try {
        me = await api.me()
      } catch {
        /* the session may have been rotated: the caller decides what to do */
      }
      onDone(me)
    } catch (err) {
      setError(errorText(t, err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      <PasswordInput label={t('password.current')} value={current} onChange={setCurrent} required maxLength={128} autoComplete="current-password" />
      <PasswordInput
        label={t('password.new')}
        value={next}
        onChange={setNext}
        required
        minLength={8}
        maxLength={128}
        autoComplete="new-password"
        hint={t('auth.passwordHint')}
      />
      <PasswordInput
        label={t('auth.confirm')}
        value={confirm}
        onChange={setConfirm}
        required
        minLength={8}
        maxLength={128}
        autoComplete="new-password"
        error={mismatch ? t('auth.mismatch') : null}
      />
      {error && <div className="rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">{error}</div>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={busy || mismatch}>
          {busy && <Loader2 size={16} className="spin" aria-hidden />} {t('password.submit')}
        </Button>
        {!forced && onSkip && <Button variant="ghost" onClick={onSkip} disabled={busy}>{t('password.skip')}</Button>}
      </div>
    </form>
  )
}

/** must_change_password: mandatory for a generated password, otherwise it can be skipped for now. */
export function ChangePasswordScreen({ me, onSkip }: { me: Me; onSkip: () => void }) {
  const { t } = useTranslation()
  const { setMe, logout } = useAuth()
  // The field may be absent in /auth/me: then we play safe and require the change.
  const forced = me.password_generated !== false
  return (
    <Shell icon={<KeyRound size={22} />} title={t('password.title')}>
      <p className="text-muted -mt-2">{forced ? t('password.textForced') : t('password.textOptional')}</p>
      <ChangePasswordForm
        forced={forced}
        onSkip={onSkip}
        onDone={(next) => {
          if (next && !next.must_change_password) setMe(next)
          else if (next) setMe({ ...next, must_change_password: false })
          else void logout()
        }}
      />
      <button type="button" className="text-sm text-muted hover:underline cursor-pointer self-start inline-flex items-center gap-1.5" onClick={() => void logout()}>
        <LogOut size={14} aria-hidden /> {t('auth.logout')}
      </button>
    </Shell>
  )
}

/** A director who registered with a signature must set an email and password before anything else. */
export function SetCredentialsScreen() {
  const { t } = useTranslation()
  const { setMe, logout } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const mismatch = confirm !== '' && confirm !== password

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (!validPassword(password)) return setError(t('apiErrors.invalid_password'))
    if (password !== confirm) return setError(t('auth.mismatch'))
    setBusy(true)
    try {
      await api.setCredentials({ email: email.trim(), password })
      setMe(await api.me())
    } catch (err) {
      setError(errorText(t, err))
      setBusy(false)
    }
  }

  return (
    <Shell icon={<ShieldCheck size={22} />} title={t('credentials.title')}>
      <p className="text-muted -mt-2">{t('credentials.text')}</p>
      <form className="flex flex-col gap-4" onSubmit={submit}>
        <Input label={t('auth.email')} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" />
        <PasswordInput label={t('auth.password')} value={password} onChange={setPassword} required minLength={8} maxLength={128} autoComplete="new-password" hint={t('auth.passwordHint')} />
        <PasswordInput
          label={t('auth.confirm')}
          value={confirm}
          onChange={setConfirm}
          required
          minLength={8}
          maxLength={128}
          autoComplete="new-password"
          error={mismatch ? t('auth.mismatch') : null}
        />
        {error && <div className="rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">{error}</div>}
        <Button type="submit" variant="primary" disabled={busy || mismatch}>
          {busy && <Loader2 size={16} className="spin" aria-hidden />} {t('credentials.submit')}
        </Button>
      </form>
      <button type="button" className="text-sm text-muted hover:underline cursor-pointer self-start inline-flex items-center gap-1.5" onClick={() => void logout()}>
        <LogOut size={14} aria-hidden /> {t('auth.logout')}
      </button>
    </Shell>
  )
}

/** The operator of the platform blocked the company: nothing else is available. */
export function BannedScreen({ me }: { me: Me }) {
  const { t } = useTranslation()
  const { logout } = useAuth()
  return (
    <Shell icon={<Ban size={22} className="text-danger" />} title={t('banned.title')}>
      <div role="alert" className="flex flex-col gap-3">
        <p className="text-muted -mt-2">{t('banned.text', { company: me.company_name })}</p>
        <div className="rounded-control bg-danger-soft p-3 text-sm">
          <p className="text-xs text-muted">{t('banned.reason')}</p>
          <p className="font-medium text-danger break-words">{me.ban_reason?.trim() || t('banned.noReason')}</p>
        </div>
      </div>
      <p className="text-sm text-muted">{t('banned.contact')}</p>
      <Button onClick={() => void logout()} className="self-start">
        <LogOut size={16} aria-hidden /> {t('auth.logout')}
      </Button>
    </Shell>
  )
}
