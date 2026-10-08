/** fetch 封装:统一错误(ApiError)、401 跳登录页(与 legacy 行为一致)。 */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** 发起 /api 请求并统一处理网络/401/后端错误,返回原始 Response。
 * JSON 响应走 apiFetch;文本/二进制响应(如文件下载)直接用本函数。 */
export async function apiResponse(path: string, init?: RequestInit): Promise<Response> {
  let res: Response
  try {
    res = await fetch(`/api${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    })
  } catch {
    throw new ApiError(0, '网络错误,无法连接后端')
  }
  if (res.status === 401) {
    // 会话失效:整页跳登录,与 auth middleware 的 302 语义一致;
    // 已在登录页则不重复跳(跳转即整页刷新,会成循环)。
    if (window.location.pathname !== '/login') window.location.href = '/login'
    throw new ApiError(401, '未认证,已跳转登录页')
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (body?.detail != null) detail = String(body.detail)
    } catch {
      // 非 JSON 错误响应,保留 statusText
    }
    throw new ApiError(res.status, detail)
  }
  return res
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiResponse(path, init)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}
