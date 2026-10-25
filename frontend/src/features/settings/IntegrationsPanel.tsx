import { useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Loader2, PlugZap, RefreshCw, Unplug } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Badge } from '../../components/shared/StatusBadge'
import type { Tone } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { useMe } from '../../app/auth'
import { useEditGuard, usePerms } from '../../app/perms'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import { useSyncIntegrations } from '../../hooks/queries'
import type { IntegrationCheck, IntegrationItem, IntegrationsView } from '../../types'
import { TelegramBotForm } from './TelegramBotForm'

const STATUS_TONE: Record<string, Tone> = { ok: 'success', error: 'danger', unchecked: 'warning', not_connected: 'neutral', sim: 'neutral' }
// Объекты 1С, которые нужно отметить в составе OData (docs/integrator.md)
const OBJECTS = ['Контрагенты', 'ДоговорыКонтрагентов', 'СчетНаОплатуПокупателю', 'СчетНаОплатуПоставщика', 'ПоступлениеНаРасчетныйСчет', 'СписаниеСРасчетногоСчета', 'ПлатежноеПоручение']

function Section({ title, badges, children, id }: { title: string; badges: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section id={id} className="rounded-card border border-line p-4 flex flex-col gap-3 scroll-mt-20">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <div className="flex flex-wrap items-center gap-1.5">{badges}</div>
      </div>
      {children}
    </section>
  )
}

function CheckResult({ check }: { check: IntegrationCheck }) {
  const { t } = useTranslation()
  if (check.ok) {
    return (
      <Notice tone="success" title={t('integrations.onec.checkOk')}>
        {t('integrations.onec.checkDocs', { count: check.documents ?? 0 })}
        {check.more ? ` ${t('integrations.onec.checkMore')}` : ''}
        {check.optional_missing && check.optional_missing.length > 0 && (
          <p className="mt-1 text-xs">{t('integrations.onec.optionalMissing', { names: check.optional_missing.join(', ') })}</p>
        )}
      </Notice>
    )
  }
  return (
    <Notice tone="danger" title={t(`integrations.codes.${check.code}`, { defaultValue: check.message })}>
      {check.missing && check.missing.length > 0 && <p>{t('integrations.onec.missing', { names: check.missing.join(', ') })}</p>}
      <p className="text-xs">{t(`integrations.hints.${check.code}`, { defaultValue: '' })}</p>
    </Notice>
  )
}

