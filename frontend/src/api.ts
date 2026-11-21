let token = '';
export function setToken(value: string) { token = value; }
export class ApiError extends Error { constructor(message: string, public status: number) { super(message); this.name = 'ApiError'; } }
export async function api<T>(path: string, method = 'GET', data?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Accept: 'application/json', ...(method === 'GET' ? {} : { 'Content-Type': 'application/json', 'X-Workbench-Token': token }) },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
  const content = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = content?.detail;
    if (response.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event('workbench:auth-required'));
    throw new ApiError(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `服务请求失败（${response.status}）`, response.status);
  }
  return content as T;
}

/** 原始文本 body 请求（CSV 导入用）：自动带上会话令牌。 */
export async function apiRaw(path: string, body: string, headers: Record<string, string> = {}): Promise<Response> {
  const response = await fetch('/api' + path, {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Accept: 'application/json', 'Content-Type': 'text/csv; charset=utf-8', 'X-Workbench-Token': token, ...headers },
    body,
  });
  if (response.status === 401) window.dispatchEvent(new Event('workbench:auth-required'));
  return response;
}
