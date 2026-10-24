import type { ReactNode } from 'react'
import { AlertCircle, Inbox, Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '../ui/Button'

export function LoadingState({ label, height = 200 }: { label?: string; height?: number }) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col items-center justify-center gap-3 text-muted py-8" style={{ minHeight: height }} role="status" aria-live="polite">
      <Loader2 size={22} className="spin" />
      <span className="text-sm">{label ?? t('common.loading')}</span>
      <div className="w-full max-w-md flex flex-col gap-2 px-6">
        <div className="skeleton h-3 w-full" />
        <div className="skeleton h-3 w-4/5" />
      </div>
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message?: string; onRetry?: () => void }) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col items-center justify-center gap-3 p-8 text-center" role="alert">
      <span className="grid place-items-center size-11 rounded-full bg-danger-soft text-danger">
        <AlertCircle size={22} />
      </span>
      <div>
        <p className="font-semibold">{t('errors.loadTitle')}</p>
        <p className="text-sm text-muted mt-1 max-w-sm">{message ?? t('errors.loadDefault')}</p>
      </div>
      {onRetry && <Button onClick={onRetry}>{t('common.retry')}</Button>}
    </div>
  )
}

interface EmptyProps {
  title: string
  description?: string
  action?: ReactNode
  icon?: ReactNode
}

export function EmptyState({ title, description, action, icon }: EmptyProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 p-8 text-center">
      <span className="grid place-items-center size-11 rounded-full bg-primary-soft text-primary">{icon ?? <Inbox size={22} />}</span>
      <div>
        <p className="font-semibold">{title}</p>
        {description && <p className="text-sm text-muted mt-1 max-w-sm">{description}</p>}
      </div>
      {action}
    </div>
  )
}
