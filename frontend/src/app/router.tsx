import { Suspense, lazy } from 'react'
import { Navigate, createBrowserRouter } from 'react-router-dom'
import { AppLayout } from '../components/layout/AppLayout'
import { LoadingState } from '../components/shared/States'

const DashboardPage = lazy(() => import('../features/dashboard/DashboardPage'))
const ForecastPage = lazy(() => import('../features/forecast/ForecastPage'))
const TransactionsPage = lazy(() => import('../features/transactions/TransactionsPage'))
const ObligationsPage = lazy(() => import('../features/obligations/ObligationsPage'))
const ScenariosPage = lazy(() => import('../features/scenarios/ScenariosPage'))
const PlanVsActualPage = lazy(() => import('../features/plan-vs-actual/PlanVsActualPage'))
const AssistantPage = lazy(() => import('../features/assistant/AssistantPage'))
const SettingsPage = lazy(() => import('../features/settings/SettingsPage'))

const page = (el: React.ReactNode) => <Suspense fallback={<LoadingState height={300} />}>{el}</Suspense>

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: '/dashboard', element: page(<DashboardPage />) },
      { path: '/forecast', element: page(<ForecastPage />) },
      { path: '/transactions', element: page(<TransactionsPage />) },
      { path: '/obligations', element: page(<ObligationsPage />) },
      { path: '/scenarios', element: page(<ScenariosPage />) },
      { path: '/plan-vs-actual', element: page(<PlanVsActualPage />) },
      { path: '/assistant', element: page(<AssistantPage />) },
      { path: '/settings', element: page(<SettingsPage />) },
      { path: '*', element: <Navigate to="/dashboard" replace /> },
    ],
  },
])
