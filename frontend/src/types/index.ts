/**
 * Types of the public API (docs/knowledge-base.md §5). Money is a decimal string in kopecks (BigInt-safe);
 * dates are ISO strings (YYYY-MM-DD). Field names mirror the JSON exactly.
 */

/** Kopecks as a decimal string, e.g. "-1250050". */
export type Kop = string

// ---------- auth ----------
/** Пароль верен, но нужен второй фактор: сессии ещё нет. `setup` — первый вход, 2FA надо подключить. */
export interface MfaChallenge {
  mfa: 'setup' | 'verify'
  mfa_token: string
}
export interface MfaSetup {
  secret: string
  otpauth_uri: string
  /** SVG-картинка QR, нарисованная сервером. */
  qr_svg: string
}
/** Вход завершён; при подключении 2FA приходят резервные коды (показываются один раз). */
export type MeWithCodes = Me & { backup_codes?: string[]; backup_codes_left?: number }

export interface Me {
  /** null when the account signs in only with an electronic signature. */
  email: string | null
  company_id: string
  company_name: string
  company_inn?: string
  company_ogrn?: string
  ecp?: boolean
  /** Роль в компании. Старый бэкенд мог не отдавать поле — тогда считаем пользователя директором. */
  role?: Role
  user_id?: string
  permissions?: Permissions
  /** Нужно сменить пароль (первый вход сотрудника / сброс директором). */
  must_change_password?: boolean
  /** Пароль сгенерирован сервером: смена обязательна. Если поля нет — считаем смену обязательной. */
  password_generated?: boolean
  /** Директор, вошедший по ЭЦП, ещё не задал email и пароль. */
  password_required?: boolean
  company_banned?: boolean
  ban_reason?: string | null
}

/** director — «Владелец», admin — «Администратор», editor — «Аналитик», viewer — «Наблюдатель». */
export type Role = 'director' | 'admin' | 'editor' | 'viewer'
export interface Permissions {
  read: boolean
  edit: boolean
  /** команда, название компании, настройки уведомлений: директор и администратор */
  admin: boolean
  /** только директор */
  manage: boolean
}

export interface Member {
  user_id: string
  /** null у участника, вошедшего только по ЭЦП (служебный адрес наружу не отдаётся). */
  email: string | null
  role: Role
  created_at: string
  is_me?: boolean
}

export interface NewMemberRequest {
  email: string
  role: Exclude<Role, 'director'>
  /** Пароль задаёт директор или администратор. */
  password?: string
  /** Либо пароль генерирует сервер и возвращает его один раз. */
  generate?: boolean
}

export interface NewMemberResult {
  user_id: string
  email: string
  role: Role
  /** Только если generate=true; повторно получить нельзя. */
  password?: string
}


// ---------- forecast ----------
export type RiskLevel = 'none' | 'low' | 'medium' | 'high'

export interface RiskItem {
  date: string
  amount: Kop
  title: string
  source: string
  overdue: boolean
}

export interface RiskWarning {
  code: string
  severity: 'high' | 'medium' | 'low'
  date: string | null
  message: string
  reasons: RiskItem[]
}

export interface Risk {
  level: RiskLevel
  /** Calibrated probability (0..1) of at least one negative balance in the horizon. */
  shortfall_probability: number
  /** Probability straight from the simulation, before calibration. */
  raw_shortfall_probability?: number
  first_break_date: string | null
  possible_break_date: string | null
  median_path_deficit: Kop
  max_deficit_p90: Kop
  deficit_given_break_median: Kop
  warnings: RiskWarning[]
}

export interface ForecastDay {
  date: string
  p10: Kop
  p50: Kop
  p90: Kop
  prob_negative: number
  prob_broken_by: number
}

export interface Rule {
  key: string
  counterparty: string
  category: string
  direction: 'in' | 'out'
  kind: 'monthly' | 'weekly'
  anchors: number[]
  amount: Kop
  occurrences: number
  confidence: number
  last_date?: string
}

export type QualityLevel = 'good' | 'medium' | 'low'

export interface ForecastResult {
  as_of: string
  /** Requested horizon in days from today. */
  horizon: number
  /** Days between the statement end and today, estimated by the model (the first points of `days`). */
  bridge_days?: number
  opening_balance: Kop
  model_version: string
  seed: number
  n_paths: number
  quality: { level: QualityLevel | string; history_days: number; notes: string[] }
  days: ForecastDay[]
  risk: Risk
  rules: Rule[]
  planned: RiskItem[]
}

export interface ForecastRun {
  run_id: string
  created_at: string
  /** Data changed after this calculation and recomputation did not happen. */
  stale: boolean
  /** The server recomputed the forecast while answering this request. */
  recomputed?: boolean
  scenario: unknown
  result: ForecastResult
}

export interface ScenarioParams {
  delay_days: number
  delay_counterparty: string | null
  revenue_drop_pct: number
  fixed_cost_growth_pct: number
  extra_expense_kop: number | null
  extra_expense_date: string | null
  extra_income_kop: number | null
  extra_income_date: string | null
}

