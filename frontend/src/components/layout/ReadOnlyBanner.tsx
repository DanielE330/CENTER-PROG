import { useTranslation } from 'react-i18next'
import { Eye } from 'lucide-react'
import { usePerms } from '../../app/perms'

/** Shown to a read-only employee, so disabled buttons are not a mystery. */
export function ReadOnlyBanner() {
  const { t } = useTranslation()
  const { edit } = usePerms()
  if (edit) return null
  return (
    <div role="status" className="flex items-center gap-2 border-b border-line bg-warning-soft px-4 py-2 text-[13px] sm:px-6 lg:px-8">
      <Eye size={15} className="shrink-0 text-warning" aria-hidden /> {t('perms.readOnlyBanner')}
    </div>
  )
}
