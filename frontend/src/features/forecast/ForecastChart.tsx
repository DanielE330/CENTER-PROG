import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import type { ForecastPoint } from '../../types'
import { formatCompact, formatCurrency, formatDate, formatShortDate } from '../../lib/formatters'
import { cssVar } from '../../lib/utils'
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

export function ForecastChart({ points, today, shortageDate, compare, forecastName = 'Прогноз', height = 380 }: Props) {
  const theme = useTheme()
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

    const dates = points.map((p) => p.date)
    const compareByDate = new Map(compare?.points.map((c) => [c.date, c.balance]))
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

    if (hasBand) {
      series.push(
        {
          name: 'band-base', type: 'line', stack: 'band', symbol: 'none', silent: true,
          lineStyle: { opacity: 0 }, tooltip: { show: false },
          data: points.map((p) => (p.lower != null ? p.lower : null)),
        },
        {
          name: 'Диапазон неопределённости', type: 'line', stack: 'band', symbol: 'none', silent: true,
          lineStyle: { opacity: 0 }, areaStyle: { color: primary, opacity: 0.1 },
          data: points.map((p) => (p.lower != null && p.upper != null ? p.upper - p.lower : null)),
        },
      )
    }

    series.push({
      name: 'Факт',
      type: 'line',
      symbol: 'none',
      lineStyle: { color: neutral, width: 2 },
      itemStyle: { color: neutral },
      data: points.map((p) => p.actual),
    })

    if (compare) {
      series.push({
        name: compare.name,
        type: 'line',
        symbol: 'none',
        lineStyle: { color: muted, width: 2, type: 'dashed' },
        itemStyle: { color: muted },
        data: points.map((p) => compareByDate.get(p.date) ?? null),
      })
    }

    series.push({
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
          { yAxis: 0, lineStyle: { color: ink, type: 'dashed', width: 1 }, label: { formatter: '0 ₽', color: ink, position: 'insideEndTop' } },
          ...(dates.includes(today)
            ? [{ xAxis: today, lineStyle: { color: muted, type: 'solid' as const, width: 1 }, label: { formatter: 'Сегодня', color: muted, position: 'insideEndTop' as const } }]
            : []),
        ],
      },
      markArea: deficitAreas.length
        ? { silent: true, itemStyle: { color: danger, opacity: 0.12 }, data: deficitAreas }
        : undefined,
      markPoint: shortagePoint?.forecast != null
        ? {
            symbolSize: 46,
            itemStyle: { color: danger },
            label: { formatter: '!', color: onDanger, fontWeight: 700, fontSize: 16 },
            data: [{ name: 'Дата нехватки', coord: [shortagePoint.date, shortagePoint.forecast] }],
          }
        : undefined,
    })

    // Negative stretches are redrawn in red (plus neighbouring points so the segment connects).
    if (deficitAreas.length) {
      const neg = (i: number) => points[i]?.forecast != null && (points[i].forecast as number) < 0
      series.push({
        name: 'Дефицит',
        type: 'line',
        symbol: 'none',
        silent: true,
        z: 5,
        lineStyle: { color: danger, width: 2.5 },
        itemStyle: { color: danger },
        data: points.map((p, i) => (neg(i) || neg(i - 1) || neg(i + 1) ? p.forecast : null)),
      })
    }

    return {
      animation: false,
      color: [primary],
      grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
      legend: {
        top: 0, left: 0, icon: 'roundRect', itemWidth: 14, itemHeight: 3, textStyle: { color: muted, fontSize: 12 },
        data: [
          'Факт', forecastName,
          ...(deficitAreas.length ? ['Дефицит'] : []),
          ...(compare ? [compare.name] : []),
          ...(hasBand ? ['Диапазон неопределённости'] : []),
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
        axisLabel: { color: muted, fontSize: 12, formatter: (v: number) => `${v / 1_000_000} млн` },
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
          const isActual = p.actual != null && p.date < today
          const bal = isActual ? p.actual : p.forecast
          const cmp = compare ? compareByDate.get(p.date) : undefined
          const row = (k: string, v: string, color?: string) =>
            `<div style="display:flex;justify-content:space-between;gap:20px"><span style="color:${muted}">${k}</span><b style="font-variant-numeric:tabular-nums;${color ? `color:${color}` : ''}">${v}</b></div>`
          return [
            `<div style="min-width:210px"><div style="font-weight:600;margin-bottom:4px">${formatDate(p.date)} · ${isActual ? 'факт' : 'прогноз'}</div>`,
            row(isActual ? 'Остаток (факт)' : `Остаток (${forecastName.toLowerCase()})`, bal != null ? formatCurrency(bal) : 'нет данных', bal != null && bal < 0 ? danger : undefined),
            cmp !== undefined ? row(compare!.name, cmp != null ? formatCurrency(cmp) : 'нет данных') : '',
            p.lower != null && p.upper != null && !isActual ? row('Диапазон', `${formatCompact(p.lower)} … ${formatCompact(p.upper)}`) : '',
            row('Поступления', formatCurrency(p.inflow), primary),
            row('Платежи', formatCurrency(p.outflow), warning),
            '</div>',
          ].join('')
        },
      },
      series,
    }
  }, [points, today, shortageDate, compare, forecastName, theme])

  return (
    <div role="img" aria-label="График прогноза остатка денежных средств" style={{ width: '100%', height }}>
      <ReactECharts option={option} notMerge style={{ width: '100%', height: '100%' }} />
    </div>
  )
}
