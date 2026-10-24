import { lazy, Suspense, useState } from 'react'
import { RouterProvider } from 'react-router-dom'
import { Providers } from './app/providers'
import { useAuth } from './app/auth'
import { router } from './app/router'
import { LoadingState } from './components/shared/States'

const AuthPage = lazy(() => import('./features/auth/AuthPage'))
const BannedScreen = lazy(() => import('./features/auth/AccessGates').then((m) => ({ default: m.BannedScreen })))
const SetCredentialsScreen = lazy(() => import('./features/auth/AccessGates').then((m) => ({ default: m.SetCredentialsScreen })))
const ChangePasswordScreen = lazy(() => import('./features/auth/AccessGates').then((m) => ({ default: m.ChangePasswordScreen })))

function Gate() {
  const { status, me } = useAuth()
  // «Skip» for a password the director chose: only until the page is reloaded.
  const [skipped, setSkipped] = useState(false)
  // /operator — панель оператора платформы: свой вход и своя сессия, вход в компанию не требуется
  if (/^\/operator\/?$/.test(window.location.pathname)) return <RouterProvider router={router} />
  if (status === 'loading') return <LoadingState height={400} />
  if (status === 'anon') {
    return (
      <Suspense fallback={<LoadingState height={400} />}>
        <AuthPage />
      </Suspense>
    )
  }
  const screen = !me
    ? null
    : me.company_banned
      ? <BannedScreen me={me} />
      : me.password_required
        ? <SetCredentialsScreen />
        : me.must_change_password && !(skipped && me.password_generated === false)
          ? <ChangePasswordScreen me={me} onSkip={() => setSkipped(true)} />
          : null
  if (screen) return <Suspense fallback={<LoadingState height={400} />}>{screen}</Suspense>
  return <RouterProvider router={router} />
}

export default function App() {
  return (
    <Providers>
      <Gate />
    </Providers>
  )
}
