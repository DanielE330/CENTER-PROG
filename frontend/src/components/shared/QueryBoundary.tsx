import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { errorText } from '../../i18n/helpers'
import { ErrorState, LoadingState } from './States'

interface Props<T> {
  query: UseQueryResult<T>
  children: (data: T) => ReactNode
  loadingHeight?: number
}

/** Handles loading and error once so pages only render the success / empty cases. */
export function QueryBoundary<T>({ query, children, loadingHeight }: Props<T>) {
  const { t } = useTranslation()
  if (query.isPending) return <div className="card"><LoadingState height={loadingHeight} /></div>
  if (query.isError) return <div className="card"><ErrorState message={errorText(t, query.error)} onRetry={() => query.refetch()} /></div>
  return <>{children(query.data as T)}</>
}
