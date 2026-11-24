export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly details: unknown) {
    super(message)
  }
}

export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api/v1' + path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    const details = error.message ?? error.detail
    const message = typeof details === 'object' ? details?.message : details
    throw new ApiError(typeof message === 'string' ? message : `请求失败 (${response.status})`, response.status, details)
  }
  return response.json()
}

