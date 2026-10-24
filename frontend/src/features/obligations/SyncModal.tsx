import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import { Modal } from '../../components/ui/Modal'
import { Input } from '../../components/ui/Input'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { useSyncIntegrations } from '../../hooks/queries'
import { errorText } from '../../i18n/helpers'

/** Pulls invoices and contracts from 1C / EDO / bank (via the integrator) into expected events. */
export function SyncModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation()
  const sync = useSyncIntegrations()
  const [code, setCode] = useState('C001')

  function submit(e: FormEvent) {
    e.preventDefault()
    sync.mutate(code.trim())
  }

  return (
    <Modal open={open} onClose={onClose} title={t('sync.title')}>
      <form className="flex flex-col gap-4" onSubmit={submit}>
        <p className="text-[13px] text-muted">{t('sync.hint')}</p>
        <Input
          label={t('sync.code')}
          hint={t('sync.codeHint')}
          value={code}
          onChange={(e) => { setCode(e.target.value); sync.reset() }}
          required
          maxLength={32}
          pattern="[A-Za-z0-9_\-]+"
          autoComplete="off"
        />
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={sync.isPending || !code.trim()}>
            {sync.isPending && <Loader2 size={16} className="spin" aria-hidden />}
            {sync.isPending ? t('sync.running') : t('sync.run')}
          </Button>
          <Button variant="ghost" onClick={onClose}>{t('common.close')}</Button>
        </div>
        {sync.isSuccess && (
          <Notice tone="success" title={t('sync.doneTitle')}>
            {t('sync.doneText', { created: sync.data.created, skipped: sync.data.skipped })}
          </Notice>
        )}
        {sync.isError && <Notice tone="danger">{errorText(t, sync.error)}</Notice>}
      </form>
    </Modal>
  )
}
