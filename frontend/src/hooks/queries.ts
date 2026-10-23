import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { useDemoState } from '../lib/demoState'

/** Query keys include the demo state so switching it refetches. */
export function useForecast() {
  const demo = useDemoState()
  return useQuery({ queryKey: ['forecast', demo], queryFn: api.getForecast })
}
export function useTransactions() {
  const demo = useDemoState()
  return useQuery({ queryKey: ['transactions', demo], queryFn: api.getTransactions })
}
export function useObligations() {
  const demo = useDemoState()
  return useQuery({ queryKey: ['obligations', demo], queryFn: api.getObligations })
}
export function usePlanActual(periodDays: number) {
  const demo = useDemoState()
  return useQuery({ queryKey: ['plan-actual', periodDays, demo], queryFn: () => api.getPlanActual(periodDays) })
}
