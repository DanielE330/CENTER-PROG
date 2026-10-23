import { useState } from 'react'
import { addDays, format, parseISO } from 'date-fns'
import { Plus, RotateCcw, Trash2 } from 'lucide-react'
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
  const locked = scenario.readOnly
  const minDate = format(addDays(parseISO(today), 1), 'yyyy-MM-dd')
  const [extra, setExtra] = useState({ kind: 'receipt' as ExtraItem['kind'], name: '', date: minDate, amount: '' })
  const amountNum = Number(extra.amount)
  const extraValid = extra.name.trim() !== '' && amountNum > 0 && extra.date >= minDate

  return (
    <div className="flex flex-col gap-5">
      <Card title="Изменяемые платежи и поступления" subtitle={locked ? 'Базовый прогноз нельзя изменить — создайте или выберите сценарий.' : 'Измените дату или сумму позиции — результат пересчитается.'}>
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Позиция</th>
                <th>Дата</th>
                <th className="num">Сумма, ₽</th>
                <th aria-label="Сброс" />
              </tr>
            </thead>
            <tbody>
              {items.map((o) => {
                const ov = scenario.overrides[o.id]
                const changed = ov != null && (ov.date !== undefined || ov.amount !== undefined)
                return (
                  <tr key={o.id} className={changed ? 'bg-primary-soft' : undefined}>
                    <td className="min-w-[180px]">
                      <p className="font-medium">{o.name}</p>
                      <p className="text-xs text-muted">
                        {o.kind === 'payment' ? 'Платёж' : 'Поступление'} · план {formatDate(o.date, 'd MMM')} · {formatCurrency(o.amount)}
                        {changed && <b className="text-primary"> · изменено</b>}
                      </p>
                    </td>
                    <td>
                      <input
                        type="date"
                        aria-label={`Дата: ${o.name}`}
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
                        aria-label={`Сумма: ${o.name}`}
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
                        <button onClick={() => onOverride(o.id, null)} aria-label="Сбросить" title="Сбросить" className="p-1.5 rounded-md text-muted hover:bg-app cursor-pointer">
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

      <Card title="Добавить ожидаемую операцию" subtitle="Например, ожидаемое поступление от нового контракта.">
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
            label="Тип"
            disabled={locked}
            value={extra.kind}
            onChange={(v) => setExtra({ ...extra, kind: v as ExtraItem['kind'] })}
            options={[{ value: 'receipt', label: 'Поступление' }, { value: 'payment', label: 'Платёж' }]}
          />
          <Input label="Название" disabled={locked} value={extra.name} onChange={(e) => setExtra({ ...extra, name: e.target.value })} placeholder="Например, предоплата клиента" />
          <Input label="Дата" type="date" disabled={locked} min={minDate} value={extra.date} onChange={(e) => setExtra({ ...extra, date: e.target.value })} />
          <Input label="Сумма, ₽" type="number" min={0} step={100000} disabled={locked} value={extra.amount} onChange={(e) => setExtra({ ...extra, amount: e.target.value })} />
          <Button type="submit" variant="primary" disabled={locked || !extraValid}>
            <Plus size={16} /> Добавить
          </Button>
        </form>

        {scenario.extras.length > 0 && (
          <ul className="px-5 pb-4 divide-y divide-line">
            {scenario.extras.map((x) => (
              <li key={x.id} className="flex items-center gap-3 py-2 text-sm">
                <span className="flex-1 min-w-0 truncate">
                  <b>{x.name}</b> <span className="text-muted">· {x.kind === 'receipt' ? 'поступление' : 'платёж'} · {formatDate(x.date, 'd MMM')}</span>
                </span>
                <span className="tnum font-semibold">{formatCurrency(x.amount)}</span>
                <button onClick={() => onRemoveExtra(x.id)} aria-label="Удалить" className="p-1.5 rounded-md text-muted hover:bg-app cursor-pointer">
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
