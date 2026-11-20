/** One place that knows how to talk to the loopback control server. */
export type Api = <T>(path: string, body?: unknown) => Promise<T>

export function createApiClient(readToken: () => string): Api {
  return async <T,>(path: string, body?: unknown): Promise<T> => {
    const headers: Record<string, string> = body
      ? { 'Content-Type': 'application/json', 'X-Control-Token': readToken() }
      : {}
    const response = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers,
      body: body ? JSON.stringify(body) : undefined
    })
    const data = await response.json()
    if (!response.ok) throw Error(data.error || '请求失败')
    return data as T
  }
}
