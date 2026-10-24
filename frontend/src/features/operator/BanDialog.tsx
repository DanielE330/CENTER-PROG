import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import { Modal } from '../../components/ui/Modal'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { errorText } from '../../i18n/helpers'
import { useBanMutation } from './operatorApi'
import type { OperatorCompany } from './operatorApi'

export const REASON_MIN = 3
export const REASON_MAX = 500

/** Whitespace is collapsed the same way the server does it before the length check. */
export const normalizeReason = (s: string) => s.split(/\s+/).filter(Boolean).join(' ')

interface Props {
  company: OperatorCompany
  /** `ban` asks for a mandatory reason, `unban` only asks for confirmation. */
  mode: 'ban' | 'unban'
  onClose: () => void
}

export function BanDialog({ company, mode, onClose }: Props) {
  const { t } = useTranslation()
  const m = useBanMutation()
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)
  const ban = mode === 'ban'
  const clean = normalizeReason(reason)
  const reasonError = ban && touched && clean.length < REASON_MIN ? t('operator.ban.reasonRequired', { min: REASON_MIN }) : null

  function submit(e: FormEvent) {
    e.preventDefault()
    setTouched(true)
    if (ban && clean.length < REASON_MIN) return
    m.mutate({ id: company.id, reason: ban ? clean : null }, { onSuccess: onClose })
  }

  return (
    <Modal open title={ban ? t('operator.ban.title', { name: company.name }) : t('operator.unban.title', { name: company.name })} onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <p className="text-sm">{ban ? t('operator.ban.effect') : t('operator.unban.effect')}</p>
        {ban && (
          <div className="flex flex-col gap-1.5">
            <label htmlFor="ban-reason" className="text-[13px] font-medium">
              {t('operator.ban.reason')} <span aria-hidden>*</span>
            </label>
            <textarea
              id="ban-reason"
              className="control h-auto py-2 min-h-24 resize-y"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              onBlur={() => setTouched(true)}
              maxLength={REASON_MAX}
              required
              aria-required
              aria-invalid={reasonError ? true : undefined}
              aria-describedby="ban-reason-hint"
            />
            <p id="ban-reason-hint" className={reasonError ? 'text-xs text-danger' : 'text-xs text-muted'} role={reasonError ? 'alert' : undefined}>
              {reasonError ?? t('operator.ban.reasonHint', { max: REASON_MAX })}
            </p>
          </div>
        )}
        {m.isError && <Notice tone="danger">{errorText(t, m.error)}</Notice>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose} disabled={m.isPending}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" variant={ban ? 'danger' : 'primary'} disabled={m.isPending}>
            {m.isPending && <Loader2 size={16} className="spin" aria-hidden />}
            {ban ? t('operator.ban.submit') : t('operator.unban.submit')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
