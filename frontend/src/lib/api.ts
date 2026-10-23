/**
 * Frontend API abstraction. Components never import mock data directly — they call these
 * functions through hooks. To connect Django, set VITE_API_URL and the `request` branch is used.
 */
import type {
  AssistantMessage,
  ForecastResponse,
  ImportResult,
  Obligation,
  PlanActualResponse,
  Transaction,
} from '../types'
import { buildAssistantReply, buildForecast, buildObligations, buildPlanActual, buildTransactions } from '../mocks/data'
import { getDemoState } from './demoState'
import { sleep } from './utils'

const BASE_URL = import.meta.env.VITE_API_URL as string | undefined
const USE_MOCK = !BASE_URL

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, { headers: { 'Content-Type': 'application/json' }, ...init })
  if (!res.ok) throw new Error(`Ошибка API: ${res.status}`)
  return res.json() as Promise<T>
}

/** Simulates network latency and the forced demo states (loading / error). */
async function mock<T>(produce: () => T, latency = 450): Promise<T> {
  const demo = getDemoState()
  await sleep(demo === 'loading' ? 5000 : latency)
  if (demo === 'error') throw new Error('Не удалось получить данные от сервера (демо-ошибка).')
  return produce()
}

export const api = {
  getForecast: (): Promise<ForecastResponse> =>
    USE_MOCK ? mock(() => buildForecast(getDemoState())) : request('/forecast/'),

  getTransactions: (): Promise<Transaction[]> =>
    USE_MOCK ? mock(() => (getDemoState() === 'empty' ? [] : buildTransactions())) : request('/transactions/'),

  getObligations: (): Promise<Obligation[]> =>
    USE_MOCK ? mock(() => (getDemoState() === 'empty' ? [] : buildObligations())) : request('/obligations/'),

  getPlanActual: (periodDays: number): Promise<PlanActualResponse> =>
    USE_MOCK ? mock(() => buildPlanActual(periodDays, getDemoState())) : request(`/plan-vs-actual/?days=${periodDays}`),

  askAssistant: async (question: string): Promise<AssistantMessage> => {
    if (!USE_MOCK) return request('/assistant/', { method: 'POST', body: JSON.stringify({ question }) })
    return mock(() => {
      const { text, basedOn } = buildAssistantReply(question, buildForecast(getDemoState()), buildObligations())
      return { id: crypto.randomUUID(), role: 'assistant' as const, text, basedOn }
    }, 900)
  },

  /** SIMULATED — no file leaves the browser and nothing is parsed. */
  importStatement: async (file: File, forceError = false): Promise<ImportResult> => {
    await sleep(1400)
    const okExt = /\.(csv|txt|xlsx?|1c)$/i.test(file.name)
    if (forceError || !okExt || file.size > 10 * 1024 * 1024) {
      throw new Error(
        !okExt
          ? 'Неподдерживаемый формат файла. Допустимы CSV, TXT, XLS, XLSX.'
          : file.size > 10 * 1024 * 1024
            ? 'Файл больше 10 МБ.'
            : 'Не удалось распознать структуру файла (демо-ошибка).',
      )
    }
    return { fileName: file.name, operations: 128, periodFrom: '2026-09-23', periodTo: '2026-10-23', simulated: true }
  },
}
