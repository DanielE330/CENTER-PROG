// API client. The session lives in an HttpOnly cookie set by the gateway;
// tokens never reach the browser. Mutations carry a custom header (CSRF guard).

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function login(): never {
  const back = window.location.pathname + window.location.search;
  window.location.assign(`/api/v1/auth/login?return_to=${encodeURIComponent(back)}`);
  throw new ApiError(401, "redirecting to login");
}

async function request<T>(method: string, path: string, body?: unknown, raw?: BodyInit): Promise<T> {
  const headers: Record<string, string> = { accept: "application/json" };
  if (method !== "GET") headers["x-ci1-csrf"] = "1";
  if (body !== undefined) headers["content-type"] = "application/json";
  const resp = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: raw ?? (body !== undefined ? JSON.stringify(body) : undefined),
  });
  if (resp.status === 401) login();
  if (!resp.ok) {
    let message = resp.statusText;
    try {
      message = (await resp.json()).error ?? (await resp.text());
    } catch {
      /* keep status text */
    }
    throw new ApiError(resp.status, message);
  }
  return (await resp.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
  upload: <T>(path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<T>("POST", path, undefined, form);
  },
  logout: async () => {
    const r = await request<{ logout_url: string }>("POST", "/api/v1/auth/logout", {});
    window.location.assign(r.logout_url);
  },
};

export interface Me {
  sub: string;
  email?: string;
  name?: string;
  tenant_id: string;
  organization?: string;
  roles: string[];
}

export interface Point {
  date: string;
  p10_kop: number;
  p50_kop: number;
  p90_kop: number;
  inflow_kop: number;
  outflow_kop: number;
}

export interface Driver {
  date: string;
  label: string;
  amount_kop: number;
}

export interface Gap {
  start_date: string;
  end_date: string;
  level: "warning" | "critical";
  probability: number;
  shortfall_p50_kop: number;
  shortfall_p90_kop: number;
  drivers: Driver[];
}

export interface Forecast {
  run_id?: string;
  as_of: string;
  horizon: number;
  opening_kop: number;
  model: string;
  warnings: string[];
  points: Point[];
  gaps: Gap[];
  timing_ms?: number;
  refit?: boolean;
  sources?: { transactions: number; obligations: Record<string, number>; registry_flags: number };
  patterns?: { label: string; day_of_month: number; amount_kop: number; subcategory: string }[];
}

export interface Alert {
  id: string;
  kind: string;
  severity: "info" | "warning" | "high" | "critical";
  title: string;
  body: string;
  status: string;
  updated_at: string;
}

export interface Transaction {
  id: string;
  op_date: string;
  amount_kop: number;
  counterparty_name?: string;
  counterparty_ref?: string;
  purpose?: string;
  balance_eod_kop: number;
  category_id?: string;
  category_group?: string;
}

export interface Category {
  id: string;
  name: string;
  group: string;
  archived: boolean;
}

export interface Obligation {
  id: string;
  kind: string;
  direction: "inflow" | "outflow";
  amount_kop: number;
  due_date: string;
  certainty: number;
  source: string;
  status: string;
  note?: string;
  doc_no?: string;
  doc_date?: string;
}

export interface Settings {
  id: string;
  name: string;
  tax_regime?: string;
  tax_regime_suggested?: string;
  tax_regime_confidence?: number;
  as_of: string;
  overdraft_limit_kop: number;
  min_balance_kop: number;
  llm_policy: { provider: string; external_allowed: boolean; masking: boolean };
  demo_company?: string;
}
