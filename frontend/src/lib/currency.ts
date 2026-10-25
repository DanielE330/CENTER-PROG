/**
 * Display currency. The ledger, the forecast and every API field stay in rubles (kopecks);
 * the user picks a currency to SHOW amounts in. Conversion happens on the client at the CBR rate
 * of the current date (`GET /api/v1/gov/rates`): amount_in_currency = rubles / (value / nominal).
 * It is a display feature, not accounting. Amount INPUT stays in rubles everywhere.
 *
 * The module is a tiny external store (like lib/theme.ts): formatters read it at call time, and
 * `useCurrencyVersion()` re-renders whoever needs to follow a change (the app layout re-keys the page with it).
 */
import { useSyncExternalStore } from 'react'
import { loadPrefs, savePrefs } from './prefs'

export const CURRENCY_CODES = ['RUB', 'USD', 'EUR', 'CNY', 'GBP', 'KZT', 'BYN', 'TRY', 'AED'] as const
export type CurrencyCode = (typeof CURRENCY_CODES)[number]

/** Symbols shown instead of «₽»; the code is the fallback where there is no common sign. */
export const CURRENCY_SYMBOLS: Record<CurrencyCode, string> = {
  RUB: '₽', USD: '$', EUR: '€', CNY: '¥', GBP: '£', KZT: '₸', BYN: 'Br', TRY: '₺', AED: 'AED',
}

export function isCurrencyCode(v: unknown): v is CurrencyCode {
  return typeof v === 'string' && (CURRENCY_CODES as readonly string[]).includes(v)
}

/** `value` rubles per `nominal` units of the currency, as published by the CBR (nominal is 1, 10 or 100). */
export interface RateEntry {
  nominal: number
  value: number
}

/** Response of `GET /api/v1/gov/rates`. */
export interface RatesPayload {
  /** ISO date of the rates; null for the approximate fallback set. */
  date: string | null
  source: string
  rates: Partial<Record<CurrencyCode, RateEntry>>
  stale?: boolean
  /** Approximate built-in rates: the CBR and the cache were unavailable. */
  approx?: boolean
}

const RATES_KEY = 'ci.fx'

function validRate(e: unknown): e is RateEntry {
  const r = e as RateEntry | null
  return !!r && Number.isFinite(r.nominal) && Number.isFinite(r.value) && r.nominal > 0 && r.value > 0
}

/** Keeps only well-formed entries of supported currencies; null when nothing usable is left. */
export function normalizeRates(p: unknown): RatesPayload | null {
  const raw = p as Partial<RatesPayload> | null
  if (!raw || typeof raw !== 'object' || !raw.rates || typeof raw.rates !== 'object') return null
  const rates: RatesPayload['rates'] = {}
  for (const c of CURRENCY_CODES) {
    const e = (raw.rates as Record<string, unknown>)[c]
    if (c !== 'RUB' && validRate(e)) rates[c] = { nominal: e.nominal, value: e.value }
  }
  if (Object.keys(rates).length === 0) return null
  return {
    date: typeof raw.date === 'string' ? raw.date : null,
    source: typeof raw.source === 'string' ? raw.source : 'cbr.ru',
    rates,
    stale: raw.stale === true,
    approx: raw.approx === true,
  }
}

function readRates(): RatesPayload | null {
  try {
    return normalizeRates(JSON.parse(localStorage.getItem(RATES_KEY) ?? 'null'))
  } catch {
    return null
  }
}

interface State {
  /** The user's choice. */
  selected: CurrencyCode
  rates: RatesPayload | null
}

let state: State = {
  selected: isCurrencyCode(loadPrefs().currency) ? loadPrefs().currency as CurrencyCode : 'RUB',
  rates: readRates(),
}
const listeners = new Set<() => void>()

function emit() {
  listeners.forEach((l) => l())
}

/** Rubles per ONE unit of the currency; null when no usable rate is known. */
export function rubPerUnit(code: CurrencyCode, rates: RatesPayload | null = state.rates): number | null {
  if (code === 'RUB') return 1
  const e = rates?.rates[code]
  return e && validRate(e) ? e.value / e.nominal : null
}

/** The currency amounts are really shown in: the selected one, or RUB while its rate is not available. */
export function activeCurrency(): CurrencyCode {
  return rubPerUnit(state.selected) == null ? 'RUB' : state.selected
}

export function selectedCurrency(): CurrencyCode {
  return state.selected
}

export function getRates(): RatesPayload | null {
  return state.rates
}

/** Rubles (a number, for display and charts) -> units of the active currency. */
export function rubToDisplay(rub: number): number {
  const per = rubPerUnit(activeCurrency())
  return per == null ? rub : rub / per
}

/** Kopecks -> units of the active currency. Floating point is fine: this is display only. */
export function kopToDisplay(kop: string): number {
  return rubToDisplay(Number(kop) / 100)
}

/** Chart data: rubles -> the active currency, so axes pick round ticks in the displayed unit. */
export const chartRub = rubToDisplay

export function setCurrency(code: CurrencyCode) {
  if (code === state.selected) return
  state = { ...state, selected: code }
  savePrefs({ currency: code })
  emit()
}

export function setRates(next: RatesPayload | null) {
  const rates = normalizeRates(next)
  if (!rates) return
  const cur = state.rates
  if (cur && JSON.stringify(cur) === JSON.stringify(rates)) return
  state = { ...state, rates }
  try {
    localStorage.setItem(RATES_KEY, JSON.stringify(rates))
  } catch {
    /* storage unavailable: rates live until reload */
  }
  emit()
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

/** A string that changes whenever displayed amounts change (currency or rates). Use as a React key / memo dependency. */
export function currencyVersion(): string {
  const active = activeCurrency()
  // In rubles the rates do not affect any displayed amount.
  return active === 'RUB' ? 'RUB' : `${active}|${rubPerUnit(active)}`
}

export function useCurrencyVersion(): string {
  return useSyncExternalStore(subscribe, currencyVersion)
}

export interface CurrencyView {
  selected: CurrencyCode
  active: CurrencyCode
  rates: RatesPayload | null
}

let viewCache: { v: string; view: CurrencyView } | null = null
function getView(): CurrencyView {
  const v = `${state.selected}|${currencyVersion()}`
  if (!viewCache || viewCache.v !== v) viewCache = { v, view: { selected: state.selected, active: activeCurrency(), rates: state.rates } }
  return viewCache.view
}

export function useCurrency(): CurrencyView {
  return useSyncExternalStore(subscribe, getView)
}
