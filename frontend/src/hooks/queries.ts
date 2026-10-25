import { useEffect, useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { TransactionQuery } from '../lib/api'
import { useAuth } from '../app/auth'
import { setRates } from '../lib/currency'
import { buildView } from '../features/forecast/model'
import type { ForecastView } from '../features/forecast/model'
import type { AdvisorChat, AdvisorHistoryItem, AdvisorSuggestion, ChatMode } from '../features/assistant/advisorTypes'
import type { NewExpectedEvent, ScenarioParams, TaxCalendarRequest } from '../types'

/** All company data lives under ['co', companyId, ...], so one call refreshes everything after a data change. */
function useKey() {
  const { me } = useAuth()
  const cid = me?.company_id ?? ''
  return (...k: unknown[]) => ['co', cid, ...k] as const
}

export function useInvalidateAll() {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: ['co'] })
}

/** The latest base forecast; the server recomputes it by itself when data changed (`recomputed`). */
export function useForecastRun() {
  const key = useKey()
  return useQuery({ queryKey: key('forecast'), queryFn: api.latestForecast })
}

/** Forecast plus display-only derived data. `data` is null when no forecast exists yet. */
export function useForecast() {
  const q = useForecastRun()
  const view = useMemo<ForecastView | null>(() => (q.data ? buildView(q.data) : null), [q.data])
  return { ...q, view }
}

export function useRecalc() {
  const key = useKey()
  const qc = useQueryClient()
  const all = useInvalidateAll()
  return useMutation({
    /** A number is the horizon; `{ full: true }` is "recompute from scratch" in the incremental mode. */
    mutationFn: (v: number | { horizon: number; full: boolean }) => (typeof v === 'number' ? api.createForecast(v) : api.createForecast(v.horizon, v.full)),
    onSuccess: (run) => {
      qc.setQueryData(key('forecast'), { ...run, recomputed: false })
      void all()
    },
  })
}

export function useScenario() {
  return useMutation({ mutationFn: (v: { horizon: number; params: ScenarioParams }) => api.scenario(v.horizon, v.params) })
}

export function useBacktest(enabled: boolean) {
  const key = useKey()
  return useQuery({ queryKey: key('backtest'), queryFn: api.backtest, enabled, staleTime: 5 * 60_000 })
}

export function useTransactions(q: TransactionQuery) {
  const key = useKey()
  return useQuery({ queryKey: key('transactions', q), queryFn: () => api.transactions(q), placeholderData: (prev) => prev })
}

export function useTaxonomy() {
  return useQuery({ queryKey: ['taxonomy'], queryFn: api.taxonomy, staleTime: Infinity })
}

export function useSetSubcategory() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (v: { id: number; subcategory: string }) => api.setSubcategory(v.id, v.subcategory), onSuccess: () => void all() })
}

export function useSetCategory() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (v: { id: number; category: string }) => api.setCategory(v.id, v.category), onSuccess: () => void all() })
}

export function useExpectedEvents() {
  const key = useKey()
  return useQuery({ queryKey: key('events'), queryFn: api.expectedEvents })
}

export function useCreateEvent() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (e: NewExpectedEvent) => api.createEvent(e), onSuccess: () => void all() })
}

export function useDeleteEvent() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (id: string) => api.deleteEvent(id), onSuccess: () => void all() })
}

export function useTaxCalendar() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (req: TaxCalendarRequest) => api.taxCalendar(req), onSuccess: () => void all() })
}

export function useSyncIntegrations() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (ext: string) => api.syncIntegrations(ext), onSuccess: () => void all() })
}

/** ЕНС и контрагенты: без повторов при ошибке (нет ИНН, сервис ФНС недоступен), экран покажет причину. */
export function useFnsEns() {
  const key = useKey()
  return useQuery({ queryKey: key('fns', 'ens'), queryFn: api.fnsEns, retry: false })
}

/** Без `top` — сколько задано в настройках ФНС компании. */
export function useFnsCounterparties(top?: number) {
  const key = useKey()
  return useQuery({ queryKey: key('fns', 'counterparties', top ?? 'settings'), queryFn: () => api.fnsCounterparties(top), retry: false })
}

/** Налоги на год из ФНС создают ожидаемые события, поэтому обновляются все экраны. */
export function useFnsSync() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (b: { year?: number; regime?: string }) => api.fnsSync(b), onSuccess: () => void all() })
}

export function useFnsImportEns() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (v: { filename: string; contentB64: string }) => api.fnsImportEns(v.filename, v.contentB64), onSuccess: () => void all() })
}

export function useAdvisorAsk() {
  return useMutation({ mutationFn: (v: { question: string; history: AdvisorHistoryItem[]; lang: string }) => api.advisorAsk(v.question, v.history, v.lang) })
}

/** Prompts under the chat input, built from company data and the advisor's permissions. */
export function useAdvisorPrompts(lang: string, mode: ChatMode = 'chat') {
  const key = useKey()
  return useQuery({ queryKey: key('advisor-prompts', lang, mode), queryFn: () => api.advisorPrompts(lang, mode), retry: false, staleTime: 60_000 })
}

