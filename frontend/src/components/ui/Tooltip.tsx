import type { ReactNode } from 'react'

/** CSS-only tooltip; also reachable by keyboard focus. */
/** `align="end"`: the bubble's right edge meets the trigger's (for triggers at the right edge of the screen). */
export function Tooltip({ text, children, align = 'center' }: { text: string; children: ReactNode; align?: 'center' | 'end' }) {
  return (
    <span className="relative inline-flex group" tabIndex={0}>
      {children}
      <span
        role="tooltip"
        className={`${align === 'end' ? 'right-0' : 'left-1/2 -translate-x-1/2'} pointer-events-none absolute top-full z-30 mt-1.5 w-max max-w-64 rounded-md bg-ink px-2.5 py-1.5 text-xs font-normal text-surface opacity-0 transition-opacity group-hover:opacity-100 group-focus:opacity-100`}
      >
        {text}
      </span>
    </span>
  )
}
