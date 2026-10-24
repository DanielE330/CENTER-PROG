import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import { Modal } from '../../components/ui/Modal'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { useFnsImportEns } from '../../hooks/queries'
import { errorText } from '../../i18n/helpers'
import { formatDate, formatKop } from '../../lib/formatters'

const MAX_BYTES = 1024 * 1024

function toBase64(buf: ArrayBuffer): string {
  let s = ''
  const bytes = new Uint8Array(buf)
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(s)
}

/** Загрузка справки о состоянии расчётов (XML КНД 1160080) или простого CSV. В личный кабинет ФНС мы не входим: файл выгружает владелец. */
export function FnsImportModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation()
  const imp = useFnsImportEns()
  const input = useRef<HTMLInputElement>(null)
  const [tooBig, setTooBig] = useState(false)

  async function pick(file: File | undefined) {
    imp.reset()
    setTooBig(false)
    if (!file) return
    if (file.size > MAX_BYTES) return setTooBig(true)
    imp.mutate({ filename: file.name, contentB64: toBase64(await file.arrayBuffer()) })
  }

  return (
    <Modal open={open} onClose={onClose} title={t('fns.import.title')}>
      <div className="flex flex-col gap-4">
        <ol className="list-decimal pl-5 text-[13px] text-muted flex flex-col gap-1">
          <li>{t('fns.import.step1')}</li>
          <li>{t('fns.import.step2')}</li>
          <li>{t('fns.import.step3')}</li>
        </ol>
        <p className="text-[13px] text-muted">{t('fns.import.csv')}</p>
        <input
          ref={input} type="file" accept=".xml,.csv,text/xml,text/csv" className="text-sm"
          aria-label={t('fns.import.file')} onChange={(e) => void pick(e.target.files?.[0])}
        />
        {imp.isPending && <p className="text-sm text-muted flex items-center gap-2"><Loader2 size={16} className="spin" aria-hidden /> {t('fns.import.checking')}</p>}
        {tooBig && <Notice tone="danger">{t('fns.import.tooBig')}</Notice>}
        {imp.isError && <Notice tone="danger">{errorText(t, imp.error)}</Notice>}
        {imp.isSuccess && (
          <Notice tone="success" title={t('fns.import.doneTitle')}>
            {t('fns.import.doneText', { debt: formatKop(imp.data.debt_kop), balance: formatKop(imp.data.balance_kop) })}
            {imp.data.checked_at && <> {t('fns.import.asOf', { date: formatDate(imp.data.checked_at) })}</>}
            {imp.data.warnings?.length > 0 && <ul className="mt-1 list-disc pl-5">{imp.data.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
          </Notice>
        )}
        <div><Button variant="ghost" onClick={onClose}>{t('common.close')}</Button></div>
      </div>
    </Modal>
  )
}
