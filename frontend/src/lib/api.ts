/**
 * Public API client. Same origin (the Vite dev server proxies /api to the backend), cookie `session`,
 * money as kopeck strings. Components never call fetch directly — they use hooks/queries.ts.
 */
import type {
  AccountMethods,
  AuditPage,
  ApiTokenInfo,
  Backtest,
  Department,
  ExpectedEvent,
  Goal,
  GoalStatus,
  ForecastRun,
  ImportPreview,
  ImportRow,
  KeyRate,
  Me,
  NewApiToken,
  NewInvite,
  NewExpectedEvent,
  PlanFact,
  Retro,
  ScenarioParams,
  ScenarioResponse,
  SprintHistoryItem,
  SprintView,
  SyncResult,
  TgLink,
  TgKind,
  TgMember,
  TgPrefs,
  TgRole,
  TgStatus,
  TgTeam,
  TaxCalendarRequest,
  TaxCalendarResult,
  TeamInvite,
  TeamMember,
  TransactionPage,
  WebRole,
} from '../types'
import { AppError } from '../i18n/helpers'
import { ApiError } from './errors'

export { ApiError } from './errors'

let companyId = ''
export function setCompany(id: string) {
  companyId = id
}
export const getCompanyId = () => companyId

type Listener = () => void
const unauthorized = new Set<Listener>()
/** Called when a request with a session cookie gets 401 (session expired). */
export function onUnauthorized(cb: Listener): () => void {
  unauthorized.add(cb)
  return () => unauthorized.delete(cb)
}

interface Opts {
  body?: unknown
  form?: FormData
  /** 401 is an expected answer (login form, session probe) — do not treat it as an expired session. */
  expect401?: boolean
}

async function request<T>(method: string, url: string, { body, form, expect401 }: Opts = {}): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, {
      method,
      credentials: 'same-origin',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
    })
  } catch {
    throw new AppError('errors.network')
  }
  if (res.status === 204) return undefined as T
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    if (res.status === 401 && !expect401) unauthorized.forEach((l) => l())
    throw new ApiError(res.status, data?.error?.code ?? 'error', data?.error?.message ?? `HTTP ${res.status}`)
  }
  return data as T
}

const co = (path = '') => `/api/v1/companies/${companyId}${path}`

export interface TransactionQuery {
  limit: number
  offset: number
  category?: string
  direction?: '' | 'in' | 'out'
  q?: string
  sort?: string
  order?: 'asc' | 'desc'
}

