import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { TransactionQuery } from '../lib/api'
import { useAuth } from '../app/auth'
import { buildView } from '../features/forecast/model'
import type { ForecastView } from '../features/forecast/model'
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
    mutationFn: (horizonDays: number) => api.createForecast(horizonDays),
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

export function useKeyRate() {
  const key = useKey()
  return useQuery({ queryKey: key('key-rate'), queryFn: api.keyRate, staleTime: 60 * 60_000 })
}
