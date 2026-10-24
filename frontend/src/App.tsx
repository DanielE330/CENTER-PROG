import { lazy, Suspense } from 'react'
import { RouterProvider } from 'react-router-dom'
import { Providers } from './app/providers'
import { useAuth } from './app/auth'
import { router } from './app/router'
import { LoadingState } from './components/shared/States'

const AuthPage = lazy(() => import('./features/auth/AuthPage'))

function Gate() {
  const { status } = useAuth()
  if (status === 'loading') return <LoadingState height={400} />
  if (status === 'anon') {
    return (
      <Suspense fallback={<LoadingState height={400} />}>
        <AuthPage />
      </Suspense>
    )
  }
  return <RouterProvider router={router} />
}

export default function App() {
  return (
    <Providers>
      <Gate />
    </Providers>
  )
}
