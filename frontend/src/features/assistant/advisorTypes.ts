/** Contract of POST /api/v1/companies/{cid}/advisor/ask and .../advisor/suggestions/apply. */
export interface PaymentParams {
  direction: 'in' | 'out'
  /** Kopecks, positive, decimal string. */
  amount_kop: string
  date: string
  title: string
  counterparty: string
}

export interface AdvisorSuggestion {
  id: string
  type: 'add_expected_payment'
  params: PaymentParams
  /** Server-built title in the requested language; the UI renders its own from params. */
  title: string
}

export type AdvisorNotice = 'provider_not_configured' | 'provider_unavailable' | 'budget_exceeded' | null

/** What the advisor did itself (only with the add_payments / recalc_forecast permission). */
export interface AdvisorAction {
  type: 'add_expected_payment' | 'recalc_forecast'
  id: string
  title: string
}

export interface AdvisorPrompt {
  text: string
  kind: 'gap' | 'overdue' | 'counterparty' | 'scenario' | 'action' | 'general'
}

export type AdvisorPermission =
  | 'read_forecast' | 'read_operations' | 'read_counterparties' | 'run_scenarios' | 'propose_payments' | 'add_payments' | 'recalc_forecast'

export interface AdvisorSettings {
  enabled: boolean
  permissions: AdvisorPermission[]
  show_prompts: boolean
}

export interface AdvisorSettingsView {
  settings: AdvisorSettings
  defaults: AdvisorSettings
  updated_at: string | null
  catalog: Array<{ code: AdvisorPermission; needs_edit: boolean }>
  /** Permissions for the current user (write ones dropped for viewers). */
  effective: AdvisorPermission[]
  engine: {
    engine: 'hermes' | 'llm_gateway'
    /** available: the process answers; last: outcome of the last question (a running Hermes may still lack the Codex login). */
    hermes: { configured: boolean; available: boolean; models?: string[]; last?: { ok: boolean | null; error: string | null; at: string | null } }
  } | null
}

export interface AdvisorAnswer {
  answer: string
  suggestions: AdvisorSuggestion[]
  actions?: AdvisorAction[]
  /** Next questions proposed by the advisor; shown as chips under the input. */
  followups?: string[]
  /** hermes | anthropic | openai | gemini | openrouter, or "none" for the template answer without a model. */
  provider: string
  model?: string | null
  notice?: AdvisorNotice
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number; steps: number }
  tools_used?: string[]
}

export interface AdvisorHistoryItem {
  role: 'user' | 'assistant'
  content: string
}
