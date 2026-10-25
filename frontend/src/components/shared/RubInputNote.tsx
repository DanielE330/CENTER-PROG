import { useTranslation } from 'react-i18next'
import { useCurrency } from '../../lib/currency'
import { formatKop } from '../../lib/formatters'
import { parseRubToKop } from '../../lib/money'

/**
 * Hint under an amount input: inputs are always in rubles, even when the display currency is another one.
 * Renders nothing in rubles. With a typed value it adds the equivalent in the display currency.
 */
export function RubInputNote({ value }: { value?: string | number }) {
  const { t } = useTranslation()
  const { active } = useCurrency()
  if (active === 'RUB') return null
  const kop = value == null ? null : parseRubToKop(String(value))
  return (
    <span>
      {t('currency.inputRub')}
      {kop && ` ≈ ${formatKop(kop)}`}
    </span>
  )
}
