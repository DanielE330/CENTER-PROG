import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2, ShieldCheck } from 'lucide-react'
import { api } from '../../lib/api'
import { listCertificates, signDetached } from '../../lib/cades'
import type { CertInfo } from '../../lib/cades'
import { errorText } from '../../i18n/helpers'
import { formatDate } from '../../lib/formatters'
import { Button } from '../../components/ui/Button'
import { Select } from '../../components/ui/Select'
import type { Me } from '../../types'

/** Sign-in or registration with an electronic signature (CryptoPro Browser plug-in): pick a certificate, sign the challenge. */
/** Название юрлица, ИНН и ОГРН сервер берёт из сертификата, поэтому при регистрации вводить их не нужно. */
export function EcpLogin({ onDone, register = false }: { onDone: (me: Me) => void; register?: boolean }) {
  const { t } = useTranslation()
  const [certs, setCerts] = useState<CertInfo[] | null>(null)
  const [sel, setSel] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    setError('')
    setBusy(true)
    try {
      const list = await listCertificates()
      setCerts(list)
      setSel(list[0]?.thumbprint ?? '')
      if (!list.length) setError(t('auth.ecp.noCerts'))
    } catch (e) {
      setError(errorText(t, e))
    } finally {
      setBusy(false)
    }
  }

  async function sign() {
    setError('')
    setBusy(true)
    try {
      const { challenge } = await api.ecpChallenge()
      const signature = await signDetached(sel, challenge)
      onDone(await api.ecpLogin(challenge, signature))
    } catch (e) {
      setError(errorText(t, e))
    } finally {
      setBusy(false)
    }
  }

  const alert = error && (
    <div className="rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">{error}</div>
  )

  if (!certs) {
    return (
      <div className="flex flex-col gap-3">
        <Button type="button" disabled={busy} onClick={load} className="w-full">
          {busy ? <Loader2 size={16} className="spin" aria-hidden /> : <ShieldCheck size={16} aria-hidden />}
          {busy ? t('auth.ecp.searching') : register ? t('auth.ecp.registerButton') : t('auth.ecp.button')}
        </Button>
        {alert}
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
        emptyText={t('auth.ecp.noCerts')}
      />
      {alert}
      <Button type="button" variant="primary" disabled={busy || !sel} onClick={sign} className="w-full">
        {busy && <Loader2 size={16} className="spin" aria-hidden />}
        {busy ? t('auth.ecp.signing') : register ? t('auth.ecp.signRegister') : t('auth.ecp.signIn')}
      </Button>
      <Button type="button" variant="ghost" onClick={() => { setCerts(null); setError('') }} className="w-full">{t('common.cancel')}</Button>
    </div>
  )
}
