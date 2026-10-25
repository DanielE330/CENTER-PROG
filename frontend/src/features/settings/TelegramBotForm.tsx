import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ExternalLink, Loader2, PlugZap, Unplug } from 'lucide-react'
import { Notice } from '../../components/shared/Notice'
import { Badge } from '../../components/shared/StatusBadge'
import type { Tone } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { useMe } from '../../app/auth'
import { usePerms } from '../../app/perms'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { IntegrationItem, IntegrationsView } from '../../types'

/** Формат токена BotFather (проверяется и на сервере): id бота, двоеточие, секрет. */
export const BOT_TOKEN_RE = /^\d{6,12}:[A-Za-z0-9_-]{30,50}$/
export const INTEGRATIONS_KEY = (companyId: string) => ['co', companyId, 'integrations']
const TONE: Record<string, Tone> = { ok: 'success', error: 'danger', unchecked: 'warning', not_connected: 'neutral' }

/** Статус Telegram-подключения компании (из общего запроса интеграций). */
export function useTelegramItem() {
  const me = useMe()
  const q = useQuery({ queryKey: INTEGRATIONS_KEY(me.company_id), queryFn: api.integrations })
  return { q, item: q.data?.items.find((i) => i.kind === 'telegram') }
}

/** Ввод токена СВОЕГО бота: проверка через getMe, сохранение (сервер шифрует), отключение. Токен после сохранения не показывается
 * и не хранится в состоянии. Не владелец видит только статус. Общий бот платформы остаётся запасным вариантом. */
