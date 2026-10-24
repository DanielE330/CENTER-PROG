import { useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { useTranslation } from 'react-i18next'
import { Header } from './Header'
import { useMediaQuery } from '../../hooks/useMediaQuery'

export function AppLayout() {
  const tablet = useMediaQuery('(max-width: 1279px)')
  const mobile = useMediaQuery('(max-width: 767px)')
  const [userCollapsed, setUserCollapsed] = useState<boolean | null>(null)
  const [drawer, setDrawer] = useState(false)
  const { pathname } = useLocation()
  const { t } = useTranslation()

  const collapsed = userCollapsed ?? tablet

  return (
    <div className="flex h-full">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[80] focus:rounded-control focus:bg-primary focus:px-4 focus:py-2 focus:text-on-primary"
      >
        {t('nav.skip')}
      </a>
      {!mobile && (
        <div className="shrink-0 h-full">
          <Sidebar collapsed={collapsed} onToggle={() => setUserCollapsed(!collapsed)} />
        </div>
      )}
      {mobile && drawer && (
        <div className="fixed inset-0 z-50 flex">
          <Sidebar collapsed={false} mobile onToggle={() => {}} onNavigate={() => setDrawer(false)} />
          <div className="flex-1 bg-overlay" onClick={() => setDrawer(false)} />
        </div>
      )}

      <div className="flex-1 min-w-0 flex flex-col h-full">
        <Header onMenu={() => setDrawer(true)} />
        <main id="main" tabIndex={-1} className="flex-1 overflow-y-auto overflow-x-hidden outline-none">
          {/* Re-keyed per route so the short fade replays on navigation; app state outside the page is untouched. */}
          <div key={pathname} className="page-enter">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
