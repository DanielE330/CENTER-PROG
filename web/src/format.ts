const rub = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });

export function money(kop: number | null | undefined): string {
  if (kop === null || kop === undefined) return "—";
  return `${rub.format(Math.round(kop / 100))} ₽`;
}

export function shortMoney(kop: number): string {
  const r = kop / 100;
  const abs = Math.abs(r);
  if (abs >= 1e6) return `${(r / 1e6).toFixed(1)} млн`;
  if (abs >= 1e3) return `${Math.round(r / 1e3)} тыс`;
  return `${Math.round(r)}`;
}

export function day(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}

export function dayShort(iso: string): string {
  const [, m, d] = iso.split("-");
  return `${d}.${m}`;
}

/** Probability in words first: an owner reads "скорее всего" faster than "64%". */
export function likelihood(p: number): string {
  if (p >= 0.8) return "почти наверняка";
  if (p >= 0.5) return "скорее всего";
  if (p >= 0.3) return "вполне возможно";
  return "есть риск";
}

export const GROUP_NAMES: Record<string, string> = {
  revenue: "Выручка",
  purchases: "Закупки и услуги",
  payroll: "Зарплата",
  taxes: "Налоги и взносы",
  rent_utilities: "Аренда и коммуналка",
  loans: "Кредиты и лизинг",
  own_funds: "Свои средства",
  refunds: "Возвраты",
  bank_fees: "Комиссии банка",
  other: "Прочее",
};

export const TAX_REGIMES: Record<string, string> = {
  usn_income: "УСН «доходы»",
  usn_income_expense: "УСН «доходы минус расходы»",
  osno: "ОСНО",
  eshn: "ЕСХН",
  patent: "Патент",
};

export const WARNINGS: Record<string, string> = {
  short_history: "История короче трёх месяцев: прогноз грубый, коридор широкий.",
  no_history: "Операций пока нет: прогноз строится только по будущим платежам.",
  stale_data: "Выписка давно не обновлялась: загрузите свежую.",
};