export interface ScenarioResponse {
  base: ForecastRun
  scenario: ForecastRun
  delta: { shortfall_probability: number; max_deficit_p90: Kop }
}

export interface BacktestMetric {
  horizon: number
  mae_model: Kop
  mae_last_value: Kop
  mae_drift: Kop
  model_beats_baselines_share: number
  band_coverage?: number
  n: number
}

export interface Backtest {
  origins: number
  alarm_precision: number | null
  alarm_recall: number | null
  actual_gap_windows: number
  metrics: BacktestMetric[]
  series_14?: Array<{ date: string; predicted: Kop; actual: Kop }>
  note?: string
}

// ---------- ledger ----------
export interface Transaction {
  id: number
  date: string
  amount_kop: Kop
  counterparty: string
  purpose: string
  category: string
  category_confidence: number
  manual: boolean
  /** подкатегория таксономии, группа и способ разметки (rule | tail | tenant_rule | manual | fallback) */
  subcategory?: string
  group?: string
  method?: string
}

export interface Taxonomy {
  groups: Record<string, string>
  subcategories: Record<string, { group: string; name: string; category: string }>
}

export interface TransactionPage {
  items: Transaction[]
  total: number
  limit: number
  offset: number
}

export type EventSource = 'manual' | 'invoice' | 'contract' | 'tax'

export interface ExpectedEvent {
  id: string
  date: string
  /** Signed: negative is a payment. */
  amount: Kop
  title: string
  source: string
  confidence: number
  counterparty: string
}

export interface NewExpectedEvent {
  date: string
  amount_kop: number | string
  direction: 'in' | 'out'
  title: string
  counterparty: string
  source: EventSource
  confidence: number
}

export interface TaxCalendarResult {
  created: string[]
  source: string
  amount_kop: Kop
  amount_estimated: boolean
  kind?: TaxKind
}

export type TaxKind = 'enp' | 'usn' | 'vat'

export interface TaxCalendarRequest {
  kind: TaxKind
  amountKop?: string
  usnObject?: 'income' | 'income_expenses'
}

/** Demo «Прошла неделя» (docs/microservices.md): `enabled=false` for everyone except the demo user. */
export interface DemoStatus {
  enabled: boolean
  available?: boolean
  as_of?: string | null
  cutoff?: string
  next_from?: string | null
  weeks_left?: number
  ops_left?: number
  finished?: boolean
}

export interface DemoAdvance {
  added: number
  skipped_duplicates: number
  rows: number
  from: string
  to: string
  new_as_of: string
  previous_as_of: string
  weeks_left: number
  /** How long the server took to recompute the forecast after the import. */
  recalc_ms: number
  /** The previous forecast against the actual balance on the new date; null outside its horizon. */
  forecast_vs_fact: { date: string; p10: Kop; p50: Kop; p90: Kop; actual: Kop; error: Kop; within_band: boolean } | null
}

export interface SyncResult {
  created: number
  skipped: number
  sources?: unknown
}

export interface ImportStats {
  rows_ok: number
  date_from: string
  date_to: string
  days_span: number
  has_balance: boolean
  balance_mismatches?: number
  balance_mode?: string
  missing_period_count?: number
  first_gap_dates?: Array<[string, string]>
  suspected_duplicates?: number
  opening_balance_kop?: Kop
}

export interface ImportPreview {
  import_id: string
  status?: string
  summary: {
    stats: ImportStats
    errors_total: number
    errors: Array<{ row: number; code: string }>
    duplicates_existing: number
    categories: Record<string, number>
    sample: Array<{ date: string; amount_kop: Kop; counterparty: string; purpose: string; category: string }>
  }
}

export interface ImportRow {
  import_id: string
  filename: string
  status: string
  created_at: string
  rows_ok: number | null
  errors_total: number | null
  date_from: string | null
  date_to: string | null
}

// ---------- plan / fact ----------
export type PlanFactStatus = 'paid' | 'deviation' | 'missed' | 'pending'

export interface PlanFactItem {
  date: string
  title: string
  source: string
  plan: Kop
  fact: Kop | null
  fact_date: string | null
  diff: Kop | null
  status: PlanFactStatus | string
}

export interface PlanFactWeek {
  week_start: string
  plan_in: Kop
  plan_out: Kop
  fact_in: Kop
  fact_out: Kop
}

export interface PlanFact {
  as_of: string
  days?: number
  missed: number
  note: string
  weeks: PlanFactWeek[]
  items: PlanFactItem[]
}

// ---------- gov ----------
export interface KeyRate {
  rate: number
  previous_rate?: number | null
  as_of: string
  changed_on?: string
  source?: string
  stale?: boolean
  url?: string
  history?: Array<{ date: string; rate: number }>
}

