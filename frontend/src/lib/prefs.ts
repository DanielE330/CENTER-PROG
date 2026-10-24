const KEY = 'ci.prefs'

export const HORIZONS = [7, 14, 30, 60, 90] as const

export interface Prefs {
  /** Forecast horizon (days) used when a forecast is built after an import. */
  horizon: number
}

export function loadPrefs(): Prefs {
  try {
    const p = JSON.parse(localStorage.getItem(KEY) ?? '{}')
    return { horizon: (HORIZONS as readonly number[]).includes(p.horizon) ? p.horizon : 90 }
  } catch {
    return { horizon: 90 }
  }
}

export function savePrefs(p: Prefs) {
  try {
    localStorage.setItem(KEY, JSON.stringify(p))
  } catch {
    /* storage unavailable: the choice applies until reload */
  }
}
