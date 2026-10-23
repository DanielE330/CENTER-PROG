import { useMemo, useState } from 'react'
import { Plus, FlaskConical } from 'lucide-react'
import type { ForecastResponse, Obligation } from '../../types'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { QueryBoundary } from '../../components/shared/QueryBoundary'
import { EmptyState } from '../../components/shared/States'
import { Badge } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { useForecast, useObligations } from '../../hooks/queries'
import { formatCurrency, formatDate, formatSignedCurrency } from '../../lib/formatters'
import { cn } from '../../lib/utils'
import { ForecastChart } from '../forecast/ForecastChart'
import { sliceWindow } from '../forecast/window'
import { ScenarioEditor } from './ScenarioEditor'
import { simulate } from './simulate'
import type { ExtraItem, Override, Scenario } from './simulate'

export default function ScenariosPage() {
  const forecast = useForecast()
  const obligations = useObligations()

  return (
    <PageContainer>
      <PageHeader
        title="Сценарии"
        description="Модель «Что если?»: измените даты и суммы платежей или добавьте поступление и сравните с базовым прогнозом."
      />
      <QueryBoundary query={forecast} loadingHeight={300}>
        {(f) => (
          <QueryBoundary query={obligations} loadingHeight={300}>
            {(list) => <Workspace f={f} obligations={list} />}
          </QueryBoundary>
        )}
      </QueryBoundary>
    </PageContainer>
  )
}

const BASE: Scenario = { id: 'base', name: 'Базовый прогноз', readOnly: true, overrides: {}, extras: [] }

function templates(items: Obligation[]): Scenario[] {
  const find = (name: string) => items.find((o) => o.name.includes(name))
  const supplier = find('ТехСнаб')
  const client = find('Ростов-Строй')
  const out: Scenario[] = []
  if (supplier) {
    out.push({
      id: 'tpl-delay',
      name: 'Отсрочка платежа ТехСнаб на 7 дней',
      template: true,
      overrides: { [supplier.id]: { date: shift(supplier.date, 7) } },
      extras: [],
    })
  }
  if (client) {
    out.push({
      id: 'tpl-early',
      name: 'Ростов-Строй платит на 4 дня раньше',
      template: true,
      overrides: { [client.id]: { date: shift(client.date, -4) } },
      extras: [],
    })
  }
  return out
}

