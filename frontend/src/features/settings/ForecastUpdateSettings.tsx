import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { usePerms } from '../../app/perms'
import { useMe } from '../../app/auth'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import { cn } from '../../lib/utils'
import type { ForecastUpdateMode } from '../../types'

const MODES: ForecastUpdateMode[] = ['full', 'incremental']

/** Как пересчитывать прогноз при новых данных: целиком (по умолчанию) или дописывать новые операции
 * к уже обученной модели. Режим общий для компании, меняют владелец и администраторы. */
export function ForecastUpdateSettings() {
  const { t } = useTranslation()
  const me = useMe()
  const { admin } = usePerms()
  const qc = useQueryClient()
  const key = ['co', me.company_id, 'forecast-settings']
  const q = useQuery({ queryKey: key, queryFn: api.forecastSettings })
  const save = useMutation({
    mutationFn: api.saveForecastSettings,
    onSuccess: (v) => qc.setQueryData(key, v),
  })
  const mode = q.data?.update_mode

  return (
    <Card title={t('settings.forecastUpdate.title')} subtitle={t('settings.forecastUpdate.subtitle')}>
      <div className="px-5 pb-5 flex flex-col gap-3">
        {q.isPending && <LoadingState height={80} />}
        {q.isError && <ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} />}
        {mode && (
          <fieldset className="flex flex-col gap-2" disabled={!admin || save.isPending}>
            <legend className="sr-only">{t('settings.forecastUpdate.title')}</legend>
            {MODES.map((m) => (
              <label
                key={m}
                className={cn(
                  'flex gap-3 rounded-lg border p-3 text-sm',
                  m === mode ? 'border-primary bg-primary/5' : 'border-line',
                  admin ? 'cursor-pointer' : 'cursor-not-allowed',
                )}
              >
                <input
                  type="radio"
                  name="forecast-update-mode"
                  className="mt-0.5"
                  style={{ accentColor: 'var(--c-primary)' }}
                  checked={m === mode}
                  onChange={() => save.mutate(m)}
                />
                <span className="flex flex-col gap-0.5">
                  <span className="font-medium">{t(`settings.forecastUpdate.${m}`)}</span>
                  <span className="text-xs text-muted">{t(`settings.forecastUpdate.${m}Hint`)}</span>
                </span>
              </label>
            ))}
          </fieldset>
        )}
        {!admin && <p className="text-xs text-muted">{t('perms.adminOnly')}</p>}
        {save.isSuccess && <p className="text-xs text-primary" role="status">{t('common.saved')}</p>}
        {save.isError && <Notice tone="danger">{errorText(t, save.error)}</Notice>}
      </div>
    </Card>
  )
}
