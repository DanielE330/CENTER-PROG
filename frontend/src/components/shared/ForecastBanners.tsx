import { useTranslation } from 'react-i18next'
import type { ForecastRun } from '../../types'
import { Button } from '../ui/Button'
import { Notice } from './Notice'
import { useRecalc } from '../../hooks/queries'

/** "Data changed, forecast is stale" (with a recalc action) and "forecast was recomputed automatically". */
export function ForecastBanners({ run }: { run: ForecastRun }) {
  const { t } = useTranslation()
  const recalc = useRecalc()
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
      {!run.stale && run.recomputed && (
        <Notice tone="info" title={t('banner.recomputedTitle')}>{t('banner.recomputedText')}</Notice>
      )}
    </>
  )
}
