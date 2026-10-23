import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { Header } from './Header'
import { useMediaQuery } from '../../hooks/useMediaQuery'
import { useDemoState } from '../../lib/demoState'

const demoLabel = { normal: '', loading: 'долгая загрузка', error: 'ошибка', empty: 'нет данных', incomplete: 'неполные данные' } as const

export function AppLayout() {
  const tablet = useMediaQuery('(max-width: 1279px)')
  const mobile = useMediaQuery('(max-width: 767px)')
  const [userCollapsed, setUserCollapsed] = useState<boolean | null>(null)
  const [drawer, setDrawer] = useState(false)
  const demo = useDemoState()

  const collapsed = userCollapsed ?? tablet

  return (
    <div className="flex h-full">
      {!mobile && (
        <div className="shrink-0 h-full">
          <Sidebar collapsed={collapsed} onToggle={() => setUserCollapsed(!collapsed)} />
        </div>
      )}
      {mobile && drawer && (
        <div className="fixed inset-0 z-50 flex">
          <Sidebar collapsed={false} mobile onToggle={() => {}} onNavigate={() => setDrawer(false)} />
          <div className="flex-1 bg-ink/40" onClick={() => setDrawer(false)} />
        </div>
      )}

      <div className="flex-1 min-w-0 flex flex-col h-full">
        <Header onMenu={() => setDrawer(true)} />
        {demo !== 'normal' && (
          <div className="bg-warning-soft text-warning text-[13px] px-4 sm:px-8 py-1.5 font-medium">
            Включён демонстрационный режим состояния данных: «{demoLabel[demo]}». Изменить — в «Настройках».
          </div>
        )}
        <main className="flex-1 overflow-y-auto overflow-x-hidden">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
