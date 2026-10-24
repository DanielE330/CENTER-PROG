import { lazy, Suspense } from 'react'
import { RouterProvider } from 'react-router-dom'
import { Providers } from './app/providers'
import { useAuth } from './app/auth'
import { router } from './app/router'
import { LoadingState } from './components/shared/States'
import { Notice } from './components/shared/Notice'
import { InviteGate } from './features/team/InviteGate'
import { captureInvite, getInvite } from './lib/invite'
import { useTranslation } from 'react-i18next'

captureInvite()

const AuthPage = lazy(() => import('./features/auth/AuthPage'))

function Gate() {
  const { status } = useAuth()
  const { t } = useTranslation()
  if (status === 'loading') return <LoadingState height={400} />
  if (status === 'anon') {
    return (
      <Suspense fallback={<LoadingState height={400} />}>
        {getInvite() && <div className="mx-auto max-w-md px-4 pt-6"><Notice tone="info">{t('team.inviteBanner')}</Notice></div>}
        <AuthPage />
      </Suspense>
    )
  }
  return (
    <InviteGate>
      <RouterProvider router={router} />
    </InviteGate>
  )
}

export default function App() {
  return (
    <Providers>
      <Gate />
    </Providers>
  )
}
