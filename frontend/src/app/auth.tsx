import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, onUnauthorized, setCompany } from '../lib/api'
import type { Me } from '../types'

type Status = 'loading' | 'anon' | 'user'

interface AuthCtx {
  status: Status
  me: Me | null
  /** Called after a successful login / registration / ECP login / company rename. */
  setMe: (me: Me | null) => void
  logout: () => Promise<void>
}

const Ctx = createContext<AuthCtx | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const [state, setState] = useState<{ status: Status; me: Me | null }>({ status: 'loading', me: null })

  const setMe = useCallback(
    (me: Me | null) => {
      setCompany(me?.company_id ?? '')
      // A different user must never see the previous user's cached data.
      if (!me || me.company_id !== state.me?.company_id) qc.clear()
      setState({ status: me ? 'user' : 'anon', me })
    },
    [qc, state.me?.company_id],
  )

  useEffect(() => {
    let alive = true
    api
      .me()
      .then((me) => alive && setMe(me))
      .catch(() => alive && setMe(null))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => onUnauthorized(() => setMe(null)), [setMe])

  const logout = useCallback(async () => {
    try {
      await api.logout()
    } finally {
      setMe(null)
    }
  }, [setMe])

  const value = useMemo<AuthCtx>(() => ({ ...state, setMe, logout }), [state, setMe, logout])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth(): AuthCtx {
  const v = useContext(Ctx)
  if (!v) throw new Error('AuthProvider missing')
  return v
}

/** For components rendered only for a signed-in user. */
export function useMe(): Me {
  const { me } = useAuth()
  if (!me) throw new Error('Not signed in')
  return me
}
