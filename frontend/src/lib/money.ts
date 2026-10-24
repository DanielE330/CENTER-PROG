import type { Kop } from '../types'

/** Rubles as a number — ONLY for drawing charts and for comparisons that do not need exact kopecks. */
export function kopToRub(kop: Kop): number {
  return Number(BigInt(kop) / 100n)
}

export const isNegative = (kop: Kop): boolean => BigInt(kop) < 0n
export const isOutgoing = isNegative

export function sumKop(values: Kop[]): Kop {
  return values.reduce((s, v) => s + BigInt(v), 0n).toString()
}

export function subKop(a: Kop, b: Kop): Kop {
  return (BigInt(a) - BigInt(b)).toString()
}

export function negKop(a: Kop): Kop {
  return (-BigInt(a)).toString()
}

export function absKop(a: Kop): Kop {
  const v = BigInt(a)
  return (v < 0n ? -v : v).toString()
}

/**
 * Parses a user-entered rubles amount ("12 345,67") to kopecks without float arithmetic.
 * Returns null for anything that is not a positive amount with at most two decimals.
 */
export function parseRubToKop(input: string): Kop | null {
  const t = input.replace(/[\s  ]/g, '').replace(',', '.')
  const m = /^(\d+)(?:\.(\d{1,2}))?$/.exec(t)
  if (!m) return null
  const kop = BigInt(m[1]) * 100n + BigInt((m[2] ?? '').padEnd(2, '0') || '0')
  return kop > 0n ? kop.toString() : null
}
