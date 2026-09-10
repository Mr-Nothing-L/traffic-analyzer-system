/** fetch 封装:统一错误(ApiError)、401 跳登录页(与 legacy 行为一致)。
 * path 以 /api 开头时原样使用(agent 后端直连),否则自动加 /api 前缀。 */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const url = path.startsWith('/api') ? path : `/api${path}`
  // FormData 上传等非 JSON body 不强设 Content-Type(浏览器自动带 boundary)
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) }
  if (typeof init?.body === 'string') headers['Content-Type'] = 'application/json'
  let res: Response
  try {
    res = await fetch(url, { ...init, headers })
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
      // 错误体两种形态:FastAPI 代理 {detail};agent 服务 {error:{code,message}}
      const body = (await res.json()) as { detail?: unknown; error?: { message?: unknown } }
      if (body?.detail != null) detail = String(body.detail)
      else if (body?.error?.message != null) detail = String(body.error.message)
    } catch {
      // 非 JSON 错误响应,保留 statusText
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}
