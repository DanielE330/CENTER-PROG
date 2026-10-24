import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { TransactionQuery } from '../lib/api'
import { useAuth } from '../app/auth'
import { buildView } from '../features/forecast/model'
import type { ForecastView } from '../features/forecast/model'
import type { AdvisorHistoryItem, AdvisorSuggestion } from '../features/assistant/advisorTypes'
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

export function useFnsCounterparties(top = 20) {
  const key = useKey()
  return useQuery({ queryKey: key('fns', 'counterparties', top), queryFn: () => api.fnsCounterparties(top), retry: false })
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

export function useKeyRate() {
  const key = useKey()
  return useQuery({ queryKey: key('key-rate'), queryFn: api.keyRate, staleTime: 60 * 60_000 })
}
