import { useEffect, useMemo, useRef } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import { useTranslation } from 'react-i18next'
import { formatAxisNumber, formatCurrency, formatShortDate } from '../../lib/formatters'
import { cssVar, prefersReducedMotion } from '../../lib/utils'
import { useTheme } from '../../lib/theme'

export interface BarSeries {
  name: string
  token: 'primary' | 'danger' | 'warning' | 'neutral'
  /** Lighter fill, e.g. for "plan" next to "fact". */
  soft?: boolean
  /** Rubles. Display-only. */
  data: number[]
  stack?: string
}

const TOKENS = { primary: '--c-primary', danger: '--c-danger', warning: '--c-warning', neutral: '--c-neutral-series' } as const

/** Grouped / stacked bars by week. Receives ready-made numbers — no financial calculations here. */
export function BarChart({ categories, series, height = 300, ariaLabel, weekPrefix = false }: { categories: string[]; series: BarSeries[]; height?: number; ariaLabel: string; weekPrefix?: boolean }) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const theme = useTheme()
  // A theme switch only recolours the chart: no data morphing, so nothing moves while the page cross-fades.
  const lastTheme = useRef(theme)
  const themeSwitched = lastTheme.current !== theme
  useEffect(() => {
    lastTheme.current = theme
  }, [theme])
  const option = useMemo<EChartsOption>(() => {
    const muted = cssVar('--c-muted', '#66716A')
    const line = cssVar('--c-line', '#E2E8E3')
    const surface = cssVar('--c-surface', '#FFFFFF')
    const ink = cssVar('--c-ink', '#202923')
    const label = (v: string) => (weekPrefix ? `${t('common.weekShort')} ${formatShortDate(v)}` : formatShortDate(v))
    return {
      animation: !prefersReducedMotion(),
      animationDuration: 600,
      animationDurationUpdate: themeSwitched ? 0 : 450,
      animationEasing: 'cubicOut',
      grid: { left: 8, right: 16, top: 40, bottom: 8, containLabel: true },
      legend: { top: 0, left: 0, icon: 'roundRect', itemWidth: 14, itemHeight: 8, textStyle: { color: muted, fontSize: 12 } },
      tooltip: {
        trigger: 'axis', confine: true, backgroundColor: surface, borderColor: line, textStyle: { color: ink, fontSize: 12 },
        valueFormatter: (v) => formatCurrency(v as number),
        axisPointer: { type: 'shadow' },
      },
      xAxis: { type: 'category', data: categories, axisTick: { show: false }, axisLine: { lineStyle: { color: line } }, axisLabel: { color: muted, fontSize: 12, hideOverlap: true, formatter: label } },
      yAxis: { type: 'value', axisLabel: { color: muted, fontSize: 12, formatter: (v: number) => formatAxisNumber(v) }, splitLine: { lineStyle: { color: line } } },
      series: series.map((s) => ({
        id: s.name, name: s.name, type: 'bar' as const, stack: s.stack, data: s.data,
        itemStyle: { color: cssVar(TOKENS[s.token], '#176B45'), opacity: s.soft ? 0.45 : 1, borderRadius: [3, 3, 0, 0] },
      })),
    }
  }, [categories, series, theme, lang, t, weekPrefix])
  return (
    <div role="img" aria-label={ariaLabel} style={{ width: '100%', height }}>
      <ReactECharts option={option} style={{ width: '100%', height: '100%' }} />
    </div>
  )
}
