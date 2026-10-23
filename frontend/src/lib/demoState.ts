import { useSyncExternalStore } from 'react'
import type { DemoState } from '../types'

/** Lets reviewers force loading / error / empty / incomplete states while no backend exists. */
const KEY = 'ci.demoState'
const listeners = new Set<() => void>()
let current: DemoState = 'normal'
try {
  const saved = localStorage.getItem(KEY) as DemoState | null
  if (saved) current = saved
} catch {
  /* storage unavailable */
}

export const getDemoState = () => current

export function setDemoState(next: DemoState) {
  current = next
  try {
    localStorage.setItem(KEY, next)
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l())
}

export function useDemoState(): DemoState {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
    getDemoState,
  )
}