export function useAdvisorModels() {
  const key = useKey()
  return useQuery({ queryKey: key('advisor-models'), queryFn: api.advisorModels, staleTime: 10 * 60_000, retry: false })
}

export function useAdvisorChats() {
  const key = useKey()
  return useQuery({ queryKey: key('advisor-chats'), queryFn: api.advisorChats })
}

/** A saved chat with its messages; refetched on focus so an answer that finished while the user was away shows up. */
export function useAdvisorChat(id: string | null) {
  const key = useKey()
  return useQuery({ queryKey: key('advisor-chat', id), queryFn: () => api.getChat(id as string), enabled: !!id, refetchOnWindowFocus: true })
}

/** The advisor's active strategy for Plan vs fact. */
export function useActivePlan() {
  const key = useKey()
  return useQuery({ queryKey: key('advisor-plan'), queryFn: api.activePlan, retry: false })
}

export function useDeletePlan() {
  const key = useKey()
  const qc = useQueryClient()
  return useMutation({ mutationFn: (id: string) => api.deletePlan(id), onSuccess: () => void qc.invalidateQueries({ queryKey: key('advisor-plan') }) })
}

export function useCreateChat() {
  const key = useKey()
  const qc = useQueryClient()
  return useMutation({ mutationFn: api.createChat, onSuccess: () => void qc.invalidateQueries({ queryKey: key('advisor-chats') }) })
}

export function usePatchChat() {
  const key = useKey()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { id: string; title?: string; model?: string }) => api.patchChat(v.id, { title: v.title, model: v.model }),
    onSuccess: (c) => {
      qc.setQueryData(key('advisor-chat', c.id), (old: { chat: AdvisorChat } | undefined) => (old ? { ...old, chat: c } : old))
      void qc.invalidateQueries({ queryKey: key('advisor-chats') })
    },
  })
}

export function useDeleteChat() {
  const key = useKey()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteChat(id),
    onSuccess: (_v, id) => {
      qc.removeQueries({ queryKey: key('advisor-chat', id) })
      void qc.invalidateQueries({ queryKey: key('advisor-chats') })
    },
  })
}

export const sendKey = (cid: string) => ['co', cid, 'advisor-send'] as const

/** A question in a saved chat. Lives in the mutation cache, not in the page: leaving the tab does not lose the answer,
 * and on return the page sees the question still pending (useMutationState with sendKey). */
export function useSendChat() {
  const key = useKey()
  const { me } = useAuth()
  const qc = useQueryClient()
  return useMutation({
    mutationKey: sendKey(me?.company_id ?? ''),
    mutationFn: (v: { chatId: string; question: string; lang: string }) => api.sendChat(v.chatId, v.question, v.lang),
    onSettled: (r, _e, v) => {
      void qc.invalidateQueries({ queryKey: key('advisor-chat', v.chatId) })
      void qc.invalidateQueries({ queryKey: key('advisor-chats') })
      if (r?.message.meta.plan_id) void qc.invalidateQueries({ queryKey: key('advisor-plan') })
      if (r?.message.meta.actions?.length) void qc.invalidateQueries({ queryKey: ['co'] })
    },
  })
}

/** Applying a suggestion creates an expected event, so every cached screen is refreshed. */
export function useApplySuggestion() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (s: AdvisorSuggestion) => api.advisorApply(s), onSuccess: () => void all() })
}

export function usePlanFact(days: number) {
  const key = useKey()
  return useQuery({ queryKey: key('plan-fact', days), queryFn: () => api.planFact(days) })
}

export function useImports() {
  const key = useKey()
  return useQuery({ queryKey: key('imports'), queryFn: api.imports })
}

export function useUploadStatement() {
  return useMutation({ mutationFn: (file: File) => api.uploadStatement(file) })
}

export function useConfirmImport() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: (id: string) => api.confirmImport(id), onSuccess: () => void all() })
}

/** Demo status; `enabled=false` hides the demo card. Errors (e.g. old backend) are treated as «no demo». */
export function useDemoStatus() {
  const key = useKey()
  return useQuery({ queryKey: key('demo-status'), queryFn: api.demoStatus, retry: false, staleTime: 30_000 })
}

/** One demo week goes through the regular import on the server; then every cached screen is refreshed. */
export function useAdvanceWeek() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: () => api.demoAdvanceWeek(), onSuccess: () => void all() })
}

export function useRewindWeek() {
  const all = useInvalidateAll()
  return useMutation({ mutationFn: () => api.demoRewindWeek(), onSuccess: () => void all() })
}

export function useKeyRate() {
  const key = useKey()
  return useQuery({ queryKey: key('key-rate'), queryFn: api.keyRate, staleTime: 60 * 60_000 })
}

/**
 * CBR exchange rates for the display currency (the server caches them for a day). The result is pushed into the
 * currency store, which also keeps the last good set in localStorage so the first paint already has rates.
 */
export function useCurrencyRates() {
  const q = useQuery({ queryKey: ['gov-rates'], queryFn: api.govRates, staleTime: 6 * 60 * 60_000, retry: 1 })
  useEffect(() => {
    if (q.data) setRates(q.data)
  }, [q.data])
  return q
}
