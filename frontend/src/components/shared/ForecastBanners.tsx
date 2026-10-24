import { useTranslation } from 'react-i18next'
import type { ForecastRun } from '../../types'
import { Button } from '../ui/Button'
import { Notice } from './Notice'
import { useRecalc } from '../../hooks/queries'
import { formatDate } from '../../lib/formatters'

/** "Data changed, forecast is stale" (with a recalc action), "forecast was recomputed automatically" and
 * "forecast was updated with new data without a full refit" (with a full-recompute action). */
export function ForecastBanners({ run }: { run: ForecastRun }) {
  const { t } = useTranslation()
  const recalc = useRecalc()
  const upd = run.update
  const incremental = upd?.mode === 'incremental'
  return (
    <>
      {run.stale && (
        <Notice
          tone="warning"
          title={t('banner.staleTitle')}
          action={
            <Button size="sm" disabled={recalc.isPending} onClick={() => recalc.mutate(run.result.horizon)}>
              {recalc.isPending ? t('common.calculating') : t('common.recalculate')}
            </Button>
          }
        >
          {t('banner.staleText')}
        </Notice>
      )}
      {!run.stale && run.recomputed && !incremental && (
        <Notice tone="info" title={t('banner.recomputedTitle')}>{t('banner.recomputedText')}</Notice>
      )}
      {!run.stale && upd && incremental && (
        <Notice
          tone="info"
          title={t('banner.incrementalTitle')}
          action={
            <Button size="sm" disabled={recalc.isPending} onClick={() => recalc.mutate({ horizon: run.result.horizon, full: true })}>
              {recalc.isPending ? t('common.calculating') : t('banner.fullRecalc')}
            </Button>
          }
        >
          {upd.ops_added > 0
            ? t('banner.incrementalText', { count: upd.ops_added, date: formatDate(upd.full_fit_at) })
            : t('banner.incrementalNoOps', { date: formatDate(upd.full_fit_at) })}
        </Notice>
      )}
    </>
  )
}
