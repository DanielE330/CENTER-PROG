import { Bot, CalendarClock, History, GitCompareArrows, LayoutDashboard, LineChart, Receipt, Settings, Split, Target } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type { Role } from '../../types'

export interface NavItem {
  to: string
  labelKey: string
  icon: LucideIcon
  /** Hidden for lower roles (the API enforces the same rule). */
  minRole?: Role
}

export const navItems: NavItem[] = [
  { to: '/dashboard', labelKey: 'nav.dashboard', icon: LayoutDashboard },
  { to: '/forecast', labelKey: 'nav.forecast', icon: LineChart },
  { to: '/transactions', labelKey: 'nav.transactions', icon: Receipt },
  { to: '/obligations', labelKey: 'nav.obligations', icon: CalendarClock },
  { to: '/scenarios', labelKey: 'nav.scenarios', icon: Split },
  { to: '/plan-vs-actual', labelKey: 'nav.planVsActual', icon: GitCompareArrows },
  { to: '/goals', labelKey: 'nav.goals', icon: Target },
  { to: '/assistant', labelKey: 'nav.assistant', icon: Bot },
  { to: '/activity', labelKey: 'nav.activity', icon: History, minRole: 'admin' },
  { to: '/settings', labelKey: 'nav.settings', icon: Settings },
]
