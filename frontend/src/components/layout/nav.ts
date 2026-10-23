import { Bot, CalendarClock, GitCompareArrows, LayoutDashboard, LineChart, Receipt, Settings, Split } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
}

export const navItems: NavItem[] = [
  { to: '/dashboard', label: 'Обзор', icon: LayoutDashboard },
  { to: '/forecast', label: 'Прогноз', icon: LineChart },
  { to: '/transactions', label: 'Операции', icon: Receipt },
  { to: '/obligations', label: 'Платежи и обязательства', icon: CalendarClock },
  { to: '/scenarios', label: 'Сценарии', icon: Split },
  { to: '/plan-vs-actual', label: 'План и факт', icon: GitCompareArrows },
  { to: '/assistant', label: 'AI-ассистент', icon: Bot },
  { to: '/settings', label: 'Настройки', icon: Settings },
]
