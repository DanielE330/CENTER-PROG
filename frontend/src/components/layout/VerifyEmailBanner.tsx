import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { MailCheck } from 'lucide-react'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'

/** Плашка над страницами: почта не подтверждена — рассылки не приходят. */
export function VerifyEmailBanner({ email }: { email: string }) {
  const { t } = useTranslation()
  const [state, setState] = useState<'idle' | 'busy' | 'sent'>('idle')
  const [error, setError] = useState('')
  const resend = () => {
    setState('busy')
    setError('')
    api
      .resendVerification()
      .then(() => setState('sent'))
      .catch((err) => {
        setError(errorText(t, err))
        setState('idle')
      })
  }
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-warning-soft px-4 py-2 text-[13px] sm:px-6 lg:px-8">
      <MailCheck size={15} className="shrink-0 text-warning" aria-hidden />
      <span className="min-w-0 flex-1">
        {state === 'sent' ? t('mail.banner.sent', { email }) : t('mail.banner.text', { email })}
        {error && <span className="text-danger"> {error}</span>}
      </span>
      {state !== 'sent' && (
        <button type="button" className="font-medium text-primary hover:underline cursor-pointer disabled:opacity-60" disabled={state === 'busy'} onClick={resend}>
          {t('mail.banner.resend')}
        </button>
      )}
    </div>
  )
}
