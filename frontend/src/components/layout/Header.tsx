import { useEffect, useRef, useState } from 'react'
import { Bell, Building2, ChevronDown, LogOut, Menu, Settings } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ThemeToggle } from './ThemeToggle'
import { useForecast } from '../../hooks/queries'
import { useAuth, useMe } from '../../app/auth'
import { formatShortDate } from '../../lib/formatters'
import { Badge } from '../shared/StatusBadge'

function useOutside(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && close()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close()
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, close])
  return ref
}

function Notifications() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useOutside(open, () => setOpen(false))
  const { view } = useForecast()
  const warnings = view?.r.risk.warnings ?? []
  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label={t('header.notificationsCount', { count: warnings.length })}
        aria-expanded={open}
        aria-haspopup="true"
        className="relative grid place-items-center size-10 rounded-control hover:bg-hover text-muted cursor-pointer"
      >
        <Bell size={19} aria-hidden />
        {warnings.length > 0 && <span className="absolute top-2 right-2.5 size-2 rounded-full bg-danger" aria-hidden />}
      </button>
      {open && (
        <div className="card pop-in absolute right-0 top-12 w-80 max-w-[calc(100vw-2rem)] p-3 z-40" style={{ boxShadow: 'var(--shadow-pop)' }}>
          <p className="text-sm font-semibold px-2 pb-2">{t('header.notifications')}</p>
          {warnings.length === 0 ? (
            <p className="text-sm text-muted p-2">{t('header.noNotifications')}</p>
          ) : (
            <ul className="flex flex-col max-h-80 overflow-y-auto">
              {warnings.map((w) => (
                <li key={`${w.code}-${w.date ?? ''}`}>
                  <Link to="/forecast" onClick={() => setOpen(false)} className="block rounded-control p-2 hover:bg-hover">
                    <p className="flex items-center gap-2">
                      <Badge tone={w.severity === 'high' ? 'danger' : 'warning'}>{t(`risk.severity.${w.severity}`)}</Badge>
                      {w.date && <span className="text-xs text-muted">{formatShortDate(w.date)}</span>}
                    </p>
                    <p className="text-sm mt-1">{w.message}</p>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

function Profile() {
  const { t } = useTranslation()
  const me = useMe()
  const { logout } = useAuth()
  const [open, setOpen] = useState(false)
  const ref = useOutside(open, () => setOpen(false))
  const initials = me.email.slice(0, 2).toUpperCase()
  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="true"
        aria-label={t('header.profile', { email: me.email })}
        className="flex items-center gap-2.5 pl-2 sm:pl-3 sm:border-l border-line cursor-pointer rounded-control"
      >
        <span className="grid place-items-center size-9 rounded-full bg-primary-soft text-primary text-sm font-semibold" aria-hidden>{initials}</span>
        <span className="hidden sm:block leading-tight text-left max-w-[200px]">
          <span className="block text-sm font-medium truncate">{me.email}</span>
          <span className="block text-xs text-muted">{t('header.account')}</span>
        </span>
        <ChevronDown size={16} className="text-muted hidden sm:block" aria-hidden />
      </button>
      {open && (
        <div className="card pop-in absolute right-0 top-12 w-56 p-1.5 z-40" style={{ boxShadow: 'var(--shadow-pop)' }}>
          <Link to="/settings" onClick={() => setOpen(false)} className="flex items-center gap-2.5 h-9 px-2.5 rounded-control text-sm hover:bg-hover">
            <Settings size={16} className="text-muted" aria-hidden /> {t('nav.settings')}
          </Link>
          <button
            onClick={() => void logout()}
            className="flex items-center gap-2.5 h-9 w-full px-2.5 rounded-control text-sm hover:bg-hover cursor-pointer"
          >
            <LogOut size={16} className="text-muted" aria-hidden /> {t('auth.logout')}
          </button>
        </div>
      )}
    </div>
  )
}

export function Header({ onMenu }: { onMenu: () => void }) {
  const { t } = useTranslation()
  const me = useMe()
  return (
    <header className="h-[var(--header-h)] shrink-0 bg-surface border-b border-line flex items-center gap-3 px-4 sm:px-6 lg:px-8">
      <button onClick={onMenu} aria-label={t('nav.openMenu')} className="md:hidden p-2 -ml-2 rounded-md hover:bg-hover cursor-pointer">
        <Menu size={20} />
      </button>

      <div className="flex items-center gap-2 min-w-0">
        <Building2 size={18} className="text-muted shrink-0" aria-hidden />
        <div className="leading-tight min-w-0">
          <p className="text-sm font-semibold truncate">{me.company_name}</p>
          <p className="text-xs text-muted hidden sm:block">{t('app.workspace')}</p>
        </div>
      </div>

      <div className="ml-auto flex items-center gap-1 sm:gap-2">
        <ThemeToggle />
        <Notifications />
        <Profile />
      </div>
    </header>
  )
}