export function TelegramBotForm({ item }: { item: IntegrationItem }) {
  const { t } = useTranslation()
  const me = useMe()
  const qc = useQueryClient()
  const { manage } = usePerms()
  const [token, setToken] = useState('')
  const [justConnected, setJustConnected] = useState<string | null>(null)
  const [checked, setChecked] = useState<string | null>(null)
  const own = item.connected
  const trimmed = token.trim()
  const valid = BOT_TOKEN_RE.test(trimmed)
  const showBad = trimmed !== '' && !valid
  const botUrl = (name: string) => `https://t.me/${name.replace(/^@/, '')}`

  const refresh = (v?: IntegrationsView) => {
    if (v) qc.setQueryData(INTEGRATIONS_KEY(me.company_id), v)
    else void qc.invalidateQueries({ queryKey: INTEGRATIONS_KEY(me.company_id) })
    void qc.invalidateQueries({ queryKey: ['notifications'] }) // карточка «Telegram-уведомления» сразу видит, что бот работает
  }
  const save = useMutation({
    mutationFn: () => api.saveTelegram(trimmed),
    onSuccess: (v) => {
      setToken('')
      setChecked(null)
      setJustConnected(v.check?.bot_username ?? v.items.find((i) => i.kind === 'telegram')?.bot_username ?? '')
      refresh(v)
    },
  })
  const test = useMutation({
    mutationFn: () => api.testTelegram(),
    onSuccess: (v) => {
      setJustConnected(null)
      setChecked(v.check?.ok ? (v.check.bot_username ?? '') : null)
      refresh(v)
    },
  })
  const disconnect = useMutation({
    mutationFn: () => api.disconnectTelegram(),
    onSuccess: () => {
      setToken('')
      setJustConnected(null)
      setChecked(null)
      refresh()
    },
  })
  const busy = save.isPending || test.isPending || disconnect.isPending
  const reset = () => {
    save.reset()
    test.reset()
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (valid && !busy) save.mutate()
  }
  const askDisconnect = () => {
    if (window.confirm(t('integrations.telegram.disconnectConfirm'))) disconnect.mutate()
  }
  const name = item.bot_username ? `@${item.bot_username}` : ''
  const testFailed = test.isSuccess && test.data.check && !test.data.check.ok

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={TONE[item.status] ?? 'neutral'}>{t(`integrations.status.${item.status}`)}</Badge>
        <Badge>{t(own ? 'integrations.telegram.badgeOwn' : 'integrations.telegram.badgeShared')}</Badge>
      </div>
      <p className="text-[13px] text-muted">{own ? t('integrations.telegram.ownText') : t('integrations.telegram.sharedText')}</p>

      {own ? (
        <dl className="grid gap-x-4 gap-y-1 text-[13px] sm:grid-cols-[auto_1fr]">
          {name && (
            <>
              <dt className="text-muted">{t('integrations.telegram.botLabel')}</dt>
              <dd>
                <a className="text-primary underline" href={botUrl(name)} target="_blank" rel="noopener noreferrer">{name}</a>
                {item.token_mask && <span className="ml-2 text-xs text-muted">{t('integrations.telegram.mask', { mask: item.token_mask })}</span>}
              </dd>
            </>
          )}
          <dt className="text-muted">{t('integrations.lastCheck')}</dt>
          <dd>{item.last_check_at ? formatDateTime(item.last_check_at) : t('integrations.never')}</dd>
        </dl>
      ) : (
        <p className="text-[13px] font-medium">{t('integrations.telegram.notConnected')}</p>
      )}
      {item.status === 'error' && item.last_error && <Notice tone="danger" title={t('integrations.lastError')}>{item.last_error}</Notice>}

      {!manage && <p className="text-xs text-muted">{t('integrations.telegram.managerOnly')}</p>}

      {manage && (
        <form className="flex flex-col gap-3" onSubmit={submit} autoComplete="off">
          <Input
            label={t('integrations.telegram.token')}
            name="telegram-bot-token"
            type="password"
            value={token}
            onChange={(e) => { setToken(e.target.value); reset() }}
            maxLength={80}
            autoComplete="new-password"
            spellCheck={false}
            placeholder={own ? '••••••••' : t('integrations.telegram.tokenPlaceholder')}
            aria-invalid={showBad || undefined}
            hint={own && !token ? t('integrations.telegram.tokenKept') : t('integrations.telegram.tokenHint')}
          />
          {showBad && <p className="text-xs text-danger" role="alert">{t('integrations.telegram.tokenBad')}</p>}

          <details className="rounded-control border border-line bg-app px-3 py-2 text-[13px]">
            <summary className="cursor-pointer font-medium">{t('integrations.telegram.howTitle')}</summary>
            <ol className="mt-2 list-decimal pl-5 flex flex-col gap-1 text-muted">
              {[1, 2, 3, 4, 5].map((n) => <li key={n}>{t(`integrations.telegram.how${n}`)}</li>)}
            </ol>
          </details>

          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="primary" disabled={busy || !valid}>
              {save.isPending && <Loader2 size={16} className="spin" aria-hidden />} {save.isPending ? t('integrations.telegram.saving') : t('integrations.telegram.save')}
            </Button>
            {own && (
              <Button type="button" disabled={busy} onClick={() => test.mutate()}>
                {test.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <PlugZap size={16} aria-hidden />} {t('integrations.telegram.recheck')}
              </Button>
            )}
            {own && (
              <Button type="button" variant="danger" disabled={busy} onClick={askDisconnect}>
                <Unplug size={16} aria-hidden /> {t('integrations.telegram.disconnect')}
              </Button>
            )}
          </div>

          {justConnected !== null && (
            <Notice tone="success" title={t('integrations.telegram.connectedOk', { name: justConnected ? `@${justConnected}` : '' })}>
              <p>{t('integrations.telegram.connectedNext')}</p>
              {justConnected && (
                <a className="mt-1 inline-flex items-center gap-1 text-primary underline" href={botUrl(justConnected)} target="_blank" rel="noopener noreferrer">
                  {t('integrations.telegram.openBot', { name: `@${justConnected}` })} <ExternalLink size={13} aria-hidden />
                </a>
              )}
            </Notice>
          )}
          {checked !== null && <Notice tone="success">{t('integrations.telegram.checkedOk', { name: checked ? `@${checked}` : '' })}</Notice>}
          {testFailed && <Notice tone="danger">{test.data.check?.message}</Notice>}
          {(save.isError || test.isError || disconnect.isError) && <Notice tone="danger">{errorText(t, save.error ?? test.error ?? disconnect.error)}</Notice>}
        </form>
      )}
    </div>
  )
}
