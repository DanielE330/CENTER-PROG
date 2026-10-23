import { useState } from 'react'
import { addDays, format, parseISO } from 'date-fns'
import { Plus, RotateCcw, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { Obligation } from '../../types'
import type { ExtraItem, Override, Scenario } from './simulate'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { Card } from '../../components/shared/Page'
import { formatCurrency, formatDate } from '../../lib/formatters'

interface Props {
  scenario: Scenario
  items: Obligation[]
  today: string
  onOverride: (id: string, patch: Override | null) => void
  onAddExtra: (e: Omit<ExtraItem, 'id'>) => void
  onRemoveExtra: (id: string) => void
}

export function ScenarioEditor({ scenario, items, today, onOverride, onAddExtra, onRemoveExtra }: Props) {
  const { t } = useTranslation()
  const locked = scenario.readOnly
  const minDate = format(addDays(parseISO(today), 1), 'yyyy-MM-dd')
  const [extra, setExtra] = useState({ kind: 'receipt' as ExtraItem['kind'], name: '', date: minDate, amount: '' })
  const amountNum = Number(extra.amount)
  const extraValid = extra.name.trim() !== '' && amountNum > 0 && extra.date >= minDate

  return (
    <div className="flex flex-col gap-5">
      <Card title={t('scenarios.editor.title')} subtitle={locked ? t('scenarios.editor.locked') : t('scenarios.editor.hint')}>
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('scenarios.editor.item')}</th>
                <th>{t('scenarios.editor.date')}</th>
                <th className="num">{t('scenarios.editor.amount')}</th>
                <th aria-label={t('scenarios.editor.reset')} />
              </tr>
            </thead>
            <tbody>
              {items.map((o) => {
                const ov = scenario.overrides[o.id]
                const changed = ov != null && (ov.date !== undefined || ov.amount !== undefined)
                return (
                  <tr key={o.id} className={changed ? 'bg-primary-soft' : undefined}>
                    <td className="min-w-[180px]">
                      <p className="font-medium">{t(o.nameKey)}</p>
                      <p className="text-xs text-muted">
                        {o.kind === 'payment' ? t('obligations.payment') : t('obligations.receipt')} · {t('scenarios.editor.planned', { date: formatDate(o.date, 'dayMonth') })} · {formatCurrency(o.amount)}
                        {changed && <b className="text-primary"> · {t('scenarios.editor.changed')}</b>}
                      </p>
                    </td>
                    <td>
                      <input
                        type="date"
                        aria-label={`${t('scenarios.editor.date')}: ${t(o.nameKey)}`}
                        className="control w-40"
                        disabled={locked}
                        min={minDate}
                        value={ov?.date ?? o.date}
                        onChange={(e) => e.target.value && onOverride(o.id, { ...ov, date: e.target.value })}
                      />
                    </td>
                    <td className="num">
                      <input
                        type="number"
                        aria-label={`${t('scenarios.editor.amount')}: ${t(o.nameKey)}`}
                        className="control w-36 text-right tnum"
                        disabled={locked}
                        min={0}
                        step={100000}
                        value={ov?.amount ?? o.amount}
                        onChange={(e) => onOverride(o.id, { ...ov, amount: Math.max(0, Number(e.target.value)) })}
                      />
                    </td>
                    <td>
                      {changed && !locked && (
                        <button onClick={() => onOverride(o.id, null)} aria-label={t('scenarios.editor.reset')} title={t('scenarios.editor.reset')} className="p-1.5 rounded-md text-muted hover:bg-hover cursor-pointer">
                          <RotateCcw size={15} />
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title={t('scenarios.editor.addTitle')} subtitle={t('scenarios.editor.addHint')}>
        <form
          className="px-5 pb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-[130px_1fr_150px_150px_auto] items-end"
          onSubmit={(e) => {
            e.preventDefault()
            if (!extraValid) return
            onAddExtra({ kind: extra.kind, name: extra.name.trim(), date: extra.date, amount: amountNum })
            setExtra({ ...extra, name: '', amount: '' })
          }}
        >
          <Select
            label={t('scenarios.editor.type')}
            disabled={locked}
            value={extra.kind}
            onChange={(v) => setExtra({ ...extra, kind: v as ExtraItem['kind'] })}
            options={[{ value: 'receipt', label: t('obligations.receipt') }, { value: 'payment', label: t('obligations.payment') }]}
          />
          <Input label={t('scenarios.editor.name')} disabled={locked} value={extra.name} onChange={(e) => setExtra({ ...extra, name: e.target.value })} placeholder={t('scenarios.editor.namePlaceholder')} />
          <Input label={t('scenarios.editor.date')} type="date" disabled={locked} min={minDate} value={extra.date} onChange={(e) => setExtra({ ...extra, date: e.target.value })} />
          <Input label={t('scenarios.editor.amount')} type="number" min={0} step={100000} disabled={locked} value={extra.amount} onChange={(e) => setExtra({ ...extra, amount: e.target.value })} />
          <Button type="submit" variant="primary" disabled={locked || !extraValid}>
            <Plus size={16} /> {t('scenarios.editor.add')}
          </Button>
        </form>

        {scenario.extras.length > 0 && (
          <ul className="px-5 pb-4 divide-y divide-line">
            {scenario.extras.map((x) => (
              <li key={x.id} className="flex items-center gap-3 py-2 text-sm">
                <span className="flex-1 min-w-0 truncate">
                  <b>{x.name}</b> <span className="text-muted">· {x.kind === 'receipt' ? t('obligations.receiptLower') : t('obligations.paymentLower')} · {formatDate(x.date, 'dayMonth')}</span>
                </span>
                <span className="tnum font-semibold">{formatCurrency(x.amount)}</span>
                <button onClick={() => onRemoveExtra(x.id)} aria-label={t('common.delete')} className="p-1.5 rounded-md text-muted hover:bg-hover cursor-pointer">
                  <Trash2 size={15} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
