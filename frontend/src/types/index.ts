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
/** Быстрый вход в демо-аккаунты (GET /auth/demo): без паролей; `enabled=false` — блок на странице входа не показывается. */
export interface DemoLoginAccount {
  id: 'director' | 'editor' | 'viewer'
  role: string
  email: string
}
export interface DemoLoginStatus {
  enabled: boolean
  accounts: DemoLoginAccount[]
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
  /** Почта подтверждена по ссылке из письма. Без этого рассылки не приходят. Нет поля — считаем подтверждённой. */
  email_verified?: boolean
}

/** Свои почтовые подписки: alerts — разрыв и «риск снизился», reports — риск-отчёт, итоги недели и месяца.
 * Расписание и порог общие для компании (блок «Расписание уведомлений»). */
export interface NotificationPrefs {
  alerts: boolean
  reports: boolean
  email: string | null
  email_verified: boolean
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
  /** Приглашение со ссылкой «задать пароль» ушло на почту сотрудника. */
  invite_sent?: boolean
}

/** Сброс пароля директором: ссылка ушла на почту либо (если почта недоступна) новый пароль. */
export interface MemberResetResult {
  email_sent: boolean
  email?: string | null
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
  /** `cash_gap`: уровень и вклад статей. */
  tier?: 'warning' | 'critical' | null
  drivers?: { title: string; share: number; amount: Kop }[]
  /** `risky_client`: крупный клиент в зоне риска по данным ФНС. */
  client?: { counterparty: string; risk_level: string; status: string | null; share: number; excluded: boolean } | null
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
  /** Step-by-step calculation log (absent in runs made before it existed). */
  trace?: Array<{ step: number; title: string; detail: string }>
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
  /** How the model was obtained: full refit or incremental update with new data. */
  update?: ForecastUpdate
}

export type ForecastUpdateMode = 'full' | 'incremental'

export interface ForecastUpdate {
  mode: ForecastUpdateMode
  /** full: mode_full | forced | first | reset | backdated | expired | grown; incremental: events_only | new_operations */
  reason: string
  /** Operations appended since the last full refit. */
  ops_added: number
  full_fit_at: string
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
  /** MSE, ₽² */
  mse_model_rub2?: number
  rmse_model?: Kop
  /** MAE as % of the company's median weekly turnover */
  mae_pct_turnover?: number
  n: number
}