function OnecCard({ item, view }: { item: IntegrationItem; view: IntegrationsView }) {
  const { t } = useTranslation()
  const me = useMe()
  const qc = useQueryClient()
  const { manage } = usePerms()
  const key = ['co', me.company_id, 'integrations']
  const [url, setUrl] = useState(item.url ?? '')
  const [username, setUsername] = useState(item.username ?? '')
  const [password, setPassword] = useState('')
  const [check, setCheck] = useState<IntegrationCheck | null>(null)

  // подставляем сохранённые адрес и логин, когда они загружены или изменились на сервере
  useEffect(() => {
    setUrl(item.url ?? '')
    setUsername(item.username ?? '')
  }, [item.url, item.username])

  const done = (v: IntegrationsView) => {
    qc.setQueryData(key, v)
    setCheck(v.check ?? null)
  }
  const save = useMutation({
    mutationFn: () => api.saveOnec({ url: url.trim(), username: username.trim(), ...(password ? { password } : {}) }),
    onSuccess: (v) => {
      setPassword('')
      done(v)
    },
  })
  const test = useMutation({
    mutationFn: () => api.testOnec({ url: url.trim(), username: username.trim(), ...(password ? { password } : {}) }),
    onSuccess: done,
  })
  const disconnect = useMutation({
    mutationFn: () => api.disconnectOnec(),
    onSuccess: () => {
      setPassword('')
      setUrl('')
      setUsername('')
      setCheck(null)
      void qc.invalidateQueries({ queryKey: key })
    },
  })
  const busy = save.isPending || test.isPending || disconnect.isPending
  const keyMissing = view.key_configured === false
  const filled = url.trim() !== '' && username.trim() !== ''
  const reset = () => {
    save.reset()
    test.reset()
    setCheck(null)
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate()
  }
  const askDisconnect = () => {
    if (window.confirm(t('integrations.onec.disconnectConfirm'))) disconnect.mutate()
  }

  return (
    <Section
      id="integrations-onec"
      title={t('integrations.onec.title')}
      badges={
        <>
          <Badge tone={STATUS_TONE[item.status] ?? 'neutral'}>{t(`integrations.status.${item.status}`)}</Badge>
          <Badge>{t(`integrations.source.${item.source}`)}</Badge>
        </>
      }
    >
      <p className="text-[13px] text-muted">{item.connected ? t('integrations.onec.liveText') : t('integrations.onec.simText')}</p>

      {item.connected && (
        <dl className="grid gap-x-4 gap-y-1 text-[13px] sm:grid-cols-[auto_1fr]">
          <dt className="text-muted">{t('integrations.lastCheck')}</dt>
          <dd>{item.last_check_at ? formatDateTime(item.last_check_at) : t('integrations.never')}</dd>
          <dt className="text-muted">{t('integrations.lastSync')}</dt>
          <dd>{item.last_sync_at ? formatDateTime(item.last_sync_at) : t('integrations.never')}</dd>
        </dl>
      )}
      {item.status === 'error' && item.last_error && <Notice tone="danger" title={t('integrations.lastError')}>{item.last_error}</Notice>}

      {!manage && <p className="text-xs text-muted">{t('integrations.onec.managerOnly')}</p>}

      {manage && (
        <form className="flex flex-col gap-3" onSubmit={submit} autoComplete="off">
          {keyMissing && <Notice tone="warning" title={t('integrations.onec.keyMissingTitle')}>{t('integrations.onec.keyMissing')}</Notice>}
          <Input
            label={t('integrations.onec.url')}
            hint={t('integrations.onec.urlHint')}
            placeholder="https://msk1.1cfresh.com/a/ea/4269703"
            value={url}
            onChange={(e) => { setUrl(e.target.value); reset() }}
            maxLength={300}
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
          />
          <div className="grid gap-3 sm:grid-cols-2">
            <Input label={t('integrations.onec.username')} value={username} onChange={(e) => { setUsername(e.target.value); reset() }} maxLength={100} autoComplete="off" spellCheck={false} />
            <Input
              label={t('integrations.onec.password')}
              type="password"
              value={password}
              onChange={(e) => { setPassword(e.target.value); reset() }}
              maxLength={200}
              autoComplete="new-password"
              placeholder={item.password_set ? '••••••••' : ''}
              hint={item.password_set ? t('integrations.onec.passwordKept') : t('integrations.onec.passwordHint')}
            />
          </div>

          <details className="rounded-control border border-line bg-app px-3 py-2 text-[13px]">
            <summary className="cursor-pointer font-medium">{t('integrations.onec.howTitle')}</summary>
            <ol className="mt-2 list-decimal pl-5 flex flex-col gap-1 text-muted">
              <li>{t('integrations.onec.how1')}</li>
              <li>{t('integrations.onec.how2')}</li>
              <li>{t('integrations.onec.how3')} <span className="text-ink">{OBJECTS.join(', ')}</span>.</li>
              <li>{t('integrations.onec.how4')}</li>
              <li>{t('integrations.onec.how5')}</li>
            </ol>
            <p className="mt-2 text-xs text-muted">{t('integrations.onec.howSecurity')}</p>
          </details>

          <div className="flex flex-wrap gap-2">
            <Button type="button" disabled={busy || !filled || (!password && !item.connected)} onClick={() => test.mutate()}>
              {test.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <PlugZap size={16} aria-hidden />} {t('integrations.onec.test')}
            </Button>
            <Button type="submit" variant="primary" disabled={busy || !filled || keyMissing || (!password && !item.connected)}>
              {save.isPending && <Loader2 size={16} className="spin" aria-hidden />} {save.isPending ? t('common.saving') : t('common.save')}
            </Button>
            {item.connected && (
              <Button type="button" variant="danger" disabled={busy} onClick={askDisconnect}>
                <Unplug size={16} aria-hidden /> {t('integrations.onec.disconnect')}
              </Button>
            )}
          </div>
          {check && <CheckResult check={check} />}
          {(save.isError || test.isError || disconnect.isError) && <Notice tone="danger">{errorText(t, save.error ?? test.error ?? disconnect.error)}</Notice>}
          {save.isSuccess && !check && <p className="text-xs text-primary" role="status">{t('integrations.onec.saved')}</p>}
        </form>
      )}
    </Section>
  )
}

