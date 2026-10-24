import { useTranslation } from 'react-i18next'
import { Building2, CalendarPlus, ShieldBan, ShieldCheck, Users } from 'lucide-react'
import { StatCard } from '../../components/shared/StatCard'
import { ErrorState } from '../../components/shared/States'
import { errorText } from '../../i18n/helpers'
import { formatNumber } from '../../lib/formatters'
import { useOperatorStats } from './operatorApi'

/** Short platform statistics: counts only. */
export function OperatorStats() {
  const { t } = useTranslation()
  const q = useOperatorStats()
  if (q.isError) return <ErrorState message={errorText(t, q.error)} onRetry={() => q.refetch()} />
  const s = q.data
  const v = (n: number | undefined) => (n === undefined ? '…' : formatNumber(n))
  return (
    <section aria-label={t('operator.stats.title')} aria-busy={q.isLoading} className="grid gap-4 grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      <StatCard index={0} icon={<Building2 size={16} aria-hidden />} label={t('operator.stats.total')} value={v(s?.total)} />
      <StatCard index={1} icon={<ShieldCheck size={16} aria-hidden />} label={t('operator.stats.active')} value={v(s?.active)} />
      <StatCard index={2} icon={<ShieldBan size={16} aria-hidden />} label={t('operator.stats.banned')} value={v(s?.banned)} tone={s && s.banned > 0 ? 'warning' : 'default'} />
      <StatCard index={3} icon={<CalendarPlus size={16} aria-hidden />} label={t('operator.stats.new7')} value={v(s?.new_7d)} />
      <StatCard index={4} icon={<CalendarPlus size={16} aria-hidden />} label={t('operator.stats.new30')} value={v(s?.new_30d)} />
      <StatCard index={5} icon={<Users size={16} aria-hidden />} label={t('operator.stats.users')} value={v(s?.users_total)} />
    </section>
  )
}