export interface Backtest {
  origins: number
  alarm_precision: number | null
  alarm_recall: number | null
  actual_gap_windows: number
  /** confusion matrix of the gap alarm (P ≥ alarm_threshold) against an actual gap in 30 days */
  alarm_tp?: number
  alarm_fp?: number
  alarm_fn?: number
  alarm_tn?: number
  alarm_accuracy?: number | null
  alarm_f1?: number | null
  brier?: number | null
  mean_probability?: number | null
  gap_rate?: number | null
  alarm_threshold?: number
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

export type EventSource = 'manual' | 'invoice' | 'contract' | 'tax' | 'onec'

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

/** Result of the demo "step back": the last fed week is removed and the forecast is recomputed. */
export interface DemoRewind {
  removed: number
  from: string | null
  to: string | null
  new_as_of: string | null
  previous_as_of: string
  recalc_ms: number
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
  /** откуда пришли данные 1С: имитатор или подключённая настоящая 1С (Настройки → Интеграции) */
  onec?: 'sim' | 'live' | null
  sources?: { onec?: { status: string; error?: string | null; code?: string }; edo?: { status: string }; [k: string]: unknown }
  /** проверка контрагентов с ИНН (счета 1С) в ФНС сразу после синхронизации; null — проверять было некого */
  fns?: { checked: number; flagged?: number; saved?: boolean; error?: string } | null
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
  /** каким ботом привязан чат: shared — общий бот платформы, own — бот компании */
  via?: 'shared' | 'own'
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
  /** Alert about missed planned payments (plan-fact), once per payment. */
  missed_enabled: number
  /** Ignore missed payments below this amount, kopecks. */
  missed_min_kop: number
}

export interface TgStatus {
  configured: boolean
  /** какой бот ведёт компанию: own — свой (токен директора), shared — общий бот платформы, none — бота нет */
  bot_kind?: 'own' | 'shared' | 'none'
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
export type TgKind = 'risk' | 'week' | 'month' | 'gap' | 'missed'

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

// ----- ФНС: ЕНС, налоговые события, проверка контрагентов -----
export type FnsRisk = 'low' | 'medium' | 'high' | 'unknown'

export interface FnsUpcoming {
  kind: string
  date: string
  amount_kop: Kop | null
  title: string | null
}

export interface FnsEns {
  balance_kop: Kop | null
  debt_kop: Kop
  has_debt: boolean
  upcoming: FnsUpcoming[]
  checked_at: string | null
  /** `import` — загруженная справка о состоянии расчётов */
  source: string
  source_label: string
}

export interface FnsEnsImportResult extends FnsEns {
  format: 'knd_1160080' | 'csv'
  warnings: string[]
}

export interface FnsCounterparty {
  inn: string
  name: string | null
  income_kop: Kop | null
  income_share: number | null
  tx_count: number | null
  checked: boolean
  status: string
  risk_level: FnsRisk
  tax_debt_kop?: Kop
  has_tax_debt?: boolean
  bankruptcy_flag?: boolean
  unreliable?: boolean
  mass_address?: boolean
  disqualified?: boolean
  registered_at?: string | null
  /** дата, на которую ФНС опубликовала сведения о задолженности */
  tax_debt_as_of?: string | null
  checked_at: string | null
  source: string | null
  /** причины риска по настройкам компании: tax_debt_large, liquidated, not_in_registry, ... */
  risk_reasons?: string[]
  /** состояние источников: ok, off (выключен в настройках) или unavailable:<причина> */
  sources?: Record<string, string>
  stale?: boolean
  /** откуда контрагент: выписка и/или ожидаемые события (счета и платёжки 1С) */
  origin?: ('statement' | 'expected' | 'onec')[]
  expected_in_kop?: Kop
  expected_out_kop?: Kop
  /** доля в ожидаемых поступлениях */
  expected_share?: number
}

export interface FnsCounterparties {
  items: FnsCounterparty[]
  checked_at: string
  source: string | null
  source_label: string
  /** сведения выгружены из открытых источников ФНС */
  exported_from: 'fns'
  /** дата, на которую ФНС опубликовала набор о задолженности */
  debt_as_of?: string | null
  saved: boolean
  total_income_kop: Kop | null
  settings?: FnsSettings
}

export interface FnsSettings {
  sources: { egrul: boolean; data_nalog: boolean; fedresurs: boolean }
  risk: {
    bankruptcy: boolean
    liquidation: boolean
    not_in_registry: boolean
    unreliable: boolean
    disqualified: boolean
    tax_debt: boolean
    mass_address: boolean
    large_debt_kop: number
  }
  show_statement: boolean
  show_expected: boolean
  top: number
  use_in_forecast: boolean
  check_after_sync: boolean
}

export interface FnsSettingsView {
  settings: FnsSettings
  defaults: FnsSettings
  updated_at: string | null
  info: { fedresurs_configured: boolean; debt_as_of: string | null; debt_state: string } | null
  sources_info: Record<string, string>
}

/** Настройки → Интеграции: подключения компании. Пароля в ответах нет; адрес и логин видит только владелец. */
export type IntegrationStatus = 'not_connected' | 'unchecked' | 'ok' | 'error' | 'sim'

export interface IntegrationItem {
  kind: 'onec' | 'edo' | 'bank' | 'telegram'
  connected: boolean
  /** sim/live — 1С; own/shared — Telegram (свой бот компании или общий бот платформы) */
  source: 'sim' | 'live' | 'own' | 'shared'
  status: IntegrationStatus
  last_check_at: string | null
  last_error: string | null
  last_sync_at: string | null
  url?: string
  username?: string
  password_set?: boolean
  /** Telegram: имя бота и маска токена (только для manage); сам токен сервер не отдаёт никогда */
  bot_username?: string
  token_mask?: string
}

/** Результат проверки подключения 1С; `code` — причина (bad_credentials, odata_not_published, url_forbidden, timeout…). */
export interface IntegrationCheck {
  ok: boolean
  code: string
  message: string
  documents?: number
  more?: boolean
  /** Telegram: имя бота из getMe */
  bot_username?: string
  optional_missing?: string[]
  missing?: string[]
}

export interface IntegrationsView {
  can_manage: boolean
  /** задан ли на сервере ключ шифрования паролей; null для тех, кто не управляет подключениями */
  key_configured: boolean | null
  items: IntegrationItem[]
  check?: IntegrationCheck | null
  recorded?: boolean
}

export interface OnecConnectionInput {
  url: string
  username: string
  password?: string
}

export interface FnsSyncResult {
  created: number
  skipped: number
  estimated: number
  no_amount: number
  year: number
  regime: string
  regime_source: 'request' | 'statement' | 'default'
  source: string
  source_label: string
  fetched_at: string
  items: { kind: string; title: string; date: string; amount_kop: Kop; estimated: boolean }[]
  debt_kop: Kop | null
}
