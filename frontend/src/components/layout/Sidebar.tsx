import { NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PanelLeftClose, PanelLeftOpen, Landmark, X } from 'lucide-react'
import { navItems } from './nav'
import { useMe } from '../../app/auth'
import { atLeast } from '../../lib/roles'
import { cn } from '../../lib/utils'

interface Props {
  collapsed: boolean
  onToggle: () => void
  /** Mobile drawer mode */
  mobile?: boolean
  onNavigate?: () => void
}

export function Sidebar({ collapsed, onToggle, mobile, onNavigate }: Props) {
  const { t } = useTranslation()
  const compact = collapsed && !mobile
  const me = useMe()
  return (
    <aside
      className="sidebar flex h-full flex-col bg-surface border-r border-line"
      style={{ width: compact ? 'var(--sidebar-w-collapsed)' : 'var(--sidebar-w)' }}
      aria-label={t('nav.main')}
    >
      <div className={cn('flex items-center gap-2.5 h-[var(--header-h)] border-b border-line shrink-0', compact ? 'justify-center' : 'px-5')}>
        <span className="grid place-items-center size-9 rounded-[10px] bg-primary text-on-primary shrink-0">
          <Landmark size={18} />
        </span>
        {!compact && (
          <div className="leading-tight min-w-0 flex-1">
            <p className="font-semibold truncate">{t('app.brand')}</p>
            <p className="text-xs text-muted truncate">{t('app.tagline')}</p>
          </div>
        )}
        {mobile && (
          <button onClick={onNavigate} aria-label={t('nav.closeMenu')} className="p-1.5 rounded-md text-muted hover:bg-hover cursor-pointer">
            <X size={18} />
          </button>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto p-3 flex flex-col gap-1">
        {navItems.filter((i) => !i.minRole || atLeast(me, i.minRole)).map(({ to, labelKey, icon: Icon }) => {
          const label = t(labelKey)
          return (
          <NavLink
            key={to}
            to={to}
            onClick={onNavigate}
            title={compact ? label : undefined}
            aria-label={label}
            className={({ isActive }) =>
              cn(
                'pressable flex items-center gap-3 h-10 rounded-control text-sm font-medium',
                compact ? 'justify-center' : 'px-3',
                isActive ? 'bg-primary-soft text-primary' : 'text-ink hover:bg-hover',
              )
            }
          >
            {({ isActive }) => (
              <>
                <Icon size={19} className={cn('shrink-0', isActive ? 'text-primary' : 'text-muted')} />
                {!compact && <span className="truncate">{label}</span>}
              </>
            )}
          </NavLink>
          )
        })}
      </nav>

      {!mobile && (
        <div className="p-3 border-t border-line">
          <button
            onClick={onToggle}
            aria-label={collapsed ? t('nav.expandMenu') : t('nav.collapseMenu')}
            className={cn('flex items-center gap-3 h-10 w-full rounded-control text-sm text-muted hover:bg-hover cursor-pointer', compact ? 'justify-center' : 'px-3')}
          >
            {collapsed ? <PanelLeftOpen size={19} /> : <PanelLeftClose size={19} />}
            {!compact && <span>{t('nav.collapse')}</span>}
          </button>
        </div>
      )}
    </aside>
  )
}
