/**
 * Operator API (/api/admin/v1): a separate session (cookie `op_session`, Path=/api/admin) that never mixes with the
 * company session. Only registration metadata and aggregates — no transactions or payments.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AppError } from '../../i18n/helpers'
import { ApiError } from '../../lib/errors'
import type { MfaSetup, RiskLevel } from '../../types'

export interface OperatorMe {
  email: string
  /** Резервные коды второго фактора: приходят один раз, при его подключении. */
  backup_codes?: string[]
  backup_codes_left?: number
}

/** Пароль верен, но нужен второй фактор: сессии (cookie) ещё нет. */
export interface OperatorChallenge {
  mfa: 'setup' | 'verify'
  mfa_token: string
  email: string
}

export interface OperatorMember {
  email: string
  role: 'director' | 'admin' | 'editor' | 'viewer'
  created_at: string | null
}

export interface OperatorCompany {
  id: string
  name: string
  inn: string | null
  ogrn: string | null
  created_at: string | null
  banned: boolean
  banned_at: string | null
  ban_reason: string | null
  members_count: number | null
  transactions_count: number | null
  last_transaction_date: string | null
  last_import_at: string | null
  events_count: number | null
  director_email: string | null
  /** null when the company has no forecast yet. */
  risk_level: RiskLevel | null
  shortfall_probability: number | null
  members?: OperatorMember[]
}

export interface CompanyList {
  total: number
  limit: number
  offset: number
  items: OperatorCompany[]
}

export interface OperatorStats {
  total: number
  active: number
  banned: number
  new_7d: number
  new_30d: number
  users_total: number
}

export interface BanResult {
  company_id: string
  banned: boolean
  ban_reason?: string | null
  sessions_revoked?: number | null
  revoke_failed?: number | null
}

const EXPIRED = 'operator-session-expired'

/** The session expired in the middle of work: the page shows the sign-in form again. */
export function onOperatorExpired(cb: () => void): () => void {
  window.addEventListener(EXPIRED, cb)
  return () => window.removeEventListener(EXPIRED, cb)
}

async function request<T>(method: string, url: string, body?: unknown, expect401 = false): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, {
      method,
      credentials: 'same-origin',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new AppError('errors.network')
  }
  if (res.status === 204) return undefined as T
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    if (res.status === 401 && !expect401) window.dispatchEvent(new Event(EXPIRED))
    throw new ApiError(res.status, data?.error?.code ?? 'error', data?.error?.message ?? `HTTP ${res.status}`)
  }
  return data as T
}

export interface CompanyQuery {
  query: string
  banned: boolean
  limit: number
  offset: number
}

const base = '/api/admin/v1'

export const opApi = {
  me: async (): Promise<OperatorMe | null> => {
    try {
      return await request<OperatorMe>('GET', `${base}/me`, undefined, true)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null
      throw e
    }
  },
  login: (email: string, password: string) => request<OperatorMe | OperatorChallenge>('POST', `${base}/login`, { email, password }, true),
  mfaSetup: (mfa_token: string) => request<MfaSetup>('POST', `${base}/mfa/setup`, { mfa_token }, true),
  mfaEnable: (mfa_token: string, code: string) => request<OperatorMe>('POST', `${base}/mfa/enable`, { mfa_token, code }, true),
  mfaVerify: (mfa_token: string, code: string) => request<OperatorMe>('POST', `${base}/mfa/verify`, { mfa_token, code }, true),
  logout: () => request<void>('POST', `${base}/logout`, {}, true),
  stats: () => request<OperatorStats>('GET', `${base}/stats`),
  companies: (q: CompanyQuery) => {
    const p = new URLSearchParams({ limit: String(q.limit), offset: String(q.offset) })
    if (q.query.trim()) p.set('query', q.query.trim())
    if (q.banned) p.set('banned', 'true')
    return request<CompanyList>('GET', `${base}/companies?${p}`)
  },
  company: (id: string) => request<OperatorCompany>('GET', `${base}/companies/${encodeURIComponent(id)}`),
  ban: (id: string, reason: string) => request<BanResult>('POST', `${base}/companies/${encodeURIComponent(id)}/ban`, { reason }),
  unban: (id: string) => request<BanResult>('POST', `${base}/companies/${encodeURIComponent(id)}/unban`, {}),
}

export const opKeys = {
  me: ['operator', 'me'] as const,
  all: ['operator'] as const,
}

export const useOperatorMe = () => useQuery({ queryKey: opKeys.me, queryFn: opApi.me, staleTime: 5 * 60_000 })
export const useOperatorStats = () => useQuery({ queryKey: ['operator', 'stats'], queryFn: opApi.stats, staleTime: 15_000 })
export const useOperatorCompanies = (q: CompanyQuery) =>
  useQuery({ queryKey: ['operator', 'companies', q], queryFn: () => opApi.companies(q), staleTime: 15_000, placeholderData: (prev) => prev })
export const useOperatorCompany = (id: string) => useQuery({ queryKey: ['operator', 'company', id], queryFn: () => opApi.company(id), staleTime: 15_000 })

/** Ban / unban; afterwards the list, the card and the counters are re-read. */
export function useBanMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string | null }) => (reason === null ? opApi.unban(id) : opApi.ban(id, reason)),
    onSuccess: () => qc.invalidateQueries({ queryKey: opKeys.all, predicate: (q) => q.queryKey[1] !== 'me' }),
  })
}
