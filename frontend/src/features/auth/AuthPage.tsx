import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Landmark, Loader2 } from 'lucide-react'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import { useAuth } from '../../app/auth'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { PasswordInput } from '../../components/ui/PasswordInput'
import { ThemeToggle } from '../../components/layout/ThemeToggle'
import { LanguageSwitch } from '../../components/layout/LanguageSwitch'
import { EcpLogin } from './EcpLogin'

export default function AuthPage() {
  const { t } = useTranslation()
  const { setMe } = useAuth()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [company, setCompany] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const reg = mode === 'register'
  const mismatch = reg && confirm !== '' && confirm !== password

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (reg && password !== confirm) return setError(t('auth.mismatch'))
    setBusy(true)
    try {
      const me = reg
        ? await api.register({ email, password, password_confirm: confirm, company_name: company.trim() })
        : await api.login(email, password)
      setMe(me)
    } catch (err) {
      setError(errorText(t, err))
      setBusy(false)
    }
  }

  return (
    <div className="min-h-full flex flex-col">
      <div className="flex justify-end items-center gap-2 p-4 sm:p-6">
        <LanguageSwitch compact />
        <ThemeToggle />
      </div>
      <main className="flex-1 grid place-items-center px-4 pb-10">
        <form className="card enter w-full max-w-[420px] p-6 sm:p-8 flex flex-col gap-4" onSubmit={submit} noValidate={false}>
          <div className="flex items-center gap-2.5">
            <span className="grid place-items-center size-9 rounded-[10px] bg-primary text-on-primary shrink-0" aria-hidden>
              <Landmark size={18} />
            </span>
            <div className="leading-tight">
              <p className="font-semibold">{t('app.brand')}</p>
              <p className="text-xs text-muted">{t('app.tagline')}</p>
            </div>
          </div>
          <div>
            <h1 className="text-2xl font-semibold">{reg ? t('auth.registerTitle') : t('auth.loginTitle')}</h1>
            <p className="text-muted mt-1">{t('auth.subtitle')}</p>
            <p className="text-[13px] text-muted mt-2">{reg ? t('auth.directorNoteRegister') : t('auth.directorNoteLogin')}</p>
          </div>

          {reg && (
            <Input
              label={t('auth.company')}
              value={company}
              onChange={(e) => setCompany(e.target.value)}
              required
              minLength={2}
              maxLength={200}
              placeholder={t('auth.companyPlaceholder')}
              autoComplete="organization"
            />
          )}
          <Input
            label={t('auth.email')}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoComplete="username"
          />
          <PasswordInput
            label={t('auth.password')}
            value={password}
            onChange={setPassword}
            required
            minLength={8}
            maxLength={128}
            autoComplete={reg ? 'new-password' : 'current-password'}
            hint={reg ? t('auth.passwordHint') : undefined}
          />
          {reg && (
            <PasswordInput
              label={t('auth.confirm')}
              value={confirm}
              onChange={setConfirm}
              required
              minLength={8}
              autoComplete="new-password"
              error={mismatch ? t('auth.mismatch') : null}
            />
          )}

          {error && (
            <div className="rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">{error}</div>
          )}

          <Button type="submit" variant="primary" disabled={busy || mismatch} className="w-full">
            {busy && <Loader2 size={16} className="spin" aria-hidden />}
            {reg ? t('auth.register') : t('auth.login')}
          </Button>

          <div className="flex items-center gap-3 text-xs text-muted" aria-hidden>
            <span className="h-px flex-1 bg-line" /> {t('auth.or')} <span className="h-px flex-1 bg-line" />
          </div>
          <EcpLogin onDone={setMe} register={reg} />

          <button
            type="button"
            className="text-sm text-primary font-medium hover:underline cursor-pointer self-center"
            onClick={() => {
              setMode(reg ? 'login' : 'register')
              setError('')
              setConfirm('')
            }}
          >
            {reg ? t('auth.haveAccount') : t('auth.noAccount')}
          </button>
        </form>
      </main>
    </div>
  )
}
