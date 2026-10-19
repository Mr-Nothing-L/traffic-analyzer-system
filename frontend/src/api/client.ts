/** fetch 封装:统一错误(ApiError)、401 跳登录页(与 legacy 行为一致)。 */

export class ApiError extends Error {
  /** transport=true:错误来自网络/代理层而非后端应用——fetch 抛异常,或错误响应体
   * 不是 `{detail}` 应用 JSON 结构(如裸 "Bad Request" 文本、代理 HTML 错误页)。
   * 此类错误重试通常有效,UI 据此提供显式重试入口,不做静默自动重试。 */
  public readonly transport: boolean

  constructor(
    public readonly status: number,
    message: string,
    transport = false,
  ) {
    super(message)
    this.name = 'ApiError'
    this.transport = transport
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
    throw new ApiError(0, '网络错误,无法连接后端', true)
  }
  if (res.status === 401) {
    // 会话失效:整页跳登录,与 auth middleware 的 302 语义一致;
    // 已在登录页则不重复跳(跳转即整页刷新,会成循环)。
    if (window.location.pathname !== '/login') window.location.href = '/login'
    throw new ApiError(401, '未认证,已跳转登录页')
  }
  if (!res.ok) {
    let detail: string | null = null
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (body?.detail != null) detail = String(body.detail)
    } catch {
      // 非 JSON 错误响应
    }
    if (detail == null) {
      // 响应体不是应用 JSON 错误结构:判定为网络/代理层错误(transport)
      throw new ApiError(res.status, res.statusText || `HTTP ${res.status}`, true)
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
