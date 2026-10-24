import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowLeft, ShieldBan, ShieldCheck } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { DataTable } from '../../components/shared/DataTable'
import type { Column } from '../../components/shared/DataTable'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { errorText } from '../../i18n/helpers'
import { formatNumber } from '../../lib/formatters'
import { BanDialog } from './BanDialog'
import { BanBadge, RiskCell } from './CompanyStatus'
import { safeDate, safeDateTime } from './format'
import { useOperatorCompany } from './operatorApi'
import type { OperatorMember } from './operatorApi'

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 break-words">{children}</dd>
    </div>
  )
}

/** Company card: registration data, members with roles, dates, forecast risk, block / unblock. */
export function CompanyDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const { t } = useTranslation()
  const q = useOperatorCompany(id)
  const [dialog, setDialog] = useState<'ban' | 'unban' | null>(null)
  const c = q.data

  const columns: Column<OperatorMember>[] = [
    { key: 'email', header: t('auth.email'), render: (m) => <span className="break-all">{m.email}</span> },
    { key: 'role', header: t('operator.detail.role'), render: (m) => t(`role.${m.role}`, { defaultValue: m.role }) },
    { key: 'since', header: t('operator.detail.memberSince'), render: (m) => <span className="nowrap">{safeDate(m.created_at)}</span> },
  ]

  return (
    <div className="flex flex-col gap-5">
      <div>
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft size={16} aria-hidden />
          {t('operator.detail.back')}
        </Button>
      </div>

      {q.isError ? (
        <ErrorState message={errorText(t, q.error)} onRetry={() => q.refetch()} />
      ) : !c ? (
        <LoadingState height={240} />
      ) : (
        <>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <h2 className="text-2xl font-semibold break-words">{c.name || t('operator.noName')}</h2>
              <p className="mt-1.5">
                <BanBadge banned={c.banned} />
              </p>
            </div>
            {c.banned ? (
              <Button variant="primary" onClick={() => setDialog('unban')}>
                <ShieldCheck size={16} aria-hidden />
                {t('operator.unban.action')}
              </Button>
            ) : (
              <Button variant="danger" onClick={() => setDialog('ban')}>
                <ShieldBan size={16} aria-hidden />
                {t('operator.ban.action')}
              </Button>
            )}
          </div>

          {c.banned && (
            <Notice tone="danger" title={t('operator.detail.bannedAt', { date: safeDateTime(c.banned_at) })}>
              <span className="break-words">{t('operator.detail.reason', { reason: c.ban_reason ?? '—' })}</span>
            </Notice>
          )}

          <Card title={t('operator.detail.about')}>
            <dl className="grid gap-4 px-5 pb-5 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 text-sm">
              <Fact label={t('operator.detail.director')}>{c.director_email || '—'}</Fact>
              <Fact label={t('operator.col.inn')}>{c.inn || '—'}</Fact>
              <Fact label={t('operator.detail.ogrn')}>{c.ogrn || '—'}</Fact>
              <Fact label={t('operator.col.registered')}>{safeDateTime(c.created_at)}</Fact>
              <Fact label={t('operator.col.lastImport')}>{safeDateTime(c.last_import_at)}</Fact>
              <Fact label={t('operator.detail.lastOperation')}>{safeDate(c.last_transaction_date)}</Fact>
              <Fact label={t('operator.col.risk')}>
                <RiskCell company={c} />
              </Fact>
              <Fact label={t('operator.col.members')}>{c.members_count == null ? '—' : formatNumber(c.members_count)}</Fact>
              <Fact label={t('operator.detail.loaded')}>{c.transactions_count == null ? '—' : formatNumber(c.transactions_count)}</Fact>
              <Fact label={t('operator.detail.planned')}>{c.events_count == null ? '—' : formatNumber(c.events_count)}</Fact>
            </dl>
          </Card>

          <Card title={t('operator.detail.members')}>
            {c.members && c.members.length > 0 ? (
              <DataTable columns={columns} rows={c.members} getRowKey={(m) => m.email} />
            ) : (
              <p className="px-5 pb-5 text-sm text-muted">{t('operator.detail.noMembers')}</p>
            )}
          </Card>

          <p className="text-[13px] text-muted">{t('operator.privacy')}</p>
          {dialog && <BanDialog company={c} mode={dialog} onClose={() => setDialog(null)} />}
        </>
      )}
    </div>
  )
}
