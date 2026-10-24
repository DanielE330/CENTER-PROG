import { Bot, CalendarClock, GitCompareArrows, LayoutDashboard, LineChart, Receipt, Settings, Split } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export interface NavItem {
  to: string
  labelKey: string
  icon: LucideIcon
}

export const navItems: NavItem[] = [
  { to: '/dashboard', labelKey: 'nav.dashboard', icon: LayoutDashboard },
  { to: '/forecast', labelKey: 'nav.forecast', icon: LineChart },
  { to: '/transactions', labelKey: 'nav.transactions', icon: Receipt },
  { to: '/obligations', labelKey: 'nav.obligations', icon: CalendarClock },
  { to: '/scenarios', labelKey: 'nav.scenarios', icon: Split },
  { to: '/plan-vs-actual', labelKey: 'nav.planVsActual', icon: GitCompareArrows },
  { to: '/assistant', labelKey: 'nav.assistant', icon: Bot },
  { to: '/settings', labelKey: 'nav.settings', icon: Settings },
]