function shift(iso: string, days: number) {
  const d = new Date(`${iso}T00:00:00`)
  d.setDate(d.getDate() + days)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function Workspace({ f, obligations }: { f: ForecastResponse; obligations: Obligation[] }) {
  const items = useMemo(
    () => obligations.filter((o) => (o.status === 'planned' || o.status === 'expected') && o.date > f.today).sort((a, b) => a.date.localeCompare(b.date)),
    [obligations, f.today],
  )
  const [scenarios, setScenarios] = useState<Scenario[]>(() => [BASE, ...templates(items)])
  const [activeId, setActiveId] = useState('base')
  const active = scenarios.find((s) => s.id === activeId) ?? BASE

  const update = (fn: (s: Scenario) => Scenario) => setScenarios((all) => all.map((s) => (s.id === active.id ? fn(s) : s)))

  const opening = f.summary.currentBalance
  const result = useMemo(() => {
    if (opening == null) return null
    const common = { opening, today: f.today, horizonDays: f.horizonDays, items }
    return {
      base: simulate({ ...common, scenario: BASE }),
      scenario: simulate({ ...common, scenario: active }),
    }
  }, [opening, f.today, f.horizonDays, items, active])

  if (opening == null || f.points.length === 0 || !result) {
    return (
      <div className="card">
        <EmptyState icon={<FlaskConical size={22} />} title="Сценарии недоступны" description="Для моделирования нужен известный текущий остаток и данные о платежах. Импортируйте выписку." />
      </div>
    )
  }

  const addScenario = () => {
    const n = scenarios.filter((s) => !s.template && !s.readOnly).length + 1
    const s: Scenario = { id: `custom-${Date.now()}`, name: `Мой сценарий ${n}`, overrides: {}, extras: [] }
    setScenarios([...scenarios, s])
    setActiveId(s.id)
  }

  const { base, scenario } = result
  const chartPoints = sliceWindow(f.points, f.today, 7, f.horizonDays).map((p) => {
    if (p.date <= f.today) return p
    const sp = scenario.points.find((x) => x.date === p.date)
    // Uncertainty ranges belong to the baseline model only; they are not carried over to a scenario.
    return sp ? { ...p, forecast: sp.balance, lower: null, upper: null, inflow: sp.inflow, outflow: sp.outflow } : p
  })
  const isBase = active.id === 'base'

  return (
    <>
      <div className="flex flex-wrap items-center gap-2" role="tablist" aria-label="Сценарии">
        {scenarios.map((s) => (
          <button
            key={s.id}
            role="tab"
            aria-selected={s.id === activeId}
            onClick={() => setActiveId(s.id)}
            className={cn(
              'h-9 px-3.5 rounded-full border text-sm font-medium cursor-pointer',
              s.id === activeId ? 'bg-primary-soft border-primary text-primary' : 'bg-surface border-line hover:bg-hover',
            )}
          >
            {s.name}
          </button>
        ))}
        <Button onClick={addScenario} size="sm"><Plus size={14} /> Новый сценарий</Button>
      </div>

      <div className="rounded-card border border-warning/40 bg-warning-soft p-3 text-[13px]">
        <b className="text-warning">Демонстрационный расчёт в браузере.</b>{' '}
        Остаток на день = текущий остаток + поступления − платежи до этой даты включительно. Просроченные и выполненные позиции не учитываются.
        Результат — иллюстрация правил, а не прогноз модели. Шаблоны сценариев содержат только заранее заданные входные изменения; итоги считаются заново.
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Compare
          label="Минимальный остаток"
          base={formatCurrency(base.minBalance)}
          value={formatCurrency(scenario.minBalance)}
          delta={isBase ? null : scenario.minBalance - base.minBalance}
          negative={scenario.minBalance < 0}
          note={formatDate(scenario.minDate)}
        />
        <Compare
          label="Дата нехватки"
          base={base.shortageDate ? formatDate(base.shortageDate) : 'нет'}
          value={scenario.shortageDate ? formatDate(scenario.shortageDate) : 'нет'}
          negative={scenario.shortageDate != null}
          note={scenario.shortageDate ? 'Дефицит' : 'Нехватки не ожидается'}
        />
        <Compare
          label="Дефицит"
          base={base.deficit != null ? formatCurrency(base.deficit) : 'нет'}
          value={scenario.deficit != null ? formatCurrency(scenario.deficit) : 'нет'}
          negative={scenario.deficit != null}
          delta={isBase || (base.deficit == null && scenario.deficit == null) ? null : (scenario.deficit ?? 0) - (base.deficit ?? 0)}
          invertDelta
        />
        <Compare
          label="Остаток на конец периода"
          base={formatCurrency(base.points.at(-1)!.balance)}
          value={formatCurrency(scenario.points.at(-1)!.balance)}
          delta={isBase ? null : scenario.points.at(-1)!.balance - base.points.at(-1)!.balance}
          negative={scenario.points.at(-1)!.balance < 0}
        />
      </div>

      <Card
        title="Базовый прогноз и сценарий"
        subtitle={isBase ? 'Выберите или создайте сценарий, чтобы увидеть отличия.' : `Сценарий: ${active.name}`}
        action={<Badge tone="warning">Демо-расчёт</Badge>}
      >
        <div className="px-3 pb-4 sm:px-5">
          <ForecastChart
            points={chartPoints}
            today={f.today}
            shortageDate={scenario.shortageDate}
            forecastName={isBase ? 'Базовый прогноз' : 'Сценарий'}
            compare={isBase ? undefined : { name: 'Базовый прогноз', points: base.points.map((p) => ({ date: p.date, balance: p.balance })) }}
            height={380}
          />
        </div>
      </Card>

      <ScenarioEditor
        scenario={active}
        items={items}
        today={f.today}
        onOverride={(id, patch: Override | null) =>
          update((s) => {
            const overrides = { ...s.overrides }
            if (patch == null) delete overrides[id]
            else overrides[id] = patch
            return { ...s, overrides }
          })
        }
        onAddExtra={(e: Omit<ExtraItem, 'id'>) => update((s) => ({ ...s, extras: [...s.extras, { ...e, id: `x-${Date.now()}` }] }))}
        onRemoveExtra={(id) => update((s) => ({ ...s, extras: s.extras.filter((x) => x.id !== id) }))}
      />
    </>
  )
}

interface CompareProps {
  label: string
  base: string
  value: string
  delta?: number | null
  /** For deficits, a rise is bad. */
  invertDelta?: boolean
  negative?: boolean
  note?: string
}

function Compare({ label, base, value, delta, invertDelta, negative, note }: CompareProps) {
  const good = delta != null && delta !== 0 && (invertDelta ? delta < 0 : delta > 0)
  return (
    <div className="card p-5 flex flex-col gap-1.5 min-w-0">
      <p className="text-[13px] text-muted">{label}</p>
      <p className={cn('tnum text-2xl font-semibold leading-tight', negative && 'text-danger')}>{value}</p>
      <p className="text-[13px] text-muted">База: <span className="tnum">{base}</span></p>
      {delta != null && delta !== 0 && (
        <p className={cn('tnum text-[13px] font-medium', good ? 'text-primary' : 'text-danger')}>
          {good ? 'Лучше' : 'Хуже'} базы: {formatSignedCurrency(delta)}
        </p>
      )}
      {note && <p className="text-xs text-muted">{note}</p>}
    </div>
  )
}
