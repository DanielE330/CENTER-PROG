import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { FlaskConical, Loader2, Plus, Trash2 } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Badge } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { DatePicker } from '../../components/ui/DatePicker'
import { Select } from '../../components/ui/Select'
import { useForecast, useScenario } from '../../hooks/queries'
import { formatDate, formatKop, formatNumber, formatPercent } from '../../lib/formatters'
import { parseRubToKop } from '../../lib/money'
import { errorText } from '../../i18n/helpers'
import { cn } from '../../lib/utils'
import type { ForecastResult, ScenarioResponse } from '../../types'
import { ForecastChart } from '../forecast/ForecastChart'
import { buildView } from '../forecast/model'
import type { ForecastView } from '../forecast/model'
import { ZERO, loadSaved, storeSaved, toPayload } from './params'
import type { FormParams, ParamError, SavedScenario } from './params'

const PRESETS: Array<{ id: string; p: Partial<FormParams> }> = [
  { id: 'delay', p: { delay_days: 21 } },
  { id: 'revenue', p: { revenue_drop_pct: 20 } },
  { id: 'costs', p: { fixed_cost_growth_pct: 15 } },
  { id: 'expense', p: { extra_expense: '300000' } },
]

export default function ScenariosPage() {
  const { t } = useTranslation()
  const q = useForecast()
  return (
    <PageContainer>
      <PageHeader title={t('scenarios.title')} description={t('scenarios.description')} />
      {q.isPending && <div className="card"><LoadingState height={300} /></div>}
      {q.isError && <div className="card"><ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} /></div>}
      {!q.isPending && !q.isError && !q.view && (
        <div className="card">
          <EmptyState
            icon={<FlaskConical size={22} aria-hidden />}
            title={t('scenarios.unavailableTitle')}
            description={t('scenarios.unavailableText')}
            action={<Link to="/settings" className="text-primary font-medium hover:underline text-sm">{t('forecast.goToImport')}</Link>}
          />
        </div>
      )}
      {q.view && <Workspace v={q.view} />}
    </PageContainer>
  )
}

function minDay(r: ForecastResult) {
  return r.days.reduce((m, d) => (BigInt(d.p50) < BigInt(m.p50) ? d : m), r.days[0])
}

