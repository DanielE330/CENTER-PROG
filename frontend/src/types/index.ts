/** Types mirror the future Django API responses. Dates are ISO strings (YYYY-MM-DD). */

import type { MsgParams } from '../i18n/helpers'

export type DataSource = 'mock' | 'api'

export interface ForecastPoint {
  date: string
  /** Confirmed historical balance. null for future dates. */
  actual: number | null
  /** Forecast balance. null for past dates. */
  forecast: number | null
  /** Uncertainty range — present only if the model supplies it. */
  lower?: number | null
  upper?: number | null
  inflow: number
  outflow: number
}

export type RiskLevel = 'low' | 'medium' | 'high' | 'unknown'

export interface RiskFactor {
  /** Selects the translated label and description (risk.factors.<id>). */
  id: string
  params?: MsgParams
  /** Amount in RUB that relates to this factor, if known. */
  amount?: number
}

export interface RiskAssessment {
  level: RiskLevel
  shortageDate: string | null
  deficitAmount: number | null
  /** Model estimate in 0..1. Absent when the model does not provide it. */
  probability?: number
  factors: RiskFactor[]
}

export interface DataQuality {
  /** 0..100 */
  completeness: number
  /** Codes translated by the UI (quality.<code>). */
  warnings: string[]
}

export interface ForecastSummary {
  currentBalance: number | null
  minBalance: number | null
  minBalanceDate: string | null
  expectedInflows: number
  expectedOutflows: number
}

export interface ForecastResponse {
  source: DataSource
  generatedAt: string
  today: string
  horizonDays: number
  points: ForecastPoint[]
  summary: ForecastSummary
  risk: RiskAssessment
  quality: DataQuality
}

export type ObligationKind = 'payment' | 'receipt'
export type ObligationStatus = 'planned' | 'expected' | 'completed' | 'overdue'

export interface Obligation {
  id: string
  kind: ObligationKind
  /** Translation key of the item name (mock.names.*). */
  nameKey: string
  date: string
  /** Always positive; direction is given by kind. */
  amount: number
  status: ObligationStatus
  /** Translation key of the note (mock.notes.*). */
  noteKey?: string
}

export type TransactionStatus = 'completed' | 'pending'

export interface Transaction {
  id: string
  date: string
  counterpartyKey: string
  descriptionKey: string
  descriptionParams?: MsgParams
  amount: number
  direction: 'in' | 'out'
  /** Translation key (categories.*). */
  categoryKey?: string
  status?: TransactionStatus
}

export interface PlanActualRow {
  date: string
  plannedIn: number
  actualIn: number
  plannedOut: number
  actualOut: number
  plannedBalance: number
  actualBalance: number
}

export interface PlanActualResponse {
  source: DataSource
  periodDays: number
  rows: PlanActualRow[]
  available: boolean
}

export interface AssistantReplyPart {
  key: string
  params?: MsgParams
}

export interface AssistantMessage {
  id: string
  role: 'user' | 'assistant'
  /** Raw text typed by the user. */
  text?: string
  /** Assistant answers are keys + neutral params so they re-translate on language change. */
  parts?: AssistantReplyPart[]
  /** Translation keys of the data sets the answer was built from. */
  basedOn?: string[]
}

export interface ImportResult {
  fileName: string
  operations: number
  periodFrom: string
  periodTo: string
  simulated: true
}

export type DemoState = 'normal' | 'loading' | 'error' | 'empty' | 'incomplete'
