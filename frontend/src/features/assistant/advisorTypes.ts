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

export interface AdvisorAnswer {
  answer: string
  suggestions: AdvisorSuggestion[]
  /** anthropic | openai | gemini | openrouter, or "none" for the template answer without a model. */
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
