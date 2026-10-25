import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Loader2, Send, Trash2, Users, User } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { useTelegramItem, TelegramBotForm } from './TelegramBotForm'
import { usePerms } from '../../app/perms'
import { RubInputNote } from '../../components/shared/RubInputNote'
import { Button } from '../../components/ui/Button'
import { Checkbox } from '../../components/ui/Checkbox'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { useMe } from '../../app/auth'
import { isAdmin } from '../../lib/roles'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { TgLink, TgPrefs } from '../../types'

const KEY = ['notifications'] as const
const ZONES = ['Europe/Kaliningrad', 'Europe/Moscow', 'Europe/Samara', 'Asia/Yekaterinburg', 'Asia/Omsk', 'Asia/Krasnoyarsk', 'Asia/Irkutsk', 'Asia/Yakutsk', 'Asia/Vladivostok', 'Asia/Magadan', 'Asia/Kamchatka']
const GAP_DAYS = [3, 5, 7, 10, 14]
const THRESHOLDS = [0.1, 0.2, 0.3, 0.5, 0.7]
const MONTH_DAYS = [1, 2, 3, 5, 10, 15, 20, 25]
const iso = (sec: number) => new Date(sec * 1000).toISOString()

/** Bot connection (deep link), connected chats and the schedule of alerts and reports. Goals are rated in the bot. */
export function TelegramNotifications() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const admin = isAdmin(useMe())
  const { manage } = usePerms()
  const tg = useTelegramItem()
  const [link, setLink] = useState<TgLink | null>(null)
  const status = useQuery({ queryKey: KEY, queryFn: api.tgStatus, refetchInterval: link ? 4000 : false })
  const [draft, setDraft] = useState<TgPrefs | null>(null)
  const [saved, setSaved] = useState(false)
  const [delivered, setDelivered] = useState<number | null>(null)
  const days = t('telegram.days', { returnObjects: true }) as unknown as string[]

  useEffect(() => {
    if (status.data && !draft) setDraft(status.data.prefs)
  }, [status.data, draft])
  // a chat has appeared: the link has done its job
  const chatCount = status.data?.chats.length ?? 0
  useEffect(() => setLink(null), [chatCount])

  const makeLink = useMutation({ mutationFn: api.tgLink, onSuccess: setLink })
  const save = useMutation({
    mutationFn: (p: TgPrefs) => api.tgSavePrefs(p),
    onSuccess: (p) => {
      setDraft(p)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
      void qc.invalidateQueries({ queryKey: KEY })
    },
  })
  const unlink = useMutation({ mutationFn: (id: number) => api.tgUnlink(id), onSuccess: () => void qc.invalidateQueries({ queryKey: KEY }) })
  const test = useMutation({ mutationFn: api.tgTest, onSuccess: (r) => setDelivered(r.delivered) })

  const set = <K extends keyof TgPrefs>(k: K, v: TgPrefs[K]) => {
    setDraft((d) => (d ? { ...d, [k]: v } : d))
    setSaved(false)
  }
  const flag = (k: 'risk_enabled' | 'week_enabled' | 'month_enabled' | 'gap_enabled' | 'missed_enabled') => (
    <Checkbox label={t(`telegram.${k.replace('_enabled', '')}`)} checked={!!draft?.[k]} onChange={(e) => set(k, e.target.checked ? 1 : 0)} />
  )
  const dayOptions = days.map((d, i) => ({ value: i, label: d }))
  const zones = draft && !ZONES.includes(draft.tz) ? [draft.tz, ...ZONES] : ZONES

  return (
    <div id="schedule" className="scroll-mt-6">
    <Card title={t('telegram.title')} subtitle={t('telegram.subtitle')}>
      <div className="px-5 pb-5 flex flex-col gap-4">
        {status.isPending && <LoadingState height={80} />}
        {status.isError && <ErrorState message={errorText(t, status.error)} onRetry={() => void status.refetch()} />}
        {status.data && !status.data.configured && (
          manage && tg.item ? (
            <div className="flex flex-col gap-3">
              <Notice tone="warning">{t('telegram.notConfigured')}</Notice>
              <TelegramBotForm item={tg.item} />
            </div>
          ) : (
            <Notice tone="warning">{t('telegram.notConfigured')}</Notice>
          )
        )}
        {status.data?.configured && (
          <p className="text-xs text-muted">
            {status.data.bot_kind === 'own' ? t('telegram.botOwn', { name: `@${status.data.bot_username}` }) : t('telegram.botShared', { name: status.data.bot_username ? `@${status.data.bot_username}` : '—' })}
            {' '}<a href="#integrations-telegram" className="text-primary underline">{t('integrations.telegram.title')}</a>
          </p>
        )}

        {status.data?.configured && (
          <>
            <div>
              <p className="text-[13px] font-medium mb-2">{t('telegram.chats')}</p>
              {status.data.chats.length === 0 ? (
                <p className="text-sm text-muted">{t('telegram.noChats')}</p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {status.data.chats.map((c) => (
                    <li key={c.chat_id} className="rounded-control border border-line px-3.5 py-2.5 flex items-center justify-between gap-3">
                      <div className="flex items-center gap-2.5 min-w-0">
                        {c.kind === 'private' ? <User size={16} className="text-muted shrink-0" aria-hidden /> : <Users size={16} className="text-muted shrink-0" aria-hidden />}
                        <div className="min-w-0">
                          <p className="text-sm font-medium truncate">{c.title || t(`telegram.${c.kind}`)} <span className="text-xs text-muted font-normal">· {t(`telegram.${c.kind}`)}</span></p>
                          {status.data?.bot_kind && status.data.bot_kind !== 'none' && c.via && c.via !== (status.data.bot_kind === 'own' ? 'own' : 'shared') && (
                            <p className="text-xs text-warning">{t('telegram.relink')}</p>
                          )}
                          <p className="text-xs text-muted truncate">{t('telegram.linkedAt', { value: formatDateTime(iso(c.linked_at)) })}</p>
                        </div>
                      </div>
                      {admin && (
                        <Button size="sm" disabled={unlink.isPending} onClick={() => unlink.mutate(c.chat_id)}>
                          <Trash2 size={14} aria-hidden /> {t('telegram.disconnect')}
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {link ? (
              <Notice tone="info" title={t('telegram.connect')}>
                <p className="mb-2">{t('telegram.connectHint')}</p>
                <div className="flex flex-wrap gap-2">
                  <a className="text-primary underline" href={link.link} target="_blank" rel="noopener noreferrer">{t('telegram.openPrivate')}</a>
                  <span aria-hidden>·</span>
                  <a className="text-primary underline" href={link.group_link} target="_blank" rel="noopener noreferrer">{t('telegram.openGroup')}</a>
                </div>
              </Notice>
            ) : (
              <div className="flex flex-wrap gap-3">
                <Button variant="primary" disabled={makeLink.isPending} onClick={() => makeLink.mutate()}>
                  {makeLink.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <Send size={16} aria-hidden />} {t('telegram.connect')}
                </Button>
                {admin && status.data.chats.length > 0 && (
                  <Button disabled={test.isPending} onClick={() => test.mutate()}>{t('telegram.test')}</Button>
                )}
              </div>
            )}
            {delivered != null && <Notice tone="success">{t('telegram.testSent', { count: delivered })}</Notice>}
          </>
        )}

        {/* расписание и пороги общие для Telegram и почты: показываются и без бота */}
        {admin && draft && (
          <div className="flex flex-col gap-4 border-t border-line pt-4">
            <p className="text-[13px] font-medium">{t('telegram.schedule')}</p>
            <Select label={t('telegram.tz')} value={draft.tz} onChange={(v) => set('tz', v)} options={zones.map((z) => ({ value: z, label: z }))} />

            {(['risk', 'week'] as const).map((k) => (
              <div key={k} className="flex flex-col gap-2">
                {flag(`${k}_enabled`)}
                <div className="grid grid-cols-2 gap-3">
                  <Select aria-label={t('telegram.day')} value={draft[`${k}_dow`]} onChange={(v) => set(`${k}_dow`, Number(v))} options={dayOptions} />
                  <Input aria-label={t('telegram.time')} type="time" value={draft[`${k}_time`]} onChange={(e) => set(`${k}_time`, e.target.value)} />
                </div>
              </div>
            ))}

            <div className="flex flex-col gap-2">
              {flag('month_enabled')}
              <div className="grid grid-cols-2 gap-3">
                <Select aria-label={t('telegram.monthDay')} value={draft.month_day} onChange={(v) => set('month_day', Number(v))}
                  options={(MONTH_DAYS.includes(draft.month_day) ? MONTH_DAYS : [draft.month_day, ...MONTH_DAYS]).map((d) => ({ value: d, label: `${d}` }))} />
                <Input aria-label={t('telegram.time')} type="time" value={draft.month_time} onChange={(e) => set('month_time', e.target.value)} />
              </div>
            </div>

            <div className="flex flex-col gap-2">
              {flag('gap_enabled')}
              <div className="grid grid-cols-2 gap-3">
                <Select label={t('telegram.gapDays')} value={draft.gap_days} onChange={(v) => set('gap_days', Number(v))}
                  options={(GAP_DAYS.includes(draft.gap_days) ? GAP_DAYS : [draft.gap_days, ...GAP_DAYS]).map((d) => ({ value: d, label: t('common.daysCount', { count: d }) }))} />
                <Select label={t('telegram.gapThreshold')} value={draft.gap_threshold} onChange={(v) => set('gap_threshold', Number(v))}
                  options={(THRESHOLDS.includes(draft.gap_threshold) ? THRESHOLDS : [draft.gap_threshold, ...THRESHOLDS]).map((v) => ({ value: v, label: `${Math.round(v * 100)}%` }))} />
              </div>
              <p className="text-xs text-muted">{t('telegram.gapHint')}</p>
            </div>

            <div className="flex flex-col gap-2">
              {flag('missed_enabled')}
              <Input label={t('telegram.missedMin')} hint={<RubInputNote value={Math.round(draft.missed_min_kop / 100)} />} type="number" min={0} step={1000} inputMode="numeric"
                value={Math.round(draft.missed_min_kop / 100)}
                onChange={(e) => set('missed_min_kop', Math.max(0, Math.round(Number(e.target.value) || 0)) * 100)} />
              <p className="text-xs text-muted">{t('telegram.missedHint')}</p>
            </div>

            <div className="flex flex-col gap-1">
              <Checkbox label={t('telegram.aiConsent')} checked={!!draft.ai_consent} disabled={!status.data?.advisor} onChange={(e) => set('ai_consent', e.target.checked ? 1 : 0)} />
              <p className="text-xs text-muted">{status.data?.advisor ? t('telegram.aiConsentHint') : t('telegram.advisorOff')}</p>
            </div>

            {(save.isError || unlink.isError || test.isError || makeLink.isError) && (
              <Notice tone="danger">{errorText(t, save.error ?? unlink.error ?? test.error ?? makeLink.error)}</Notice>
            )}
            <div className="flex items-center gap-3">
              <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate(draft)}>{save.isPending ? t('common.saving') : t('telegram.save')}</Button>
              {saved && <span className="text-sm text-primary" role="status">{t('telegram.saved')}</span>}
            </div>
          </div>
        )}
      </div>
    </Card>
    </div>
  )
}
