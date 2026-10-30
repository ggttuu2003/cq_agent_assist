/** 本地个人资料只含昵称，不代表已登录用户。 */
export type User = { id: number; display_name: string };
export type Message = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  requestId?: string;
  model?: string | null;
  status?: 'pending' | 'error' | 'cancelled';
};
export type Conversation = {
  id: string;
  title: string;
  modelName: string;
  updatedAt: number;
  messages: Message[];
  generating?: boolean;
};

type SavedConversation = {
  id: string; title: string; model_name: string; updated_at: string; generating: boolean;
  messages: { id: string; role: 'user' | 'assistant'; content: string; request_id: string; model: string | null }[];
};

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** 所有请求由同源 /api 代理转发，不在前端保存模型密钥。 */
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options, credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail :
      response.status === 422 ? '输入格式不正确，请检查后重试。' : `服务暂时不可用（${response.status}）`;
    throw new ApiError(detail, response.status);
  }
  return data as T;
}

/** 将数据库接口的时间和字段名转成页面使用的结构。 */
function toConversation(data: SavedConversation): Conversation {
  return {
    id: data.id, title: data.title, modelName: data.model_name, updatedAt: Date.parse(data.updated_at), generating: data.generating,
    messages: data.messages.map((message) => ({
      id: message.id, role: message.role, content: message.content,
      requestId: message.request_id, model: message.model,
    })),
  };
}

export const getProfile = (signal?: AbortSignal) => request<User>('/profile', {
  signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
});
export const updateProfile = (display_name: string) => request<User>('/profile', {
  method: 'PUT', body: JSON.stringify({ display_name }),
});

export async function getDefaultModel(signal: AbortSignal): Promise<string> {
  const data = await request<{ model: string }>('/models/default', { signal });
  return data.model.trim();
}

export async function listConversations(signal?: AbortSignal): Promise<Conversation[]> {
  return (await request<SavedConversation[]>('/conversations', { signal })).map(toConversation);
}
/** 首次问题交给后端生成并保存标题，页面直接使用接口返回的标题。 */
export async function createConversation(firstMessage: string): Promise<Conversation> {
  return toConversation(await request<SavedConversation>('/conversations', {
    method: 'POST', body: JSON.stringify({ first_message: firstMessage }),
  }));
}
export async function getConversation(id: string, signal?: AbortSignal): Promise<Conversation> {
  return toConversation(await request<SavedConversation>(`/conversations/${id}`, {
    signal: signal ?? AbortSignal.timeout(15000),
  }));
}
export const removeConversation = (id: string) => request<void>(`/conversations/${id}`, { method: 'DELETE' });

/** 标题由服务端保存，刷新页面后仍以数据库中的值为准。 */
export async function renameConversation(id: string, title: string): Promise<Conversation> {
  return toConversation(await request<SavedConversation>(`/conversations/${id}`, {
    method: 'PATCH', body: JSON.stringify({ title }), signal: AbortSignal.timeout(15000),
  }));
}

/** 重试时复用请求编号，服务端会返回已经保存的回答，避免重复问答。 */
export async function sendMessage(id: string, message: string, requestId: string, signal: AbortSignal): Promise<Conversation> {
  return toConversation(await request<SavedConversation>(`/conversations/${id}/messages`, {
    method: 'POST', body: JSON.stringify({ message, request_id: requestId }), signal,
  }));
}
