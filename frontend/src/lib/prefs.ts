const KEY = 'ci.prefs'

export const HORIZONS = [7, 14, 30, 60, 90] as const

export interface Prefs {
  /** Forecast horizon (days) used when a forecast is built after an import. */
  horizon: number
  /** Display currency code (amounts are stored in rubles, shown converted at the CBR rate). */
  currency: string
}

const DEFAULTS: Prefs = { horizon: 90, currency: 'RUB' }

export function loadPrefs(): Prefs {
  try {
    const p = JSON.parse(localStorage.getItem(KEY) ?? '{}')
    return {
      horizon: (HORIZONS as readonly number[]).includes(p.horizon) ? p.horizon : DEFAULTS.horizon,
      currency: typeof p.currency === 'string' && /^[A-Z]{3}$/.test(p.currency) ? p.currency : DEFAULTS.currency,
    }
  } catch {
    return { ...DEFAULTS }
  }
}

/** Merges into the stored prefs, so saving one setting never drops another. */
export function savePrefs(p: Partial<Prefs>) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...loadPrefs(), ...p }))
  } catch {
    /* storage unavailable: the choice applies until reload */
  }
}
