import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { usePerms } from '../../app/perms'
import { useMe } from '../../app/auth'
import { api } from '../../lib/api'
import { formatDate } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { FnsSettings } from '../../types'

const SOURCES = ['egrul', 'data_nalog', 'fedresurs'] as const
const RISKS = ['bankruptcy', 'liquidation', 'tax_debt', 'unreliable', 'disqualified', 'mass_address', 'not_in_registry'] as const

export function Toggle({ checked, onChange, label, hint, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint?: string; disabled?: boolean }) {
  return (
    <label className={`flex gap-3 rounded-lg border border-line p-3 text-sm ${disabled ? 'cursor-not-allowed opacity-70' : 'cursor-pointer'}`}>
      <input type="checkbox" className="mt-0.5" style={{ accentColor: 'var(--c-primary)' }} checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="flex flex-col gap-0.5">
        <span className="font-medium">{label}</span>
        {hint && <span className="text-xs text-muted">{hint}</span>}
      </span>
    </label>
  )
}

/** Что компания хочет видеть из ФНС: источники, признаки риска, кого проверять и учитывать ли риск в прогнозе.
 * Все сведения — из открытых реестров ФНС; меняют владелец и администраторы. */
export function FnsSettingsCard() {
  const { t } = useTranslation()
  const me = useMe()
  const { admin } = usePerms()
  const qc = useQueryClient()
  const key = ['co', me.company_id, 'fns-settings']
  const q = useQuery({ queryKey: key, queryFn: api.fnsSettings })
  const [draft, setDraft] = useState<FnsSettings | null>(null)
  useEffect(() => {
    if (q.data) setDraft(q.data.settings)
  }, [q.data])
  // переход по ссылке «Настроить» из таблицы контрагентов (/settings#fns)
  const loaded = !!q.data
  useEffect(() => {
    if (loaded && window.location.hash === '#fns') document.getElementById('fns')?.scrollIntoView({ block: 'start' })
  }, [loaded])
  const save = useMutation({
    mutationFn: api.saveFnsSettings,
    onSuccess: (v) => {
      qc.setQueryData(key, v)
      void qc.invalidateQueries({ queryKey: ['co', me.company_id] })
    },
  })
  const set = (patch: (s: FnsSettings) => FnsSettings) => {
    save.reset()
    setDraft((d) => (d ? patch(structuredClone(d)) : d))
  }
  const off = !admin || save.isPending
  const info = q.data?.info

  return (
    <Card title={t('settings.fns.title')} subtitle={t('settings.fns.subtitle')}>
      <div id="fns" className="px-5 pb-5 flex flex-col gap-4 scroll-mt-20">
        {q.isPending && <LoadingState height={120} />}
        {q.isError && <ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} />}
        {draft && (
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              save.mutate(draft)
            }}
          >
            <fieldset className="flex flex-col gap-2" disabled={off}>
              <legend className="text-sm font-semibold mb-1">{t('settings.fns.sources')}</legend>
              {SOURCES.map((s) => (
                <Toggle
                  key={s}
                  checked={draft.sources[s]}
                  disabled={off}
                  onChange={(v) => set((d) => ({ ...d, sources: { ...d.sources, [s]: v } }))}
                  label={t(`fns.sourceName.${s}`)}
                  hint={
                    s === 'data_nalog' && info?.debt_as_of
                      ? t('settings.fns.debtAsOf', { date: formatDate(info.debt_as_of) })
                      : s === 'fedresurs' && info && !info.fedresurs_configured
                        ? t('settings.fns.fedresursNoLogin')
                        : t(`settings.fns.sourceHint.${s}`)
                  }
                />
              ))}
            </fieldset>

            <fieldset className="flex flex-col gap-2" disabled={off}>
              <legend className="text-sm font-semibold mb-1">{t('settings.fns.risk')}</legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {RISKS.map((r) => (
                  <Toggle key={r} checked={draft.risk[r]} disabled={off} onChange={(v) => set((d) => ({ ...d, risk: { ...d.risk, [r]: v } }))} label={t(`settings.fns.riskName.${r}`)} hint={r === 'not_in_registry' ? t('settings.fns.notInRegistryHint') : undefined} />
                ))}
              </div>
              <Input
                label={t('settings.fns.largeDebt')}
                hint={t('settings.fns.largeDebtHint')}
                type="number"
                min={0}
                step={1000}
                value={Math.round(draft.risk.large_debt_kop / 100)}
                readOnly={off}
                onChange={(e) => set((d) => ({ ...d, risk: { ...d.risk, large_debt_kop: Math.max(0, Math.round(Number(e.target.value) || 0)) * 100 } }))}
              />
            </fieldset>

            <fieldset className="flex flex-col gap-2" disabled={off}>
              <legend className="text-sm font-semibold mb-1">{t('settings.fns.show')}</legend>
              <div className="grid gap-2 sm:grid-cols-2">
                <Toggle checked={draft.show_statement} disabled={off} onChange={(v) => set((d) => ({ ...d, show_statement: v }))} label={t('settings.fns.showStatement')} />
                <Toggle checked={draft.show_expected} disabled={off} onChange={(v) => set((d) => ({ ...d, show_expected: v }))} label={t('settings.fns.showExpected')} />
              </div>
              <Input
                label={t('settings.fns.top')}
                type="number"
                min={1}
                max={50}
                value={draft.top}
                readOnly={off}
                onChange={(e) => set((d) => ({ ...d, top: Math.min(50, Math.max(1, Math.round(Number(e.target.value) || 1))) }))}
              />
            </fieldset>

            <fieldset className="flex flex-col gap-2" disabled={off}>
              <legend className="text-sm font-semibold mb-1">{t('settings.fns.use')}</legend>
              <Toggle checked={draft.use_in_forecast} disabled={off} onChange={(v) => set((d) => ({ ...d, use_in_forecast: v }))} label={t('settings.fns.useInForecast')} hint={t('settings.fns.useInForecastHint')} />
              <Toggle checked={draft.check_after_sync} disabled={off} onChange={(v) => set((d) => ({ ...d, check_after_sync: v }))} label={t('settings.fns.checkAfterSync')} hint={t('settings.fns.checkAfterSyncHint')} />
            </fieldset>

            {admin && (
              <div className="flex flex-wrap gap-2">
                <Button type="submit" variant="primary" disabled={save.isPending}>{t('common.save')}</Button>
                {q.data && (
                  <Button type="button" disabled={save.isPending} onClick={() => set(() => structuredClone(q.data.defaults))}>
                    {t('settings.fns.reset')}
                  </Button>
                )}
              </div>
            )}
          </form>
        )}
        {!admin && <p className="text-xs text-muted">{t('perms.adminOnly')}</p>}
        {save.isSuccess && <p className="text-xs text-primary" role="status">{t('settings.fns.saved')}</p>}
        {save.isError && <Notice tone="danger">{errorText(t, save.error)}</Notice>}
      </div>
    </Card>
  )
}