export const api = {
  // ----- auth -----
  me: () => request<Me>('GET', '/api/v1/auth/me', { expect401: true }),
  login: (email: string, password: string) => request<Me>('POST', '/api/v1/auth/login', { body: { email, password }, expect401: true }),
  register: (b: { email: string; password: string; password_confirm: string; company_name: string }) =>
    request<Me>('POST', '/api/v1/auth/register', { body: b, expect401: true }),
  ecpChallenge: () => request<{ challenge: string }>('POST', '/api/v1/auth/ecp/challenge', { body: {}, expect401: true }),
  /** Регистрация и вход по ЭЦП одним запросом: данные компании сервер берёт из сертификата. */
  ecpLogin: (challenge: string, signature: string) =>
    request<Me>('POST', '/api/v1/auth/ecp/login', { body: { challenge, signature }, expect401: true }),
  logout: () => request<void>('POST', '/api/v1/auth/logout', { body: {}, expect401: true }),

  // ----- account: sign-in methods and API tokens -----
  accountMethods: () => request<AccountMethods>('GET', '/api/v1/account/methods'),
  addEmail: (b: { email: string; password: string; password_confirm: string }) => request<AccountMethods>('POST', '/api/v1/account/email', { body: b }),
  removeEmail: () => request<AccountMethods>('DELETE', '/api/v1/account/email'),
  addSignature: (challenge: string, signature: string) => request<AccountMethods>('POST', '/api/v1/account/signatures', { body: { challenge, signature } }),
  removeSignature: (id: string) => request<AccountMethods>('DELETE', `/api/v1/account/signatures/${encodeURIComponent(id)}`),
  apiTokens: () => request<{ items: ApiTokenInfo[] }>('GET', '/api/v1/account/tokens'),
  createApiToken: (name: string) => request<NewApiToken>('POST', '/api/v1/account/tokens', { body: { name } }),
  deleteApiToken: (id: string) => request<void>('DELETE', `/api/v1/account/tokens/${encodeURIComponent(id)}`),

  // ----- company team: members, invites, roles -----
  teamMembers: () => request<{ items: TeamMember[] }>('GET', '/api/v1/team/members'),
  teamInvites: () => request<{ items: TeamInvite[] }>('GET', '/api/v1/team/invites'),
  createInvite: (role: Exclude<WebRole, 'owner'>, email?: string) =>
    request<NewInvite>('POST', '/api/v1/team/invites', { body: { role, ...(email ? { email } : {}) } }),
  transferOwnership: (userId: string) => request<unknown>('POST', '/api/v1/team/transfer', { body: { user_id: userId } }),
  activity: (q: { limit: number; offset: number; q?: string }) => {
    const p = new URLSearchParams({ limit: String(q.limit), offset: String(q.offset), q: q.q ?? '' })
    return request<AuditPage>('GET', `/api/v1/team/activity?${p}`)
  },
  deleteInvite: (id: string) => request<void>('DELETE', `/api/v1/team/invites/${encodeURIComponent(id)}`),
  setMemberRole: (uid: string, role: Exclude<WebRole, 'owner'>) => request<unknown>('PATCH', `/api/v1/team/members/${encodeURIComponent(uid)}`, { body: { role } }),
  removeMember: (uid: string) => request<void>('DELETE', `/api/v1/team/members/${encodeURIComponent(uid)}`),
  leaveTeam: () => request<void>('POST', '/api/v1/team/leave', { body: {} }),
  acceptInvite: (code: string) => request<{ tenant_id: string; role: WebRole }>('POST', '/api/v1/team/accept', { body: { code } }),

  // ----- company -----
  renameCompany: (name: string) => request<{ company_name: string }>('PATCH', co(), { body: { name } }),

  // ----- forecast -----
  /** null when no forecast has been built yet. */
  latestForecast: async (): Promise<ForecastRun | null> => {
    try {
      return await request<ForecastRun>('GET', co('/forecasts/latest'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null
      throw e
    }
  },
  createForecast: (horizonDays: number) => request<ForecastRun>('POST', co('/forecasts'), { body: { horizon_days: horizonDays } }),
  scenario: (horizonDays: number, scenario: ScenarioParams) =>
    request<ScenarioResponse>('POST', co('/scenarios'), { body: { horizon_days: horizonDays, scenario } }),
  backtest: () => request<Backtest>('GET', co('/backtest')),

  // ----- ledger -----
  transactions: (q: TransactionQuery) => {
    const p = new URLSearchParams({
      limit: String(q.limit),
      offset: String(q.offset),
      category: q.category ?? '',
      direction: q.direction ?? '',
      q: q.q ?? '',
      sort: q.sort ?? 'date',
      order: q.order ?? 'desc',
    })
    return request<TransactionPage>('GET', co(`/transactions?${p}`))
  },
  setCategory: (id: number, category: string) => request<unknown>('PATCH', co(`/transactions/${id}/category`), { body: { category } }),
  expectedEvents: () => request<{ items: ExpectedEvent[] }>('GET', co('/expected-events')),
  createEvent: (e: NewExpectedEvent) => request<unknown>('POST', co('/expected-events'), { body: e }),
  deleteEvent: (id: string) => request<void>('DELETE', co(`/expected-events/${id}`)),
  taxCalendar: ({ kind, amountKop, usnObject }: TaxCalendarRequest) =>
    request<TaxCalendarResult>('POST', co('/expected-events/tax-calendar'), {
      body: { kind, ...(amountKop ? { amount_kop: amountKop } : {}), ...(kind === 'usn' && usnObject ? { usn_object: usnObject } : {}) },
    }),
  syncIntegrations: (externalCompany: string) =>
    request<SyncResult>('POST', co('/integrations/sync'), { body: { external_company: externalCompany } }),

  // ----- imports -----
  imports: () => request<{ items: ImportRow[] }>('GET', co('/imports')),
  uploadStatement: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<ImportPreview>('POST', co('/imports'), { form })
  },
  confirmImport: (id: string) => request<{ inserted: number; skipped_duplicates: number; status: string }>('POST', co(`/imports/${id}/confirm`), { body: {} }),

  // ----- Telegram notifications -----
  tgStatus: () => request<TgStatus>('GET', co('/notifications')),
  tgLink: () => request<TgLink>('POST', co('/notifications/link'), { body: {} }),
  tgSavePrefs: (p: Partial<TgPrefs>) => request<TgPrefs>('PUT', co('/notifications/prefs'), { body: p }),
  tgUnlink: (chatId: number) => request<void>('DELETE', co(`/notifications/chats/${chatId}`)),
  tgTest: () => request<{ delivered: number }>('POST', co('/notifications/test'), { body: {} }),

  // ----- weekly goals (agile cycle) -----
  tgSprint: () => request<SprintView>('GET', co('/notifications/sprint')),
  tgHistory: () => request<{ items: SprintHistoryItem[] }>('GET', co('/notifications/history')),
  tgAddGoal: (b: { title: string; target_kop?: number }) => request<Goal>('POST', co('/notifications/goals'), { body: b }),
  tgPatchGoal: (id: number, b: { status?: GoalStatus; title?: string; actual_kop?: number }) => request<Goal>('PATCH', co(`/notifications/goals/${id}`), { body: b }),
  tgDeleteGoal: (id: number) => request<void>('DELETE', co(`/notifications/goals/${id}`)),
  tgRetro: (b: { week_start: string; mood?: number; blockers?: string }) => request<Retro>('POST', co('/notifications/retro'), { body: b }),

  // ----- team: departments, roles, group policies -----
  tgTeam: () => request<TgTeam>('GET', co('/notifications/team')),
  tgAddDepartment: (b: { name: string; kinds: TgKind[]; allow_excel: boolean; default_role: TgRole }) => request<Department>('POST', co('/notifications/departments'), { body: b }),
  tgPatchDepartment: (id: number, b: Partial<{ name: string; kinds: TgKind[]; allow_excel: boolean; default_role: TgRole }>) =>
    request<Department>('PATCH', co(`/notifications/departments/${id}`), { body: b }),
  tgDeleteDepartment: (id: number) => request<void>('DELETE', co(`/notifications/departments/${id}`)),
  tgChatDepartment: (chatId: number, departmentId: number | null) => request<unknown>('PATCH', co(`/notifications/chats/${chatId}`), { body: { department_id: departmentId } }),
  tgPatchMember: (uid: number, b: { role?: TgRole | ''; department_id?: number | null }) => request<TgMember>('PATCH', co(`/notifications/members/${uid}`), { body: b }),
  tgDeleteMember: (uid: number) => request<void>('DELETE', co(`/notifications/members/${uid}`)),

  // ----- plan / fact, gov -----
  planFact: (days: number) => request<PlanFact>('GET', co(`/plan-fact?days=${days}`)),
  keyRate: () => request<KeyRate>('GET', '/api/v1/gov/key-rate'),
}
