import { useSyncExternalStore } from 'react'

export type Theme = 'light' | 'dark'

const KEY = 'ci.theme'
const listeners = new Set<() => void>()

/** Light is the default; only an explicit user choice (saved in localStorage) switches to dark. */
function read(): Theme {
  try {
    return localStorage.getItem(KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

let current: Theme = typeof document !== 'undefined' && document.documentElement.dataset.theme === 'dark' ? 'dark' : read()

export const getTheme = () => current

export function setTheme(next: Theme) {
  const root = document.documentElement
  root.classList.add('theme-transition')
  root.dataset.theme = next
  current = next
  try {
    localStorage.setItem(KEY, next)
  } catch {
    /* storage unavailable: theme still applies for this session */
  }
  listeners.forEach((l) => l())
  window.setTimeout(() => root.classList.remove('theme-transition'), 250)
}

export function useTheme(): Theme {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
    getTheme,
  )
}
