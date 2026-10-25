import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { FileUp, Landmark, Loader2, RefreshCw } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { Badge } from '../../components/shared/StatusBadge'
import { Notice } from '../../components/shared/Notice'
import { LoadingState } from '../../components/shared/States'
import { Button } from '../../components/ui/Button'
import { useEditGuard } from '../../app/perms'
import { useFnsEns, useFnsSync } from '../../hooks/queries'
import { formatDate, formatDateTime, formatKop } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import { ApiError } from '../../lib/errors'
import { sourceText } from './labels'
import { FnsImportModal } from './FnsImportModal'

/** ЕНС по данным ФНС: сальдо, недоимка, ближайшие платежи; обновление налогов на год и загрузка справки. */
export function FnsEnsCard() {
  const { t } = useTranslation()
  const ens = useFnsEns()
  const sync = useFnsSync()
  const guard = useEditGuard()
  const [importOpen, setImportOpen] = useState(false)
  const err = ens.error
  const noInn = err instanceof ApiError && err.code === 'inn_required'
  const notImported = err instanceof ApiError && err.code === 'fns_ens_not_imported'
  const e = ens.data

  return (
    <Card title={t('fns.ens.title')} subtitle={t('fns.ens.subtitle')}>
      <div className="flex flex-col gap-3 px-5 pb-5">
        {ens.isPending && <LoadingState height={90} />}
        {noInn && (
          <Notice tone="warning" title={t('fns.noInnTitle')}>
            {t('fns.noInnText')} <Link to="/settings" className="underline">{t('fns.toSettings')}</Link>
          </Notice>
        )}
        {notImported && <Notice tone="info" title={t('fns.ens.notImportedTitle')}>{t('fns.ens.notImportedText')}</Notice>}
        {ens.isError && !noInn && !notImported && <Notice tone="danger">{errorText(t, err)}</Notice>}
        {e && (
          <>
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-muted">{t('fns.ens.balance')}</dt>
                <dd className="tnum text-lg font-semibold">{formatKop(e.balance_kop)}</dd>
              </div>
              <div>
                <dt className="text-muted">{t('fns.ens.debt')}</dt>
                <dd className={'tnum text-lg font-semibold ' + (e.has_debt ? 'text-danger' : '')}>{e.has_debt ? formatKop(e.debt_kop) : t('fns.ens.noDebt')}</dd>
              </div>
            </dl>
            {e.has_debt && <Notice tone="danger">{t('fns.ens.debtNote')}</Notice>}
            {e.upcoming.length > 0 && (
              <ul className="divide-y divide-line text-sm">
                {e.upcoming.slice(0, 3).map((u) => (
                  <li key={u.date + u.kind} className="flex items-center justify-between gap-3 py-1.5">
                    <span className="truncate">{u.title ?? t('fns.ens.payment')} · {formatDate(u.date, 'dayMonth')}</span>
                    <span className="tnum font-semibold shrink-0">{formatKop(u.amount_kop)}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="text-xs text-muted flex flex-wrap items-center gap-1.5">
              <Landmark size={13} aria-hidden />
              <Badge tone="neutral">{sourceText(t, e.source)}</Badge>
              {e.checked_at && <span>{t('fns.checkedAt', { at: formatDateTime(e.checked_at) })}</span>}
            </p>
          </>
        )}

        <div className="flex flex-wrap gap-2">
          <Button variant="primary" disabled={sync.isPending || guard.lock.disabled || noInn} title={guard.lock.title} onClick={() => sync.mutate({})}>
            {sync.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <RefreshCw size={16} aria-hidden />} {t('fns.sync')}
          </Button>
          <Button onClick={() => setImportOpen(true)} disabled={guard.lock.disabled || noInn} title={guard.lock.title}>
            <FileUp size={16} aria-hidden /> {t('fns.importOpen')}
          </Button>
        </div>
        {sync.isSuccess && (
          <Notice tone="success" title={t('fns.syncDoneTitle')}>
            {t('fns.syncDoneText', { created: sync.data.created, skipped: sync.data.skipped, year: sync.data.year })}
            {sync.data.estimated > 0 && <> {t('fns.syncEstimated', { count: sync.data.estimated })}</>}
            {sync.data.no_amount > 0 && <> {t('fns.syncNoAmount', { count: sync.data.no_amount })}</>}
          </Notice>
        )}
        {sync.isError && <Notice tone="danger">{errorText(t, sync.error)}</Notice>}
      </div>
      <FnsImportModal open={importOpen} onClose={() => setImportOpen(false)} />
    </Card>
  )
}
