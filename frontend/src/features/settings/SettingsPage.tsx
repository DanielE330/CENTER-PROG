import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Check, LogOut } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Badge } from '../../components/shared/StatusBadge'
import { Notice } from '../../components/shared/Notice'
import { LanguageSwitch } from '../../components/layout/LanguageSwitch'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { useAuth, useMe } from '../../app/auth'
import { api } from '../../lib/api'
import { useForecastRun, useImports, useRecalc } from '../../hooks/queries'
import { HORIZONS, loadPrefs, savePrefs } from '../../lib/prefs'
import { formatDate, formatDateTime, formatNumber } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { ImportRow } from '../../types'
import { ImportStatement } from './ImportStatement'
import { AccountMethods } from './AccountMethods'
import { ApiTokens } from './ApiTokens'
import { TelegramNotifications } from './TelegramNotifications'
import { TeamPolicies } from './TeamPolicies'
import { LeaveTeam, TeamMembers } from './TeamMembers'
import { canEdit, isAdmin } from '../../lib/roles'

export default function SettingsPage() {
  const { t } = useTranslation()
  const me = useMe()
  const { setMe, logout } = useAuth()
  const imports = useImports()
  const [name, setName] = useState(me.company_name)
  const [horizon, setHorizon] = useState(() => loadPrefs().horizon)
  const [saved, setSaved] = useState(false)
  const forecastRun = useForecastRun()
  const recalc = useRecalc()

  const rename = useMutation({
    mutationFn: (n: string) => api.renameCompany(n),
    onSuccess: (r) => setMe({ ...me, company_name: r.company_name }),
  })

  const submitName = (e: FormEvent) => {
    e.preventDefault()
    rename.mutate(name.trim())
  }

  const savePref = () => {
    savePrefs({ horizon })
    // уже построенный прогноз пересобираем с новым горизонтом, чтобы настройка была видна сразу
    if (forecastRun.data && forecastRun.data.result.horizon !== horizon) recalc.mutate(horizon)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  const columns: Column<ImportRow>[] = [
    { key: 'file', header: t('settings.history.file'), render: (i) => <span className="font-medium break-all">{i.filename}</span> },
    {
      key: 'status', header: t('settings.history.status'),
      render: (i) => i.status === 'confirmed'
        ? <Badge tone="success">{t('settings.history.confirmed')}</Badge>
        : i.status === 'preview' ? <Badge tone="warning">{t('settings.history.preview')}</Badge> : <Badge>{i.status}</Badge>,
    },
    { key: 'period', header: t('settings.history.period'), render: (i) => (i.date_from && i.date_to ? `${formatDate(i.date_from, 'dayMonth')} — ${formatDate(i.date_to)}` : '—') },
    { key: 'rows', header: t('settings.history.rows'), numeric: true, render: (i) => (i.rows_ok == null ? '—' : formatNumber(i.rows_ok)) },
    { key: 'rejected', header: t('settings.history.rejected'), numeric: true, render: (i) => (i.errors_total ? <Badge tone="warning">{i.errors_total}</Badge> : '0') },
    { key: 'at', header: t('settings.history.uploaded'), render: (i) => <span className="whitespace-nowrap">{formatDateTime(i.created_at)}</span> },
  ]

  return (
    <PageContainer>
      <PageHeader
        title={t('settings.title')}
        description={t('settings.description')}
        actions={<Button onClick={() => void logout()}><LogOut size={16} aria-hidden /> {t('auth.logout')}</Button>}
      />

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="flex flex-col gap-5">
          <Card title={t('settings.language.title')} subtitle={t('settings.language.subtitle')}>
            <div className="px-5 pb-5"><LanguageSwitch /></div>
          </Card>

          <Card title={t('settings.org.title')} subtitle={t('settings.org.subtitle')}>
            <form className="px-5 pb-5 flex flex-col gap-4" onSubmit={submitName}>
              <Input label={t('settings.org.name')} value={name} onChange={(e) => { setName(e.target.value); rename.reset() }} required minLength={2} maxLength={200} />
              {me.company_inn && <Input label={t('settings.org.inn')} value={me.company_inn} readOnly className="tnum" />}
              {me.company_ogrn && <Input label={t('settings.org.ogrn')} value={me.company_ogrn} readOnly className="tnum" />}
              <Select label={t('settings.org.currency')} disabled onChange={() => {}} value="RUB" options={[{ value: 'RUB', label: t('settings.org.rub') }]} />
              {rename.isSuccess && <Notice tone="success">{t('settings.org.saved')}</Notice>}
              {rename.isError && <Notice tone="danger">{errorText(t, rename.error)}</Notice>}
              <div>
                <Button type="submit" variant="primary" disabled={rename.isPending || name.trim() === me.company_name || name.trim().length < 2}>
                  {rename.isPending ? t('common.saving') : t('common.save')}
                </Button>
              </div>
            </form>
          </Card>

          <TelegramNotifications />

          {isAdmin(me) && <TeamMembers />}
          <LeaveTeam />
          {isAdmin(me) && <TeamPolicies />}

          <AccountMethods />

          <ApiTokens />

          <Card title={t('settings.prefs.title')} subtitle={t('settings.prefs.subtitle')}>
            <div className="px-5 pb-5 flex flex-col gap-4">
              <Select
                label={t('settings.prefs.horizon')}
                hint={t('settings.prefs.horizonHint')}
                value={horizon}
                onChange={(v) => { setHorizon(Number(v)); setSaved(false) }}
                options={HORIZONS.map((d) => ({ value: d, label: t('common.daysCount', { count: d }) }))}
              />
              <div className="flex items-center gap-3">
                <Button variant="primary" disabled={recalc.isPending} onClick={savePref}>{t('common.save')}</Button>
                {saved && (
                  <span className="inline-flex items-center gap-1 text-sm text-primary" role="status">
                    <Check size={16} aria-hidden /> {t('common.saved')}
                  </span>
                )}
              </div>
            </div>
          </Card>
        </div>

        {canEdit(me) && (
          <Card title={t('import.title')} subtitle={t('import.subtitle')}>
            <div className="px-5 pb-5"><ImportStatement /></div>
          </Card>
        )}
      </div>

      <Card title={t('settings.history.title')}>
        {imports.isPending && <LoadingState height={100} />}
        {imports.isError && <ErrorState message={errorText(t, imports.error)} onRetry={() => void imports.refetch()} />}
        {imports.data && (imports.data.items.length === 0
          ? <p className="px-5 pb-5 text-sm text-muted">{t('settings.history.empty')}</p>
          : <DataTable columns={columns} rows={imports.data.items} getRowKey={(i) => i.import_id} />)}
      </Card>
    </PageContainer>
  )
}
