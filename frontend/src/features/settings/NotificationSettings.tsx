import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Loader2, Mail, Send } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Badge } from '../../components/shared/StatusBadge'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Checkbox } from '../../components/ui/Checkbox'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import type { NotificationPrefs } from '../../types'

const KEY = ['account', 'notifications']

/** Свои письма: алерты о кассовом разрыве и отчёты. Что и когда отправлять, решает общее расписание компании
 * (блок «Расписание уведомлений»), здесь каждый сотрудник только включает или выключает почту для себя. */
export function NotificationSettings() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const prefs = useQuery({ queryKey: KEY, queryFn: api.notificationPrefs })
  const save = useMutation({
    mutationFn: (b: Partial<NotificationPrefs>) => api.saveNotificationPrefs(b),
    onSuccess: (v) => qc.setQueryData(KEY, v),
  })
  const test = useMutation({ mutationFn: api.testEmail })
  const resend = useMutation({ mutationFn: api.resendVerification })

  const p = prefs.data

  // ссылка «Настроить уведомления» из письма ведёт сюда: блок появляется после загрузки, поэтому прокручиваем сами
  useEffect(() => {
    if (p && window.location.hash === '#notifications') document.getElementById('notifications')?.scrollIntoView({ block: 'start' })
  }, [p])
  const locked = !p?.email_verified
  const busy = save.isPending
  const error = save.error ?? test.error ?? resend.error

  return (
    <div id="notifications" className="scroll-mt-6">
      <Card title={t('mail.settings.title')} subtitle={t('mail.settings.subtitle')}>
        <div className="px-5 pb-5 flex flex-col gap-4">
          {prefs.isPending && <LoadingState height={80} />}
          {prefs.isError && <ErrorState message={errorText(t, prefs.error)} onRetry={() => void prefs.refetch()} />}
          {p && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Mail size={16} className="text-muted" aria-hidden />
                <span className="font-medium break-all">{p.email ?? '—'}</span>
                {p.email_verified ? <Badge tone="success">{t('mail.settings.verified')}</Badge> : <Badge tone="warning">{t('mail.settings.unverified')}</Badge>}
              </div>
              {locked && (
                <Notice tone="warning" title={t('mail.settings.lockedTitle')}>
                  <p>{t('mail.settings.lockedText')}</p>
                  <Button size="sm" className="mt-2" disabled={resend.isPending || resend.isSuccess} onClick={() => resend.mutate()}>
                    {resend.isPending && <Loader2 size={14} className="spin" aria-hidden />}
                    {resend.isSuccess ? t('mail.banner.sentShort') : t('mail.banner.resend')}
                  </Button>
                </Notice>
              )}

              <fieldset className="flex flex-col gap-3" disabled={locked || busy}>
                <Checkbox
                  label={t('mail.settings.alerts')}
                  checked={p.alerts}
                  disabled={locked || busy}
                  onChange={(e) => save.mutate({ alerts: e.target.checked })}
                />
                <p className="text-xs text-muted -mt-2 pl-7">{t('mail.settings.alertsHint')}</p>
                <Checkbox
                  label={t('mail.settings.reports')}
                  checked={p.reports}
                  disabled={locked || busy}
                  onChange={(e) => save.mutate({ reports: e.target.checked })}
                />
                <p className="text-xs text-muted -mt-2 pl-7">{t('mail.settings.reportsHint')}</p>
              </fieldset>

              <p className="text-xs text-muted">
                {t('mail.settings.schedule')}{' '}
                <a
                  href="#schedule"
                  className="text-primary font-medium hover:underline"
                  onClick={(e) => {
                    e.preventDefault()
                    document.getElementById('schedule')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                  }}
                >
                  {t('mail.settings.scheduleLink')}
                </a>
              </p>

              <div className="flex flex-wrap gap-2">
                <Button size="sm" disabled={locked || test.isPending} onClick={() => test.mutate()}>
                  {test.isPending ? <Loader2 size={14} className="spin" aria-hidden /> : <Send size={14} aria-hidden />} {t('mail.settings.test')}
                </Button>
              </div>
              {save.isSuccess && !error && <p className="text-xs text-primary" role="status">{t('common.saved')}</p>}
              {test.isSuccess && <Notice tone="success">{t('mail.settings.testSent', { email: test.data.email })}</Notice>}
              {error != null && <Notice tone="danger">{errorText(t, error)}</Notice>}
            </>
          )}
        </div>
      </Card>
    </div>
  )
}
