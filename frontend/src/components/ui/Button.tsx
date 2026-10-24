import type { ButtonHTMLAttributes } from 'react'
import { cn } from '../../lib/utils'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'md' | 'sm'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
}

const variants: Record<Variant, string> = {
  primary: 'bg-primary text-on-primary enabled:hover:bg-primary-hover border-transparent',
  secondary: 'bg-surface text-ink border-line enabled:hover:bg-hover',
  ghost: 'bg-transparent text-ink border-transparent enabled:hover:bg-hover',
  danger: 'bg-surface text-danger border-line enabled:hover:bg-danger-soft',
}

export function Button({ variant = 'secondary', size = 'md', className, type = 'button', ...rest }: Props) {
  return (
    <button
      type={type}
      className={cn(
        'inline-flex items-center justify-center gap-2 border font-medium whitespace-nowrap cursor-pointer',
        'rounded-control disabled:opacity-50 disabled:cursor-not-allowed',
        size === 'md' ? 'h-10 px-4 text-sm' : 'h-8 px-3 text-[13px]',
        variants[variant],
        className,
      )}
      {...rest}
    />
  )
}
