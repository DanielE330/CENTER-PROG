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
  kind: 'gap' | 'overdue' | 'counterparty' | 'scenario' | 'action' | 'plan' | 'guide' | 'general'
}

export type AdvisorPermission =
  | 'read_forecast' | 'read_operations' | 'read_counterparties' | 'run_scenarios' | 'propose_payments' | 'make_plans' | 'add_payments' | 'recalc_forecast'

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

// ----- saved chats (advisor/chats.py) -----
/** chat — consultant; plan — builds a strategy for the Plan vs fact screen. */
export type ChatMode = 'chat' | 'plan'
/** Reasoning depth, fixed when the chat starts. */
export type Reasoning = 'low' | 'medium' | 'high' | 'xhigh'

export interface AdvisorChat {
  id: string
  title: string
  mode: ChatMode
  /** Subscription model; empty — the default one. */
  model: string
  reasoning: Reasoning
  created_at: string
  updated_at: string
  messages?: number
}

export interface ChatMessageMeta {
  suggestions?: AdvisorSuggestion[]
  actions?: AdvisorAction[]
  followups?: string[]
  provider?: string
  model?: string | null
  notice?: AdvisorNotice
  tools_used?: string[]
  reasoning?: Reasoning
  /** Set when this answer saved a strategy (plan mode). */
  plan_id?: string
}

export interface StoredMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  meta: ChatMessageMeta
  created_at: string
}

export interface AdvisorModels {
  models: string[]
  efforts: Reasoning[]
  default_model: string | null
  default_effort: Reasoning
}

export interface StrategyStep {
  title: string
  detail: string
  kind: 'income' | 'expense' | 'reserve' | 'negotiation' | 'tax' | 'financing' | 'control'
  due_date: string | null
  /** Rubles: plus — money comes in or stays, minus — goes out. */
  amount_rub: number | null
}

export interface StrategyPlan {
  id: string
  chat_id: string | null
  created_at: string
  title: string
  summary: string
  goal: string
  horizon_days: number | null
  steps: StrategyStep[]
  risks: string[]
  checkpoints: string[]
}
