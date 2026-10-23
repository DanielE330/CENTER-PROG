import type { ReactNode } from 'react'

/** CSS-only tooltip; also reachable by keyboard focus. */
export function Tooltip({ text, children }: { text: string; children: ReactNode }) {
  return (
    <span className="relative inline-flex group" tabIndex={0}>
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute left-1/2 top-full z-30 mt-1.5 w-max max-w-64 -translate-x-1/2 rounded-md bg-ink px-2.5 py-1.5 text-xs font-normal text-surface opacity-0 transition-opacity group-hover:opacity-100 group-focus:opacity-100"
      >
        {text}
      </span>
    </span>
  )
}