// ---------- view models (display-only, computed from the API data) ----------
/** One point of the balance chart. Values are rubles (numbers) — for drawing only. */
export interface ForecastPoint {
  date: string
  /** Confirmed balance (only the opening point). */
  actual: number | null
  forecast: number | null
  lower?: number | null
  upper?: number | null
  inflow: number
  outflow: number
  /** Probability of a negative balance on this day (0..1). */
  probNegative?: number
}

export type ObligationKind = 'payment' | 'receipt'
export type ObligationStatus = 'planned' | 'expected' | 'completed' | 'overdue'

export interface Obligation {
  id: string
  kind: ObligationKind
  title: string
  date: string
  /** Signed kopecks. */
  amount: Kop
  source: string
  status: ObligationStatus
  /** Present for rows that came from plan/fact. */
  fact?: Kop | null
  factDate?: string | null
  diff?: Kop | null
}

export interface AssistantMessage {
  id: string
  role: 'user' | 'assistant'
  /** Raw text typed by the user. */
  text?: string
  /** Assistant answers are keys + neutral params so they re-translate on language change. */
  parts?: Array<{ key: string; params?: import('../i18n/helpers').MsgParams }>
}

export interface AccountSignature {
  id: string
  label: string
  created_at: number
}

/** Ways to sign in. At least one is always left. */
export interface AccountMethods {
  email: string | null
  signatures: AccountSignature[]
}

export interface ApiTokenInfo {
  id: string
  name: string
  prefix: string
  created_at: number
  last_used: number | null
  /** Только в списке директора (`scope=company`): владелец токена. */
  owner_email?: string | null
  user_id?: string | null
  scope?: 'read' | 'write' | string
  mine?: boolean
}

/** `token` is returned once, at creation. */
export interface NewApiToken extends ApiTokenInfo {
  token: string
}

/** Telegram notifications (service `notifier`). Booleans in prefs come as 0/1. */
export interface TgChat {
  chat_id: number
  kind: 'private' | 'group'
  title: string
  linked_at: number
  department_id: number | null
}

export interface TgPrefs {
  tz: string
  risk_enabled: number
  risk_dow: number
  risk_time: string
  week_enabled: number
  week_dow: number
  week_time: string
  month_enabled: number
  month_day: number
  month_time: string
  gap_enabled: number
  gap_days: number
  gap_threshold: number
  /** Admin allows sending company statistics to the cloud advisor (Hermes). */
  ai_consent: number
}

export interface TgStatus {
  configured: boolean
  /** Hermes advisor is connected (otherwise reports use rules only). */
  advisor: boolean
  bot_username: string
  chats: TgChat[]
  prefs: TgPrefs
}

export interface TgLink {
  code: string
  expires_in: number
  link: string
  group_link: string
}

export type GoalStatus = 'open' | 'done' | 'partial' | 'failed' | 'skipped'

/** Weekly (sprint) goal. `target_kop` is a plain number in kopecks, not a decimal string. */
export interface Goal {
  id: number
  week_start: string
  kind: string
  title: string
  target_kop: number | null
  due: string | null
  status: GoalStatus
  source: 'auto' | 'user'
}

export interface Retro {
  week_start: string
  mood: number | null
  blockers: string
  note: string
}

export interface SprintView {
  today: string
  sprint: string | null
  goals: Goal[]
  prev_sprint: string | null
  prev_goals: Goal[]
  velocity: number | null
  retro: Retro | null
}

export interface SprintHistoryItem {
  week_start: string
  velocity: number | null
  done: number
  total: number
  retro: Retro | null
}

export type TgRole = 'viewer' | 'rater' | 'admin'
export type TgKind = 'risk' | 'week' | 'month' | 'gap'

export interface Department {
  id: number
  name: string
  kinds: TgKind[]
  allow_excel: number
  default_role: TgRole
}

export interface TgMember {
  tg_user_id: number
  name: string
  /** null — role comes from the department policy. */
  role: TgRole | null
  department_id: number | null
}

export interface TgTeam {
  roles: TgRole[]
  kinds: TgKind[]
  departments: Department[]
  chats: TgChat[]
  members: TgMember[]
}

export interface TeamInvite {
  id: string
  role: Exclude<Role, 'director'>
  email: string | null
  created_at: number
  expires_at: number
}

export interface NewInvite {
  code: string
  role: Exclude<Role, 'director'>
  email: string | null
  /** The invitation e-mail was accepted by the SMTP server; otherwise the link must be sent by hand. */
  emailed: boolean
  /** The hourly e-mail limit of the company was reached. */
  email_limited?: boolean
  expires_at: number
}

export interface AuditItem {
  id: number
  at: string
  actor: string
  /** E-mail or certificate name of a web user; absent for Telegram (`tg:<id>`) and system actors. */
  actor_name?: string
  action: string
  object_id: string | null
  outcome: string
}

export interface AuditPage {
  items: AuditItem[]
  total: number
  limit: number
  offset: number
}
