import type { ReactNode } from 'react'

export function PageContainer({ children }: { children: ReactNode }) {
  return <div className="mx-auto w-full max-w-[1400px] p-4 sm:p-6 lg:p-8 flex flex-col gap-5">{children}</div>
}

interface HeaderProps {
  title: string
  description?: ReactNode
  actions?: ReactNode
}

export function PageHeader({ title, description, actions }: HeaderProps) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <h1 className="text-2xl sm:text-[28px] font-semibold leading-tight">{title}</h1>
        {description && <div className="mt-1 text-muted text-[15px]">{description}</div>}
      </div>
      {actions && <div className="flex flex-col gap-2 sm:flex-row sm:items-center shrink-0">{actions}</div>}
    </div>
  )
}

interface CardProps {
  title?: string
  subtitle?: ReactNode
  action?: ReactNode
  children: ReactNode
  className?: string
}

export function Card({ title, subtitle, action, children, className = '' }: CardProps) {
  return (
    <section className={`card enter ${className}`}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-3 px-5 pt-4 pb-3">
          <div className="min-w-0">
            {title && <h2 className="text-base font-semibold">{title}</h2>}
            {subtitle && <p className="text-[13px] text-muted mt-0.5">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  )
}
