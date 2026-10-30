/** 本地个人资料只含昵称，不代表已登录用户。 */
export type User = { id: number; display_name: string };
export type Message = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  requestId?: string;
  model?: string | null;
  status?: 'pending' | 'error' | 'cancelled';
  notice?: string;
};
export type Conversation = {
  id: string;
  title: string;
  modelName: string;
  updatedAt: number;
  messages: Message[];
  generating?: boolean;
};

type SavedMessage = { id: string; role: 'user' | 'assistant'; content: string; request_id: string; model: string | null };
type SavedConversation = {
  id: string; title: string; model_name: string; updated_at: string; generating: boolean;
  messages: SavedMessage[];
};

export type ConversationTurn = {
  requestId: string; conversationId: string; updatedAt: number; messages: Message[];
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

/** 解析一个完整 SSE 事件；注释是心跳，文字片段立即交给聊天页面。 */
function readStreamEvent(block: string, id: string, requestId: string,
  onDelta: (text: string) => void): ConversationTurn | undefined {
  const lines = block.split(/\r?\n/);
  const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
  const payload = lines.filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^ /, '')).join('\n');
  if (!payload || !['delta', 'done', 'error'].includes(event ?? '')) return;
  const data = JSON.parse(payload);
  if (data.request_id !== requestId) throw new Error('回答请求编号不匹配，请重新打开会话。');
  if (event === 'error') throw new Error(typeof data.message === 'string' ? data.message : '生成失败，请重试。');
  if (event === 'delta') {
    if (typeof data.text !== 'string') throw new Error('回答片段格式不正确，请重试。');
    onDelta(data.text);
    return;
  }
  if (data.conversation_id !== id || !Array.isArray(data.messages) || data.messages.length !== 2 ||
    data.generating !== false || !Number.isFinite(Date.parse(data.updated_at)) ||
    data.messages.some((item: SavedMessage, index: number) => !item || typeof item.id !== 'string' ||
      typeof item.content !== 'string' || item.request_id !== requestId || item.role !== (index ? 'assistant' : 'user'))) {
    throw new Error('回答完成数据不正确，请重新打开会话。');
  }
  return {
    requestId, conversationId: id, updatedAt: Date.parse(data.updated_at),
    messages: data.messages.map((item: SavedMessage) => ({
      id: item.id, role: item.role, content: item.content, requestId: item.request_id, model: item.model,
    })),
  };
}

/** 用 POST 读取流，缓冲跨网络分块的事件和 UTF-8 字符，只在收到 done 后完成。 */
export async function streamMessage(id: string, message: string, requestId: string,
  signal: AbortSignal, onDelta: (text: string) => void): Promise<ConversationTurn> {
  const response = await fetch(`/api/conversations/${id}/messages/stream`, {
    method: 'POST', credentials: 'same-origin', cache: 'no-store', signal,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ message, request_id: requestId }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new ApiError(typeof data?.detail === 'string' ? data.detail : '发送失败，请重试。', response.status);
  }
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error('服务未返回流式回答，请检查后端版本。');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary: RegExpMatchArray | null;
      while ((boundary = buffer.match(/\r?\n\r?\n/))) {
        const index = boundary.index!;
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + boundary[0].length);
        const result = readStreamEvent(block, id, requestId, onDelta);
        if (result) return result;
      }
      if (done) throw new Error('连接已中断，正在核对回答是否保存。');
    }
  } finally {
    await reader.cancel().catch(() => { /* 连接可能已因停止或网络故障关闭。 */ });
    reader.releaseLock();
  }
}