/** Свой Telegram-бот компании: токен вводит директор, без токена работает общий бот платформы. Подробности — в TelegramBotForm. */
function TelegramCard({ item }: { item: IntegrationItem }) {
  const { t } = useTranslation()
  return (
    <Section id="integrations-telegram" title={t('integrations.telegram.title')} badges={null}>
      <TelegramBotForm item={item} />
      <p className="text-xs text-muted">
        <a href="#schedule" className="text-primary underline">{t('integrations.telegram.scheduleLink')}</a>
      </p>
    </Section>
  )
}

function SimulatorCard({ kind }: { kind: 'edo' }) {
  const { t } = useTranslation()
  return (
    <Section title={t(`integrations.${kind}.title`)} badges={null}>
      <p className="text-[13px] text-muted">{t(`integrations.${kind}.text`)}</p>
    </Section>
  )
}

/** Настройки → Интеграции: подключения компании. 1С вводит и отключает владелец (право manage), остальные видят статус
 * без адреса и логина. Пароль нигде не показывается: после сохранения поле очищается, сервер хранит его зашифрованным. */
export function IntegrationsPanel() {
  const { t } = useTranslation()
  const me = useMe()
  const { lock } = useEditGuard()
  const q = useQuery({ queryKey: ['co', me.company_id, 'integrations'], queryFn: api.integrations })
  const sync = useSyncIntegrations()
  const loaded = !!q.data
  useEffect(() => {
    if (loaded && window.location.hash === '#integrations') document.getElementById('integrations')?.scrollIntoView({ block: 'start' })
  }, [loaded])
  const onec = q.data?.items.find((i) => i.kind === 'onec')
  const telegram = q.data?.items.find((i) => i.kind === 'telegram')
  const onecErr = sync.data?.sources?.onec?.status === 'error' ? sync.data.sources.onec : null

  return (
    <Card title={t('integrations.title')} subtitle={t('integrations.subtitle')}>
      <div id="integrations" className="px-5 pb-5 flex flex-col gap-4 scroll-mt-20">
        {q.isPending && <LoadingState height={120} />}
        {q.isError && <ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} />}
        {q.data && onec && (
          <>
            <OnecCard item={onec} view={q.data} />
            {telegram && <TelegramCard item={telegram} />}
            <Section title={t('integrations.fns.title')} badges={<Badge>{t('integrations.fns.badge')}</Badge>}>
              <p className="text-[13px] text-muted">
                {t('integrations.fns.text')}{' '}
                <a href="#fns" className="text-primary underline">{t('integrations.fns.link')}</a>
              </p>
            </Section>
            <SimulatorCard kind="edo" />
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-3">
                <Button variant="primary" disabled={sync.isPending || lock.disabled} title={lock.title} onClick={() => sync.mutate('C001')}>
                  {sync.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <RefreshCw size={16} aria-hidden />} {sync.isPending ? t('sync.running') : t('integrations.syncNow')}
                </Button>
                <span className="text-xs text-muted">{t('integrations.syncHint')}</span>
              </div>
              {sync.isSuccess && (
                <Notice tone={onecErr ? 'warning' : 'success'} title={t('sync.doneTitle')}>
                  {t('sync.doneText', { created: sync.data.created, skipped: sync.data.skipped })}{' '}
                  {sync.data.onec && t(`integrations.syncSource.${sync.data.onec}`)}
                  {onecErr && <p className="mt-1">{t('integrations.syncOnecError', { error: onecErr.error ?? '' })}</p>}
                </Notice>
              )}
              {sync.isError && <Notice tone="danger">{errorText(t, sync.error)}</Notice>}
            </div>
          </>
        )}
      </div>
    </Card>
  )
}
