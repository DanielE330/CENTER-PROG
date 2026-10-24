import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { Loader2, ShieldCheck } from 'lucide-react'
import { errorText } from '../../i18n/helpers'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { PasswordInput } from '../../components/ui/PasswordInput'
import { ThemeToggle } from '../../components/layout/ThemeToggle'
import { LanguageSwitch } from '../../components/layout/LanguageSwitch'
import { opApi, opKeys } from './operatorApi'

/** Sign-in form of the platform operator. Not linked from the company sign-in page. */
export function OperatorLogin() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      const me = await opApi.login(email.trim(), password)
      setPassword('')
      qc.setQueryData(opKeys.me, me)
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
      <main id="main" className="flex-1 grid place-items-center px-4 pb-10">
        <form className="card enter w-full max-w-[420px] p-6 sm:p-8 flex flex-col gap-4" onSubmit={submit}>
          <div className="flex items-center gap-2.5">
            <span className="grid place-items-center size-9 rounded-[10px] bg-primary text-on-primary shrink-0" aria-hidden>
              <ShieldCheck size={18} />
            </span>
            <div className="leading-tight">
              <p className="font-semibold">{t('app.brand')}</p>
              <p className="text-xs text-muted">{t('operator.brand')}</p>
            </div>
          </div>
          <div>
            <h1 className="text-2xl font-semibold">{t('operator.login.title')}</h1>
            <p className="text-muted mt-1">{t('operator.login.subtitle')}</p>
          </div>
          <Input
            label={t('auth.email')}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            maxLength={254}
            autoComplete="username"
          />
          <PasswordInput label={t('auth.password')} value={password} onChange={setPassword} required maxLength={128} autoComplete="current-password" />
          {error && (
            <div className="rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">
              {error}
            </div>
          )}
          <Button type="submit" variant="primary" disabled={busy} className="w-full">
            {busy && <Loader2 size={16} className="spin" aria-hidden />}
            {t('operator.login.submit')}
          </Button>
        </form>
      </main>
    </div>
  )
}
