/** Error returned by the API: {"error":{"code","message"}} plus the HTTP status. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message)
  }
}

/** The company has no confirmed statement yet (422 no_data / no_balance / not_enough_history). */
export const isNoData = (e: unknown): boolean =>
  e instanceof ApiError && e.status === 422 && ['no_data', 'no_balance', 'not_enough_history'].includes(e.code)
