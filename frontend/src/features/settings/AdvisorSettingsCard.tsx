import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Badge } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { usePerms } from '../../app/perms'
import { useMe } from '../../app/auth'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import type { AdvisorSettings } from '../assistant/advisorTypes'
import { Toggle } from './FnsSettingsCard'

/** Что разрешено ИИ-консультанту (Hermes): чтение, сценарии, предложения и запись в план. Права выдают владелец и
 * администраторы; право записи действует только для пользователей с правом изменения данных. */
export function AdvisorSettingsCard() {
  const { t } = useTranslation()
  const me = useMe()
  const { admin } = usePerms()
  const qc = useQueryClient()
  const key = ['co', me.company_id, 'advisor-settings']
  const q = useQuery({ queryKey: key, queryFn: api.advisorSettings })
  const [draft, setDraft] = useState<AdvisorSettings | null>(null)
  useEffect(() => {
    if (q.data) setDraft(q.data.settings)
  }, [q.data])
  const loaded = !!q.data
  useEffect(() => {
    if (loaded && window.location.hash === '#advisor') document.getElementById('advisor')?.scrollIntoView({ block: 'start' })
  }, [loaded])
  const save = useMutation({
    mutationFn: api.saveAdvisorSettings,
    onSuccess: (v) => {
      qc.setQueryData(key, v)
      void qc.invalidateQueries({ queryKey: ['co', me.company_id, 'advisor-prompts'] })
    },
  })
  const set = (patch: (s: AdvisorSettings) => AdvisorSettings) => {
    save.reset()
    setDraft((d) => (d ? patch(structuredClone(d)) : d))
  }
  const off = !admin || save.isPending
  const engine = q.data?.engine
  const hermes = engine?.hermes

  return (
    <Card
      title={t('settings.advisor.title')}
      subtitle={t('settings.advisor.subtitle')}
      action={
        engine && (
          <Badge tone={engine.engine !== 'hermes' ? 'neutral' : !hermes?.available || hermes.last?.ok === false ? 'warning' : hermes.last?.ok ? 'success' : 'neutral'}>
            {engine.engine !== 'hermes'
              ? t('settings.advisor.gateway')
              : !hermes?.available
                ? t('settings.advisor.hermesDown')
                : hermes.last?.ok === false
                  ? t('settings.advisor.hermesFailed')
                  : hermes.last?.ok
                    ? t('settings.advisor.hermesOn')
                    : t('settings.advisor.hermesStarted')}
          </Badge>
        )
      }
    >
      <div id="advisor" className="px-5 pb-5 flex flex-col gap-4 scroll-mt-20">
        {q.isPending && <LoadingState height={120} />}
        {q.isError && <ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} />}
        {draft && q.data && (
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              save.mutate(draft)
            }}
          >
            <Toggle checked={draft.enabled} disabled={off} onChange={(v) => set((d) => ({ ...d, enabled: v }))} label={t('settings.advisor.enabled')} hint={t('settings.advisor.enabledHint')} />
            <fieldset className="flex flex-col gap-2" disabled={off || !draft.enabled}>
              <legend className="text-sm font-semibold mb-1">{t('settings.advisor.permissions')}</legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {q.data.catalog.map((p) => (
                  <Toggle
                    key={p.code}
                    checked={draft.permissions.includes(p.code)}
                    disabled={off || !draft.enabled}
                    onChange={(v) => set((d) => ({ ...d, permissions: v ? [...d.permissions, p.code] : d.permissions.filter((x) => x !== p.code) }))}
                    label={t(`settings.advisor.perm.${p.code}`)}
                    hint={p.needs_edit ? t('settings.advisor.writeHint') : t(`settings.advisor.permHint.${p.code}`)}
                  />
                ))}
              </div>
            </fieldset>
            <Toggle checked={draft.show_prompts} disabled={off || !draft.enabled} onChange={(v) => set((d) => ({ ...d, show_prompts: v }))} label={t('settings.advisor.showPrompts')} hint={t('settings.advisor.showPromptsHint')} />
            {admin && (
              <div className="flex flex-wrap gap-2">
                <Button type="submit" variant="primary" disabled={save.isPending}>{t('common.save')}</Button>
                <Button type="button" disabled={save.isPending} onClick={() => set(() => structuredClone(q.data.defaults))}>
                  {t('settings.advisor.reset')}
                </Button>
              </div>
            )}
          </form>
        )}
        {!admin && <p className="text-xs text-muted">{t('perms.adminOnly')}</p>}
        {save.isSuccess && <p className="text-xs text-primary" role="status">{t('settings.advisor.saved')}</p>}
        {save.isError && <Notice tone="danger">{errorText(t, save.error)}</Notice>}
      </div>
    </Card>
  )
}
