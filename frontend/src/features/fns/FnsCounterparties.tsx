import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Card } from '../../components/shared/Page'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { Badge } from '../../components/shared/StatusBadge'
import { EmptyState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { useFnsCounterparties } from '../../hooks/queries'
import { formatDate, formatKop, formatPercent } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import { ApiError } from '../../lib/errors'
import type { FnsCounterparty, FnsRisk, FnsSettings } from '../../types'
import { riskReasons, riskTone, sourceText, statusLabel } from './labels'

const enabledSources = (s?: FnsSettings) =>
  s ? (['egrul', 'data_nalog', 'fedresurs'] as const).filter((k) => s.sources[k]) : ['egrul', 'data_nalog']

const RANK: Record<FnsRisk, number> = { high: 3, medium: 2, low: 1, unknown: 0 }
/** С какой доли ожидаемых поступлений клиент считается крупным для предупреждения. */
const BIG_SHARE = 0.1

/** Контрагент только из ожидаемых событий (счета 1С): долю считаем от ожидаемых поступлений, а не от выписки. */
const expectedOnly = (c: FnsCounterparty) => !!c.origin?.includes('expected') && !c.origin?.includes('statement')
const shareOf = (c: FnsCounterparty) => (expectedOnly(c) ? c.expected_share : c.income_share) ?? null

/** Таблица «Контрагенты и риски» и предупреждение о рисковом клиенте. Данные выгружены из открытых реестров ФНС;
 * что проверять и что считать риском, компания задаёт в «Настройки → Сведения ФНС». */
export function FnsCounterparties() {
  const { t } = useTranslation()
  const q = useFnsCounterparties()
  const noInn = q.error instanceof ApiError && q.error.code === 'inn_required'
  if (noInn) return null // подсказку про ИНН уже показывает карточка ЕНС
  const items = q.data?.items ?? []
  const risky = items.filter((c) => c.checked && c.risk_level === 'high' && (shareOf(c) ?? 0) >= BIG_SHARE)

  const columns: Column<FnsCounterparty>[] = [
    {
      key: 'name', header: t('fns.cp.name'), sortValue: (c) => c.name ?? c.inn,
      render: (c) => (
        <span className="flex flex-col min-w-[150px]">
          <span className="font-medium">{c.name ?? c.inn}</span>
          <span className="text-xs text-muted">
            {t('fns.cp.inn', { inn: c.inn })}
            {c.origin?.includes('onec') && <> · {t('fns.cp.fromOnec')}</>}
            {Number(c.expected_in_kop ?? 0) > 0 && <> · {t('fns.cp.expectedIn', { amount: formatKop(c.expected_in_kop) })}</>}
            {Number(c.expected_out_kop ?? 0) > 0 && <> · {t('fns.cp.expectedOut', { amount: formatKop(c.expected_out_kop) })}</>}
          </span>
          {c.checked && riskReasons(t, c).length > 0 && <span className="text-xs text-muted">{riskReasons(t, c).join(' · ')}</span>}
        </span>
      ),
    },
    {
      key: 'share', header: t('fns.cp.share'), numeric: true, sortValue: (c) => shareOf(c) ?? 0,
      render: (c) => {
        const s = shareOf(c)
        if (s == null) return '—'
        return expectedOnly(c) ? <span title={t('fns.cp.expectedShareHint')}>{formatPercent(s)} <span className="text-xs text-muted">{t('fns.cp.expectedMark')}</span></span> : formatPercent(s)
      },
    },
    { key: 'status', header: t('fns.cp.status'), sortValue: (c) => c.status, render: (c) => (c.checked ? <span title={c.sources?.egrul === 'off' ? t('fns.cp.egrulOff') : undefined}>{statusLabel(t, c.status)}</span> : '—') },
    {
      key: 'debt', header: t('fns.cp.debt'), numeric: true, sortValue: (c) => Number(c.tax_debt_kop ?? 0),
      render: (c) => {
        if (!c.checked) return '—'
        // дата, на которую ФНС опубликовала сведения о задолженности
        const asOf = c.tax_debt_as_of ? <span className="block text-xs text-muted">{t('fns.cp.debtAsOf', { date: formatDate(c.tax_debt_as_of) })}<br />{t('fns.sourceName.data_nalog')}</span> : null
        return c.has_tax_debt ? <span><span className="text-danger font-semibold">{formatKop(c.tax_debt_kop)}</span>{asOf}</span> : <span>{t('fns.cp.noDebt')}{asOf}</span>
      },
    },
    {
      key: 'risk', header: t('fns.cp.risk'), sortValue: (c) => RANK[c.risk_level],
      render: (c) => <Badge tone={riskTone[c.risk_level]}>{t(`fns.risk.${c.risk_level}`)}</Badge>,
    },
  ]

  return (
    <Card title={t('fns.cp.title')} subtitle={t('fns.cp.subtitle')}>
      <div className="flex flex-col gap-3 pb-4">
        {risky.map((c) => (
          <div key={c.inn} className="px-5">
            <Notice tone="danger" title={t('fns.cp.riskyTitle', { name: c.name ?? c.inn, share: formatPercent(shareOf(c) ?? 0) })}>
              {t('fns.cp.riskyText')} {riskReasons(t, c).join(', ')}.{' '}
              <Link to="/scenarios" className="underline">{t('fns.cp.riskyScenario')}</Link>
            </Notice>
          </div>
        ))}
        {q.isPending && <LoadingState height={120} />}
        {q.isError && <div className="px-5"><Notice tone="danger">{errorText(t, q.error)}</Notice></div>}
        {q.data && items.length === 0 && <EmptyState title={t('fns.cp.emptyTitle')} description={t('fns.cp.emptyText')} />}
        {q.data && items.length > 0 && (
          <>
            <DataTable columns={columns} rows={items} getRowKey={(c) => c.inn} defaultSort={{ key: 'share', dir: 'desc' }} rowClassName={(c) => (c.checked && c.risk_level === 'high' ? 'bg-danger-soft' : undefined)} />
            <p className="px-5 text-xs text-muted flex flex-wrap items-center gap-1.5">
              <Badge tone="neutral">{t('fns.cp.exported')}</Badge>
              <span>{enabledSources(q.data.settings).map((s) => sourceText(t, s)).join(', ')}</span>
              {q.data.debt_as_of && <span>· {t('fns.cp.debtDataAsOf', { date: formatDate(q.data.debt_as_of) })}</span>}
              {q.data.checked_at && <span>· {t('fns.cp.checked', { date: formatDate(q.data.checked_at) })}</span>}
              {q.data.settings && !q.data.settings.use_in_forecast && <span>· {t('fns.cp.notInForecast')}</span>}
              {!q.data.saved && <span>· {t('fns.cp.notSaved')}</span>}
              <Link to="/settings#fns" className="underline">{t('fns.cp.configure')}</Link>
            </p>
          </>
        )}
      </div>
    </Card>
  )
}
