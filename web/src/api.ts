export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function req<T>(method: string, url: string, body?: unknown, form?: FormData): Promise<T> {
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: form ?? (body ? JSON.stringify(body) : undefined),
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data?.error?.code ?? "error", data?.error?.message ?? "Ошибка запроса");
  return data as T;
}

export const api = {
  get: <T,>(u: string) => req<T>("GET", u),
  post: <T,>(u: string, b?: unknown) => req<T>("POST", u, b ?? {}),
  del: (u: string) => req<void>("DELETE", u),
  patch: <T,>(u: string, b: unknown) => req<T>("PATCH", u, b),
  upload: <T,>(u: string, f: File) => {
    const fd = new FormData();
    fd.append("file", f);
    return req<T>("POST", u, undefined, fd);
  },
};

/** Суммы приходят строками в копейках. Форматируем через BigInt, без Number-арифметики. */
export function rub(kop: string | null | undefined, opts: { sign?: boolean } = {}): string {
  if (kop == null) return "—";
  const v = BigInt(kop);
  const neg = v < 0n;
  const abs = neg ? -v : v;
  const r = (abs + 50n) / 100n;
  const s = r.toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return `${neg ? "−" : opts.sign ? "+" : ""}${s} ₽`;
}

/** Только для графиков (отображение). */
export const chartRub = (kop: string) => Number(BigInt(kop) / 100n);

export const pct = (x: number) => `${Math.round(x * 100)}%`;
export const dateRu = (iso: string) => new Date(iso + "T00:00:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

export const CATEGORY_RU: Record<string, string> = {
  revenue: "Выручка", other_income: "Прочие поступления", suppliers: "Поставщики", payroll: "Зарплата", rent: "Аренда",
  taxes: "Налоги и взносы", loans: "Кредиты и займы", utilities: "Коммунальные и связь", bank_fees: "Банковские комиссии",
  marketing: "Маркетинг", transfers: "Внутренние переводы", other_expense: "Прочие расходы",
};
