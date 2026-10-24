import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import { Modal } from '../../components/ui/Modal'
import { Input } from '../../components/ui/Input'
import { DatePicker } from '../../components/ui/DatePicker'
import { Select } from '../../components/ui/Select'
import { Button } from '../../components/ui/Button'
import { Notice } from '../../components/shared/Notice'
import { useCreateEvent, useTaxCalendar } from '../../hooks/queries'
import { parseRubToKop } from '../../lib/money'
import { errorText } from '../../i18n/helpers'
import { formatDate, formatKop } from '../../lib/formatters'
import type { EventSource, TaxCalendarResult, TaxKind } from '../../types'
import { sourceLabel } from './labels'

const SOURCES: EventSource[] = ['manual', 'invoice', 'contract', 'tax']

const TAX_KINDS: TaxKind[] = ['enp', 'usn', 'vat']

const empty = { date: '', amount: '', direction: 'in', title: '', counterparty: '', source: 'manual', confidence: 90 }

export function AddEventModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation()
  const create = useCreateEvent()
  const tax = useTaxCalendar()
  const [f, setF] = useState(empty)
  const [taxAmount, setTaxAmount] = useState('')
  const [taxKind, setTaxKind] = useState<TaxKind>('enp')
  const [usnObject, setUsnObject] = useState<'income' | 'income_expenses'>('income')
  const [error, setError] = useState<string | null>(null)
  const [taxResult, setTaxResult] = useState<TaxCalendarResult | null>(null)
  const set = <K extends keyof typeof empty>(k: K, v: (typeof empty)[K]) => setF((x) => ({ ...x, [k]: v }))
  const busy = create.isPending || tax.isPending

  function close() {
    setError(null)
    setTaxResult(null)
    onClose()
  }

  async function add(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setTaxResult(null)
    const kop = parseRubToKop(f.amount)
    if (!kop) return setError(t('obligations.add.badAmount'))
    try {
      await create.mutateAsync({
        date: f.date, amount_kop: kop, direction: f.direction as 'in' | 'out', title: f.title.trim(),
        counterparty: f.counterparty.trim(), source: f.source as EventSource, confidence: f.confidence / 100,
      })
      setF(empty)
      onClose()
    } catch (err) {
      setError(errorText(t, err))
    }
  }

  async function addTax(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setTaxResult(null)
    const kop = taxAmount.trim() ? parseRubToKop(taxAmount) : null
    if (taxAmount.trim() && !kop) return setError(t('obligations.add.badTaxAmount'))
    try {
      setTaxResult(await tax.mutateAsync({ kind: taxKind, amountKop: kop ?? undefined, usnObject }))
    } catch (err) {
      setError(errorText(t, err))
    }
  }

  return (
    <Modal open={open} onClose={close} title={t('obligations.add.title')}>
      <div className="flex flex-col gap-4">
        <p className="text-[13px] text-muted">{t('obligations.add.hint')}</p>
        <form className="flex flex-col gap-3" onSubmit={add}>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <DatePicker label={t('obligations.add.date')} required value={f.date} onChange={(v) => set('date', v)} />
            <Select
              label={t('obligations.add.type')}
              value={f.direction}
              onChange={(v) => set('direction', v)}
              options={[{ value: 'in', label: t('obligations.receipt') }, { value: 'out', label: t('obligations.payment') }]}
            />
            <Input label={t('obligations.add.amount')} inputMode="decimal" required placeholder="300 000" value={f.amount} onChange={(e) => set('amount', e.target.value)} />
            <Input label={t('obligations.add.name')} required maxLength={200} value={f.title} onChange={(e) => set('title', e.target.value)} />
            <Input label={t('obligations.add.counterparty')} value={f.counterparty} onChange={(e) => set('counterparty', e.target.value)} />
            <Select
              label={t('obligations.add.source')}
              value={f.source}
              onChange={(v) => set('source', v)}
              options={SOURCES.map((s) => ({ value: s, label: sourceLabel(t, s) }))}
            />
            <Input
              label={t('obligations.add.confidence')}
              hint={t('obligations.add.confidenceHint')}
              type="number" min={0} max={100}
              value={f.confidence}
              onChange={(e) => set('confidence', Number(e.target.value))}
            />
          </div>
          <div className="flex gap-2">
            <Button type="submit" variant="primary" disabled={busy}>
              {create.isPending && <Loader2 size={16} className="spin" aria-hidden />}
              {t('obligations.add.submit')}
            </Button>
            <Button variant="ghost" onClick={close}>{t('common.cancel')}</Button>
          </div>
        </form>

        <hr className="border-line" />

        <form className="flex flex-col gap-3" onSubmit={addTax}>
          <Select
            label={t('obligations.tax.kind')}
            value={taxKind}
            onChange={(v) => setTaxKind(v as TaxKind)}
            options={TAX_KINDS.map((k) => ({ value: k, label: t(`obligations.tax.kinds.${k}`) }))}
          />
          {taxKind === 'usn' && (
            <Select
              label={t('obligations.tax.usnObject')}
              value={usnObject}
              onChange={(v) => setUsnObject(v as 'income' | 'income_expenses')}
              options={[
                { value: 'income', label: t('obligations.tax.usnIncome') },
                { value: 'income_expenses', label: t('obligations.tax.usnIncomeExpenses') },
              ]}
            />
          )}
          <Input
            label={t('obligations.tax.label')}
            hint={t('obligations.tax.hint')}
            inputMode="decimal"
            value={taxAmount}
            onChange={(e) => setTaxAmount(e.target.value)}
          />
          <div>
            <Button type="submit" disabled={busy}>
              {tax.isPending && <Loader2 size={16} className="spin" aria-hidden />}
              {t(`obligations.tax.submit.${taxKind}`)}
            </Button>
          </div>
        </form>

        {taxResult && (
          <Notice tone="success" title={t('obligations.tax.doneTitle', { count: taxResult.created.length })}>
            {taxResult.created.length > 0
              ? t('obligations.tax.doneText', {
                  dates: taxResult.created.map((d) => formatDate(d)).join(', '),
                  amount: formatKop(taxResult.amount_kop),
                  source: taxResult.source,
                })
              : t('obligations.tax.nothingNew')}
            {taxResult.amount_estimated && ` ${t('obligations.tax.estimated')}`}
          </Notice>
        )}
        {error && <Notice tone="danger">{error}</Notice>}
      </div>
    </Modal>
  )
}
