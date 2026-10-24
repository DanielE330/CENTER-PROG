import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import type { MeWithCodes, MfaSetup } from '../../types'

interface Props {
  /** `setup` — первый вход: подключаем приложение-аутентификатор; `verify` — обычный вход с кодом. */
  mode: 'setup' | 'verify'
  ticket: string
  onDone: (me: MeWithCodes) => void
  onCancel: () => void
}

/** Второй шаг входа. При первом входе показывает QR и после подтверждения кодом — резервные коды (один раз). */
export function MfaStep({ mode, ticket, onDone, onCancel }: Props) {
  const { t } = useTranslation()
  const [setup, setSetup] = useState<MfaSetup | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState<MeWithCodes | null>(null)
  const [ack, setAck] = useState(false)

  useEffect(() => {
    if (mode !== 'setup') return
    api.mfaSetup(ticket).then(setSetup).catch((e) => setError(errorText(t, e)))
  }, [mode, ticket, t])

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      const me = mode === 'setup' ? await api.mfaEnable(ticket, code) : await api.mfaVerify(ticket, code)
      // сессия уже выдана, но резервные коды нужно показать до входа в приложение
      if (me.backup_codes?.length) setSaved(me)
      else onDone(me)
    } catch (err) {
      setError(errorText(t, err))
    } finally {
      setBusy(false)
    }
  }

  if (saved) {
    const codes = saved.backup_codes ?? []
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold">{t('auth.mfa.backupTitle')}</h1>
        <p className="text-muted">{t('auth.mfa.backupText')}</p>
        <ul className="grid grid-cols-2 gap-2 font-mono text-sm rounded-control bg-hover p-3" aria-label={t('auth.mfa.backupTitle')}>
          {codes.map((c) => <li key={c}>{c}</li>)}
        </ul>
        <div className="flex gap-2">
          <Button type="button" variant="secondary" onClick={() => navigator.clipboard?.writeText(codes.join('\n'))}>
            {t('auth.mfa.copy')}
          </Button>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          {t('auth.mfa.saved')}
        </label>
        <Button type="button" variant="primary" disabled={!ack} onClick={() => onDone(saved)}>
          {t('auth.mfa.continue')}
        </Button>
      </div>
    )
  }

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      <div>
        <h1 className="text-2xl font-semibold">{mode === 'setup' ? t('auth.mfa.setupTitle') : t('auth.mfa.verifyTitle')}</h1>
        <p className="text-muted mt-1">{mode === 'setup' ? t('auth.mfa.setupText') : t('auth.mfa.verifyText')}</p>
      </div>
      {mode === 'setup' && setup && (
        <div className="flex flex-col items-center gap-3">
          {setup.qr_svg && (
            <img
              className="size-44 rounded-control bg-white p-2"
              alt={t('auth.mfa.qrAlt')}
              src={`data:image/svg+xml;utf8,${encodeURIComponent(setup.qr_svg)}`}
            />
          )}
          <p className="text-xs text-muted text-center">{t('auth.mfa.manual')}</p>
          <code className="font-mono text-sm break-all select-all text-center">{setup.secret}</code>
        </div>
      )}
      <Input
        label={t('auth.mfa.code')}
        value={code}
        onChange={(e) => setCode(e.target.value)}
        required
        autoFocus
        autoComplete="one-time-code"
        inputMode={mode === 'setup' ? 'numeric' : 'text'}
        maxLength={12}
        hint={mode === 'verify' ? t('auth.mfa.backupHint') : undefined}
      />
      {error && <div className="rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">{error}</div>}
      <Button type="submit" variant="primary" disabled={busy || (mode === 'setup' && !setup) || code.trim().length < 6} className="w-full">
        {busy && <Loader2 size={16} className="spin" aria-hidden />}
        {mode === 'setup' ? t('auth.mfa.enable') : t('auth.mfa.confirm')}
      </Button>
      <button type="button" className="text-sm text-primary font-medium hover:underline cursor-pointer self-center" onClick={onCancel}>
        {t('auth.mfa.back')}
      </button>
    </form>
  )
}