function Workspace({ v }: { v: ForecastView }) {
  const { t } = useTranslation()
  const calc = useScenario()
  const [p, setP] = useState<FormParams>(ZERO)
  const [name, setName] = useState('')
  const [saved, setSaved] = useState<SavedScenario[]>(loadSaved)
  const [formError, setFormError] = useState<ParamError | null>(null)
  const [out, setOut] = useState<{ res: ScenarioResponse; base: ForecastView; scenario: ForecastView } | null>(null)

  const set = <K extends keyof FormParams>(k: K, val: FormParams[K]) => setP((x) => ({ ...x, [k]: val }))
  const num = (k: 'delay_days' | 'revenue_drop_pct' | 'fixed_cost_growth_pct') => (e: React.ChangeEvent<HTMLInputElement>) => set(k, Math.max(0, Number(e.target.value) || 0))

  const clientOptions = useMemo(() => {
    const seen = new Set<string>()
    const rows = [{ value: '', label: t('scenarios.params.largestClient') }]
    for (const r of v.r.rules) {
      const value = r.counterparty || r.key
      if (seen.has(value)) continue
      seen.add(value)
      rows.push({ value: r.counterparty, label: `${value} · ${formatKop(r.amount, { sign: true })}` })
    }
    return rows
  }, [v.r.rules, t])

  function run(params: FormParams) {
    setFormError(null)
    const res = toPayload(params, parseRubToKop)
    if ('error' in res) return setFormError(res.error)
    calc.mutate(
      { horizon: v.r.horizon, params: res.payload },
      { onSuccess: (r) => setOut({ res: r, base: buildView(r.base), scenario: buildView(r.scenario) }) },
    )
  }

  function preset(x: Partial<FormParams>) {
    const next = { ...ZERO, ...x }
    if (x.extra_expense) next.extra_expense_date = v.r.days[Math.min(9, v.r.days.length - 1)]?.date ?? ''
    setP(next)
    setName('')
    run(next)
  }

  function save() {
    const n = name.trim() || t('scenarios.savedDefault', { n: saved.length + 1 })
    const next = [...saved, { id: crypto.randomUUID(), name: n, params: p }]
    setSaved(next)
    storeSaved(next)
    setName('')
  }

  function remove(id: string) {
    const next = saved.filter((s) => s.id !== id)
    setSaved(next)
    storeSaved(next)
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    run(p)
  }

  const cp = v.r.rules.find((r) => r.counterparty === p.delay_counterparty)

  return (
    <>
      <Card title={t('scenarios.presetsTitle')}>
        <div className="px-5 pb-5 flex flex-col gap-4">
          <ul className="flex flex-wrap gap-2">
            {PRESETS.map((x) => (
              <li key={x.id}>
                <button type="button" disabled={calc.isPending} onClick={() => preset(x.p)} className="rounded-full border border-line bg-surface px-3.5 py-1.5 text-[13px] enabled:hover:bg-primary-soft enabled:hover:border-primary cursor-pointer disabled:opacity-50">
                  {t(`scenarios.presets.${x.id}`)}
                </button>
              </li>
            ))}
          </ul>
          {saved.length > 0 && (
            <div>
              <h3 className="text-[13px] text-muted mb-2">{t('scenarios.savedTitle')}</h3>
              <ul className="flex flex-wrap gap-2">
                {saved.map((x) => (
                  <li key={x.id} className="inline-flex items-center rounded-full border border-line bg-surface">
                    <button type="button" disabled={calc.isPending} onClick={() => { setP(x.params); run(x.params) }} className="pl-3.5 pr-2 py-1.5 text-[13px] rounded-l-full enabled:hover:bg-primary-soft cursor-pointer disabled:opacity-50">
                      {x.name}
                    </button>
                    <button type="button" aria-label={t('scenarios.deleteSaved', { name: x.name })} onClick={() => remove(x.id)} className="px-2 py-1.5 text-muted hover:text-danger rounded-r-full hover:bg-hover cursor-pointer">
                      <Trash2 size={13} aria-hidden />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </Card>

      <Card title={t('scenarios.params.title')} subtitle={t('scenarios.params.subtitle')}>
        <form className="px-5 pb-5 flex flex-col gap-4" onSubmit={submit}>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Select
              label={t('scenarios.params.counterparty')}
              value={p.delay_counterparty}
              onChange={(val) => set('delay_counterparty', val)}
              options={clientOptions}
            />
            <Input
              label={t('scenarios.params.delay')}
              hint={cp ? (cp.direction === 'in' ? t('scenarios.params.delayIn') : t('scenarios.params.delayOut')) : t('scenarios.params.delayIn')}
              type="number" min={0} max={90} value={p.delay_days} onChange={num('delay_days')}
            />
            <Input label={t('scenarios.params.revenueDrop')} type="number" min={0} max={100} value={p.revenue_drop_pct} onChange={num('revenue_drop_pct')} />
            <Input label={t('scenarios.params.costGrowth')} type="number" min={0} max={300} value={p.fixed_cost_growth_pct} onChange={num('fixed_cost_growth_pct')} />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Input label={t('scenarios.params.extraExpense')} inputMode="decimal" placeholder={t('scenarios.params.extraExpensePh')} value={p.extra_expense} onChange={(e) => set('extra_expense', e.target.value)} />
            <DatePicker label={t('scenarios.params.extraExpenseDate')} value={p.extra_expense_date} onChange={(v) => set('extra_expense_date', v)} />
            <Input label={t('scenarios.params.extraIncome')} inputMode="decimal" placeholder={t('scenarios.params.extraIncomePh')} value={p.extra_income} onChange={(e) => set('extra_income', e.target.value)} />
            <DatePicker label={t('scenarios.params.extraIncomeDate')} value={p.extra_income_date} onChange={(v) => set('extra_income_date', v)} />
          </div>

          {formError && <Notice tone="danger">{t(`scenarios.errors.${formError}`)}</Notice>}
          {calc.isError && <Notice tone="danger">{errorText(t, calc.error)}</Notice>}

          <div className="flex flex-wrap items-end gap-2">
            <Button type="submit" variant="primary" disabled={calc.isPending}>
              {calc.isPending && <Loader2 size={16} className="spin" aria-hidden />}
              {calc.isPending ? t('common.calculating') : t('scenarios.params.run')}
            </Button>
            <Button variant="ghost" onClick={() => { setP(ZERO); setOut(null); setFormError(null); calc.reset() }}>{t('common.reset')}</Button>
            <div className="flex-1" />
            <div className="w-full sm:w-56">
              <Input aria-label={t('scenarios.params.name')} placeholder={t('scenarios.params.name')} maxLength={60} value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <Button onClick={save}><Plus size={16} aria-hidden /> {t('common.save')}</Button>
          </div>
        </form>
      </Card>

      {out && <Result out={out} />}
    </>
  )
}

function Result({ out }: { out: { res: ScenarioResponse; base: ForecastView; scenario: ForecastView } }) {
  const { t } = useTranslation()
  const { base, scenario, res } = out
  const b = base.r
  const s = scenario.r
  const bMin = minDay(b)
  const sMin = minDay(s)
  const pb = b.risk.shortfall_probability
  const ps = s.risk.shortfall_probability
  const sGap = scenario.gapDate
  const bGap = base.gapDate
  const dMin = (BigInt(sMin.p50) - BigInt(bMin.p50)).toString()
  const dDef = res.delta.max_deficit_p90

  const concl =
    ps < 0.05
      ? { tone: 'success' as const, text: t('scenarios.concl.safe') }
      : {
          tone: ps >= 0.5 ? ('danger' as const) : ('warning' as const),
          text: t('scenarios.concl.risk', {
            direction: t(ps >= pb ? 'scenarios.concl.up' : 'scenarios.concl.down'),
          }),
        }

  return (
    <>
      <Notice tone={concl.tone} title={t('scenarios.concl.title')}>{concl.text}</Notice>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Compare
          label={t('scenarios.cmp.minBalance')}
          base={formatKop(bMin.p50)} value={formatKop(sMin.p50)}
          delta={dMin} negative={BigInt(sMin.p50) < 0n} note={formatDate(sMin.date)}
        />
        <Compare
          label={t('scenarios.cmp.probability')}
          base={formatPercent(pb)} value={formatPercent(ps)}
          deltaText={Math.abs(ps - pb) < 0.0005 ? null : `${ps > pb ? '+' : '−'}${formatNumber(Math.abs(ps - pb) * 100)} ${t('scenarios.cmp.pp')}`}
          worse={ps > pb} negative={s.risk.level === 'high'}
          note={t(`risk.badge.${s.risk.level}`)}
        />
        <Compare
          label={t('scenarios.cmp.shortageDate')}
          base={bGap && b.risk.level !== 'none' ? formatDate(bGap) : t('common.none')}
          value={sGap && s.risk.level !== 'none' ? formatDate(sGap) : t('common.none')}
          negative={s.risk.level !== 'none' && sGap != null}
        />
        <Compare
          label={t('scenarios.cmp.deficit')}
          base={formatKop(b.risk.max_deficit_p90)} value={formatKop(s.risk.max_deficit_p90)}
          delta={dDef} invertDelta negative={BigInt(s.risk.max_deficit_p90) > 0n && s.risk.level !== 'none'}
        />
      </div>

      <Card
        title={t('scenarios.chartTitle')}
        subtitle={t('scenarios.chartHint')}
        action={<Badge>{t('scenarios.sameSeed')}</Badge>}
      >
        <div className="px-3 pb-4 sm:px-5">
          <ForecastChart
            points={scenario.points}
            today={s.as_of}
            shortageDate={s.risk.level === 'none' ? null : sGap}
            forecastName={t('scenarios.scenario')}
            compare={{ name: t('scenarios.base'), points: base.points.map((p) => ({ date: p.date, balance: p.forecast })) }}
            height={380}
          />
        </div>
      </Card>
    </>
  )
}

interface CompareProps {
  label: string
  base: string
  value: string
  /** Signed kopecks difference (scenario − base). */
  delta?: string | null
  deltaText?: string | null
  worse?: boolean
  /** For deficits, a rise is bad. */
  invertDelta?: boolean
  negative?: boolean
  note?: string
}

function Compare({ label, base, value, delta, deltaText, worse, invertDelta, negative, note }: CompareProps) {
  const { t } = useTranslation()
  let text: string | null = deltaText ?? null
  let bad = worse ?? false
  if (delta != null && BigInt(delta) !== 0n) {
    const d = BigInt(delta)
    bad = invertDelta ? d > 0n : d < 0n
    text = formatKop(delta, { sign: true })
  }
  return (
    <div className="card enter p-5 flex flex-col gap-1.5 min-w-0">
      <p className="text-[13px] text-muted">{label}</p>
      <p className={cn('tnum text-2xl font-semibold leading-tight', negative && 'text-danger')}>{value}</p>
      <p className="text-[13px] text-muted">{t('scenarios.cmp.base')}: <span className="tnum">{base}</span></p>
      {text && (
        <p className={cn('tnum text-[13px] font-medium', bad ? 'text-danger' : 'text-primary')}>
          {t(bad ? 'scenarios.cmp.worse' : 'scenarios.cmp.better', { delta: text })}
        </p>
      )}
      {note && <p className="text-xs text-muted">{note}</p>}
    </div>
  )
}
