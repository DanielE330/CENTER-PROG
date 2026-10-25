import { useEffect, useMemo, useRef } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import type { ForecastPoint } from '../../types'
import { formatAxisNumber, formatChartCompact, formatChartMoney, formatDate, formatPercent, formatShortDate } from '../../lib/formatters'
import { chartRub, useCurrencyVersion } from '../../lib/currency'
import { cssVar, prefersReducedMotion } from '../../lib/utils'
import { useTranslation } from 'react-i18next'
import { useTheme } from '../../lib/theme'

export interface CompareSeries {
  name: string
  points: Array<{ date: string; balance: number | null }>
}

interface Props {
  /** Data to draw. The chart performs no financial calculations. */
  points: ForecastPoint[]
  today: string
  shortageDate?: string | null
  compare?: CompareSeries
  /** Label for the main forecast line. */
  forecastName?: string
  height?: number
}

export function ForecastChart({ points: rubPoints, today, shortageDate, compare, forecastName: forecastNameProp, height = 380 }: Props) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const theme = useTheme()
  // Data arrive in rubles; the chart draws in the display currency so axes pick round ticks in that unit.
  const currency = useCurrencyVersion()
  // A theme switch only recolours the chart: no data morphing, so nothing moves while the page cross-fades.
  const lastTheme = useRef(theme)
  const themeSwitched = lastTheme.current !== theme
  useEffect(() => {
    lastTheme.current = theme
  }, [theme])
  const forecastName = forecastNameProp ?? t('chart.forecast')
  const option = useMemo<EChartsOption>(() => {
    const primary = cssVar('--c-primary', '#176B45')
    const danger = cssVar('--c-danger', '#DC5252')
    const warning = cssVar('--c-warning', '#D97706')
    const muted = cssVar('--c-muted', '#66716A')
    const neutral = cssVar('--c-neutral-series', '#9AA59E')
    const line = cssVar('--c-line', '#E2E8E3')
    const ink = cssVar('--c-ink', '#202923')
    const surface = cssVar('--c-surface', '#FFFFFF')
    const onDanger = cssVar('--c-on-primary', '#FFFFFF')

    const scale = (v: number | null | undefined) => (v == null ? v : chartRub(v))
    const points: ForecastPoint[] = rubPoints.map((p) => ({ ...p, actual: scale(p.actual) ?? null, forecast: scale(p.forecast) ?? null, lower: scale(p.lower) ?? null, upper: scale(p.upper) ?? null, inflow: chartRub(p.inflow), outflow: chartRub(p.outflow) }))
    const animate = !prefersReducedMotion() && points.length <= 400
    const dates = points.map((p) => p.date)
    const compareByDate = new Map(compare?.points.map((c) => [c.date, c.balance == null ? null : chartRub(c.balance)]))
    const hasBand = points.some((p) => p.lower != null && p.upper != null)

    // Contiguous ranges where the supplied forecast is below zero.
    const deficitAreas: Array<[{ xAxis: string }, { xAxis: string }]> = []
    let start: string | null = null
    points.forEach((p, i) => {
      const neg = p.forecast != null && p.forecast < 0
      if (neg && start == null) start = p.date
      if ((!neg || i === points.length - 1) && start != null) {
        deficitAreas.push([{ xAxis: start }, { xAxis: neg ? p.date : points[i - 1].date }])
        start = null
      }
    })

    const shortagePoint = shortageDate ? points.find((p) => p.date === shortageDate) : undefined

    const series: NonNullable<EChartsOption['series']> = []

    // Every series is always present (empty when unused) and has a stable id: with merge updates
    // ECharts then morphs old data into new data, and nothing stale is left behind.
    series.push(
      {
        id: 'band-base', name: 'band-base', type: 'line', stack: 'band', symbol: 'none', silent: true,
        lineStyle: { opacity: 0 }, tooltip: { show: false },
        data: hasBand ? points.map((p) => (p.lower != null ? p.lower : null)) : [],
      },
      {
        id: 'band', name: t('chart.band'), type: 'line', stack: 'band', symbol: 'none', silent: true,
        lineStyle: { opacity: 0 }, areaStyle: { color: primary, opacity: 0.1 },
        data: hasBand ? points.map((p) => (p.lower != null && p.upper != null ? p.upper - p.lower : null)) : [],
      },
    )

    series.push({
      id: 'actual',
      name: t('chart.actual'),
      type: 'line',
      symbol: 'circle',
      symbolSize: 9,
      lineStyle: { color: neutral, width: 2 },
      itemStyle: { color: neutral },
      data: points.map((p) => p.actual),
    })

    series.push({
      id: 'compare',
      name: compare?.name ?? 'compare',
      type: 'line',
      symbol: 'none',
      lineStyle: { color: muted, width: 2, type: 'dashed' },
      itemStyle: { color: muted },
      data: compare ? points.map((p) => compareByDate.get(p.date) ?? null) : [],
    })

    series.push({
      id: 'forecast',
      name: forecastName,
      type: 'line',
      symbol: 'none',
      lineStyle: { color: primary, width: 2.5 },
      itemStyle: { color: primary },
      data: points.map((p) => p.forecast),
      markLine: {
        symbol: 'none',
        silent: true,
        data: [
          { yAxis: 0, lineStyle: { color: ink, type: 'dashed', width: 1 }, label: { formatter: formatChartMoney(0), color: ink, position: 'insideEndTop' } },
          ...(dates.includes(today)
            ? [{ xAxis: today, lineStyle: { color: muted, type: 'solid' as const, width: 1 }, label: { show: dates.indexOf(today) > 2, formatter: t('chart.today'), color: muted, position: 'insideEndTop' as const } }]
            : []),
        ],
      },
      markArea: { silent: true, itemStyle: { color: danger, opacity: 0.12 }, data: deficitAreas },
      markPoint: {
        symbolSize: 46,
        itemStyle: { color: danger },
        label: { formatter: '!', color: onDanger, fontWeight: 700, fontSize: 16 },
        data: shortagePoint?.forecast != null ? [{ name: t('chart.shortageDate'), coord: [shortagePoint.date, shortagePoint.forecast] }] : [],
      },
    })

    // Negative stretches are redrawn in red (plus neighbouring points so the segment connects).
    const neg = (i: number) => points[i]?.forecast != null && (points[i].forecast as number) < 0
    series.push({
      id: 'deficit',
      name: t('chart.deficit'),
      type: 'line',
      symbol: 'none',
      silent: true,
      z: 5,
      lineStyle: { color: danger, width: 2.5 },
      itemStyle: { color: danger },
      data: deficitAreas.length ? points.map((p, i) => (neg(i) || neg(i - 1) || neg(i + 1) ? p.forecast : null)) : [],
    })

    return {
      // Moderate, easing-based motion; skipped for reduced-motion users and very long series.
      animation: animate,
      animationDuration: 600,
      animationDurationUpdate: themeSwitched ? 0 : 450,
      animationEasing: 'cubicOut',
      animationEasingUpdate: 'cubicOut',
      color: [primary],
      grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
      legend: {
        top: 0, left: 0, icon: 'roundRect', itemWidth: 14, itemHeight: 3, textStyle: { color: muted, fontSize: 12 },
        data: [
          t('chart.actual'), forecastName,
          ...(deficitAreas.length ? [t('chart.deficit')] : []),
          ...(compare ? [compare.name] : []),
          ...(hasBand ? [t('chart.band')] : []),
        ],
      },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: line } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 12, formatter: (v: string) => formatShortDate(v), hideOverlap: true },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: muted, fontSize: 12, formatter: (v: number) => formatAxisNumber(v) },
        splitLine: { lineStyle: { color: line } },
      },
      tooltip: {
        trigger: 'axis',
        confine: true,
        backgroundColor: surface,
        borderColor: line,
        textStyle: { color: ink, fontSize: 12 },
        extraCssText: 'box-shadow:0 8px 24px rgba(0,0,0,.18);',
        axisPointer: { type: 'line', lineStyle: { color: neutral } },
        formatter: (params: unknown) => {
          const idx = (params as Array<{ dataIndex: number }>)[0]?.dataIndex ?? 0
          const p = points[idx]
          if (!p) return ''
          const isActual = p.actual != null
          const bal = isActual ? p.actual : p.forecast
          const cmp = compare ? compareByDate.get(p.date) : undefined
          const row = (k: string, v: string, color?: string) =>
            `<div style="display:flex;justify-content:space-between;gap:20px"><span style="color:${muted}">${k}</span><b style="font-variant-numeric:tabular-nums;${color ? `color:${color}` : ''}">${v}</b></div>`
          return [
            `<div style="min-width:210px"><div style="font-weight:600;margin-bottom:4px">${formatDate(p.date)} · ${isActual ? t('chart.tooltip.actual') : t('chart.tooltip.forecast')}</div>`,
            row(isActual ? t('chart.tooltip.balanceActual') : t('chart.tooltip.balanceOf', { name: forecastName.toLowerCase() }), bal != null ? formatChartMoney(bal) : t('common.noData'), bal != null && bal < 0 ? danger : undefined),
            cmp !== undefined ? row(compare!.name, cmp != null ? formatChartMoney(cmp) : t('common.noData')) : '',
            p.lower != null && p.upper != null && !isActual ? row(t('chart.tooltip.range'), `${formatChartCompact(p.lower)} … ${formatChartCompact(p.upper)}`) : '',
            p.probNegative != null && !isActual ? row(t('chart.tooltip.probNegative'), formatPercent(p.probNegative), p.probNegative >= 0.25 ? danger : undefined) : '',
            p.inflow || p.outflow ? row(t('chart.tooltip.inflow'), formatChartMoney(p.inflow), primary) : '',
            p.inflow || p.outflow ? row(t('chart.tooltip.outflow'), formatChartMoney(p.outflow), warning) : '',
            '</div>',
          ].join('')
        },
      },
      series,
    }
  }, [rubPoints, today, shortageDate, compare, forecastName, theme, lang, t, currency])

  return (
    <div role="img" aria-label={t('chart.aria')} style={{ width: '100%', height }}>
      <ReactECharts option={option} style={{ width: '100%', height: '100%' }} />
    </div>
  )
}
