export type Item = { date: string; amount: string; title: string; source: string; overdue: boolean };
export type Warning = { code: string; severity: "high" | "medium" | "low"; date: string | null; message: string; reasons: Item[] };
export type Day = { date: string; p10: string; p50: string; p90: string; prob_negative: number; prob_broken_by: number };
export type Risk = {
  level: "none" | "low" | "medium" | "high"; shortfall_probability: number; first_break_date: string | null;
  possible_break_date: string | null; median_path_deficit: string; max_deficit_p90: string; deficit_given_break_median: string; warnings: Warning[];
};
export type Rule = { key: string; counterparty: string; category: string; direction: string; kind: string; anchors: number[]; amount: string; occurrences: number; confidence: number };
export type Result = {
  as_of: string; horizon: number; opening_balance: string; model_version: string; seed: number; n_paths: number;
  quality: { level: string; history_days: number; notes: string[] }; days: Day[]; risk: Risk; rules: Rule[]; planned: Item[];
};
export type Run = { run_id: string; created_at: string; stale: boolean; scenario: unknown; result: Result };
export type Tx = { id: number; date: string; amount_kop: string; counterparty: string; purpose: string; category: string; category_confidence: number; manual: boolean };
export type Ev = { id: string; date: string; amount: string; title: string; source: string; confidence: number; counterparty: string };
