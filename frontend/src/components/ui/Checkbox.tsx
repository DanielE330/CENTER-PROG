import type { InputHTMLAttributes, ReactNode } from 'react'
import { cn } from '../../lib/utils'

interface Props extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label: ReactNode
}

/** Native checkbox (styled in globals.css) inside a label, so clicking the text toggles it. */
export function Checkbox({ label, className, disabled, ...rest }: Props) {
  return (
    <label className={cn('inline-flex items-center gap-2.5 text-sm', disabled ? 'cursor-not-allowed text-muted' : 'cursor-pointer', className)}>
      <input type="checkbox" disabled={disabled} {...rest} />
      <span>{label}</span>
    </label>
  )
}
